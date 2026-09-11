"""Streaming ASR client for llama-server (Qwen3-ASR GGUF).

Sends the trailing window of captured audio to ``POST /v1/chat/completions``
with ``stream: true`` and yields text deltas via SSE. Works with both the
final JSON result and streaming deltas.

Known llama.cpp quirk (issue #26749): the model emits a
``language XXXX<asr_text>`` metadata prefix that the server does not strip;
we strip it here with :data:`METADATA_RE`.
"""

from __future__ import annotations

import base64
import io
import json
import re
from collections.abc import Iterator

import httpx
import numpy as np
import soundfile as sf

METADATA_RE = re.compile(r"language\s+\S+?<asr_text>\s*")


def strip_metadata(text: str) -> str:
    return METADATA_RE.sub("", text)


def wav_base64(pcm: np.ndarray, sample_rate: int = 16_000) -> str:
    buf = io.BytesIO()
    sf.write(buf, pcm, sample_rate, format="WAV", subtype="PCM_16")
    return base64.b64encode(buf.getvalue()).decode("ascii")


class AsrClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8081",
        model: str = "qwen3-asr",
        language: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.language = language
        self._client = httpx.Client(timeout=timeout)

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
        """Non-streaming convenience call; returns final text."""
        payload = {
            "model": self.model,
            "messages": self._messages(wav_base64(pcm, sample_rate)),
            "stream": False,
        }
        resp = self._client.post(f"{self.base_url}/v1/chat/completions", json=payload)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


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
