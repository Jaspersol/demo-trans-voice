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
(see ``segmenter.normalize_punct``). A block the worker refuses because its
backlog is full keeps its cursor and is retried on the next tick, so a slow
server costs latency but never drops speech.

Threads:
  - capture (sounddevice callback thread, via MicCapture)
  - asr_loop: silence timing -> ASR calls -> segmentation -> TTS queue
  - asr-worker: one background transcription request at a time (AsrWorker)
  - tts-pump: drains segmented sentences into the TTS worker pool
  - playback (AudioPlayer thread)
"""

from __future__ import annotations

import argparse
import contextlib
import queue
import signal
import sys
import threading
import time
from collections import deque
from enum import Enum

import numpy as np

from pipeline.asr_client import AsrClient, AsrWorker, strip_metadata
from pipeline.capture import SAMPLE_RATE, MicCapture, RingBuffer
from pipeline.config import parse_args_with_config, resolve_path
from pipeline.playback import AudioPlayer
from pipeline.segmenter import CUT_PUNCT, Segmenter, normalize_punct
from pipeline.tts_workers import TtsWorkerPool

MIN_BLOCK_S = 0.3  # shorter audio than this is not worth an ASR request
MIN_VOICED_FRACTION = 0.15  # a block must contain this much speech, else it is noise
# The silence timer reads a 0.05 s energy window and, once quiet, is polled every
# 0.05 s: a pause is therefore *measured* to start up to ~0.1 s later than it
# really did (window fill + one tick). Adding that back makes sentence_gap_ms /
# utterance_gap_ms mean "the pause you actually make". The tick that first sees
# the speech end is the coarser asr_interval_ms one, so this is an approximation.
SILENCE_OFFSET_MS = 100.0
# Quiet-state poll cadence: the gap timers are sampled at this rate, so a slower
# tick would swallow most of a 450 ms sentence pause. Not configurable on
# purpose — one 0.05 s energy read costs ~4 us, there is nothing to save here.
IDLE_POLL_S = 0.05


class CallOutcome(Enum):
    """What :meth:`Pipeline._call_asr` did with one audio block."""

    QUEUED = "queued"  # worker accepted it; the cursor may advance
    SKIPPED = "skipped"  # not worth a request (too short / evicted / no speech)
    BUSY = "busy"  # backlog full: the audio is still owed, retry it


class Pipeline:
    def __init__(self, args: argparse.Namespace) -> None:
        self.ring = RingBuffer(max_seconds=args.window_seconds + 2.0)
        # A block must never ask for more audio than the ring still holds: cap
        # it (1 s of slack for the read), so a jammed backlog splits the audio
        # into consecutive blocks instead of letting the head be evicted.
        self._max_block_s = self.ring.capacity_seconds - 1.0
        self.asr = AsrClient(base_url=args.asr_url, model=args.asr_model, language=args.language)
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
            speaker_path=resolve_path(args.speaker_ref),
        )
        self.sentences: queue.Queue[str] = queue.Queue()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.args = args
        # Speaker output = play only after the utterance ended; input gate
        # (stop listening while playing) follows the same switch.
        self.hold_mode = args.mute_during_tts
        self.asr_worker = AsrWorker(self.asr, stream=args.asr_stream, emit_ms=args.stream_emit_ms)
        mode = "流式增量" if args.asr_stream else "批式(整段)"
        playback = (
            "外放: 说完播放 + 播放期停听" if self.hold_mode else "非外放: 合成即播 + 全程收听"
        )
        print(
            f"[asr] 模式: {mode} | {playback} | 停顿: 句={args.sentence_gap_ms:g}ms "
            f"段={args.utterance_gap_ms:g}ms | 门限={args.silence_threshold:g} "
            f"max_split={args.max_split_chars}",
            flush=True,
        )

    def asr_loop(self) -> None:
        interval = self.args.asr_interval_ms / 1000.0
        threshold = self.args.silence_threshold
        sentence_gap = self.args.sentence_gap_ms
        utterance_gap = self.args.utterance_gap_ms
        chunk_cap = self.args.window_seconds
        stream_mode = self.args.asr_stream
        mute = self.args.mute_during_tts

        # Set when a full backlog refused a block: it holds that block's `pause`
        # flag, i.e. its audio is still owed and gets retried on the next tick
        # (None = nothing owed).
        held_pause: bool | None = None
        block_start = 0.0  # stream time where the pending audio block begins
        leftover = False  # last submit was cut short: audio is still waiting
        called = False  # this quiet span already made its ASR call
        dirty = False  # something happened since the last utterance end
        quiet_since: float | None = None
        pending: deque[dict] = deque()  # per block: {pause, pushed: already-pushed text prefix}
        gated = False  # speaker output: playing, so stop listening
        gated_speech = False  # speech arrived while gated: it is skipped
        ambient_log_at = time.monotonic() + 1.0

        while not self._stop.is_set():
            now_s = self.ring.seconds
            now_t = time.monotonic()
            # Short window: the silence timer must notice a pause promptly — a
            # wider read (0.2 s) plus the poll cadence would swallow most of a
            # 450 ms sentence pause. A syllable dip only *starts* the timer and is
            # cleared as soon as sound returns, so a dip can never fire a gap.
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
                # Partial results repeat the window's text so far, so only the
                # new suffix is pushed; the sentence boundary is appended once,
                # on the block's final result. The worker already gates the
                # language metadata, but strip again here: bookkeeping must
                # never reach the segmenter/TTS, and `pushed` is a text prefix
                # (not a length) so a re-stripped head cannot be mistaken for
                # a short read and cut off the beginning of the transcript.
                raw = strip_metadata(res.text)
                prev = blk["pushed"]
                chunk = normalize_punct(raw[len(prev) :] if raw.startswith(prev) else raw)
                blk["pushed"] = raw
                if not res.partial:
                    pending.popleft()
                    if blk["pause"]:
                        tail = normalize_punct(raw)
                        if tail and tail[-1] not in CUT_PUNCT:
                            chunk = (chunk + "。") if chunk else "。"
                if chunk:
                    for s in self.segmenter.push(chunk):
                        self.sentences.put(s)

            # A block the worker refused because its backlog was full is still
            # owed: retry it before anything else can move the cursor past its
            # audio. The refused block's `pause` rides along, so a forced
            # long-speech chunk does not suddenly gain a sentence boundary.
            if held_pause is not None:
                outcome, end = self._submit_block(
                    block_start, now_s, threshold, pause=held_pause, pending=pending
                )
                if outcome is not CallOutcome.BUSY:
                    held_pause = None
                    block_start = end
                    leftover = end < now_s
                    called = True

            if mute and gated:
                if not self._playback_drained(pending):
                    # Voice over the playback is dropped on purpose (the mic
                    # would otherwise transcribe our own TTS), but say so: a
                    # silent drop reads exactly like a dead pipeline.
                    if not quiet:
                        gated_speech = True
                    # Small slices: playback can end mid-interval and the gate
                    # must reopen as soon as the audio is done.
                    self._stop.wait(min(interval, IDLE_POLL_S))
                    continue
                if gated_speech:
                    print(
                        f"[asr] 外放停听: 播放期间跳过 {now_s - block_start:.1f}s 音频"
                        f"（含语音；播放期不转写自己的 TTS）",
                        flush=True,
                    )
                gated_speech = False
                # playback over: skip the played span (our own TTS, plus whatever
                # the speaker said over it), resume immediately
                block_start = now_s
                held_pause = None
                leftover = False
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
                # Nothing awake and nothing owed: the audio still behind the
                # cursor is silence only (`dirty` clears at the segment end,
                # which pins the cursor to that instant), so drop it. Left
                # alone, a pause longer than the ring ages the cursor out and
                # the next call reports a backlog loss that never happened —
                # and ships a full window of mostly silence, whose SKIPPED
                # verdict then throws away the speech the new utterance had
                # already started with.
                if not dirty and held_pause is None and not leftover:
                    block_start = now_s
            else:
                quiet_since = None
                called = False
                silence_ms = 0.0
                dirty = True  # speech happened: there will be something to process

            # Long unbroken speech: call anyway so the block cannot grow forever.
            # `leftover` means the previous submit was cut at the ring cap, so
            # the rest of that speech ships on the next tick instead of waiting
            # for the age to build up again.
            if not quiet and (leftover or now_s - block_start >= chunk_cap) and held_pause is None:
                outcome, end = self._submit_block(
                    block_start, now_s, threshold, pause=False, pending=pending
                )
                if outcome is CallOutcome.BUSY:
                    held_pause = False  # keep the audio, split it on the retry
                else:
                    block_start = end
                    leftover = end < now_s

            # Sentence gap (streaming only): the pause ends this sentence, so its
            # audio goes out now — one complete sentence per request.
            if (
                stream_mode
                and quiet
                and silence_ms >= sentence_gap
                and not called
                and held_pause is None
            ):
                outcome, end = self._submit_block(
                    block_start, now_s, threshold, pause=True, pending=pending
                )
                if outcome is CallOutcome.BUSY:
                    held_pause = True  # owed: the retry step above picks it up
                else:
                    block_start = end
                    leftover = end < now_s
                    called = True
                    if outcome is CallOutcome.QUEUED:
                        dirty = True

            # Utterance gap: the segment is over. Both modes use the same
            # absolute silence — utterance_gap_ms counted from the last speech,
            # NOT from the sentence gap: streaming already fired this sentence's
            # call at G1, so this branch is only the "stopped talking"
            # confirmation that flushes the rest and starts playback.
            if quiet and silence_ms >= utterance_gap and dirty and held_pause is None:
                # Batch: this is the utterance's one request. Streaming normally
                # has nothing left (the G1 call took the speech; the trailing
                # pause alone is SKIPPED), but a jam can leave audio behind —
                # send that before closing the segment.
                if leftover or not stream_mode:
                    outcome, end = self._submit_block(
                        block_start, now_s, threshold, pause=True, pending=pending
                    )
                    if outcome is CallOutcome.BUSY:
                        # no room: keep its audio and the segment open, retry
                        # instead of dropping it
                        held_pause = True
                    else:
                        block_start = end
                        leftover = end < now_s
                if held_pause is None and not leftover:
                    # segment over: flush, release playback, clear the span
                    block_start = now_s
                    leftover = False
                    for s in self.segmenter.flush():
                        self.sentences.put(s)
                    if mute:
                        self.player.release()  # start playing now
                        gated = True
                    dirty = False
                    quiet_since = None
                    called = False

            if quiet and ambient_log_at and now_t >= ambient_log_at:
                # informational: gives a number to calibrate silence_threshold.
                # Sampled on a quiet tick on purpose: taken unconditionally it
                # lands inside the user's first sentence, reports *speech* as
                # the noise floor and tells them to raise the threshold above
                # their own voice — after which nothing is ever detected again.
                ambient = self.ring.energy(0.5)
                print(
                    f"[asr] 环境RMS≈{ambient:.5f} 门限={threshold:g}"
                    f"（噪声被当有声就调高 silence_threshold，漏字就调低）",
                    flush=True,
                )
                if ambient >= threshold:
                    # deterministic misconfiguration: silence can never be
                    # detected, so no pause ever triggers and 外放 never starts
                    print(
                        f"[asr] 警告：门限 {threshold:g} 不高于环境底噪 {ambient:.5f} —— "
                        f"静默永远不成立（不会按停顿切句、外放也不会开始播放）。"
                        f"先把 silence_threshold 提到 {ambient * 2:.2f}~{ambient * 3:.2f}，"
                        f"并确认说话时 GUI 状态栏显示【有声】",
                        file=sys.stderr,
                        flush=True,
                    )
                ambient_log_at = None

            if quiet:
                self._stop.wait(IDLE_POLL_S)
            else:
                self._wait_tick(interval)

    def _submit_block(
        self,
        start: float,
        now_s: float,
        threshold: float,
        pause: bool,
        pending: deque[dict],
    ) -> tuple[CallOutcome, float]:
        """Submit ``[start, end)`` with ``end`` capped at what the ring can still
        return. A jammed backlog then splits the audio into consecutive blocks
        instead of letting one block outgrow the ring and be dropped whole;
        ``end < now_s`` tells the caller there is audio left over.

        The ring only holds the trailing ``capacity_seconds`` of audio: if the
        cursor sat un-submitted longer than that (a backlog jam), its head is
        already gone. Resync to the oldest audio still available — the evicted
        head is lost either way, but the newest ~9 s are kept instead of the
        whole block being refused. Returns the outcome and the window end that
        was asked for."""
        oldest = now_s - self._max_block_s
        dropped = 0.0
        if start < oldest:
            dropped = oldest - start
            start = oldest
        end = min(now_s, start + self._max_block_s)
        outcome = self._call_asr(start, end, threshold, pause=pause, pending=pending)
        if dropped and outcome is not CallOutcome.BUSY:
            # only once the retry actually goes through, not on every attempt
            print(
                f"[asr] 积压:丢弃最早 {dropped:.1f}s(环 {self.ring.capacity_seconds:.0f}s),"
                f"保留最近 {end - start:.1f}s",
                file=sys.stderr,
            )
        return outcome, end

    def _call_asr(
        self,
        start: float,
        end: float,
        threshold: float,
        pause: bool,
        pending: deque[dict],
    ) -> CallOutcome:
        """Send one contiguous audio block; ``pause`` marks a pause-triggered block
        (its transcript gets a sentence boundary appended).

        ``BUSY`` means the block was *not* accepted because the worker's backlog
        is full — the caller must keep its cursor and retry, or that audio (and
        the text that would have come from it) is lost."""
        if end - start < MIN_BLOCK_S:
            return CallOutcome.SKIPPED
        audio = self.ring.read_since(start)
        if audio is None:
            print(
                f"[asr] 音频窗口溢出,丢失 {end - start:.1f}s — 重同步",
                file=sys.stderr,
            )
            return CallOutcome.SKIPPED
        # Submit exactly the requested window: it may be capped (see
        # _submit_block) and the ring read always runs to *now*, so without this
        # the block would carry the samples written since the tick and the next
        # block would repeat them.
        span = int((end - start) * SAMPLE_RATE)
        if span < len(audio):
            audio = audio[:span]
        if self._speech_fraction(audio, threshold) < MIN_VOICED_FRACTION:
            # pure silence/noise: not worth a request (and keeps the model quiet)
            return CallOutcome.SKIPPED
        if not self.asr_worker.submit(start, end, audio):
            return CallOutcome.BUSY
        pending.append({"pause": pause, "pushed": ""})
        if self.hold_mode:
            # a new segment: buffer its audio until the utterance ends
            self.player.set_hold(True)
        return CallOutcome.QUEUED

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
        """Sleep one poll interval, staying interruptible by :meth:`shutdown`."""
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
        # Acquire every resource first, then start the consuming threads. The
        # mic device is the one step that fails on bad config (unknown index or
        # name, PortAudio refusing the stream): it used to sit *after* the
        # threads were started and *outside* the try, so its exception escaped
        # run() without ever reaching shutdown() — leaving worker threads and
        # the httpx pools alive. It is now inside the ExitStack, and every exit
        # path (error, Ctrl+C, normal stop) funnels through shutdown().
        # Capture-before-consumers is safe: the ring buffer keeps the audio
        # from stream time 0 until asr_loop reads it.
        self._install_sigint()
        try:
            with contextlib.ExitStack() as stack:
                if self._stop.is_set():  # stopped before anything was acquired
                    return
                capture = MicCapture(
                    self.ring, device=self.args.mic_device, gain=self.args.mic_gain
                )
                stack.enter_context(capture)
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
                print("running — Ctrl+C to stop", flush=True)
                while not self._stop.is_set():
                    time.sleep(0.2)
        except Exception as exc:
            # Chinese, user-facing: a device/startup failure is a configuration
            # problem, not an internal bug. The traceback still follows from the
            # re-raise for whoever needs the detail.
            print(f"[pipeline] 启动失败: {exc}", file=sys.stderr, flush=True)
            raise
        finally:
            self.shutdown()

    def _install_sigint(self) -> None:
        """Ctrl+C stops the CLI; a GUI runs this on a worker thread, where
        signal handlers cannot be installed (and Tk owns the main thread)."""
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGINT, self._request_stop)

    def _request_stop(self, *_):  # noqa: ANN002
        self.shutdown()

    def shutdown(self) -> None:
        """Stop everything; safe to call twice (signal handler, GUI Stop, and
        run()'s finally all land here — the first call wins)."""
        if self._stop.is_set():
            return
        self._stop.set()
        self.asr_worker.stop()
        self.asr.close()
        self.tts_pool.stop()
        self.player.close_queue()
        self.player.stop()
        # Join the loop threads we started from here: they read the ring and the
        # sentence queue, so nothing they touch may outlive them.
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(timeout=5.0)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """CLI > config.ini > built-in defaults. See pipeline/config.py."""
    return parse_args_with_config(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    Pipeline(args).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
