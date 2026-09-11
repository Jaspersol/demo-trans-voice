import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.config import load_config  # noqa: E402
from pipeline.app import parse_args  # noqa: E402


def write_cfg(tmp_path: Path, body: str) -> str:
    p = tmp_path / "cfg.ini"
    p.write_text(body, encoding="utf-8")
    return str(p)


def test_load_config_types(tmp_path):
    cfg = load_config(write_cfg(tmp_path, "[pipeline]\nasr_url=http://x:1\ntts_workers=3\nwindow_seconds=4.5\n"))
    assert cfg["asr_url"] == "http://x:1"
    assert cfg["tts_workers"] == 3
    assert cfg["window_seconds"] == 4.5


def test_empty_value_means_unset(tmp_path):
    cfg = load_config(write_cfg(tmp_path, "[pipeline]\noutput_device =\nmic_device=\n"))
    assert "output_device" not in cfg
    assert "mic_device" not in cfg


def test_unknown_key_rejected(tmp_path):
    with pytest.raises(ValueError, match="unknown key"):
        load_config(write_cfg(tmp_path, "[pipeline]\nbogus_key=1\n"))


def test_bad_value_rejected(tmp_path):
    with pytest.raises(ValueError, match="bad value"):
        load_config(write_cfg(tmp_path, "[pipeline]\ntts_workers=abc\n"))


def test_missing_file_yields_empty(tmp_path):
    assert load_config(str(tmp_path / "nope.ini")) == {}


def test_config_and_cli_layering(tmp_path):
    cfg_path = write_cfg(
        tmp_path,
        "[pipeline]\noutput_device = MY_SPEAKER\ntts_workers = 4\n",
    )
    argv = ["-c", cfg_path]
    a = parse_args(argv)
    assert a.output_device == "MY_SPEAKER"   # from config
    assert a.tts_workers == 4                # from config
    assert a.asr_url == "http://127.0.0.1:8081"  # hard default

    b = parse_args(argv + ["--output-device", "CABLE Input", "--tts-workers", "1"])
    assert b.output_device == "CABLE Input"  # CLI wins
    assert b.tts_workers == 1                # CLI wins
    assert a.asr_url == b.asr_url            # untouched key stays
