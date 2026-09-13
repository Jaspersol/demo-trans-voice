"""TTS worker pool: per-sentence synthesis via the llama-server ``/tts`` API.

Requires the TTS-enabled llama-server image (llama.cpp PR #26603,
``llama-cuda-tts:local``). The model is resident in VRAM; each request streams
raw float32 PCM as frames are generated (``stream: true``), so playback can
start before synthesis finishes. Chunks are handed to the AudioPlayer under a
single seq with ``final=True`` on the last chunk; the player plays segments
in seq order, starting each as soon as its first chunk arrives. Jobs are
processed in order (workers=1 serial), so playback order always matches.
"""

from __future__ import annotations

import queue
import threading

import httpx
import numpy as np

from pipeline.playback import AudioPlayer


class TtsWorkerPool:
    def __init__(
        self,
        tts_url: str,
        player: AudioPlayer,
        lang: str = "zh",
        workers: int = 2,
        timeout: float = 600.0,
        chunk_ms: float = 100.0,
        speaker_path: str | None = None,
    ) -> None:
        self._url = tts_url.rstrip("/")
        self._player = player
        self._lang = lang
        self._workers = max(1, workers)
        self._client = httpx.Client(timeout=timeout)
        self._chunk = 24_000 * chunk_ms / 1000.0  # samples per emitted chunk
        self._speaker_b64: str | None = None
        if speaker_path:
            import base64
            self._speaker_b64 = base64.b64encode(open(speaker_path, "rb").read()).decode("ascii")
        self._jobs: queue.Queue[tuple[int, str] | None] = queue.Queue()
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._active = 0          # synthesis jobs currently in flight
        self._active_lock = threading.Lock()

    def submit(self, sentence: str) -> int:
        with self._seq_lock:
            seq = self._seq
            self._seq += 1
        with self._active_lock:
            self._active += 1
        self._jobs.put((seq, sentence))
        return seq

    @property
    def busy(self) -> bool:
        """True while any synthesis job is still in flight."""
        with self._active_lock:
            return self._active > 0

    def start(self) -> None:
        for i in range(self._workers):
            t = threading.Thread(target=self._loop, name=f"tts-worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        for _ in self._threads:
            self._jobs.put(None)

    # -- internals -----------------------------------------------------------

    def _synthesize(self, seq: int, sentence: str) -> None:
        rate = 24_000
        sent_any = False
        byte_buf = b""  # carries partial floats across TCP chunks
        samples = np.zeros(0, dtype=np.float32)
        body = {"input": sentence, "lang": self._lang,
                "stream": True, "response_format": "pcm"}
        if self._speaker_b64:
            body["speaker_ref_b64"] = self._speaker_b64
        with self._client.stream("POST", f"{self._url}/tts", json=body) as resp:
            resp.raise_for_status()
            for raw in resp.iter_bytes():
                byte_buf += raw
                usable = len(byte_buf) - len(byte_buf) % 4
                if usable == 0:
                    continue
                samples = np.concatenate(
                    [samples, np.frombuffer(byte_buf[:usable], dtype="<f4")])
                byte_buf = byte_buf[usable:]
                while len(samples) >= self._chunk:
                    self._player.put(seq, samples[: int(self._chunk)], rate)
                    sent_any = True
                    samples = samples[int(self._chunk):]
        if len(samples) or not sent_any:
            self._player.put(seq, samples, rate, final=True)
        else:
            self._player.put(seq, np.zeros(0, dtype=np.float32), rate, final=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                return
            seq, sentence = item
            try:
                self._synthesize(seq, sentence)
            except Exception as e:
                print(f"[tts] {sentence!r} failed: {e}")
                # tombstone: the player must see a final (empty) segment for
                # this seq, otherwise it stalls on reorder and drags every
                # later sentence down with it
                self._player.put(seq, np.zeros(0, dtype=np.float32), 24_000, final=True)
            finally:
                with self._active_lock:
                    self._active -= 1
