import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from pipeline.capture import RingBuffer  # noqa: E402


def test_read_since_cursor_semantics():
    ring = RingBuffer(max_seconds=5)
    ring.write(np.ones(16000, dtype=np.float32) * 1)
    ring.write(np.ones(16000, dtype=np.float32) * 2)
    ring.write(np.ones(16000, dtype=np.float32) * 3)
    assert abs(ring.seconds - 3.0) < 0.01
    seg = ring.read_since(2.0)
    assert len(seg) == 16000 and seg[0] == 3.0
    seg = ring.read_since(ring.seconds)
    assert len(seg) == 0


def test_read_since_true_clock_after_wrap():
    ring = RingBuffer(max_seconds=5)
    ring.write(np.ones(16000, dtype=np.float32) * 1)
    ring.write(np.ones(16000, dtype=np.float32) * 2)
    ring.write(np.ones(16000, dtype=np.float32) * 3)
    ring.write(np.ones(48000, dtype=np.float32) * 4)  # total 6s, cap 5
    assert abs(ring.seconds - 6.0) < 0.01  # true clock, not saturated
    seg = ring.read_since(1.0)  # oldest retained edge
    assert seg is not None and len(seg) == 80000 and seg[0] == 2.0
    assert ring.read_since(0.5) is None  # stale


def test_read_since_empty_when_no_new_audio():
    ring = RingBuffer(max_seconds=5)
    ring.write(np.ones(8000, dtype=np.float32))
    assert len(ring.read_since(ring.seconds)) == 0
