"""Sequenced playback of whole-utterance TTS segments.

Producers (one per TTS synthesis job) push *complete* utterances as
``(seq, np.float32 array, sample_rate)`` via :meth:`AudioPlayer.put`. Because
synthesis jobs run in parallel, segments can arrive out of order: the player
buffers early segments and waits up to ``reorder_timeout_ms`` for a missing
segment, then skips ahead (latency over completeness).
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable

import numpy as np

from pipeline.capture import _normalize_name

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
        reorder_timeout_ms: float = 3000.0,
    ) -> None:
        import sounddevice as sd

        self._sd = sd
        self._device = device
        self._gap_ms = gap_ms
        self._channels = channels
        self._reorder_timeout = reorder_timeout_ms / 1000.0
        self._pending: dict[int, dict] = {}  # seq -> {"rate": int, "chunks": [np.ndarray], "done": bool}
        self._next_seq = 0
        self._cv = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stream = None
        self._opened_rate = 24_000
        self._stop = threading.Event()

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

    def cancel_before(self, seq: int) -> None:
        """Drop all pending segments with sequence < seq (user stopped talking
        and stale utterances must not be spoken)."""
        with self._cv:
            for k in [k for k in self._pending if k < seq]:
                del self._pending[k]

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
        self._stream = stream
        return stream

    def _run(self) -> None:
        while not self._stop.is_set():
            item = self._next_ready()
            if item is None:
                continue
            seq, seg = item
            rate = seg["rate"]
            stream = self._ensure_stream(rate)
            try:
                for pcm in seg["chunks"]:
                    stream.write(np.ascontiguousarray(pcm.reshape(-1, 1)))
                gap = int(self._gap_ms / 1000.0 * rate)
                if gap > 0:
                    stream.write(np.zeros((gap, self._channels), dtype=np.float32))
            except Exception:
                pass
            finally:
                with self._cv:
                    self._next_seq = seq + 1
                    self._pending.pop(seq, None)

    def _next_ready(self):
        """Wait for completed segment ``next_seq``; on timeout skip to the
        lowest available sequence (a failed synthesis job must not stall)."""
        deadline = self._reorder_timeout
        with self._cv:
            while not self._stop.is_set():
                seg = self._pending.get(self._next_seq)
                if seg is not None and seg["done"]:
                    return self._next_seq, seg
                if self._pending:
                    lowest = min(self._pending)
                    if lowest == self._next_seq:
                        # in-flight segment, not done yet: wait for its final chunk
                        if not self._cv.wait_for(lambda: self._pending.get(self._next_seq, {}).get("done") or self._stop.is_set(), timeout=deadline):
                            self._next_seq = lowest + 1  # give up on it
                        continue
                    if lowest > self._next_seq:
                        if not self._cv.wait_for(lambda: (self._next_seq in self._pending and self._pending[self._next_seq]["done"]) or self._stop.is_set(), timeout=deadline):
                            self._next_seq = lowest
                            continue
                        continue
                    # lowest < next_seq: stale, drop it
                    self._pending.pop(lowest)
                    continue
                self._cv.wait(timeout=0.2)
        return None


def list_devices() -> Callable[[], str]:
    """Helper for CLI: returns a printable device table."""
    import sounddevice as sd

    def render() -> str:
        return str(sd.query_devices())

    return render
