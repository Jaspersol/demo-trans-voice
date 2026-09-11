"""Throwaway smoke: fake ASR SSE server + streaming /tts fake server.

Proves: ASR stream -> strip -> stable prefix -> segmenter -> TtsWorkerPool
(streaming /tts, chunked PCM with final marker) -> AudioPlayer ordered
playback with parallel workers.

Run:  .venv/Scripts/python.exe tests/smoke_e2e.py
"""

from __future__ import annotations

import io
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import soundfile as sf

sys.path.insert(0, ".")

from pipeline.asr_client import AsrClient, strip_metadata  # noqa: E402
from pipeline.playback import AudioPlayer  # noqa: E402
from pipeline.segmenter import Segmenter  # noqa: E402
from pipeline.stable_prefix import StablePrefix  # noqa: E402
from pipeline.tts_workers import TtsWorkerPool  # noqa: E402

SR = 24_000
FULL_TEXT = "language Chinese<asr_text>今天天气不错。我们去公园散步吧，然后吃午饭。"
PARTIAL = "language Chinese<asr_text>今天天气不错。我们去公园散步吧"


class FakeAsr(BaseHTTPRequestHandler):
    _calls = 0

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0)))
        FakeAsr._calls += 1
        text = FULL_TEXT if FakeAsr._calls >= 2 else PARTIAL
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        third = max(1, len(text) // 3)
        for i in range(0, len(text), third):
            chunk = {"choices": [{"delta": {"content": text[i : i + third]}}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


def tone_pcm(text: str) -> bytes:
    freq = 440.0 if "今天" in text else 330.0
    t = np.linspace(0, 0.2, int(SR * 0.2), endpoint=False)
    return (0.3 * np.sin(2 * np.pi * freq * t)).astype("<f4").tobytes()


class FakeTtsStream(BaseHTTPRequestHandler):
    """Streams /tts as raw float32 PCM in two chunks."""

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        raw = self.rfile.read(n).decode("utf-8", "replace")
        ctype = self.headers.get("content-type", "")
        if "multipart" in ctype:
            # crude multipart parse: find input= value
            import re
            m = re.search(r'name="input"\r?\n\r?\n(.*?)\r?\n--', raw, re.S)
            text = m.group(1) if m else ""
        else:
            text = json.loads(raw).get("input", "")
        pcm = tone_pcm(text)
        self.send_response(200)
        self.send_header("Content-Type", "audio/pcm;rate=24000")
        self.end_headers()
        half = len(pcm) // 2
        self.wfile.write(pcm[:half])
        self.wfile.write(pcm[half:])


class Recorder:
    rate = SR

    def __init__(self) -> None:
        self.written: list[np.ndarray] = []

    def write(self, buf) -> None:
        self.written.append(np.asarray(buf[:, 0], dtype=np.float32).copy())


def main() -> int:
    asr_srv = ThreadingHTTPServer(("127.0.0.1", 18081), FakeAsr)
    tts_srv = ThreadingHTTPServer(("127.0.0.1", 18082), FakeTtsStream)
    threading.Thread(target=asr_srv.serve_forever, daemon=True).start()
    threading.Thread(target=tts_srv.serve_forever, daemon=True).start()

    try:
        asr = AsrClient(base_url="http://127.0.0.1:18081", language="Chinese")

        pcm = (np.sin(np.linspace(0, 100, 16_000)) * 0.3).astype(np.float32)
        texts = []
        for _ in range(2):
            texts.append(strip_metadata("".join(asr.transcribe_stream(pcm))))
        FakeAsr._calls = 1
        texts.append(strip_metadata("".join(asr.transcribe_stream(pcm))))
        assert "然后吃午饭。" in texts[-1], texts

        stable = StablePrefix(confirm_chars=1)
        seg = Segmenter()
        player = AudioPlayer(reorder_timeout_ms=5000)
        recorder = Recorder()
        player._ensure_stream = lambda rate: recorder  # type: ignore[method-assign]

        pool = TtsWorkerPool(tts_url="http://127.0.0.1:18082", player=player, lang="zh", workers=2)
        player.start()
        pool.start()

        for t in texts:
            new = stable.update(t)
            for s in seg.push(new):
                pool.submit(s)
        for s in seg.flush():
            pool.submit(s)

        deadline = time.time() + 20
        while time.time() < deadline:
            with player._cv:
                pending = len(player._pending)
                nxt = player._next_seq
            if pending == 0 and nxt >= pool._seq and recorder.written:
                break
            time.sleep(0.1)

        player.stop()
        pool.stop()

        total = np.concatenate(recorder.written) if recorder.written else np.zeros(0)
        assert len(total) >= 2 * int(SR * 0.2), f"expected >= 2 segments, got {len(total)} samples"
        assert np.isfinite(total).all()
        print(
            f"SMOKE OK: asr-stream, {len(recorder.written)} playback writes, "
            f"{len(total) / SR:.2f}s audio"
        )
        return 0
    finally:
        asr_srv.shutdown()
        tts_srv.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
