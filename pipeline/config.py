"""Config file support for pipeline.app.

Config format: INI, one section [pipeline]. CLI arguments override config
values; config overrides built-in defaults. Unknown keys are errors.

Usage:
  python -m pipeline.app                      # reads config.ini if present
  python -m pipeline.app -c myconfig.ini
  python -m pipeline.app --asr-url http://x    # CLI wins over config
"""

from __future__ import annotations

import argparse
import configparser
import os
from pathlib import Path

DEFAULT_CONFIG_PATH = "config.ini"

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
    "confirm_chars": int,
    "clause_min_chars": int,
    "hard_max_chars": int,
    "idle_flush_ms": float,
    "silence_threshold": float,
    "silence_margin": float,
    "asr_idle_interval_ms": float,
    "reorder_timeout_ms": float,
    "gap_ms": float,
    "split_on_comma": bool,
    "asr_context_seconds": float,
}


def load_config(path: str | None) -> dict:
    """Read INI config; return {option: typed value}. Missing file -> {}."""
    if path is None:
        # auto-detect default file next to the package, then CWD
        for candidate in (
            Path(__file__).resolve().parent.parent / DEFAULT_CONFIG_PATH,
            Path.cwd() / DEFAULT_CONFIG_PATH,
        ):
            if candidate.is_file():
                path = str(candidate)
                break
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
            values[key] = t(raw)
        except ValueError as e:
            raise ValueError(f"config.ini: bad value for '{key}': {raw!r} ({e})") from e
    return values


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
    p.add_argument("-c", "--config", default=None, help=f"config file (default: {DEFAULT_CONFIG_PATH} if present)")
    p.add_argument("--asr-url", default=None, help="ASR llama-server base URL")
    p.add_argument("--asr-model", default=None, help="ASR model name for API payload")
    p.add_argument("--language", default=None, help="prefill language hint for ASR")
    p.add_argument("--tts-url", default=None, help="tts-wrapper base URL")
    p.add_argument("--tts-workers", type=int, default=None)
    p.add_argument("--tts-lang", default=None)
    p.add_argument("--speaker-ref", default=None, help="3s reference wav for voice cloning (local path)")
    p.add_argument("--mic-device", default=None, help="input device index/name")
    p.add_argument("--mic-gain", type=float, default=None, help="software mic gain multiplier")
    p.add_argument("--output-device", default=None, help="output device (e.g. CABLE Input)")
    p.add_argument("--window-seconds", type=float, default=None, help="ASR sliding window length")
    p.add_argument("--asr-interval-ms", type=float, default=None)
    p.add_argument("--confirm-chars", type=int, default=None)
    p.add_argument("--clause-min-chars", type=int, default=None)
    p.add_argument("--hard-max-chars", type=int, default=None)
    p.add_argument("--idle-flush-ms", type=float, default=None)
    p.add_argument("--silence-threshold", type=float, default=None, help="absolute RMS floor for silence gate")
    p.add_argument("--silence-margin", type=float, default=None, help="silence threshold = noise-floor EMA x margin")
    p.add_argument("--asr-idle-interval-ms", type=float, default=None, help="poll interval after prolonged silence")
    p.add_argument("--reorder-timeout-ms", type=float, default=None, help="max wait for a missing TTS segment before skipping")
    p.add_argument("--gap-ms", type=float, default=None, help="silence between TTS segments (0 when cloning)")
    p.add_argument("--split-on-comma", dest="split_on_comma", action="store_true", default=None, help="force comma splitting on")
    p.add_argument("--no-comma-split", dest="split_on_comma", action="store_false", help="disable comma splitting: full sentences only")
    p.add_argument("--asr-context-seconds", type=float, default=None, help="trailing context audio sent with each ASR chunk")

    # hard defaults used when neither config nor CLI provides a value
    hard_defaults = {
        "asr_url": "http://127.0.0.1:8081",
        "asr_model": "qwen3-asr",
        "silence_threshold": 0.0005,
        "silence_margin": 3.0,
        "asr_idle_interval_ms": 1500.0,
        "tts_url": "http://127.0.0.1:8082",
        "tts_lang": "zh",
        "speaker_ref": None,
        "tts_workers": 2,
        "mic_device": None,
        "mic_gain": 8.0,
        "output_device": None,
        "window_seconds": 8.0,
        "asr_interval_ms": 600.0,
        "confirm_chars": 2,
        "clause_min_chars": 8,
        "hard_max_chars": 20,
        "idle_flush_ms": 800.0,
        "silence_threshold": 0.004,
        "reorder_timeout_ms": 3000.0,
        "gap_ms": 60.0,
        "split_on_comma": True,
        "asr_context_seconds": 2.0,
    }

    args = p.parse_args(argv)
    for key, hard in hard_defaults.items():
        cli_val = getattr(args, key)
        if cli_val is not None:
            continue  # CLI wins
        if key in cfg:
            setattr(args, key, cfg[key])
        else:
            setattr(args, key, hard)
    return args
