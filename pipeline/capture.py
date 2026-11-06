"""Microphone capture: sounddevice reader feeding a bounded ring buffer.

Design:
  - 16 kHz mono float32, 512-sample callbacks (~32 ms frames).
  - RingBuffer keeps the trailing ``max_seconds`` of audio; readers pull
    arbitrary windows (e.g. the last 10 s every 600 ms) without blocking
    the audio callback.
  - ``energy()`` exposes RMS so the orchestration loop can skip ASR calls
    during silence (configurable in app.py, not hardwired here).
"""

from __future__ import annotations

import contextlib
import re
import threading

import numpy as np

SAMPLE_RATE = 16_000
FRAME = 512  # samples per callback (~32 ms)

# A plain number is a PortAudio device index ("3", "+3", " 3 "): matching it as
# a name fragment used to pick whichever device happened to contain "3".
_DEVICE_INDEX_RE = re.compile(r"[+-]?\d+")


class RingBuffer:
    def __init__(self, max_seconds: float = 12.0) -> None:
        self._cap = int(max_seconds * SAMPLE_RATE)
        self._buf = np.zeros(self._cap, dtype=np.float32)
        self._write = 0  # next write index
        self._filled = 0  # valid samples retained (saturates at cap)
        self._total = 0  # total samples ever written (true stream clock)
        self._lock = threading.Lock()

    def write(self, samples: np.ndarray) -> None:
        n = len(samples)
        if n >= self._cap:
            with self._lock:
                self._buf[:] = samples[-self._cap :]
                self._write = 0
                self._filled = self._cap
                self._total += n
            return
        with self._lock:
            end = self._write + n
            if end <= self._cap:
                self._buf[self._write : end] = samples
            else:
                first = self._cap - self._write
                self._buf[self._write :] = samples[:first]
                self._buf[: end - self._cap] = samples[first:]
            self._write = end % self._cap
            self._total += n
            self._filled = min(self._filled + n, self._cap)

    def read_last(self, seconds: float) -> np.ndarray:
        """Return a copy of the trailing ``seconds`` of audio."""
        with self._lock:
            n = min(int(seconds * SAMPLE_RATE), self._filled)
            if n == 0:
                return np.zeros(0, dtype=np.float32)
            return self._slice((self._write - n) % self._cap, n)

    def _slice(self, start: int, n: int) -> np.ndarray:
        """Copy ``n`` samples starting at ring index ``start``, joining the two
        pieces when the window wraps past the buffer end. Caller holds the lock."""
        if start + n <= self._cap:
            return self._buf[start : start + n].copy()
        return np.concatenate([self._buf[start:], self._buf[: start + n - self._cap]])

    @property
    def capacity_seconds(self) -> float:
        """Audio the ring retains: a wider window can never be read back."""
        return self._cap / SAMPLE_RATE

    @property
    def seconds(self) -> float:
        """True stream time (total written), not retained duration — used as
        the ASR cursor reference so it never goes backwards after wrap."""
        with self._lock:
            return self._total / SAMPLE_RATE

    def read_since(self, cursor_seconds: float, max_seconds: float = 12.0) -> np.ndarray | None:
        """Return audio written after ``cursor_seconds`` of total stream time
        (a cursor previously obtained from :attr:`seconds`). Returns None if
        the cursor has fallen out of the ring buffer (caller should resync
        by resetting its cursor to :attr:`seconds`)."""
        with self._lock:
            total = self._total / SAMPLE_RATE
            n = int((total - cursor_seconds) * SAMPLE_RATE)
            if n <= 0:
                return np.zeros(0, dtype=np.float32)
            if n > self._filled:  # requested audio has been evicted
                return None
            return self._slice((self._write - n) % self._cap, n)

    def energy(self, seconds: float = 1.0) -> float:
        chunk = self.read_last(seconds)
        if len(chunk) == 0:
            return 0.0
        return float(np.sqrt(np.mean(chunk.astype(np.float64) ** 2)))


def _normalize_name(name: str) -> str:
    """Comparison key for device names: lowercased, whitespace removed (Windows
    lists one driver several times as ``Realtek(R) Audio``/``Realtek(R)Audio``)."""
    return "".join(str(name).lower().split())


class MicCapture:
    """Runs a sounddevice InputStream on the real microphone.

    ``gain`` multiplies incoming samples (software AGC substitute for quiet
    mics); keep below ~20 to avoid clipping typical speech peaks.
    """

    def __init__(
        self,
        ring: RingBuffer,
        device: int | str | None = None,
        gain: float = 1.0,
    ) -> None:
        import sounddevice as sd

        self.ring = ring
        self._gain = float(gain)
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=FRAME,
            device=self._resolve_device(sd, device),
            callback=self._callback,
        )

    @staticmethod
    def _resolve_device(sd, device, kind: str = "input") -> int | None:  # noqa: ANN001
        """Resolve a device id, or match by normalized name substring (ids are
        unstable across Windows re-enumerations; names survive). Normalization
        strips case and whitespace so 'Realtek(R)Audio' matches
        'Realtek(R) Audio'. A purely numeric string is an index, never a name
        substring (a mic called 'USB Audio 3' must not swallow ``--mic-device 3``)."""
        if device is None or isinstance(device, int):
            return device
        device = str(device).strip()
        if _DEVICE_INDEX_RE.fullmatch(device):
            return int(device)
        if not device:
            raise ValueError(f"empty {kind} device value")
        want = _normalize_name(device)
        ch_key = "max_input_channels" if kind == "input" else "max_output_channels"
        matches = [
            (i, d)
            for i, d in enumerate(sd.query_devices())
            if d.get(ch_key, 0) > 0 and want in _normalize_name(d["name"])
        ]
        if not matches:
            raise ValueError(f"no {kind} device matching {device!r}")
        return matches[0][0]

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        if status:
            pass  # overflow etc.: drop silently, ring keeps latest audio
        mono = indata[:, 0].copy()
        if self._gain != 1.0:
            np.multiply(mono, self._gain, out=mono)
            np.clip(mono, -1.0, 1.0, out=mono)
        self.ring.write(mono)

    def __enter__(self) -> MicCapture:
        try:
            self._stream.start()
        except Exception:
            # The stream object was already allocated in __init__: closing it
            # here keeps a failed start from leaking the device handle (the
            # caller only ever exits the context it actually entered).
            with contextlib.suppress(Exception):
                self._stream.close()
            raise
        return self

    def __exit__(self, *exc) -> None:  # noqa: ANN002
        self._stream.stop()
        self._stream.close()
