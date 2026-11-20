"""Gate 1 tests for tenant-safe thread ids.

WHAT: Round-trip and rejection cases for make/parse_thread_id.
WHERE: Verifies `agents/threads.py`.
WHY: Ambiguous thread ids could let one tenant's checkpoint be addressed as another's.
"""
import pytest

from agents.threads import make_thread_id, parse_thread_id


def test_round_trip():
    tid = make_thread_id("t1", "d1", "r1")
    assert tid == "t1:d1:r1"
    assert parse_thread_id(tid) == ("t1", "d1", "r1")


@pytest.mark.parametrize("parts", [("", "d", "r"), ("t", " ", "r"), ("t:x", "d", "r"), ("t", "d", "a:b")])
def test_rejects_bad_parts(parts):
    with pytest.raises(ValueError):
        make_thread_id(*parts)


@pytest.mark.parametrize("bad", ["a:b", "a:b:c:d", "a::c", ""])
def test_parse_rejects_malformed(bad):
    with pytest.raises(ValueError):
        parse_thread_id(bad)
