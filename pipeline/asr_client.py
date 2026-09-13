"""ASR worker thread for llama-server (Qwen3-ASR GGUF).

Decouples the blocking ``POST /v1/chat/completions`` call from the pipeline's
slow loop: the loop snapshots the audio window, stamps it with cursor times,
and enqueues it; a dedicated worker transcribes and delivers ``(snapshot_end,
text)``. The slow loop never stalls on network/decode latency, so the ring
cursor stays current and a slow server can no longer overflow the buffer.

Streaming: SSE deltas arrive while the model still decodes, so the server's
transcription overlaps the next encode. Delta semantics are batch-equivalent:
one text result per window.

Known llama.cpp quirk (issue #26749): the model emits a
``language XXXX<asr_text>`` metadata prefix that the server does not strip;
we strip it here with :data:`METADATA_RE`.
"""

from __future__ import annotations

import base64
import io
import json
import re
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass

import httpx
import numpy as np
import soundfile as sf

METADATA_RE = re.compile(r"language\s+\S+?<asr_text>\s*")

READ_TIMEOUT_S = 3.0


def strip_metadata(text: str) -> str:
    return METADATA_RE.sub("", text)


def wav_base64(pcm: np.ndarray, sample_rate: int = 16_000) -> str:
    buf = io.BytesIO()
    sf.write(buf, pcm, sample_rate, format="WAV", subtype="PCM_16")
    return base64.b64encode(buf.getvalue()).decode("ascii")


@dataclass
class AsrRequest:
    """One snapshot window queued for transcription."""

    window_start: float  # stream-seconds where this snapshot begins
    window_end: float    # stream-seconds where it ends (= caller's cursor)
    pcm: np.ndarray



@dataclass
class AsrResult:
    window_start: float
    window_end: float
    text: str
    partial: bool = False  # True: the window is still being transcribed


class AsrClient:
    """SSE transcription request for llama-server."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8081",
        model: str = "qwen3-asr",
        language: str | None = None,
        connect_timeout: float = 2.0,
        read_timeout: float = READ_TIMEOUT_S,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.language = language
        # A hung server must freeze the pipeline for connect only, not for a
        # global 30 s: connect failure is a fast error; decode may legitimately
        # take seconds on long windows. Read timeout stays short: a stalled
        # window is retried whole, blocking the loop for 10 s used to overflow
        # the ring buffer and drop up to its full capacity of speech.
        self._client = httpx.Client(timeout=httpx.Timeout(connect=connect_timeout, read=read_timeout, write=30.0, pool=2.0))

    def _messages(self, audio_b64: str) -> list[dict]:
        content: list[dict] = [
            {"type": "input_audio", "input_audio": {"data": audio_b64, "format": "wav"}}
        ]
        if self.language:
            # prefill the assistant turn with the language prefix the model
            # expects, so it skips emitting the metadata itself
            return [
                {"role": "user", "content": content},
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": f"language {self.language}<asr_text>"}],
                },
            ]
        return [{"role": "user", "content": content}]

    def transcribe_stream(self, pcm: np.ndarray, sample_rate: int = 16_000) -> Iterator[str]:
        """Yield text deltas as they arrive (SSE). Blocks while streaming."""
        payload = {
            "model": self.model,
            "messages": self._messages(wav_base64(pcm, sample_rate)),
            "stream": True,
        }
        with self._client.stream(
            "POST", f"{self.base_url}/v1/chat/completions", json=payload
        ) as resp:
            resp.raise_for_status()
            yield from _sse_content_deltas(resp.iter_lines())

    def transcribe(self, pcm: np.ndarray, sample_rate: int = 16_000) -> str:
        """Non-streaming transcription; returns final text."""
        payload = {
            "model": self.model,
            "messages": self._messages(wav_base64(pcm, sample_rate)),
            "stream": False,
        }
        resp = self._client.post(f"{self.base_url}/v1/chat/completions", json=payload)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def close(self) -> None:
        """Release the HTTP connection pool (called once from Pipeline.shutdown)."""
        self._client.close()


def _sse_content_deltas(lines: Iterator[str | bytes]) -> Iterator[str]:
    for raw in lines:
        line = raw.decode() if isinstance(raw, bytes) else raw
        if not line or not line.startswith("data: "):
            continue
        data = line[len("data: "):]
        if data.strip() == "[DONE]":
            return
        obj = json.loads(data)
        choices = obj.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        text = delta.get("content")
        if text:
            yield text


class AsrWorker:
    """Single transcription thread with a snapshot queue and a result pipe.

    The caller (slow loop) submits immutable audio snapshots; the worker
    transcribes them in submission order and emits results via :meth:`results`.
    The loop never blocks on the network, so the ring cursor stays current and
    slow ASR requests can no longer overflow the capture ring.

    ``stream=True`` publishes the accumulating text of a window as
    ``partial=True`` results (throttled to ``emit_ms``), so segmentation and
    TTS can start before the window finishes; the window's completion is
    signalled by a final ``partial=False`` result. ``stream=False`` performs a
    single batch request and publishes exactly one final result per window."""

    def __init__(
        self,
        client: AsrClient,
        max_backlog: int = 3,
        stream: bool = True,
        emit_ms: float = 200.0,
    ) -> None:
        self._client = client
        self._jobs: list[AsrRequest] = []  # bounded, max_backlog snapshots
        self._results: list[AsrResult] = []  # completed, in submission order
        self._max_backlog = max_backlog
        self._busy = False  # a request is in flight
        self._stream = stream
        self._emit_s = max(emit_ms, 0.0) / 1000.0
        self._stop = threading.Event()
        self._wake = threading.Condition()  # guards jobs/results/busy
        self._thread: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        """True while a request is in flight or snapshots are still queued:
        the caller uses this to know ASR still has text to produce."""
        with self._wake:
            return self._busy or bool(self._jobs)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="asr-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._wake:
            self._wake.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def submit(self, window_start: float, window_end: float, pcm: np.ndarray) -> bool:
        """Queue a snapshot window. Returns False when the worker is busy and
        the backlog cap is hit: the caller keeps its cursor and retries that
        same audio on the next tick — the block is never dropped."""
        with self._wake:
            if self._busy and len(self._jobs) >= self._max_backlog:
                return False
            self._jobs.append(AsrRequest(window_start=window_start, window_end=window_end, pcm=pcm))
            self._wake.notify_all()
        return True

    def results(self) -> list[AsrResult]:
        """Drain completed results in submission order."""
        with self._wake:
            out = self._results
            self._results = []
            return out

    def _loop(self) -> None:
        while not self._stop.is_set():
            with self._wake:
                while not self._jobs and not self._stop.is_set():
                    self._wake.wait(timeout=0.2)
                if self._stop.is_set() and not self._jobs:
                    return
                job = self._jobs.pop(0)
                self._busy = True
            try:
                if self._stream:
                    self._transcribe_streaming(job)
                else:
                    text = strip_metadata(self._client.transcribe(job.pcm)).strip()
                    self._publish(job, text, partial=False)
            except Exception as exc:
                # A failed snapshot must still publish (the caller's cursor and
                # gate logic depend on it), but it must not be silent: without
                # this line a dead/wrong ASR server looks exactly like "nothing
                # to transcribe" — no text, no TTS, no error, forever.
                print(
                    f"[asr] 请求失败 ({job.window_end - job.window_start:.1f}s 音频): {exc!r}",
                    file=sys.stderr,
                    flush=True,
                )
                self._publish(job, "", partial=False)

    def _transcribe_streaming(self, job: AsrRequest) -> None:
        """Publish the growing text of one window as partial results."""
        seen = ""
        emitted_at = 0.0
        for delta in self._client.transcribe_stream(job.pcm):
            seen += delta
            now = time.monotonic()
            if self._emit_s and now - emitted_at < self._emit_s:
                continue
            emitted_at = now
            # strip_metadata also inside a window: the model may prefix its
            # first delta with the language metadata line
            text = strip_metadata(seen).strip()
            if text:
                self._publish(job, text, partial=True)
        self._publish(job, strip_metadata(seen).strip(), partial=False)

    def _publish(self, job: AsrRequest, text: str, partial: bool) -> None:
        with self._wake:
            self._results.append(
                AsrResult(
                    window_start=job.window_start,
                    window_end=job.window_end,
                    text=text,
                    partial=partial,
                )
            )
            if not partial:
                self._busy = False
            self._wake.notify_all()
