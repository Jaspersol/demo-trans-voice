"""Stable-prefix tracker for sliding-window ASR.

Each ASR pass sees the trailing N seconds of audio and rewrites "the text of
that window". Consecutive passes overlap, so raw outputs repeat and mutate at
the tail. The tracker:

  1. finds the common prefix of the new full-window text and the previous one,
  2. commits text up to ``common - confirm_chars``: a character must survive
     ``confirm_chars`` additional observations before it leaves the unstable
     tail and is handed to the segmenter,
  3. returns the newly committed text from :meth:`update`.

``confirm_chars`` trades latency for stability; 1~2 is right for a 0.6 s
re-decode cadence. On ASR regressions (window text contradicts history) the
committed region is left untouched; the unstable tail simply re-forms.
"""

from __future__ import annotations


class StablePrefix:
    def __init__(self, confirm_chars: int = 2) -> None:
        if confirm_chars < 1:
            raise ValueError("confirm_chars must be >= 1")
        self._confirm = confirm_chars
        self._last_window: str = ""
        self._committed: str = ""

    @property
    def committed(self) -> str:
        return self._committed

    @property
    def last_window(self) -> str:
        return self._last_window

    def update(self, window_text: str) -> str:
        """Feed the newest full-window transcription; return newly committed text."""
        common = _common_prefix_len(self._last_window, window_text)
        limit = max(common - self._confirm, 0)
        new = ""
        if limit > len(self._committed):
            new = window_text[len(self._committed) : limit]
            self._committed = window_text[:limit]
        elif limit < len(self._committed):
            # regression inside the committed region: roll back to the safe point
            self._committed = self._committed[:limit]
        self._last_window = window_text
        return new

    def flush(self) -> str:
        """Commit the entire last window (speech ended: no more confirmations
        are coming). Call once on silence, then reset()."""
        tail = self._last_window[len(self._committed):]
        self._committed = self._last_window
        return tail

    def reset(self) -> None:
        self._last_window = ""
        self._committed = ""


def _common_prefix_len(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


class IncrementalStablePrefix(StablePrefix):
    """Stable-prefix tracker for *incremental* ASR windows.

    Each ASR pass transcribes only the NEW audio since the previous pass
    (audio the model has never seen), so transcriptions concatenate instead
    of overlapping. ``update_incremental`` appends the chunk to the full
    text and applies the same confirmation rule: commit up to
    ``len(full) - confirm_chars``, return the newly committed text.

    On a revision of the tail (chunk text contradicts what preceded it —
    rare with incremental windows, since the model never re-reads audio),
    :meth:`revise` replaces the tail.
    """

    def __init__(self, confirm_chars: int = 2) -> None:
        super().__init__(confirm_chars)
        self._pending_tail: str = ""  # observed, uncommitted

    def update_incremental(self, chunk_text: str) -> str:
        """Feed the transcription of the newest audio chunk only."""
        self._pending_tail += chunk_text
        return self._commit_tail()

    def revise(self, new_tail: str) -> str:
        """Replace the unconfirmed tail (ASR corrected itself). Returns text
        the caller must undo (rarely needed; segmenter handles re-runs)."""
        self._pending_tail = new_tail
        return ""

    def _commit_tail(self) -> str:
        limit = max(len(self._pending_tail) - self._confirm, 0)
        new = self._pending_tail[:limit]
        self._committed += new
        self._pending_tail = self._pending_tail[limit:]
        return new

    @property
    def full_text(self) -> str:
        return self._committed + self._pending_tail

    def flush(self) -> str:
        """Commit the entire pending tail. Returns it for the segmenter."""
        tail = self._pending_tail
        self._committed += tail
        self._pending_tail = ""
        return tail

    def reset(self) -> None:
        super().reset()
        self._pending_tail = ""


def strip_known_prefix(transcript: str, known: str) -> str:
    """Remove the head of ``transcript`` that re-states already-tracked text
    (ASR context priming re-transcribes the trailing context). Matches the
    longest suffix of ``known`` against the transcript head; on mismatch
    trims the longest common run so partial context doesn't leak through."""
    if not known:
        return transcript
    for k in range(min(len(known), len(transcript)), 0, -1):
        if transcript.startswith(known[-k:]):
            return transcript[k:]
    n = min(len(known), len(transcript))
    i = 0
    while i < n and known[-(i + 1)] == transcript[i]:
        i += 1
    return transcript[i:]
