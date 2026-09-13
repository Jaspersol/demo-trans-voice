"""Sequenced playback of whole-utterance TTS segments.

Producers push segments as ``(seq, np.float32 array, sample_rate)`` via
:meth:`AudioPlayer.put`. With the single serial synthesis worker segments
arrive strictly in order; the player blocks until segment ``next_seq`` is
complete (a failed synthesis job lands an empty tombstone, so nothing
blocks forever) and plays segments one by one.
"""

from __future__ import annotations

import threading
import time

import numpy as np

DEFAULT_GAP_MS = 60.0


def resolve_device(sd, device, kind: str = "output") -> int | None:  # noqa: ANN001
    """Resolve device id or fuzzy name; forwards ints/None untouched."""
    from pipeline.capture import MicCapture

    return MicCapture._resolve_device(sd, device, kind=kind)



class AudioPlayer:
    def __init__(
        self,
        device: int | str | None = None,
        gap_ms: float = DEFAULT_GAP_MS,
        channels: int = 1,
    ) -> None:
        import sounddevice as sd

        self._sd = sd
        self._device = device
        self._gap_ms = gap_ms
        self._channels = channels
        self._pending: dict[int, dict] = {}  # seq -> {"rate": int, "chunks": [np.ndarray], "done": bool}
        self._next_seq = 0
        self._cv = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stream = None
        self._opened_rate = 24_000
        self._stop = threading.Event()
        self._hold = False  # True: buffer segments, do not play them yet
        self._playing = False  # True while an utterance is being written out
        self._busy_until = 0.0  # monotonic time until which playback still sounds
        self._drain_ms = 400.0  # device buffer drain hold-off after last write

    def set_drain_ms(self, ms: float) -> None:
        """Extra hold-off after the last PCM write during which is_playing
        stays True (covers sound-card buffer drain time)."""
        self._drain_ms = ms

    def set_hold(self, hold: bool) -> None:
        """Hold (or release) buffered playback: while held, completed segments
        stay in the pool and the playback thread writes nothing."""
        with self._cv:
            self._hold = hold
            if not hold:
                self._cv.notify_all()

    def release(self) -> None:
        """Start playing whatever is buffered (idempotent)."""
        self.set_hold(False)

    @property
    def pending_audio(self) -> bool:
        """True when a segment is buffered and not yet played."""
        with self._cv:
            return bool(self._pending)

    @property
    def is_playing(self) -> bool:
        """True while TTS audio is actively sounding or queued to sound:
        covers the write phase AND a drain window after the last write
        (device-buffer latency means sound outlives the write call)."""
        return self._playing or time.monotonic() < self._busy_until

    def put(self, seq: int, pcm: np.ndarray, rate: int, final: bool = False) -> None:
        """Deliver one chunk of segment ``seq``. Chunks must arrive in order
        within a segment; the segment becomes playable once ``final=True``."""
        with self._cv:
            seg = self._pending.setdefault(seq, {"rate": rate, "chunks": [], "done": False})
            seg["rate"] = rate
            if len(pcm):
                seg["chunks"].append(pcm)
            if final:
                seg["done"] = True
            self._cv.notify()


    def close_queue(self) -> None:
        with self._cv:
            self._cv.notify()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="tts-playback", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.close_queue()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    # -- internals -----------------------------------------------------------

    def _ensure_stream(self, rate: int):
        if self._stream is not None and self._opened_rate == rate:
            return self._stream
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
        stream = self._sd.OutputStream(
            samplerate=rate,
            channels=self._channels,
            dtype="float32",
            device=resolve_device(self._sd, self._device, kind="output"),
            blocksize=0,  # let PortAudio pick low-latency
        )
        stream.start()
        # keep it: the player reuses one device stream for every segment of
        # that rate (otherwise each sentence opens a new one and leaks the old)
        self._stream = stream
        self._opened_rate = rate
        return stream

    def _run(self) -> None:
        while not self._stop.is_set():
            item = self._next_ready()
            if item is None:
                with self._cv:
                    self._playing = bool(self._pending)
                continue
            seq, seg = item
            rate = seg["rate"]
            self._playing = True
            stream = self._ensure_stream(rate)
            try:
                for pcm in seg["chunks"]:
                    stream.write(np.ascontiguousarray(pcm.reshape(-1, 1)))
                gap = int(self._gap_ms / 1000.0 * rate)
                if gap > 0:
                    stream.write(np.zeros((gap, self._channels), dtype=np.float32))
                # stream.write() blocks until the data is in the device
                # buffer, so the segment has already been sounding *while*
                # it was written: only the buffer's residual audio remains
                # after the loop. Adding written/rate here kept the gate
                # closed for the whole segment length after it had ended.
                self._busy_until = time.monotonic() + self._drain_ms / 1000.0
            except Exception:
                pass
            finally:
                with self._cv:
                    self._next_seq = seq + 1
                    self._pending.pop(seq, None)
                    self._playing = bool(self._pending)

    def _next_ready(self):
        """Wait for completed segment ``next_seq``. Segments arrive strictly
        in order (single serial synthesis worker) and every seq eventually
        gets a final chunk — a synthesis failure lands an empty tombstone —
        so no reorder-skip: just block until ``next_seq`` is done. Stale
        (lower-seq) leftovers are dropped defensively.

        While ``_hold`` is set nothing is returned: synthesis keeps filling
        the pool, playback waits for the caller's release."""
        with self._cv:
            while not self._stop.is_set():
                if self._hold:
                    self._cv.wait(timeout=0.2)
                    continue
                # stale leftovers first
                for s in [k for k in self._pending if k < self._next_seq]:
                    self._pending.pop(s, None)
                seg = self._pending.get(self._next_seq)
                if seg is not None and seg["done"]:
                    return self._next_seq, seg
                self._cv.wait(timeout=0.2)
        return None
