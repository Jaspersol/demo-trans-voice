"""Thread orchestration: mic -> ASR -> segmenter -> TTS subprocesses -> player.

Threads:
  - capture (sounddevice callback thread, via MicCapture)
  - asr_loop: every ``asr_interval_ms`` sends the trailing window for
    transcription, diffs stable prefixes, feeds the segmenter
  - TtsWorkerPool: N worker threads running ``llama-tts`` subprocesses per
    sentence; completed segments land in the player out-of-order-safe queue
  - playback (AudioPlayer thread)
"""

from __future__ import annotations

import argparse
import queue
import signal
import sys
import threading
import time

from pipeline.asr_client import AsrClient, strip_metadata
from pipeline.capture import MicCapture, RingBuffer
from pipeline.playback import AudioPlayer
from pipeline.segmenter import Segmenter
from pipeline.stable_prefix import IncrementalStablePrefix, strip_known_prefix
from pipeline.tts_workers import TtsWorkerPool
from pipeline.config import parse_args_with_config



class Pipeline:
    def __init__(self, args: argparse.Namespace) -> None:
        self.ring = RingBuffer(max_seconds=args.window_seconds + 2.0)
        self.asr = AsrClient(
            base_url=args.asr_url, model=args.asr_model, language=args.language
        )
        self.segmenter = Segmenter(
            clause_min_chars=args.clause_min_chars,
            hard_max_chars=args.hard_max_chars,
            idle_flush_ms=args.idle_flush_ms,
            split_on_comma=args.split_on_comma,
        )
        self.stable = IncrementalStablePrefix(confirm_chars=args.confirm_chars)
        # cloned voice returns whole utterances with natural trailing pause;
        # inter-segment gap would double up. Streaming (no clone) keeps 60ms.
        gap_ms = 0.0 if args.speaker_ref else self.args.gap_ms
        self.player = AudioPlayer(device=args.output_device, reorder_timeout_ms=args.reorder_timeout_ms, gap_ms=gap_ms)
        self.tts_pool = TtsWorkerPool(
            tts_url=args.tts_url,
            player=self.player,
            lang=args.tts_lang,
            workers=args.tts_workers,
            speaker_path=args.speaker_ref,
        )
        self.sentences: queue.Queue[str] = queue.Queue()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.args = args

    def asr_loop(self) -> None:
        interval = self.args.asr_interval_ms / 1000.0
        idle_interval = self.args.asr_idle_interval_ms / 1000.0
        margin = self.args.silence_margin          # threshold = floor * margin
        floor_abs = self.args.silence_threshold    # absolute fallback floor
        noise_floor = floor_abs                    # EMA of ambient RMS
        silent_streak = 0
        asr_cursor = 0.0  # stream-time cursor: audio already sent to ASR
        while not self._stop.is_set():
            start = time.monotonic()
            energy = self.ring.energy(0.5)

            # P3: adaptive silence threshold — track the noise floor with a
            # slow EMA while quiet; speech threshold rides above it. The
            # absolute floor still applies so a hot mic can't self-trigger.
            if energy < noise_floor * 2:
                noise_floor = 0.9 * noise_floor + 0.1 * energy
            threshold = max(noise_floor * margin, floor_abs)

            if energy < threshold:
                silent_streak += 1
                # silence: pause detected. Commit the pending tail (speech ended,
                # no further confirmations coming), feed the segmenter, then let
                # idle_flush emit any partial sentence. The cursor jumps to now:
                # trailing silence is dropped so the next utterance starts clean.
                tail = self.stable.flush()
                self.stable.reset()
                asr_cursor = self.ring.seconds
                if tail:
                    for s in self.segmenter.push(tail):
                        self.sentences.put(s)
                for s in self.segmenter.poll():
                    self.sentences.put(s)
            else:
                silent_streak = 0
                # P2+fix: incremental with context priming. Send trailing
                # context (last context_s) + new audio so the ASR has language
                # context; strip the tracked text prefix from the transcript.
                # Without context the ASR swallows boundary words (缺字).
                now = self.ring.seconds
                ctx_start = max(asr_cursor - self.args.asr_context_seconds, 0.0)
                audio_all = self.ring.read_since(ctx_start)
                if audio_all is not None and len(audio_all) >= int(0.6 * 16_000):
                    try:
                        text = strip_metadata(self.asr.transcribe(audio_all)).strip()
                        # TODO: 调试 ASR 识别结果；确认正常后删除本行打印
                        print(f"[asr] 识别结果: {text}")
                        if text:
                            chunk = strip_known_prefix(text, self.stable.full_text)
                            new = self.stable.update_incremental(chunk)
                            if new:
                                for s in self.segmenter.push(new):
                                    self.sentences.put(s)
                        asr_cursor = now
                    except Exception as e:  # server hiccup: keep the loop alive
                        print(f"[asr] error: {e}", file=sys.stderr)

            # P5: back off the poll rate while idle; snap back on any sound
            wait_s = idle_interval if silent_streak >= 3 else interval
            elapsed = time.monotonic() - start
            self._stop.wait(max(0.0, wait_s - elapsed))

    def tts_pump(self) -> None:
        """Drain segmented sentences into the TTS worker pool."""
        while not self._stop.is_set():
            try:
                sentence = self.sentences.get(timeout=0.5)
            except queue.Empty:
                continue
            self.tts_pool.submit(sentence)

    # -- lifecycle -----------------------------------------------------------

    def run(self) -> None:
        self.player.start()
        self.tts_pool.start()
        threads = [
            threading.Thread(target=self.asr_loop, name="asr-loop", daemon=True),
            threading.Thread(target=self.tts_pump, name="tts-pump", daemon=True),
        ]
        for t in threads:
            t.start()
        self._threads = threads

        with MicCapture(self.ring, device=self.args.mic_device, gain=self.args.mic_gain):
            print("running — Ctrl+C to stop")
            while True:
                signal.signal(signal.SIGINT, self._request_stop)
                if self._stop.is_set():
                    break
                time.sleep(0.2)

    def _request_stop(self, *_):  # noqa: ANN002
        self.shutdown()

    def shutdown(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        self.tts_pool.stop()
        self.player.close_queue()
        self.player.stop()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """CLI > config.ini > built-in defaults. See pipeline/config.py."""
    return parse_args_with_config(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    pipeline = Pipeline(args)
    try:
        pipeline.run()
    except KeyboardInterrupt:
        pass
    finally:
        pipeline.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
