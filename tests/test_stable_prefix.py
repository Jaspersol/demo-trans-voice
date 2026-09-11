from pipeline.stable_prefix import StablePrefix


def test_first_pass_commits_nothing():
    sp = StablePrefix(confirm_chars=2)
    assert sp.update("你好世界") == ""
    assert sp.committed == ""


def test_confirm_chars_gates_commit():
    sp = StablePrefix(confirm_chars=2)
    sp.update("你好世界")      # common=0 (no previous) -> nothing
    sp.update("你好世界")      # common=4, limit=2 -> commit "你好"
    assert sp.committed == "你好"
    sp.update("你好世界吗")    # common=4 -> limit=2, no growth
    assert sp.committed == "你好"

def test_growth_commits_incrementally():
    sp = StablePrefix(confirm_chars=1)
    # each pass commits what was common in the *previous* pass
    assert sp.update("你") == ""
    assert sp.update("你好") == ""          # common=1, limit=0
    assert sp.update("你好世") == "你"      # common=2, limit=1
    assert sp.update("你好世界") == "好"    # common=3, limit=2
    assert sp.committed == "你好"


def test_tail_mutation_does_not_commit():
    sp = StablePrefix(confirm_chars=2)
    sp.update("你好世界")
    sp.update("你好世界")  # committed "你"
    # tail hypothesis changed but common prefix intact
    assert sp.update("你好世间") == ""
    assert sp.committed == "你"


def test_regression_rolls_back():
    sp = StablePrefix(confirm_chars=1)
    sp.update("abcde")
    sp.update("abcde")  # committed "abcd"
    # regression: different char at position 2 -> committed shrinks to 1
    assert sp.update("abXde") == ""
    assert sp.committed == "a"


def test_reset():
    sp = StablePrefix()
    sp.update("hello")
    sp.update("hello")
    assert sp.committed != ""
    sp.reset()
    assert sp.committed == ""
    assert sp.update("hello") == ""


def test_shorter_window_no_regression_beyond_common():
    sp = StablePrefix(confirm_chars=1)
    sp.update("abcdef")
    sp.update("abcdef")
    assert sp.committed == "abcde"
    # window shrank (e.g. speech ended, window truncated): common=6 -> limit=5
    assert sp.update("abcdef") == ""
    assert sp.committed == "abcde"


def test_flush_commits_entire_tail():
    sp = StablePrefix(confirm_chars=2)
    sp.update("你好世界")
    sp.update("你好世界")   # committed "你好"
    tail = sp.flush()
    assert tail == "世界"   # unconfirmed tail handed back
    assert sp.committed == "你好世界"


def test_flush_empty_when_fully_committed():
    sp = StablePrefix(confirm_chars=1)
    sp.update("abc")
    sp.update("abc")        # common=3, limit=2 → committed "ab"
    expected_tail = sp.last_window[len(sp.committed):]  # "c"
    tail = sp.flush()
    assert tail == expected_tail
    assert sp.committed == sp.last_window


from pipeline.stable_prefix import IncrementalStablePrefix  # noqa: E402


def test_incremental_concatenates_chunks():
    sp = IncrementalStablePrefix(confirm_chars=1)
    # commit everything except the last `confirm` char of the accumulated tail
    assert sp.update_incremental("今天") == "今"
    assert sp.update_incremental("天气") == "天天"  # pending "天天天气", commit through "天"
    assert sp.committed == "今天天"


def test_incremental_flush_commits_tail():
    sp = IncrementalStablePrefix(confirm_chars=2)
    sp.update_incremental("今天天气不错")   # 6 chars, commit 4, pending "不错"
    tail = sp.flush()
    assert tail == "不错"
    assert sp.committed == "今天天气不错"


def test_incremental_full_text():
    sp = IncrementalStablePrefix(confirm_chars=2)
    sp.update_incremental("abc")
    sp.update_incremental("def")
    assert sp.full_text == "abcdef"


def test_incremental_revise_replaces_tail():
    sp = IncrementalStablePrefix(confirm_chars=2)
    sp.update_incremental("今天天气")        # committed "今天", pending "天气"
    sp.revise("天气好")                      # correct the tail
    tail = sp.flush()
    assert tail == "天气好"
    assert sp.committed == "今天天气好"  # revise corrected pending; flush commits all
