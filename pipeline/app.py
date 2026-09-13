"""Thread orchestration: mic -> pause-chunked ASR -> segmenter -> TTS pool -> playback.

Timing model — two knobs, no sliding windows:

  - ``sentence_gap_ms``: a silence this long ends a sentence. **Streaming only**:
    it also triggers the ASR call for that sentence's audio block; the transcript
    gets a sentence boundary appended (``。``) and is segmented for TTS.
  - ``utterance_gap_ms``: a silence this long ends the utterance. Streaming:
    the remaining text is flushed and — with ``mute_during_tts`` — playback
    starts. Batch: the whole audio block goes out as one non-streaming request.

Each ASR call covers a contiguous audio block whose end lies *inside* a pause, so
the model never sees a word cut in half, nothing is re-transcribed (no context,
no overlap de-duplication) and the model's own punctuation is only a prosody hint
(see ``segmenter.normalize_punct``).

Threads:
  - capture (sounddevice callback thread, via MicCapture)
  - asr_loop: silence timing -> ASR calls -> segmentation -> TTS queue
  - asr-worker: one background transcription request at a time (AsrWorker)
  - tts-pump: drains segmented sentences into the TTS worker pool
  - playback (AudioPlayer thread)
"""

from __future__ import annotations

import argparse
import queue
import signal
import sys
import threading
import time
from collections import deque

import numpy as np

from pipeline.asr_client import AsrClient, AsrWorker
from pipeline.capture import SAMPLE_RATE, MicCapture, RingBuffer
from pipeline.config import parse_args_with_config
from pipeline.playback import AudioPlayer
from pipeline.segmenter import CUT_PUNCT, Segmenter, normalize_punct
from pipeline.tts_workers import TtsWorkerPool

MIN_BLOCK_S = 0.3          # shorter audio than this is not worth an ASR request
MIN_VOICED_FRACTION = 0.15  # a block must contain this much speech, else it is noise
# The silence timer reads a 0.05 s energy window and is polled at ~0.05 s, so a
# measured spin-up lags the real pause by roughly this much. Adding it back makes
# sentence_gap_ms / utterance_gap_ms mean "the pause you actually make".
SILENCE_OFFSET_MS = 100.0


class Pipeline:
    def __init__(self, args: argparse.Namespace) -> None:
        self.ring = RingBuffer(max_seconds=args.window_seconds + 2.0)
        self.asr = AsrClient(
            base_url=args.asr_url, model=args.asr_model, language=args.language
        )
        self.segmenter = Segmenter(max_chars=args.max_split_chars)
        # cloned voice returns whole utterances with natural trailing pause;
        # inter-segment gap would double up. Streaming (no clone) keeps 60ms.
        gap_ms = 0.0 if args.speaker_ref else args.gap_ms
        self.player = AudioPlayer(device=args.output_device, gap_ms=gap_ms)
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
        # Speaker output = play only after the utterance ended; input gate
        # (stop listening while playing) follows the same switch.
        self.hold_mode = args.mute_during_tts
        self.asr_worker = AsrWorker(
            self.asr, stream=args.asr_stream, emit_ms=args.stream_emit_ms
        )
        mode = "流式增量" if args.asr_stream else "批式(整段)"
        playback = "外放: 说完播放 + 播放期停听" if self.hold_mode else "非外放: 合成即播 + 全程收听"
        print(
            f"[asr] 模式: {mode} | {playback} | 停顿: 句={args.sentence_gap_ms:g}ms "
            f"段={args.utterance_gap_ms:g}ms | 门限={args.silence_threshold:g} "
            f"max_split={args.max_split_chars}",
            flush=True,
        )

    def asr_loop(self) -> None:
        interval = self.args.asr_interval_ms / 1000.0
        idle_interval = self.args.asr_idle_interval_ms / 1000.0
        threshold = self.args.silence_threshold
        sentence_gap = self.args.sentence_gap_ms
        utterance_gap = self.args.utterance_gap_ms
        chunk_cap = self.args.window_seconds
        stream_mode = self.args.asr_stream
        mute = self.args.mute_during_tts

        block_start = 0.0   # stream time where the pending audio block begins
        called = False      # this quiet span already made its ASR call
        dirty = False       # something happened since the last utterance end
        quiet_since: float | None = None
        pending: deque[dict] = deque()  # per submitted block: {pause, pushed}
        gated = False       # speaker output: playing, so stop listening
        ambient_log_at = time.monotonic() + 1.0

        while not self._stop.is_set():
            now_s = self.ring.seconds
            now_t = time.monotonic()
            # Short window: the silence timer must notice a pause promptly (a 0.2 s
            # window plus the poll cadence would swallow most of a 0.6 s pause). A
            # syllable dip only *starts* the timer and is cleared as soon as sound
            # returns, so a dip can never fire a gap.
            quiet = self.ring.energy(0.05) < threshold

            # Absorb finished blocks first, before any gate decision: the
            # gate must see the segment's own text/audio, and a batch result
            # is only visible here (SSE partials repeat the block's text so
            # far, so only the new suffix is pushed; the sentence boundary is
            # appended once, on the block's final result).
            for res in self.asr_worker.results():
                if not pending:  # result without a tracked block (shouldn't happen)
                    continue
                blk = pending[0]
                raw = res.text
                chunk = normalize_punct(raw[blk["pushed"]:])
                blk["pushed"] = len(raw)
                if not res.partial:
                    pending.popleft()
                    if blk["pause"]:
                        tail = normalize_punct(raw)
                        if tail and tail[-1] not in CUT_PUNCT:
                            chunk = (chunk + "。") if chunk else "。"
                if chunk:
                    for s in self.segmenter.push(chunk):
                        self.sentences.put(s)

            if mute and gated:
                if not self._playback_drained(pending):
                    # Small slices: playback can end mid-interval and the gate
                    # must reopen as soon as the audio is done.
                    self._stop.wait(min(interval, 0.05))
                    continue
                # playback over: skip the played span, resume immediately
                block_start = now_s
                quiet_since = None
                called = False
                gated = False

            if quiet:
                if quiet_since is None:
                    quiet_since = now_t
                # +offset: the 0.05 s energy window needs that long to fill with
                # silence, so the raw pause is this much longer than what is
                # measured here. Compensating keeps the two gap settings meaning
                # "the pause you actually make".
                silence_ms = (now_t - quiet_since) * 1000.0 + SILENCE_OFFSET_MS
            else:
                quiet_since = None
                called = False
                silence_ms = 0.0
                dirty = True  # speech happened: there will be something to process

            # Long unbroken speech: call anyway so the block cannot grow forever.
            if not quiet and now_s - block_start >= chunk_cap:
                if self._call_asr(block_start, now_s, threshold, pause=False, pending=pending):
                    dirty = True
                block_start = now_s

            # Sentence gap (streaming only): the pause ends this sentence, so its
            # audio goes out now — one complete sentence per request.
            if stream_mode and quiet and silence_ms >= sentence_gap and not called:
                if self._call_asr(block_start, now_s, threshold, pause=True, pending=pending):
                    dirty = True
                block_start = now_s
                called = True

            # Utterance gap: the segment is over. Streaming counts the sentence
            # gap *plus* the utterance gap (G1+G2) — the utterance gap is the
            # extra silence that confirms the speaker is done; batch has no
            # sentence gap at all and fires on G2 alone.
            end_at = sentence_gap + utterance_gap if stream_mode else utterance_gap
            if quiet and silence_ms >= end_at and dirty:
                if not stream_mode:
                    if self._call_asr(block_start, now_s, threshold, pause=True, pending=pending):
                        dirty = True
                block_start = now_s
                for s in self.segmenter.flush():
                    self.sentences.put(s)
                if mute:
                    self.player.release()  # start playing now
                    gated = True
                dirty = False
                quiet_since = None
                called = False

            if ambient_log_at and now_t >= ambient_log_at:
                # informational: gives a number to calibrate silence_threshold
                print(
                    f"[asr] 环境RMS≈{self.ring.energy(0.5):.5f} 门限={threshold:g}"
                    f"（噪声被当有声就调高 silence_threshold，漏字就调低）",
                    flush=True,
                )
                ambient_log_at = None

            if quiet:
                self._stop.wait(min(idle_interval, 0.05))
            else:
                self._wait_tick(interval)

    def _call_asr(
        self,
        start: float,
        end: float,
        threshold: float,
        pause: bool,
        pending: deque[dict],
    ) -> bool:
        """Send one contiguous audio block; ``pause`` marks a pause-triggered block
        (its transcript gets a sentence boundary appended). Returns True when the
        block was queued, False when it was skipped (too short, evicted, no speech)."""
        if end - start < MIN_BLOCK_S:
            return False
        audio = self.ring.read_since(start)
        if audio is None:
            print(
                f"[asr] 音频窗口溢出,丢失 {end - start:.1f}s — 重同步",
                file=sys.stderr,
            )
            return False
        if self._speech_fraction(audio, threshold) < MIN_VOICED_FRACTION:
            return False  # pure silence/noise: not worth a request (and keeps the model quiet)
        if self.asr_worker.submit(start, end, audio):
            pending.append({"pause": pause, "pushed": 0})
            if self.hold_mode:
                # a new segment: buffer its audio until the utterance ends
                self.player.set_hold(True)
            return True
        return False

    @staticmethod
    def _speech_fraction(pcm: np.ndarray, threshold: float) -> float:
        """Fraction of 20 ms frames whose RMS reaches ``threshold``.

        Cheap speech-presence test for one block: stationary noise and short
        bursts sit below the threshold for most frames, real speech does not."""
        frame = int(0.02 * SAMPLE_RATE)
        n = len(pcm) // frame
        if n == 0:
            return 0.0
        rms = np.sqrt(np.mean(pcm[: n * frame].reshape(n, frame) ** 2, axis=1))
        return float((rms >= threshold).sum()) / n

    def _playback_drained(self, pending: deque) -> bool:
        """True when the segment is fully spoken: no submitted block waiting to
        be absorbed, nothing sounding, nothing buffered, no queued/segmented
        text, no transcription or synthesis in flight. Listening may resume.

        pending matters for batch mode: there the utterance-end request is
        submitted and its result only reaches the segmenter on a later pass, so
        in between every other signal here is idle - checking it last would
        reopen the gate in the middle of the segment's own transcription."""
        return (
            not pending
            and not self.player.is_playing
            and not self.player.pending_audio
            and self.sentences.empty()
            and self.segmenter.empty
            and not self.tts_pool.busy
            and not self.asr_worker.busy
        )

    def _wait_tick(self, interval: float) -> None:
        self._stop.wait(interval)

    def tts_pump(self) -> None:
        """Drain segmented sentences into the TTS worker pool."""
        while not self._stop.is_set():
            try:
                sentence = self.sentences.get(timeout=0.5)
            except queue.Empty:
                continue
            # one line per TTS call: what is actually being synthesized
            print(f"[tts] {sentence}", flush=True)
            self.tts_pool.submit(sentence)

    def run(self) -> None:
        # Start every consuming thread BEFORE opening the mic stream: the
        # capture callback (audio thread) writes into the ring immediately,
        # and it must never find a consumer missing.
        if self.hold_mode:
            self.player.set_hold(True)  # play only when the segment ended
        self.player.start()
        self.tts_pool.start()
        self.asr_worker.start()
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
        self.asr_worker.stop()
        self.tts_pool.stop()
        self.player.close_queue()
        self.player.stop()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """CLI > config.ini > built-in defaults. See pipeline/config.py."""
    return parse_args_with_config(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    Pipeline(args).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
