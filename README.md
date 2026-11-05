<div align="center">

<img src="docs/logo.png" width="120" alt="trans-voice logo">

# trans-voice

### Real-time speech-to-speech: your voice comes back out in another voice, sentence by sentence, as you speak

**Microphone → ring buffer → pause-chunked ASR → punctuation normalisation → sentence splitting → streaming TTS → ordered playback. Two knobs decide when a sentence ends and when a whole utterance ends.**

[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-lightgrey.svg)](#requirements)
[![Tkinter](https://img.shields.io/badge/UI-Tkinter-orange.svg)](https://docs.python.org/3/library/tkinter.html)
[![ASR](https://img.shields.io/badge/ASR-llama--server%20Qwen3--ASR-4B8BBE.svg)](https://github.com/ggml-org/llama.cpp)
[![License](https://img.shields.io/badge/license-not%20set-lightgrey.svg)](#license)

English | [中文](README_ZH.md)

**[Quick Start](#quick-start) · [Configuration](#configuration) · [Features](#features) · [Architecture](#architecture) · [FAQ](#faq)**

</div>

## Why trans-voice?

Chunked "record, stop, transcribe, synthesise" tools add a second of silence to every turn and break mid-word when you talk longer than their buffer. Sliding-window approaches fix that by re-transcribing overlapping audio — and then spend the rest of the codebase de-duplicating and rolling back partial results.

This pipeline takes the other route: **there is no sliding window, no context overlap and no de-duplication.** Every ASR request covers one contiguous block of audio whose end sits *inside* a pause, so the model never sees a word cut in half and never has to be corrected afterwards. Your own punctuation is treated as a prosody hint; the pause is the sentence boundary.

- **Pause-driven, not timer-driven** — `sentence_gap_ms` ends a sentence (streaming mode also uses it to fire that sentence's ASR call), `utterance_gap_ms` ends the whole segment
- **Two modes, one code path** — streaming SSE (one request per sentence) or batch (one request per segment); both share segmentation, TTS and playback
- **Never drops speech** — a block the worker refuses because its backlog is full keeps its cursor and is retried on the next tick; a slow server costs latency, not words
- **Optional speaker mode** — when `mute_during_tts` is on, synthesis waits until you stop talking, playback starts in one burst, and the mic is muted until playback drains, so the app never transcribes its own voice
- **Console and GUI** — the same options as a CLI (`python -m pipeline.app`) or a Tkinter control panel with a live log and level meter (`trans-voice-gui`)
- **No third-party UI dependency** — Tkinter for the panel, raw `ctypes` for the tray icon, so a frozen build ships one executable and nothing else

## Requirements

- **Python 3.11 or newer** (the build target is 3.11)
- **A sound device** — one input (your microphone) and one output; on Windows, "CABLE Input" plus a virtual cable is the usual setup for feeding another app
- **An ASR server** — `llama-server` serving Qwen3-ASR, answering `POST /v1/chat/completions`
- **A TTS server** — `llama-server` with a `/tts` endpoint (`Dockerfile.tts` in this repository builds one), optionally taking a reference clip for voice cloning
- **Windows for the GUI and tray** — the tray icon is Win32-only; the console pipeline itself is portable

## Quick Start

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
.venv\Scripts\python.exe -m pipeline.app
```

Or open the control panel, which seeds every field from `config.ini` and the built-in defaults:

```bat
.venv\Scripts\python.exe -m pipeline.gui
```

Copy `config.ini` from the repository if you want to edit settings by hand; the pipeline reads it from the working directory, or from `-c <path>`.

### Bring up the two servers

Both are `llama-server` instances. The TTS side needs a build with the `/tts` endpoint — `Dockerfile.tts` compiles exactly that:

```bat
docker build -t llama-cuda-tts:local -f Dockerfile.tts .
docker run --gpus all -p 45102:8080 llama-cuda-tts:local ^
  -m /models/tts-model.gguf --ctx-size 4096
```

The ASR side is a stock `llama-server` with a Qwen3-ASR model, listening on 8081 by default. The shipped `config.ini` expects `45101` / `45102`, so either publish the containers on those ports or edit the two URLs.

## Configuration

Precedence is **command line > `config.ini` > hard-coded defaults**; an empty value in the INI means "not set", and an unknown key is an error rather than being silently ignored.

Hard defaults are in `pipeline/config.py`; the shipped `config.ini` overrides them for this machine. Rows marked ⚙ differ between the two, and the table shows the `config.ini` value in that case.

| Key | `config.ini` (hard default) | Meaning |
| --- | --- | --- |
| `asr_url` / `asr_model` | `http://127.0.0.1:45101` / `qwen3-asr` (`8081`) | ASR server base URL and the model name sent in the payload |
| `language` | `Chinese` (`None`) | Language hint prefixed into the ASR prompt, so the model skips its metadata preamble |
| `tts_url` / `tts_lang` | `http://127.0.0.1:45102` / `zh` (`8082`) | TTS server base URL and the `lang` field |
| `tts_workers` ⚙ | `1` (`2`) | Synthesis workers; 1 keeps playback strictly in `seq` order |
| `speaker_ref` | `speaker.wav` (`None`) | Reference WAV for voice cloning; when set, `gap_ms` is forced to 0 |
| `mic_device` / `output_device` | `USB2.0` / `CABLE Input` (`None`) | Index or name fragment; normalised (case and spaces ignored) before matching |
| `mic_gain` | `8` | Software gain applied in the capture callback, clipped to ±1 |
| `sentence_gap_ms` | `450` | **G1**: silence this long ends a sentence (streaming only; triggers that sentence's ASR call) |
| `utterance_gap_ms` | `1200` | **G2**: silence this long ends the segment — flush, batch ASR, and start playback |
| `window_seconds` | `8` | Hard cap on one ASR request's audio; also sets the capture ring to this + 2 s |
| `max_split_chars` | `40` | Forced cut length for an unpunctuated long sentence |
| `silence_threshold` | `0.03` (`0.01`) | Fixed RMS gate; must sit between the room's noise floor and speech level |
| `asr_interval_ms` ⚙ | `400` (`600`) | Poll cadence while there is speech; only affects silence-timer precision |
| `asr_stream` ⚙ | `false` (`true`) | `true` = SSE deltas, one request per sentence; `false` = one batch request per segment |
| `stream_emit_ms` | `200` | Throttle for publishing partial transcripts |
| `mute_during_tts` ⚙ | `true` (`false`) | `true` = speaker mode: playback starts after G2 and the mic is muted until it drains |
| `gap_ms` ⚙ | `0` (`60`) | Silence inserted between TTS segments; forced to 0 when cloning |

The two knobs worth understanding are `sentence_gap_ms` and `utterance_gap_ms`: they are the *lower bound* of the measured pause, not its exact length, because the silence timer samples on a poll tick.

## Features

### Capture and segmentation

- **Fixed-rate capture** — 16 kHz mono float32, 512-sample frames, gain applied and clipped in the sounddevice callback
- **Ring buffer with a stream clock** — `seconds` is cumulative written audio; a read whose cursor has been overwritten returns `None` so the caller can resynchronise instead of silently returning garbage
- **Three-way submission outcome** — `QUEUED` / `SKIPPED` (too short, window overflowed, or under 15 % voiced) / `BUSY` (backlog full, cursor retained and retried next tick)
- **Punctuation as a hint only** — the model's `,;:` and full stops become `，`; only `? ! ！ ？` and the `。` appended at a pause survive as sentence ends

### TTS and playback

- **Streaming PCM** — `POST /tts` with `stream: true`, 24 kHz floats, reassembled on 4-byte boundaries into 100 ms chunks
- **Strict ordering** — the player waits for `next_seq`, so a slow sentence delays the queue but never reorders it
- **Device reuse** — one `OutputStream` is cached per sample rate; reopening per segment costs 20–240 ms and leaks the previous stream
- **Guaranteed release** — every segment ends with `final`, and a failed synthesis sends a zero-length tombstone, so playback can't wait forever

### Control panel and tray

- **Form mirrors the CLI** — one field per option, seeded from `config.ini` plus the defaults, with device pickers and a reference-audio browse button
- **Live log and level meter** — `sys.stdout`/`stderr` are swapped for a queue-backed stream while running, so `[asr]`/`[tts]` lines land in the log pane; the meter shows current RMS against the threshold, which is what you calibrate `silence_threshold` against
- **Saving keeps your comments** — `config.save_config()` rewrites only the affected line in `[pipeline]`, preserving comments, other sections and CRLF endings
- **Minimise to tray** — a `ctypes` `Shell_NotifyIconW` icon on its own thread; the menu offers show / start-stop / quit

### Packaging

- **One spec, two executables** — `trans-voice-gui.exe` (windowed, no console) and `trans-voice.exe` (console, same CLI) in `dist/trans-voice/`
- **Editable config beside the binary** — `config.ini` and `speaker.wav` are copied next to the executables after the build and never overwritten
- **Native libraries collected explicitly** — `collect_dynamic_libs` pulls PortAudio and libsndfile from `_sounddevice_data` / `_soundfile_data`, which static analysis cannot follow

```bat
.venv\Scripts\python.exe -m pip install -e ".[build]"
.venv\Scripts\python.exe -m PyInstaller trans-voice.spec --noconfirm
```

## Architecture

Threads, all started before the microphone opens so the capture callback never finds a missing consumer:

| Thread | Responsibility |
| --- | --- |
| sounddevice callback | Gain, clip, write into the ring buffer |
| `asr-loop` | Silence timing → submit blocks → absorb results → segment → `sentences` queue |
| `asr-worker` | One transcription request at a time, results returned in submission order |
| `tts-pump` | Take a sentence, log `[tts] {sentence}`, hand it to the pool |
| `tts-worker-{0..N}` | One `POST /tts` per sentence, streaming chunks into the player |
| `tts-playback` | Write strictly in `seq` order; writes nothing while held |

Data channels: `RingBuffer` (lock-protected float32), `sentences: queue.Queue[str]`, and the player's `_pending: dict[seq, {rate, chunks, done}]`.

Two design documents go deeper than this README:

- **[flow.md](flow.md)** — full thread model, timing diagrams, the four quadrants of `asr_stream` × `mute_during_tts`, measured latencies, and the external request formats
- **[batch-speaker.md](batch-speaker.md)** — the `asr_stream=false` + `mute_during_tts=true` quadrant in full: state machine, gate conditions and edge cases

## FAQ

<details>
<summary><strong>Does it transcribe words cut off at the chunk boundary?</strong></summary>

No. A block is only submitted when its end lands inside a pause, so the boundary falls between words. When speech runs longer than `window_seconds` the block is forced out with `pause=False` — that cut *can* land mid-word, and it does not get a `。` appended for exactly that reason.

</details>

<details>
<summary><strong>Why is the measured pause longer than `sentence_gap_ms`?</strong></summary>

The silence timer samples an energy window on a poll tick: while there is speech, polling runs at `asr_interval_ms` (400 ms by default), plus a 100 ms offset that compensates for the energy window filling up. So the configured value is a lower bound, and the real trigger lands within roughly `asr_interval_ms` + 50 ms after it. Lowering `asr_interval_ms` tightens the error to about 50 ms.

</details>

<details>
<summary><strong>Nothing is transcribed and the log looks dead.</strong></summary>

The usual cause is `silence_threshold` sitting above your speech level: every tick is judged as silence, so nothing is ever submitted, and silence produces no log lines. Run the GUI and watch the level meter, or read the `[asr] 环境RMS≈…` line printed on the first idle tick, then set the threshold between the noise floor and speech.

</details>

<details>
<summary><strong>Speaker mode transcribes my own TTS output.</strong></summary>

Check that `mute_during_tts` is actually on. With it on, the pipeline holds playback until the utterance ends, then mutes the mic until playback drains — and the drain test needs *all* of: no pending blocks, nothing playing, no pending audio, an empty sentence queue, an empty segmenter, an idle TTS pool and an idle ASR worker. Dropping that last condition is what makes an app hear itself.

</details>

<details>
<summary><strong>Which mode should I use?</strong></summary>

Streaming (`asr_stream = true`) gives the lowest latency to first sound: each sentence is synthesised as it is finished. Batch (`false`) sends the whole segment at once — the transcript doesn't exist before G2, so audio can only start after you stop speaking; with a 1–2 sentence utterance it is often indistinguishable and costs fewer requests.

</details>

<details>
<summary><strong>Can I run it on Linux or in a container?</strong></summary>

The console pipeline is portable — it needs PortAudio, libsndfile and the two HTTP servers. The Tkinter panel works anywhere Tk is available, but the tray icon is Win32 `ctypes` and simply isn't created when `tray_available()` is false. `Dockerfile.tts` is for building the TTS server, not for running the client.

</details>

<details>
<summary><strong>Does it support languages other than Chinese?</strong></summary>

The `language` hint is passed through to ASR, and TTS gets `tts_lang`. The sentence splitter cuts on `。！？!?…` plus `，`, so Latin-script languages work but split on `，` rather than commas; `max_split_chars` still guarantees a cut for unpunctuated runs.

</details>

## Contributing

```bat
.venv\Scripts\python.exe -m pip install -e ".[build]"
.venv\Scripts\python.exe -m ruff format .
.venv\Scripts\python.exe -m ruff check .
```

Conventions, as recorded in `pyproject.toml`: docstrings and inline comments are English and explain *why* (the constraint, the trap, the history); anything a user reads — `print` lines, GUI labels, argparse help, error messages — stays Chinese, and one message never mixes both.

**Tech stack**: Python 3.11 · numpy · sounddevice · soundfile · httpx · Tkinter · ctypes (Win32 tray) · PyInstaller · ruff

## License

No license file is included in this repository yet — copyright of the respective authors, all rights reserved. Open an issue if you need explicit permission to reuse it.
