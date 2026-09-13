"""Sentence segmenter for the pause-driven pipeline.

Text arrives one *sentence block* at a time (the ASR call happenend because the
speaker paused), so the splitter only has to:

1. Cut right after a sentence-final mark — ``?!`` kept from the model, plus the
   ``。`` the pipeline itself appends at a pause (see :func:`normalize_punct`,
   which downgrades every other model mark to a comma so a mis-predicted period
   can never split a word in half).
2. Force a cut at ``max_chars`` (a long, unpunctuated run must not wait forever),
   preferring the last boundary at or before the limit.

The caller drives two entry points:
  - :meth:`Segmenter.push` when transcribed text arrives.
  - :meth:`Segmenter.flush` when the utterance ends.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Marks that end a sentence: `?`/`!` from the model (reliable) plus the `。` this
# pipeline appends at a pause.
CUT_PUNCT = "。！？!?…"
# Marks the model may emit that are downgraded to a comma: they stay as prosody
# hints for TTS but must never end a sentence (a truncated window makes the
# model invent them mid-word).
_SOFTENED = "，、；：,;:" + "。！？!?…"
# Characters where cutting a long run is least harmful.
WORD_BOUNDARY = " \t\n\u3000"
COMMA = "，"

DEFAULTS = {"max_chars": 40}


def normalize_punct(text: str) -> str:
    """Keep ``?``/``!``, downgrade everything else to a comma's role.

    Returns the text with ``。、；：`` etc. replaced by ``,`` so the splitter
    (and TTS) sees a clause break instead of a sentence end. Whitespace runs are
    collapsed."""
    out: list[str] = []
    for ch in text:
        if ch in "?!！？":
            out.append(ch)
        elif ch in _SOFTENED or ch.isspace():
            if out and out[-1] != COMMA:
                out.append(COMMA)
        else:
            out.append(ch)
    return "".join(out).strip(COMMA)


@dataclass
class Segmenter:
    max_chars: int = DEFAULTS["max_chars"]

    _buffer: str = field(default="", init=False)

    # -- state ---------------------------------------------------------------

    @property
    def empty(self) -> bool:
        """True when nothing is buffered (all text handed to TTS)."""
        return not self._buffer

    # -- input ---------------------------------------------------------------

    def push(self, text: str) -> list[str]:
        """Feed newly-transcribed text; return sentences ready for TTS."""
        if not text:
            return []
        self._buffer += text
        return self._drain()

    def flush(self) -> list[str]:
        """Force out everything buffered (utterance end)."""
        out: list[str] = []
        if self._buffer:
            out.append(self._buffer)
            self._buffer = ""
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

        # Force cut at max_chars, preferring the last boundary at or before it.
        if len(buf) >= self.max_chars:
            window = buf[: self.max_chars]
            for k in range(len(window) - 1, 0, -1):
                if window[k] in CUT_PUNCT or window[k] in COMMA or window[k] in WORD_BOUNDARY:
                    return k + 1
            return self.max_chars

        # Sentence-final mark: cut right after it (swallowing trailing marks and
        # whitespace).
        for i, ch in enumerate(buf):
            if ch in CUT_PUNCT:
                j = i + 1
                while j < len(buf) and (buf[j] in CUT_PUNCT or buf[j] in WORD_BOUNDARY):
                    j += 1
                return j
        return None
