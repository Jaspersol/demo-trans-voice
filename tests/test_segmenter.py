from pipeline.segmenter import Segmenter


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, ms: float) -> None:
        self.now += ms / 1000.0


def seg(**kw) -> tuple[Segmenter, FakeClock]:
    clock = FakeClock()
    return Segmenter(clock=clock, **kw), clock


def test_sentence_end_emits_immediately():
    s, clock = seg()
    assert s.push("你好吗") == []
    clock.advance(100)
    assert s.push("。") == ["你好吗。"]
    assert s.buffered == ""


def test_sentence_end_consumes_trailing_closers():
    s, _ = seg()
    assert s.push('Fine! " ') == ['Fine! " ']


def test_clause_cut_requires_min_length():
    s, _ = seg(clause_min_chars=8)
    assert s.push("好的，") == []  # 3-char chunk: below threshold, keep buffering
    # first qualifying boundary is at 10 chars
    assert s.push("我们继续往下走，") == ["好的，我们继续往下走，"]
    assert s.buffered == ""


def test_clause_cut_takes_first_qualifying_only():
    s, _ = seg(clause_min_chars=8)
    # first boundary qualifies at 15 chars -> emitted whole
    assert s.push("第一部分呢，然后这里是第二部分，") == ["第一部分呢，然后这里是第二部分，"]
    assert s.buffered == ""
    # 4 chars, still below threshold
    assert s.push("也够了，") == []


def test_hard_max_cuts_repeatedly_up_to_boundary():
    s, _ = seg(hard_max_chars=10)
    out = s.push("this is a long run without any punctuation at all")
    # _drain keeps cutting while a qualifying boundary exists
    assert out == ["this is a ", "long run ", "without ", "any ", "punctuatio"]
    assert s.buffered == "n at all"


def test_hard_max_cut_no_boundary():
    s, _ = seg(hard_max_chars=5)
    out = s.push("abcdefghij")
    # remainder (5 chars) is at the boundary again -> drained too
    assert out == ["abcde", "fghij"]
    assert s.buffered == ""


def test_idle_flush_after_timeout():
    s, clock = seg(idle_flush_ms=800)
    s.push("没有标点的尾巴")
    assert s.poll() == []
    clock.advance(799)
    assert s.poll() == []
    clock.advance(1)
    assert s.poll() == ["没有标点的尾巴"]
    assert s.poll() == []  # only flushes once


def test_idle_flush_resets_on_new_text():
    s, clock = seg(idle_flush_ms=800)
    s.push("abc")
    clock.advance(700)
    s.push("def")  # resets the idle timer
    clock.advance(700)
    assert s.poll() == []  # only 700ms since last push
    clock.advance(100)
    assert s.poll() == ["abcdef"]


def test_flush_forces_everything():
    s, _ = seg()
    s.push("还在缓冲的内容")
    assert s.flush() == ["还在缓冲的内容"]
    assert s.buffered == ""


def test_push_after_flush_accumulates_fresh():
    s, _ = seg()
    s.push("第一句。")
    s.flush()
    assert s.push("新的") == []
    assert s.buffered == "新的"


def test_reset_drops_buffer():
    s, _ = seg()
    s.push("部分文本")
    s.reset()
    assert s.buffered == ""
    assert s.flush() == []


def test_multi_sentence_batch():
    s, _ = seg()
    assert s.push("第一句。第二句！第三句？") == ["第一句。", "第二句！", "第三句？"]


def test_consume_iterator():
    s, _ = seg()
    got = list(s.consume(iter(["Hello ", "world. Next one!"])))
    # trailing space after "." is consumed as part of the sentence
    assert got == ["Hello world. ", "Next one!"]


def test_english_sentence_end_trailing_space_kept():
    s, _ = seg()
    assert s.push("OK. Yeah.") == ["OK. ", "Yeah."]
