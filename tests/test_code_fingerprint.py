"""B9: a hash that identifies the running code, and one that catches code from elsewhere.

`docs/V1-DESIGN-RULES.md` B9's incident is "a bind-mounted daemon ran stale code that no hash
could identify". `meshychat/urls.py:198` reports `MESHYCHAT_GIT_SHA`, a BUILD-TIME environment
variable, which by construction cannot move when the running code does. These tests cover the
two measurements in `apps/core/code_fingerprint.py` that can.

Every negative below carries a positive control in the same test (C1), because "the digest did
not change" is also what a broken hasher that returns a constant looks like.

Run:  pytest tests/test_code_fingerprint.py -v   (no database needed)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from apps.core import code_fingerprint as cf  # noqa: E402


def test_it_actually_hashed_the_tree():
    """Positive control for every stability assertion below.

    The digest of an empty stream is stable and useless. Without this, a walker that found
    no files would pass `test_the_fingerprint_is_stable` perfectly.
    """
    count = cf.file_count()
    assert count > 20, f"only {count} served files found; the walker is not seeing the tree"
    names = {p.name for p in cf.served_files()}
    assert "dbconn.py" in names and "settings.py" in names, (
        "sanity: the served packages must include apps/core and meshychat"
    )


def test_the_fingerprint_is_stable_across_calls():
    """An unordered walk would make this fail intermittently, which is the worst outcome:
    a fingerprint that reports a change on every call trains its reader to ignore it."""
    assert cf.fingerprint() == cf.fingerprint()
    assert len(cf.fingerprint()) == 12
    assert len(cf.fingerprint(short=False)) == 64


def test_tests_and_bytecode_are_outside_the_fingerprint():
    """A fingerprint that moved when a test changed would report a different running server
    for an identical server. Bytecode is derived, not source.

    HONEST NOTE ON WHAT ENFORCES THIS, found by mutation: emptying
    `cf.EXCLUDED_DIR_NAMES` does NOT change the result, so that set is defense in depth
    rather than the mechanism. Two other things already exclude both: `tests/` is not under
    `SERVED_PACKAGES`, and the walk globs `*.py`, which never matches a `.pyc`. The
    assertions below therefore name the real reasons, so a later reader does not conclude
    the exclusion set is load-bearing and delete one of the things that is.
    """
    parts = {part for p in cf.served_files() for part in p.relative_to(ROOT).parts}
    assert "tests" not in parts
    assert not any(p.suffix == ".pyc" for p in cf.served_files())

    # The actual mechanisms, asserted directly.
    assert "tests" not in cf.SERVED_PACKAGES, (
        "tests/ is excluded because it is not a served package. If it is ever added to "
        "SERVED_PACKAGES, EXCLUDED_DIR_NAMES becomes load-bearing and needs its own test."
    )
    assert all(p.suffix == ".py" for p in cf.served_files())


def test_the_fingerprint_moves_when_served_code_changes(tmp_path):
    """THE ASSERTION. Planted inside the served tree, removed afterwards.

    Uses a real file in a real served package rather than a fixture directory, because the
    thing under test is whether THIS tree's digest responds to THIS tree changing.
    """
    before = cf.fingerprint()
    planted = ROOT / "apps" / "core" / "_fingerprint_probe.py"
    assert not planted.exists(), "probe file already present; a previous run did not clean up"
    try:
        planted.write_text("# planted probe AAAAAAAA\n", encoding="utf-8")
        after_add = cf.fingerprint()
        assert after_add != before, "adding a served file did not move the fingerprint"

        # SAME LENGTH, different bytes. This is the assertion that proves CONTENT is
        # hashed. An earlier version edited to a longer string, which mutation testing
        # showed was passing on the byte-length field alone: deleting the `h.update(body)`
        # line left this test green. A same-length edit can only be detected by content.
        planted.write_text("# planted probe BBBBBBBB\n", encoding="utf-8")
        after_edit = cf.fingerprint()
        assert after_edit != after_add, (
            "a same-length content change did not move the fingerprint, so file CONTENT "
            "is not being hashed"
        )
    finally:
        planted.unlink(missing_ok=True)

    assert cf.fingerprint() == before, (
        "the fingerprint did not return to its original value after cleanup, so it depends "
        "on something other than the served file set"
    )


def test_the_fingerprint_covers_path_not_just_content(tmp_path):
    """A rename with identical bytes must move the digest, or a reorganized tree reports as
    the same running code."""
    before = cf.fingerprint()
    a = ROOT / "apps" / "core" / "_probe_a.py"
    b = ROOT / "apps" / "core" / "_probe_b.py"
    body = "# identical body\n"
    try:
        a.write_text(body, encoding="utf-8")
        with_a = cf.fingerprint()
        a.unlink()
        b.write_text(body, encoding="utf-8")
        with_b = cf.fingerprint()
        assert with_a != with_b, "same bytes at a different path hashed identically"
        assert with_a != before and with_b != before  # control: both differ from clean
    finally:
        a.unlink(missing_ok=True)
        b.unlink(missing_ok=True)


# ------------------------------------------------------- code loaded from somewhere else

def test_a_healthy_process_loads_all_of_our_code_from_the_tree():
    """The control, and the state a real server should be in."""
    stray = cf.loaded_outside_tree()
    assert stray == {}, f"modules loaded from outside the repository: {stray}"


def test_a_module_loaded_from_outside_the_tree_is_detected(tmp_path, monkeypatch):
    """SENSITIVITY CONTROL for the test above.

    Without it, `loaded_outside_tree()` returning {} proves nothing: a function that always
    returned {} would pass the healthy case forever. This is the incident's shape, a module
    of ours executing from a path that is not this tree.
    """
    assert cf.loaded_outside_tree() == {}  # positive control: clean before planting

    class _Stray:
        __file__ = str(tmp_path / "somewhere_else" / "rls.py")

    monkeypatch.setitem(sys.modules, "apps.core._stray_probe", _Stray())
    detected = cf.loaded_outside_tree()
    assert "apps.core._stray_probe" in detected, (
        "a module of ours loaded from outside the repository was not detected, so the "
        "healthy result above is not evidence of anything"
    )
    assert str(tmp_path) in detected["apps.core._stray_probe"]


def test_report_carries_the_count_beside_the_digest():
    """A digest with no denominator cannot be told apart from a digest of nothing."""
    r = cf.report()
    assert set(r) == {"code_fingerprint", "code_files", "code_loaded_outside_tree"}
    assert r["code_files"] == cf.file_count() > 0
    assert r["code_fingerprint"] == cf.fingerprint()
