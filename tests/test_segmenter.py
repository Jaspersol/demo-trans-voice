"""Table-driven checks for :func:`pipeline.segmenter.normalize_punct`.

Run from the project root: ``python -m unittest discover -s tests``. Stdlib
``unittest`` on purpose — the project ships no test framework dependency.
"""

import unittest

from pipeline.segmenter import normalize_punct


class NormalizePunctChineseTest(unittest.TestCase):
    """Chinese input: clause marks and CJK spacing become ``,``; a pause the
    model wrote as a space still ends up as a clause break."""

    CASES = [
        ("你好，世界。", "你好，世界"),
        ("你好。", "你好"),
        ("你好 世界", "你好，世界"),
        ("  你好  ，  世界  ", "你好，世界"),
        ("嗯；然后呢", "嗯，然后呢"),
        ("第一句 说完就停", "第一句，说完就停"),
        ("你好？？", "你好？？"),
        ("你好 ？ 世界", "你好？世界"),
        ("", ""),
        ("   ", ""),
    ]

    def test_table(self) -> None:
        for src, want in self.CASES:
            with self.subTest(src=src):
                self.assertEqual(normalize_punct(src), want)


class NormalizePunctEnglishTest(unittest.TestCase):
    """English input: word separators survive as one plain space (they used to
    become ``,``, which TTS read as "hello，world")."""

    CASES = [
        ("hello world", "hello world"),
        ("  hello   world  ", "hello world"),
        ("I don't know", "I don't know"),
        ("Hello? Yes!", "Hello? Yes!"),
        # ASCII '.' is not a softened mark (only 。 is), so it survives untouched
        ("Hello world. Done", "Hello world. Done"),
        ("hello, world", "hello，world"),
        ("2024 年", "2024 年"),
        ("我 use Python 写", "我 use Python 写"),
    ]

    def test_table(self) -> None:
        for src, want in self.CASES:
            with self.subTest(src=src):
                self.assertEqual(normalize_punct(src), want)


if __name__ == "__main__":
    unittest.main()
