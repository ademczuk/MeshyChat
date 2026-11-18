"""The normalizer against the REAL shapes, not fixtures resembling them.

Every value below is a shape measured live in production on 2026-09-19, with its row
count, so a test here going red means a real population would be misprojected. No
database: this module is pure, and that is the point of testing it alone.

House rules: C1, every negative carries a positive control in the same test.

Run:  pytest tests/test_mesh_normalize.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.mesh import normalize as N  # noqa: E402


def _types(p: N.Projected, edge_type: str) -> list[tuple[str, str]]:
    return [(e.dst, e.src_field) for e in p.edges if e.edge_type == edge_type]


# ------------------------------------------------------------- the five shapes of `to`

def test_to_as_a_string_projects_one_recipient():
    """16,181 rows. The majority shape, and the one the v1 validator refuses for new writes."""
    p = N.edges_for({"to": "txxxx", "body": "x"}, author_label="kxxx")
    assert _types(p, "addressed_to") == [("txxxx", "to:string")]


def test_to_as_an_array_projects_each_recipient_once():
    """5,617 rows. Duplicates collapse: two edges to one recipient are one relation."""
    p = N.edges_for({"to": ["txxxx", "kxxx", "txxxx"], "body": "x"}, author_label="uxxx")
    assert sorted(_types(p, "addressed_to")) == [("kxxx", "to:array"), ("txxxx", "to:array")]


def test_to_absent_null_or_invalid_projects_nothing_and_says_why():
    """1,655 absent, 3 null. And an invalid type must not raise: one bad historical row
    would otherwise halt the catch-up for every row after it."""
    assert _types(N.edges_for({"body": "x"}, author_label="a"), "addressed_to") == []
    assert _types(N.edges_for({"to": None}, author_label="a"), "addressed_to") == []
    p = N.edges_for({"to": 42}, author_label="a")
    assert _types(p, "addressed_to") == []
    assert p.skipped.get("to:invalid_type") == 1


def test_non_string_elements_and_empty_labels_are_skipped_and_counted():
    p = N.edges_for({"to": ["txxxx", 7, "", "  ", None]}, author_label="a")
    assert _types(p, "addressed_to") == [("txxxx", "to:array")]
    assert p.skipped["to:non_string_element"] == 2
    assert p.skipped["to:empty"] == 2


def test_cc_string_and_array_both_project():
    """617 string, 5,022 array."""
    s = N.edges_for({"to": ["a"], "cc": "b"}, author_label="x")
    a = N.edges_for({"to": ["a"], "cc": ["b", "c"]}, author_label="x")
    assert _types(s, "copied_to") == [("b", "cc:string")]
    assert sorted(_types(a, "copied_to")) == [("b", "cc:array"), ("c", "cc:array")]


# ----------------------------------------------------------------- the thread spellings

def test_thread_id_is_canonical_and_wins():
    """13,725 rows carry thread_id."""
    p = N.edges_for({"to": ["a"], "thread_id": "t1"}, author_label="x")
    assert _types(p, "in_thread") == [("t1", "thread_id")]


def test_legacy_thread_is_read_when_thread_id_is_absent():
    """6,166 rows carry only `thread`. A reader that checks thread_id alone loses them."""
    p = N.edges_for({"to": ["a"], "thread": "t2"}, author_label="x")
    assert _types(p, "in_thread") == [("t2", "thread")]


def test_both_thread_spellings_agreeing_projects_one_edge_and_no_conflict():
    """1,960 rows carry both. The pong_rollcall shape: same value under three names."""
    p = N.edges_for({"to": ["a"], "thread_id": "t", "thread": "t", "reply_to": "t"},
                    author_label="x")
    assert _types(p, "in_thread") == [("t", "thread_id")]
    assert p.conflicts == ()
    # reply_to carrying a THREAD id, not a msg/ key, is not a reply pointer.
    assert _types(p, "replies_to") == []
    assert p.skipped.get("reply_to:not_a_msg_key") == 1


def test_both_thread_spellings_disagreeing_projects_canonical_and_reports_conflict():
    """Canonical wins; the disagreement is a fact for the caller, never a second edge."""
    p = N.edges_for({"to": ["a"], "thread_id": "canon", "thread": "legacy"}, author_label="x")
    assert _types(p, "in_thread") == [("canon", "thread_id")]
    assert len(p.conflicts) == 1 and "disagree" in p.conflicts[0]


def test_threadless_message_projects_no_thread_edge():
    """3,832 real messages carry no thread field at all and only 5 of those are replies:
    they are standalone, and an absent edge is the correct projection, not a defect."""
    p = N.edges_for({"to": ["a"], "body": "hello"}, author_label="x")
    assert _types(p, "in_thread") == []


# ------------------------------------------------------------------ the reply spellings

def test_in_reply_to_is_canonical():
    p = N.edges_for({"to": ["a"], "in_reply_to": "msg/1-ab"}, author_label="x")
    assert _types(p, "replies_to") == [("msg/1-ab", "in_reply_to")]


def test_legacy_reply_to_is_read_when_it_names_a_msg_key():
    """674 rows. Zero readers in the fleet read this field before this module."""
    p = N.edges_for({"to": ["a"], "reply_to": "msg/2-cd"}, author_label="x")
    assert _types(p, "replies_to") == [("msg/2-cd", "reply_to")]


def test_a_reply_pointer_that_is_not_a_msg_key_is_refused():
    for bad in ("t1", "", "MSG/1", None, 5):
        p = N.edges_for({"to": ["a"], "in_reply_to": bad}, author_label="x")
        assert _types(p, "replies_to") == [], f"{bad!r} was accepted as a reply pointer"
    # Positive control in the same test.
    assert _types(N.edges_for({"to": ["a"], "in_reply_to": "msg/9-z"}, author_label="x"),
                  "replies_to") == [("msg/9-z", "in_reply_to")]


# ------------------------------------------------------------------------- requires_ack

def test_requires_ack_bool_and_legacy_strings_project_one_edge_per_recipient():
    for raw in (True, "true", "True"):
        p = N.edges_for({"to": ["a", "b"], "requires_ack": raw}, author_label="x")
        assert sorted(d for d, _ in _types(p, "owes_ack")) == ["a", "b"], repr(raw)


def test_requires_ack_false_falsy_strings_and_the_integer_one_do_not():
    """'false' is a truthy string. 1 is not a claim anyone made. Neither owes an ack."""
    for raw in (False, "false", "False", 1, 0, None, "yes"):
        p = N.edges_for({"to": ["a"], "requires_ack": raw}, author_label="x")
        assert _types(p, "owes_ack") == [], repr(raw)
    assert _types(N.edges_for({"to": ["a"], "requires_ack": True}, author_label="x"),
                  "owes_ack") == [("a", "to:array")]


def test_requires_ack_with_no_recipients_is_counted_not_dropped_silently():
    p = N.edges_for({"requires_ack": True}, author_label="x")
    assert _types(p, "owes_ack") == []
    assert p.skipped.get("requires_ack:no_recipients") == 1


def test_closes_projects_each_msg_key_and_counts_historical_non_msg_values():
    """MUTANT: remove the msg/ predicate; the wiki value becomes a closure edge.

    New writes reject malformed closes. The replay remains total for older rows: it keeps
    usable msg/ targets and reports, rather than raises on, every other element.
    """
    p = N.edges_for({"closes": ["msg/1759824260000-a", "wiki/receipt", 4,
                                  "msg/1759824260000-a"]}, author_label="x")
    assert _types(p, "closes") == [("msg/1759824260000-a", "closes")]
    assert p.skipped["closes:not_a_msg_key"] == 2


def test_every_edge_destination_has_the_database_safe_bound():
    """MUTANT: remove the final `dst` bound; historical rows can wedge catch-up."""
    long = "x" * 513
    p = N.edges_for({"to": long, "cc": long, "thread_id": long,
                     "in_reply_to": "msg/" + long, "closes": ["msg/" + long]},
                    author_label=long)
    assert p.edges == ()
    assert p.skipped["dst:too_long"] == 6


def test_mention_patterns_have_finite_artifact_bounds():
    """MUTANT: widen any mention pattern; an unbounded body token becomes an edge dst."""
    body = ("x" * 40 + "/repo#1 " + "owner/" + "r" * 101 + "#1 "
                "owner/repo#1234567 OI-1234567 msg/1759824260000-" + "k" * 202)
    assert N.mention_refs(body) == ()


def test_mentions_canonicalize_the_measured_artifacts_without_hash_false_positives():
    """MUTANT: remove the word boundary; x#12 becomes a false mention.

    MUTANT: remove the SHA letter-and-digit gate; decafed becomes a commit. MUTANT: skip
    fenced code; the #80 reference disappears. Artifact mentions are body text, not a
    markdown execution parser.
    """
    body = ("Owner/Repo#12 #73 PR #74 PR 75 PRs #76 and #77 issue #78 pull #79 OI-47 "
            "msg/1759824260000-other. x#12 decafed 1759824260000 a1b2c3d4 "
            "https://example.test/a1b2c3d4 ``` #80 ```")
    assert N.mention_refs(body, own_key="msg/1759824260000-self") == (
        "gh:owner/repo#12", "gh:#12", "gh:#73", "gh:#74", "gh:#75", "gh:#76", "gh:#77",
        "gh:#78", "gh:#79", "oi:OI-47", "msg:msg/1759824260000-other",
        "commit:a1b2c3d", "gh:#80")
    assert N.canonical_mention_ref("PR 73") == "gh:#73"
    assert N.canonical_mention_ref("OWNER/Repo#12") == "gh:owner/repo#12"


@pytest.mark.parametrize(("raw", "canonical"), [
    ("gh:#73", "gh:#73"), ("gh:owner/repo#73", "gh:owner/repo#73"),
    ("commit:abc1234", "commit:abc1234"), ("oi:OI-5", "oi:OI-5"),
    ("commit:1234567", "commit:1234567"),         # the index's form for a sha whose first seven are digits
    ("msg:msg/1759824260000-target", "msg:msg/1759824260000-target"),
])
def test_canonical_mention_refs_round_trip_through_the_route_parser(raw, canonical):
    """MUTANT: parse only typed prose forms; API-returned refs cannot be queried back."""
    assert N.canonical_mention_ref(raw) == canonical


def test_a_bare_key_only_a_qualified_reference_wrote_is_marked_as_an_index_key():
    """MUTANT: give every mention the src_field `body`; a search for one repository's #73 then takes another's.
    MUTANT: decide the field from the first occurrence; a bare #73 after another repo's #73 stops counting."""
    assert N.mention_ref_fields("Acme/Widget#73") == (("gh:acme/widget#73", "body"), ("gh:#73", N.QUALIFIED_ONLY_FIELD))
    assert N.mention_ref_fields("Other/Repo#73 and #73") == (("gh:other/repo#73", "body"), ("gh:#73", "body"))
    assert N.mention_ref_fields("#73 then Acme/Widget#73") == (("gh:#73", "body"), ("gh:acme/widget#73", "body"))
    assert N.mention_refs("Acme/Widget#73") == ("gh:acme/widget#73", "gh:#73")


def test_every_ref_the_index_writes_queries_back():
    """Codex on #93: a sha whose first seven characters are digits was indexed as commit:1234567, and querying that
    ref answered 422. Every ref mention_refs writes must parse to itself. MUTANT: require a letter after commit:."""
    body = "landed 1234567abcdef0 and a1b2c3d, see acme/widget#73, #74, OI-5 and msg/1759824260000-target"
    refs = N.mention_refs(body, own_key="msg/1759824260001-self")
    assert "commit:1234567" in refs and len(refs) >= 5, refs
    for ref in refs:
        assert N.canonical_mention_ref(ref) == ref, ref
    assert N.canonical_mention_ref("1234567") is None, "bare digits are a number, not a commit"


def test_mentions_reject_nonce_identifier_date_and_file_anchor_false_positives():
    """MUTANT: loosen commit or GitHub context checks; synthetic identifiers become graph refs."""
    body = ("nonce deadbeef sha256 c0ffee1 peer-a1b2c3d desktop-a1b2c3d node-a1b2c3d "
            "550e8400-e29b-41d4-a716-446655440000 msg/1759824260000-a1b2c3d "
            "https://example.test/a1b2c3d issue 2026-09-30 pull 3 times "
            "docs/DIVERGENCES.md#070; real commit a1b2c3d and PR 73")
    assert N.mention_refs(body) == ("msg:msg/1759824260000-a1b2c3d", "commit:a1b2c3d", "gh:#73")


# -------------------------------------------------------------------------- authorship

def test_author_is_the_stamped_label_and_a_lying_from_is_counted():
    """The board's rule: the actor is the authenticated row, never the body's claim."""
    p = N.edges_for({"to": ["a"], "from": "someone-else"}, author_label="realactor")
    assert _types(p, "authored_by") == [("realactor", "author_label")]
    assert p.skipped.get("from:disagrees_with_author_label") == 1
    # Agreeing `from` is not counted as a disagreement.
    q = N.edges_for({"to": ["a"], "from": "realactor"}, author_label="realactor")
    assert "from:disagrees_with_author_label" not in q.skipped


def test_a_non_object_value_yields_nothing_and_says_so():
    """State rows and cursors in the namespace are not messages."""
    for v in (None, "text", 3, ["a"]):
        p = N.edges_for(v, author_label="x")
        assert p.edges == ()
        assert p.skipped == {"value:not_an_object": 1}


def test_project_row_binds_the_row_identity_to_every_edge():
    import datetime as _dt
    when = _dt.datetime(2026, 9, 18, 23, 11, tzinfo=_dt.timezone.utc)
    re = N.project_row({"id": "00000000-0000-0000-0000-000000000001", "key": "msg/1-a",
                        "version": 2, "value": {"to": "b", "thread_id": "t"},
                        "author_label": "a", "created_at": when})
    rows = re.rows()
    assert {r[0] for r in rows} == {"00000000-0000-0000-0000-000000000001"}
    assert {r[1] for r in rows} == {"msg/1-a"}
    assert {r[2] for r in rows} == {2}
    assert {r[6] for r in rows} == {when}
    assert sorted(r[3] for r in rows) == ["addressed_to", "authored_by", "in_thread"]


@pytest.mark.parametrize("raw", ["issue #73", "pull #73", "Pull #73", "gh:issue #73", "gh:pull #73",
                                 "#73", "PR #73", "gh:#73"])
def test_every_bare_issue_form_canonicalises_to_its_number(raw):
    """codex on #89: `issue #73` and `pull #73` match the bare pattern's THIRD group, which the query
    parser never read, so they became gh:#None and found nothing, while a body saying the same was
    indexed as gh:#73.

    MUTANT: read only groups 1 and 2 again; the issue and pull forms give gh:#None."""
    assert N.canonical_mention_ref(raw) == "gh:#73"
    assert N.mention_refs("see " + raw.removeprefix("gh:")) == ("gh:#73",)


def test_an_issue_word_without_a_hash_is_not_a_reference():
    """The precision rule stays: `issue 2026-09-28` once read as gh:#2026, so the word form needs the #."""
    assert N.canonical_mention_ref("issue 73") is None
    assert N.mention_refs("see issue 73") == ()


# -------------------------------------------------------------------------- hash: and path: (issue #159)

_PIN = "09cb6def2cd18bb01d7c4579c16822e7cedaf23ce440d82fbfa87d464078e8a0"


def test_a_digest_is_kept_whole_and_its_first_8_are_an_index_key():
    """The fleet's real shapes: a sha256 after `sha256` (which commit: skips), an 8-character citation with an
    ellipsis, a 12-character head and a 40-character commit id, each kept as written.

    MUTANT: cut the token to 7 as commit: does; the full pin is no edge. MUTANT: omit the prefix key; an 8-character
    query cannot find the full pin by equality. MUTANT: mark the prefix key `body`; it reads as named."""
    body = (f"PAYLOAD v5-TR.bin: 342,690,655 B, sha256 {_PIN}. boot.bin v5 09cb6def..., "
            "index head b1526e3359bc, commit 9803258b4a0e5c1d2f3a4b5c6d7e8f9a0b1c2d3e.")
    assert N.artifact_ref_fields(body) == (
        ("path:v5-TR.bin", "body"),
        (f"hash:{_PIN}", "body"), ("hash:09cb6def", "body"),
        ("path:boot.bin", "body"),
        ("hash:b1526e3359bc", "body"), ("hash:b1526e33", N.HASH_PREFIX_FIELD),
        ("hash:9803258b4a0e5c1d2f3a4b5c6d7e8f9a0b1c2d3e", "body"), ("hash:9803258b", N.HASH_PREFIX_FIELD))
    # The body names 09cb6def itself as well, so that key is evidence (`body`); alone, the full pin writes it as an index key.
    assert N.artifact_ref_fields(f"sha256 {_PIN}") == ((f"hash:{_PIN}", "body"), ("hash:09cb6def", N.HASH_PREFIX_FIELD))


def test_hash_exclusions_each_beside_its_positive_control():
    """C1: every refusal here has a control the same test accepts.

    MUTANT: drop the nonce, peer or key-id checks; an identifier becomes a digest. MUTANT: drop the URL, UUID or
    msg-key exclusions. MUTANT: accept uppercase; a serial-like run becomes a node. MUTANT: drop the digest-word gate;
    an all-digit or all-letter run in prose becomes a hash. MUTANT: allow 65 characters or 7."""
    refused = ("nonce deadbeef1234 peer 610170d5 kid 10b1dc0834865bab 550e8400-e29b-41d4-a716-446655440000 "
               "msg/1791221066795-kxxx-3d0659ab https://example.test/a1b2c3d4e5 09CB6DEF1234 12345678 deadbeefcafe "
               "a1b2c3d 0x1234abcd " + "a1" * 32 + "b")
    assert N.artifact_ref_fields(refused) == ()
    accepted = "digest 12345678, sha256 deadbeefcafe, signed a1b2c3d4"
    assert N.mention_refs(accepted) == ("commit:a1b2c3d",)
    assert [ref for ref, _ in N.artifact_ref_fields(accepted)] == [
        "hash:12345678", "hash:deadbeefcafe", "hash:deadbeef", "hash:a1b2c3d4"]
    # A word that only contains `sha` is not a digest word.
    assert N.artifact_ref_fields("we shall 12345678 share deadbeefcafe") == ()


def test_paths_relative_and_file_names_with_a_listed_extension():
    """MUTANT: index absolute or home paths; one machine's layout becomes graph nodes. MUTANT: take a path out of a
    URL. MUTANT: list key, pem or env files. MUTANT: omit the file-name key; a file-name query misses the path."""
    body = ("see pxx-linux/sitting-v5/esp-v5.sh, then esp-v5.sh. apps/mesh/normalize.py:113 and related.py:10; "
            "shared as sampleco/MeshyChat@9803258abcde:docs/DIVERGENCES.md and .github/workflows/x.yml")
    assert [r for r in N.artifact_ref_fields(body) if r[0].startswith("path:")] == [
        ("path:pxx-linux/sitting-v5/esp-v5.sh", "body"), ("path:esp-v5.sh", "body"),
        ("path:apps/mesh/normalize.py", "body"), ("path:normalize.py", N.PATH_NAME_FIELD),
        ("path:related.py", "body"),
        ("path:docs/DIVERGENCES.md", "body"), ("path:DIVERGENCES.md", N.PATH_NAME_FIELD),
        ("path:.github/workflows/x.yml", "body"), ("path:x.yml", N.PATH_NAME_FIELD)]
    refused = ("/Users/user/pxx-sitting1/run.txt ~/.brain-svc/headers.sh ./run.sh ../a/b.sh "
               "https://github.com/a/b/blob/main/x.py id.key cert.pem prod.env brain.acmeonedev.com 1.30.0 e.g.")
    assert N.artifact_ref_fields(refused) == ()


def test_the_four_older_kinds_are_untouched_by_the_new_ones():
    """The new kinds have their own cap and their own function: 40 digests and paths in a body cannot push a msg:
    or gh: reference out, and mention_ref_fields gives exactly what it gave before.

    MUTANT: merge the two lists under one cap of 32; the msg key and #73 at the end are lost."""
    body = " ".join(f"sha256 {i:08x}{'ab' * 4} f{i}.md" for i in range(40)) + " msg/1759824260000-tail PR #73"
    legacy = N.mention_ref_fields(body)
    assert ("msg:msg/1759824260000-tail", "body") in legacy and ("gh:#73", "body") in legacy
    assert not [r for r, _ in legacy if r.startswith(("hash:", "path:"))]
    artifacts = N.artifact_ref_fields(body)
    assert len(artifacts) == 32 and all(r.startswith(("hash:", "path:")) for r, _ in artifacts)
    assert N.all_mention_fields(body) == legacy + artifacts


@pytest.mark.parametrize(("raw", "canonical"), [
    ("hash:09CB6DEF", "hash:09cb6def"), (f"hash:{_PIN}", f"hash:{_PIN}"), (_PIN, f"hash:{_PIN}"),
    ("a1b2c3d4", "commit:a1b2c3d"),               # a bare token of 7 to 40 stays a commit, as before
    ("hash:12345678", "hash:12345678"),
    ("12345678", None), ("deadbeefcafe", None),  # bare, a number or a word: only the prefix makes it a hash
    ("esp-v5.sh", "path:esp-v5.sh"), ("path:pxx-linux/sitting-v5/esp-v5.sh", "path:pxx-linux/sitting-v5/esp-v5.sh"),
    ("hash:1234567", None), ("hash:xyz", None), ("hash:" + "a" * 65, None),
    ("path:/abs/x.sh", None), ("path:x.key", None), ("path:~/x.sh", None),
])
def test_hash_and_path_queries_parse(raw, canonical):
    """MUTANT: parse a bare 8-to-40 token as hash:; every existing commit: query changes meaning, and a bare number
    becomes a digest."""
    assert N.canonical_mention_ref(raw) == canonical


def test_every_hash_and_path_ref_the_index_writes_queries_back():
    """As for the older kinds (codex on #93): a ref the index writes must parse to itself, index-only keys included."""
    body = (f"sha256 {_PIN}, digest 12345678, b1526e3359bc, pxx-linux/sitting-v5/esp-v5.sh, "
            "x@9803258abcde:docs/D.md, v1.2.tar.gz")
    refs = [ref for ref, _ in N.artifact_ref_fields(body)]
    assert len(refs) == 12, refs
    for ref in refs:
        assert N.canonical_mention_ref(ref) == ref, ref


def test_the_projection_writes_both_lists_and_the_gate_withholds_both():
    """MUTANT: project only mention_ref_fields; hash: and path: never become edges. MUTANT: gate only the older
    kinds; a forbidden body's digests are written."""
    row = {"id": "00000000-0000-0000-0000-000000000159", "key": "msg/1759824260159-a", "version": 1,
           "value": {"to": "b", "body": f"PR #73 sha256 {_PIN} esp-v5.sh"}, "author_label": "a",
           "created_at": None}
    allowed = {e.dst for e in N.project_row(row).projected.edges if e.edge_type == "mentions"}
    assert allowed == {"gh:#73", f"hash:{_PIN}", "hash:09cb6def", "path:esp-v5.sh"}
    withheld = {e.dst for e in N.project_row(row, mentions_allowed=False).projected.edges if e.edge_type == "mentions"}
    assert withheld == set()


def test_the_cap_keeps_a_reference_and_its_index_key_together():
    """Codex on #159: with 31 references before it, a directory path or a long hash took the 32nd slot and lost its
    key, and graph.mentions, which finds every hash: and path: row through that key, could never return the row.

    MUTANT: cap the references one at a time again; the last path is kept without path:last.py."""
    files = " ".join(f"f{i}.py" for i in range(31))
    for tail in (" dir/last.py", " sha256 " + "ab12" * 16):
        refs = [ref for ref, _ in N.artifact_ref_fields(files + tail)]
        assert len(refs) == 31 and refs[-1] == "path:f30.py", refs[-3:]
    fits = [ref for ref, _ in N.artifact_ref_fields(" ".join(f"f{i}.py" for i in range(30)) + " dir/last.py")]
    assert fits[-2:] == ["path:dir/last.py", "path:last.py"]
    for body in (files + " dir/last.py", " ".join(f"g{i}/h{i}.md sha256 {i:04x}cafe1234" for i in range(20))):
        refs = {ref for ref, _ in N.artifact_ref_fields(body)}
        for ref in refs:
            kind, value = ref.split(":", 1)
            key = f"hash:{value[:N.HASH_PREFIX_LEN]}" if kind == "hash" else f"path:{value.rsplit('/', 1)[-1]}"
            assert key in refs, (ref, key)


def test_an_extension_must_end_the_file_name():
    """Codex on #159: `README.md.old` was indexed as README.md, and `config.json.key` as config.json, a plain hit for a
    file the message never named, and a way round the excluded key and environment types.

    MUTANT: drop the lookahead after the extension; the three come back as the shorter names. MUTANT: allow a tilde."""
    assert N.artifact_ref_fields("README.md.old config.json.env config.json.key notes.txt.bak") == ()
    # Backups (codex on #159, round 2): a trailing tilde and a numbered backup name another file too.
    assert N.artifact_ref_fields("config.json~ `config.json~` README.md.~1~") == ()
    assert [ref for ref, _ in N.artifact_ref_fields("v1.2.tar.gz, esp-v5.sh. a.md... x.py:12 (b.json)")] == [
        "path:v1.2.tar.gz", "path:esp-v5.sh", "path:a.md", "path:x.py", "path:b.json"]


def test_no_tail_of_an_absolute_path_with_a_space_is_a_relative_path():
    """Codex on #159: in `/Users/alice/My Project/config.json` the scan restarted after the space and took
    path:Project/config.json, one machine's directory read as a repository path.

    MUTANT: drop the quoted-span exclusion; the backticked and quoted paths leak their tails. MUTANT: drop the
    one-space continuation check; the unquoted one does. MUTANT: treat an apostrophe as a quote; the quoted path after
    `it's` is no longer one span. MUTANT: keep the quote mark on the token before; an unclosed quote's tail leaks.
    MUTANT: drop the unquoted-run exclusion; a digest directory in a local path becomes a hash. MUTANT: check only
    paths for the one-space continuation; the digest directory after a space does. MUTANT: take a closed quote for
    an open one; a copy's repository destination is lost. The controls after punctuation and in plain prose are
    still taken."""
    leaks = ("`/Users/alice/My Project/config.json`, \"/Users/a/b c/d.md\", '/Users/a/e f/g.md', it's '/Users/a h/i.json' "
             "and /Users/alice/My Project/config.json and C:\\Users\\a b\\c.json and ~/x y/z.md")
    assert N.artifact_ref_fields(leaks) == ()
    # Two spaces: only the quoted span stops these (the one-space check cannot), and only if `it's` opens no quote.
    assert N.artifact_ref_fields("`/Users/a/My Big Project/x.json` it's '/Users/a/My Big Project/y.json' ok") == ()
    # An unclosed quote is no span; the one-space check still sees the path behind its quote mark.
    assert N.artifact_ref_fields("`/Users/a b/c.json and more") == ()
    # A digest inside one machine's path is that machine's directory name, not a citation, past a space as well
    # (codex on #159, round 2); after punctuation it is a citation again.
    assert N.artifact_ref_fields("/private/tmp/09cb6def1234/out.txt /Users/alice/My 09cb6def5678/out.txt") == ()
    assert [ref for ref, _ in N.artifact_ref_fields("/tmp/x, sha256 09cb6def1234")] == ["hash:09cb6def1234", "hash:09cb6def"]
    # A closed quote ends the local path: the destination of a copy is a repository path (codex on #159, round 2).
    assert [ref for ref, _ in N.artifact_ref_fields('cp "/tmp/output" docs/report.md; cp `/tmp/x` tools/r.py')] == [
        "path:docs/report.md", "path:report.md", "path:tools/r.py", "path:r.py"]
    controls = "logs in /tmp/x, then docs/a.md; it's in pxx-linux/b.sh and 'tools/c.py'"
    assert [ref for ref, _ in N.artifact_ref_fields(controls)] == [
        "path:docs/a.md", "path:a.md", "path:pxx-linux/b.sh", "path:b.sh", "path:tools/c.py", "path:c.py"]


def test_identifier_values_numbered_prose_and_overlong_paths_are_not_references():
    """memory-bxxxxx's review of #161: `peer_id: <hex>` and a pasted JSON `"kid"` became digests, `3.c` in prose a
    file, and a path reference over the 512-character dst bound kept only its file-name key, which no query resolves.

    MUTANT: the old suffix list; peer_id: and the JSON kid come back. MUTANT: keep digit-only bare names; `3.c` is a
    file. MUTANT: test only the first dotted part of the stem; versioned archives vanish. MUTANT: drop the length
    check; the overlong path writes its file-name key alone."""
    assert N.artifact_ref_fields('peer_id: a2b5be8f3, "kid": "10b1dc0834865bab", key_id=10b1dc08, PEER 610170d5') == ()
    assert [r for r, _ in N.artifact_ref_fields("the peer's digest 09cb6def1234")] == ["hash:09cb6def1234",
                                                                                         "hash:09cb6def"]
    assert N.artifact_ref_fields("see section 3.c and 4.h") == ()
    assert [r for r, _ in N.artifact_ref_fields("download 1.2.3.tar.gz, inspect 2026.report.json")] == [
        "path:1.2.3.tar.gz", "path:2026.report.json"]          # only a WHOLE digit stem is numbering
    assert [r for r, _ in N.artifact_ref_fields("a3.c and docs/3.c")] == ["path:a3.c", "path:docs/3.c", "path:3.c"]
    assert N.artifact_ref_fields(("d" * 99 + "/") * 6 + "x.md") == ()
    fits = ("d" * 99 + "/") * 4 + "y.md"
    assert [r for r, _ in N.artifact_ref_fields(fits)] == [f"path:{fits}", "path:y.md"]


def _hashes(p: N.Projected) -> list[tuple[str, str]]:
    """The projected `hash:` mentions alone. The older `commit:` kind reads a hex token by its own rules."""
    return [(dst, field) for dst, field in _types(p, "mentions") if dst.startswith("hash:")]


def test_a_key_or_peer_field_with_a_prefix_is_an_identifier_and_another_word_is_not():
    """memory-bxxxxx's review of #163: `\b` finds no boundary between `_` and `kid`, so `"signing_kid": "<hex>"`, the
    key-id field of every pasted signed row, was still a digest, and the backfill only adds rows.

    MUTANT: the leading `\b` again; every prefixed field comes back as a hash. MUTANT: no leading bound; `skid` hides
    its digest. MUTANT: no backtick or bracket before the value; the Markdown and parenthesis forms are digests.
    MUTANT: one opening delimiter at most; `kid: ["<hex>"]` and kid (`<hex>`) are digests. MUTANT: one closing mark
    after the name at most; `**signing_kid**:` and a double-backtick span are digests."""
    for name in ("signing_kid", "active_kid", "peer_kid", "result_signing_kid", "args_signing_kid", "issued_under_kid",
                 "caller_peer_id", "created_by_peer_id", "from_peer", "signing_key_id", "signing_keyid",
                 "x-signing-kid", "result.signing_kid"):
        for text in (f'"{name}": "10b1dc0834865bab"', f"{name}=10b1dc0834865bab", f"{name}: 10b1dc0834865bab",
                     f"{name.upper()} 10b1dc0834865bab", f"`{name}` = `10b1dc0834865bab`",
                     f"{name} (10b1dc0834865bab)", f"{name} [10b1dc0834865bab]",
                     # codex: the opening delimiters compose, with space between them
                     f"{name} ( 10b1dc0834865bab )", f"{name} (`10b1dc0834865bab`)", f'{name}: ["10b1dc0834865bab"]',
                     f"{name}=<10b1dc0834865bab>",
                     # codex round 3: Markdown around the field name, and around the value
                     f"**{name}**: `10b1dc0834865bab`", f"``{name}``: ``10b1dc0834865bab``",
                     f"__{name}__ = **10b1dc0834865bab**", f"*{name}* 10b1dc0834865bab"):
            assert N.artifact_ref_fields(text) == (), text
    # The fleet's own prose, hard-wrapped as the project's design log (not part of this release) writes it.
    wrapped = "0 live frames and kid `6dd0bdfd0cc2554b`; all 49 anchors carry kid\n`6dd0bdfd0cc2554b`"
    assert N.artifact_ref_fields(wrapped) == ()
    pasted = '{"error_message": null,\n  "result_signing_kid": "10b1dc0834865bab",\n  "sha256": "09cb6def1234"}'
    assert [r for r, _ in N.artifact_ref_fields(pasted)] == ["hash:09cb6def1234", "hash:09cb6def"]
    assert _hashes(N.edges_for({"body": pasted}, author_label="x")) == [
        ("hash:09cb6def1234", "body"), ("hash:09cb6def", N.HASH_PREFIX_FIELD)]
    for word in ("skid", "kidney", "sha256", "\u00e9kid", "keyid7"):
        assert [r for r, _ in N.artifact_ref_fields(f"{word} 09cb6def1234")] == ["hash:09cb6def1234",
                                                                                   "hash:09cb6def"], word


def test_the_identifier_rule_reads_the_whole_field_on_the_body_itself():
    """Codex on the second follow-up. A field that names a digest keeps its value, whatever name it ends in. And the
    rule reads the body, not a lowercased 20-character slice of it: the slice lost a field before an aligned or
    wrapped value, made a word boundary where it happened to begin, and `lower()` turned U+0130 into an `i` and a
    combining dot, which is no letter.

    MUTANT: skip without reading the field; `sha256_of_peer=<hex>` is lost. MUTANT: read the field with _HASH_WORDS;
    `md5_of_peer=<hex>` is lost. MUTANT: any word that starts `sha`; `shared_peer` is a digest. MUTANT: search the
    20-character slice;
    the wrapped JSON value is a digest and `skid` with 17 spaces hides one. MUTANT: lowercase first; the digest
    after U+0130 is lost. MUTANT: search a slice of KEY_CONTEXT characters; `skid:` at its edge reads as `kid:`.
    MUTANT: move KEY_CONTEXT; the edge is no longer at 96."""
    digest = ["hash:09cb6def1234", "hash:09cb6def"]
    for text in ("sha256_of_peer=09cb6def1234", "checksum_for_kid: 09cb6def1234", "sha-256.of-peer (09cb6def1234)",
                 "digest_key_id=`09cb6def1234`", 'sha256_of_peer: ["09cb6def1234"]', "xxhash_kid (`09cb6def1234`)",
                 "sha3_256_peer=09cb6def1234", "sha1sum_peer 09cb6def1234", "crc32_of_kid: 09cb6def1234",
                 "**sha256_of_peer**: `09cb6def1234`", "``sha256_of_peer``: ``09cb6def1234``"):
        assert [r for r, _ in N.artifact_ref_fields(text)] == digest, text
    # The real md5, sha3-256 and blake2b of b"peer" (codex): a digest's own name in the field wins, at full length.
    for field, value in (("md5_of_peer", "f8fe68b4c4cba197efa9c8bbd45f144e"),
                         ("sha3_256_of_peer", "903f2e32e8acb7a178264097db75791df182d925b952f45c08402bb69b05bd69"),
                         ("blake2b_of_peer", "1460e9ff6d4962c7688edd72c226857879550ee18dd4cc1cc5d558698c4e06c0")):
        named = N.edges_for({"body": f"{field}={value}"}, author_label="x")
        assert _hashes(named) == [(f"hash:{value}", "body"), (f"hash:{value[:8]}", N.HASH_PREFIX_FIELD)], field
    # `sha` is a digest's name with digits or alone, not as the start of another word.
    for text in ("shared_peer=8fd74872aa", "shadow_kid: 10b1dc0834865bab", "blakely_peer 8fd74872aa"):
        assert N.artifact_ref_fields(text) == (), text
    full = "0" * 24 + "09cb6def1234" + "a" * 28
    assert [r for r, _ in N.artifact_ref_fields(f"sha256_of_peer={full}")] == [f"hash:{full}", "hash:00000000"]
    assert N.artifact_ref_fields(f"created_by_peer={full}") == ()
    assert N.artifact_ref_fields('"signing_kid":\n' + " " * 16 + '"10b1dc0834865bab"') == ()
    assert N.artifact_ref_fields("signing_kid:" + " " * 30 + "10b1dc0834865bab") == ()
    assert [r for r, _ in N.artifact_ref_fields("skid" + " " * 17 + "09cb6def1234")] == digest
    assert [r for r, _ in N.artifact_ref_fields("\u0130kid 09cb6def1234")] == digest
    both = N.edges_for({"body": "\u0130kid 09cb6def1234, sha256_of_peer=09cb6def1234"}, author_label="x")
    assert _hashes(both) == [("hash:09cb6def1234", "body"), ("hash:09cb6def", N.HASH_PREFIX_FIELD)]
    wrapped = N.edges_for({"body": '"signing_kid":\n' + " " * 16 + '"10b1dc0834865bab"'}, author_label="x")
    assert _hashes(wrapped) == []
    # The field must start within KEY_CONTEXT (96) characters of its value: at the edge it is read, one further it
    # is not. And at that edge the word before it still counts: `skid:` is not `kid:`.
    assert N.artifact_ref_fields("kid:" + " " * 92 + "10b1dc0834865bab") == ()
    assert [r for r, _ in N.artifact_ref_fields("skid:" + " " * 92 + "09cb6def1234")] == digest
    assert [r for r, _ in N.artifact_ref_fields("kid:" + " " * 93 + "10b1dc0834865bab")] == ["hash:10b1dc0834865bab",
                                                                                              "hash:10b1dc08"]


def test_a_path_reference_at_the_dst_bound_keeps_its_key_and_one_over_drops_both(monkeypatch):
    """memory-bxxxxx's test from the #163 review, with the edge loop added: the path rule and edges_for share
    MAX_DST_LEN, so a reference is never dropped by one while the other keeps its file-name key.

    MUTANT: raise the bound in the path rule alone; the 513-character reference is refused by edges_for and its
    file-name key is written with nothing to resolve to. MUTANT: move MAX_DST_LEN; the edge is no longer at 512.
    MUTANT: write 512 at either site again; that site ignores the bound when it moves (codex)."""
    prefix = ("d" * 99 + "/") * 5
    at, over = prefix + "x" * 4 + ".md", prefix + "x" * 5 + ".md"
    assert len("path:" + at) == 512 and len("path:" + over) == 513
    assert [r for r, _ in N.artifact_ref_fields(at)] == ["path:" + at, "path:xxxx.md"]
    assert N.artifact_ref_fields(over) == ()
    kept = N.edges_for({"body": at}, author_label="x")
    assert _types(kept, "mentions") == [("path:" + at, "body"), ("path:xxxx.md", N.PATH_NAME_FIELD)]
    dropped = N.edges_for({"body": over}, author_label="x")
    assert _types(dropped, "mentions") == [] and "dst:too_long" not in dropped.skipped
    # Both sites read the one constant: move it, and both move with it.
    monkeypatch.setattr(N, "MAX_DST_LEN", 40)
    short_at, short_over = "d" * 30 + "/x.md", "d" * 31 + "/x.md"
    assert len("path:" + short_at) == 40 and len("path:" + short_over) == 41
    assert [r for r, _ in N.artifact_ref_fields(short_at)] == ["path:" + short_at, "path:x.md"]
    assert N.artifact_ref_fields(short_over) == ()
    assert _types(N.edges_for({"thread_id": "t" * 40}, author_label="x"), "in_thread") == [("t" * 40, "thread_id")]
    refused = N.edges_for({"thread_id": "t" * 41}, author_label="x")
    assert _types(refused, "in_thread") == [] and refused.skipped["dst:too_long"] == 1
