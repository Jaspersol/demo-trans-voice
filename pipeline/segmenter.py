"""Text buffer and sentence segmenter for the streaming pipeline.

Consumes stable ASR text prefixes and emits complete sentences to the TTS
stage according to priority rules:

1. Sentence-final punctuation (。！？；.!?) -> emit immediately.
2. Clause punctuation (，、,:;) -> emit once the buffer reaches ``clause_min_chars``.
3. No punctuation fallback -> emit at ``hard_max_chars`` on a whitespace boundary.
4. Idle flush -> emit whatever is buffered after ``idle_flush_ms`` of no new text.

The caller drives two entry points:
  - :meth:`Segmenter.push` when new stable text arrives.
  - :meth:`Segmenter.poll` periodically (timeout flush) and before shutdown.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

# Punctuation that ends a sentence outright.
SENTENCE_END = "。！？；.!?;…"
# Punctuation that separates clauses but does not end the sentence.
CLAUSE_END = "，、,:；"
# Whitespace/word boundary chars where cutting a long run is least harmful.
WORD_BOUNDARY = " \t\n\u3000"

DEFAULTS = {
    "clause_min_chars": 8,
    "hard_max_chars": 20,
    "idle_flush_ms": 800.0,
}


@dataclass
class Segmenter:
    clause_min_chars: int = DEFAULTS["clause_min_chars"]
    hard_max_chars: int = DEFAULTS["hard_max_chars"]
    idle_flush_ms: float = DEFAULTS["idle_flush_ms"]
    split_on_comma: bool = True
    clock: Callable[[], float] = time.monotonic

    _buffer: str = field(default="", init=False)
    _last_text_at: float = field(default_factory=lambda: 0.0, init=False)
    _have_text: bool = field(default=False, init=False)
    _split_on_comma: bool = field(default=True, init=False)

    def __post_init__(self) -> None:
        self._split_on_comma = self.split_on_comma

    # -- state ---------------------------------------------------------------

    @property
    def buffered(self) -> str:
        return self._buffer

    def reset(self) -> None:
        """Drop buffered text (e.g. after an ASR rollback)."""
        self._buffer = ""
        self._have_text = False

    # -- input ---------------------------------------------------------------

    def push(self, stable_text: str) -> list[str]:
        """Feed newly-stabilized text; return sentences ready for TTS."""
        self._last_text_at = self.clock()
        self._have_text = True
        self._buffer += stable_text
        return self._drain()

    def poll(self) -> list[str]:
        """Idle/timeout flush; call periodically and at end of speech."""
        out: list[str] = []
        if self._buffer and self._have_text:
            if (self.clock() - self._last_text_at) * 1000.0 >= self.idle_flush_ms:
                out.append(self._buffer)
                self._buffer = ""
                self._have_text = False
        return out

    def flush(self) -> list[str]:
        """Force out everything buffered (end of utterance / shutdown)."""
        out: list[str] = []
        if self._buffer:
            out.append(self._buffer)
            self._buffer = ""
        self._have_text = False
        return out

    # -- internals -----------------------------------------------------------

    def _drain(self) -> list[str]:
        sentences: list[str] = []
        while True:
            cut = self._find_cut()
            if cut is None:
                break
            sentences.append(self._buffer[:cut])
            self._buffer = self._buffer[cut:]
        return sentences

    def _find_cut(self) -> int | None:
        """Return the exclusive end index of the next sentence, or None."""
        buf = self._buffer

        # Rule 1: sentence-final punctuation -> cut right after it (including
        # any run of closing punctuation and trailing whitespace).
        for i, ch in enumerate(buf):
            if ch in SENTENCE_END:
                j = i + 1
                while j < len(buf) and (buf[j] in SENTENCE_END or buf[j] in WORD_BOUNDARY or buf[j] in '"\'' or buf[j] in "”』)]）」’"):
                    j += 1
                return j

        # Rule 2: comma cut, only when enabled and chunk >= clause_min_chars.
        if self._split_on_comma:
            for i, ch in enumerate(buf):
                if ch in CLAUSE_END and i + 1 >= self.clause_min_chars:
                    return i + 1

        # Rule 3: hard limit on an unbroken run -> cut at the last word
        # boundary, else mid-run.
        if len(buf) >= self.hard_max_chars:
            window = buf[: self.hard_max_chars]
            for k in range(len(window) - 1, 0, -1):
                if window[k] in WORD_BOUNDARY:
                    return k + 1
            return self.hard_max_chars

        return None

    # convenience: iterate sentences as they arrive from a text stream
    def consume(self, chunks: Iterator[str]) -> Iterator[str]:
        for chunk in chunks:
            yield from self.push(chunk)
        yield from self.flush()
