"""Config file support for pipeline.app and pipeline.gui.

Config format: INI, one section [pipeline]. CLI arguments override config
values; config overrides built-in defaults. Unknown keys are errors.

Usage:
  python -m pipeline.app                      # reads config.ini if present
  python -m pipeline.app -c myconfig.ini
  python -m pipeline.app --asr-url http://x    # CLI wins over config

``save_config`` writes values back without touching the file's comments, so the
annotated ``config.ini`` stays the single documented source of truth (the GUI's
"save" button is a thin wrapper over it).
"""

from __future__ import annotations

import argparse
import configparser
import sys
from pathlib import Path

DEFAULT_CONFIG_PATH = "config.ini"


def _app_dir() -> Path:
    """Directory that holds ``config.ini`` and assets (``speaker.wav``).

    A frozen build must find its data next to the executable, since the bundle
    itself unpacks into a temporary directory; from source it is the project
    root, i.e. the package's parent."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _app_dir()

# type converters per option, mirroring add_argument types
_VALUE_TYPES: dict[str, type] = {
    "asr_url": str,
    "asr_model": str,
    "language": str,
    "tts_url": str,
    "tts_lang": str,
    "speaker_ref": str,
    "tts_workers": int,
    "mic_device": str,
    "mic_gain": float,
    "output_device": str,
    "window_seconds": float,
    "asr_interval_ms": float,
    "gap_ms": float,
    "max_split_chars": int,
    "silence_threshold": float,
    "sentence_gap_ms": float,
    "utterance_gap_ms": float,
    "mute_during_tts": bool,
    "asr_stream": bool,
    "stream_emit_ms": float,
}

# Values used when neither the CLI/form nor config.ini provides one. Keys are
# exactly the argparse dests, so a GUI form can build a Namespace from
# ``{**HARD_DEFAULTS, **form_values}`` without going through argparse.
HARD_DEFAULTS: dict[str, object] = {
    "asr_url": "http://127.0.0.1:8081",
    "asr_model": "qwen3-asr",
    "language": None,
    "silence_threshold": 0.01,
    "sentence_gap_ms": 450.0,
    "utterance_gap_ms": 1200.0,
    "tts_url": "http://127.0.0.1:8082",
    "tts_lang": "zh",
    "speaker_ref": None,
    "tts_workers": 2,
    "mic_device": None,
    "mic_gain": 8.0,
    "output_device": None,
    "window_seconds": 8.0,
    "asr_interval_ms": 600.0,
    "max_split_chars": 40,
    "gap_ms": 60.0,
    "mute_during_tts": False,
    "asr_stream": True,
    "stream_emit_ms": 200.0,
}


def default_config_path() -> Path:
    """The config file the app would use if none is given: next to the
    executable/package when present, else ./config.ini. When neither exists yet
    the writable app folder wins: the exe's directory for a frozen build, the
    working directory for a source install."""
    for candidate in (BASE_DIR / DEFAULT_CONFIG_PATH, Path.cwd() / DEFAULT_CONFIG_PATH):
        if candidate.is_file():
            return candidate
    if getattr(sys, "frozen", False):
        return BASE_DIR / DEFAULT_CONFIG_PATH
    return Path.cwd() / DEFAULT_CONFIG_PATH


def resolve_path(value: str | None) -> str | None:
    """Resolve a relative asset path such as ``speaker_ref``.

    The CWD wins when the file is actually there (that keeps ``-c other.ini``
    plus relative paths working); otherwise fall back to BASE_DIR, so an exe
    started from any directory still finds ``speaker.wav`` beside it."""
    if not value:
        return value
    p = Path(value)
    if p.is_absolute() or p.exists():
        return value
    fallback = BASE_DIR / p
    return str(fallback) if fallback.exists() else value


def load_config(path: str | None) -> dict:
    """Read INI config; return {option: typed value}. Missing file -> {}."""
    if path is None:
        # auto-detect default file next to the exe/package, then CWD
        detected = default_config_path()
        path = str(detected) if detected.is_file() else None
    if path is None:
        return {}
    cp = configparser.ConfigParser()
    cp.read(path, encoding="utf-8")
    values: dict = {}
    if not cp.has_section("pipeline"):
        return {}
    for key, raw in cp.items("pipeline"):
        raw = raw.strip()
        if not raw:
            continue  # empty value = unset, fall through to defaults
        if key not in _VALUE_TYPES:
            raise ValueError(f"config.ini: unknown key '{key}' in [pipeline]")
        t = _VALUE_TYPES[key]
        try:
            if t is bool:
                values[key] = raw.lower() in ("true", "1", "yes", "on")
            else:
                values[key] = t(raw)
        except ValueError as e:
            raise ValueError(f"config.ini: bad value for '{key}': {raw!r} ({e})") from e

    return values


def _format_value(value: object) -> str:
    """INI text for one value. Empty string means "unset" to load_config."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def save_config(path: str, values: dict) -> str:
    """Write ``values`` into the INI at ``path``, preserving everything else.

    Each key's first *active* ``key =`` line inside [pipeline] is rewritten in
    place (the explanatory comment above it survives); keys with no line are
    appended to the section, and the section itself is created if missing. The
    file's existing line ending is preserved. Returns the path written.
    """
    unknown = sorted(set(values) - set(_VALUE_TYPES))
    if unknown:
        raise ValueError(f"config: unknown keys {unknown}")

    p = Path(path)
    if p.is_file():
        # newline="" keeps the file's own ending instead of translating it
        with p.open("r", encoding="utf-8", newline="") as fh:
            text = fh.read()
    else:
        text = ""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()

    section = "[pipeline]"
    if not lines:
        lines = [section]
    start = next((i for i, ln in enumerate(lines) if ln.strip().lower() == section), None)
    if start is None:
        lines.append(section)
        start = len(lines) - 1
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")),
        len(lines),
    )

    for key, value in values.items():
        rendered = f"{key} = {_format_value(value)}".rstrip()
        hit = None
        for i in range(start + 1, end):
            line = lines[i]
            if line.lstrip().startswith((";", "#")):
                continue
            if line.split("=", 1)[0].strip() == key:
                hit = i
                break
        if hit is not None:
            lines[hit] = rendered
        else:
            lines.insert(end, rendered)
            end += 1

    with p.open("w", encoding="utf-8", newline="") as fh:
        fh.write(newline.join(lines) + newline)
    return str(p)


def parse_args_with_config(argv: list[str] | None = None) -> argparse.Namespace:
    """Build parser, overlay config defaults, then parse argv."""
    # config file may itself be given on the CLI: pre-scan for -c/--config
    config_path: str | None = None
    if argv:
        for i, a in enumerate(argv):
            if a in ("-c", "--config") and i + 1 < len(argv):
                config_path = argv[i + 1]
                break
            if a.startswith("--config="):
                config_path = a.split("=", 1)[1]
                break

    cfg = load_config(config_path)

    p = argparse.ArgumentParser(description="Real-time ASR->TTS voice pipeline")
    p.add_argument(
        "-c",
        "--config",
        default=None,
        help=f"config file (default: {DEFAULT_CONFIG_PATH} if present)",
    )
    p.add_argument("--asr-url", default=None, help="ASR llama-server base URL")
    p.add_argument("--asr-model", default=None, help="ASR model name for API payload")
    p.add_argument("--language", default=None, help="prefill language hint for ASR")
    p.add_argument("--tts-url", default=None, help="tts-wrapper base URL")
    p.add_argument("--tts-workers", type=int, default=None)
    p.add_argument("--tts-lang", default=None)
    p.add_argument(
        "--speaker-ref",
        default=None,
        help="3s reference wav for voice cloning (local path)",
    )
    p.add_argument(
        "--mic-device",
        default=None,
        help=(
            "input device: PortAudio index (e.g. 3) or name fragment "
            "(case/space-insensitive); a plain number is always an index"
        ),
    )
    p.add_argument("--mic-gain", type=float, default=None, help="software mic gain multiplier")
    p.add_argument(
        "--output-device",
        default=None,
        help=(
            "output device: PortAudio index (e.g. 3) or name fragment "
            "(e.g. CABLE Input); a plain number is always an index"
        ),
    )
    p.add_argument(
        "--window-seconds",
        type=float,
        default=None,
        help=(
            "cap on one ASR request's audio (s): forced block when speech never "
            "pauses; also sets capture ring = this + 2 s"
        ),
    )
    p.add_argument(
        "--asr-interval-ms", type=float, default=None, help="poll cadence of the ASR loop"
    )
    p.add_argument(
        "--asr-stream",
        dest="asr_stream",
        action="store_true",
        default=None,
        help="stream ASR incrementally (SSE deltas)",
    )
    p.add_argument(
        "--no-asr-stream",
        dest="asr_stream",
        action="store_false",
        help="batch ASR: one non-streaming request per utterance (after utterance_gap_ms)",
    )
    p.add_argument(
        "--max-split-chars",
        type=int,
        default=None,
        help="force a cut at this many chars even without punctuation",
    )
    p.add_argument(
        "--silence-threshold",
        type=float,
        default=None,
        help="fixed RMS threshold (energy(0.2s) below it counts as silence)",
    )
    p.add_argument(
        "--sentence-gap-ms",
        type=float,
        default=None,
        help=(
            "sentence gap: silence this long ends a sentence "
            "(streaming: triggers the ASR call for it)"
        ),
    )
    p.add_argument(
        "--utterance-gap-ms",
        type=float,
        default=None,
        help=(
            "utterance gap: silence this long ends the segment (flush / batch ASR / start playback)"
        ),
    )
    p.add_argument(
        "--mute-during-tts",
        dest="mute_during_tts",
        action="store_true",
        default=None,
        help="gate ASR while TTS is playing",
    )
    p.add_argument(
        "--no-mute-during-tts",
        dest="mute_during_tts",
        action="store_false",
        help="keep ASR running while TTS plays (TTS not played out loud)",
    )
    p.add_argument(
        "--stream-emit-ms",
        type=float,
        default=None,
        help="streaming ASR: min interval between partial text emissions",
    )
    p.add_argument(
        "--gap-ms", type=float, default=None, help="silence between TTS segments (0 when cloning)"
    )

    args = p.parse_args(argv)
    for key, hard in HARD_DEFAULTS.items():
        cli_val = getattr(args, key)
        if cli_val is not None:
            continue  # CLI wins
        if key in cfg:
            setattr(args, key, cfg[key])
        else:
            setattr(args, key, hard)
    return args
