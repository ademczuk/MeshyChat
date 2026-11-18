"""The read-only mention audit (apps/mesh/audit.py, divergence 089): which artifact edges the extractor no longer
writes, and which digests a backfill would write. This module is the pure half, with no database; the two reads and
the CLI are in tests/test_mesh_projection.py.

House rules: C1, every negative carries a positive control in the same test.

Run:  pytest tests/test_mesh_audit.py -v
"""
from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.mesh import audit as A  # noqa: E402
from apps.mesh import normalize as N  # noqa: E402

_T0 = _dt.datetime(2026, 10, 4, 7, 27, tzinfo=_dt.timezone.utc)


def _row(body: str, key: str = "msg/1791098092093-kxxx-733e08") -> dict:
    return {"id": "0b7c0d3e-0000-4000-8000-000000000001", "key": key, "version": 1, "value": {"body": body},
            "author_label": "kxxx", "created_at": _T0}


def test_a_key_id_an_older_extractor_indexed_is_stale_and_the_rows_real_references_are_not():
    """MUTANT: list every artifact edge; the digest and the path the row does name are stale too. MUTANT: compare
    all mention kinds; the older `commit:` and `gh:` edges are listed. MUTANT: ask the embed gate; a row whose
    mentions are not allowed has every edge listed, though the extractor still writes them."""
    row = _row('"signing_kid": "10b1dc0834865bab", sha256 09cb6def1234 in docs/a.md, PR #7')
    written = ["hash:10b1dc0834865bab", "hash:10b1dc08",        # the key id, as the extractor before the fix read it
               "hash:09cb6def1234", "hash:09cb6def", "path:docs/a.md", "path:a.md",
               "gh:#7", "commit:10b1dc0", "commit:09cb6de"]
    assert A.stale_artifacts(row, written) == ["hash:10b1dc08", "hash:10b1dc0834865bab"]
    assert A.stale_artifacts(row, written + ["path:gone.md", "path:gone.md"]) == [
        "hash:10b1dc08", "hash:10b1dc0834865bab", "path:gone.md"]
    assert A.stale_artifacts(row, ["gh:#999", "commit:fffffff", "msg:msg/1-x"]) == []
    assert A.stale_artifacts(_row("nothing here"), []) == []


def test_an_index_key_another_reference_still_needs_is_not_stale():
    """The 8-character key is shared: the key id's own edge goes, but a digest in the same row with the same first
    eight characters keeps the key. MUTANT: derive staleness from the stale reference's prefix; the key is listed
    and a delete would leave the digest unreachable."""
    row = _row("kid 10b1dc0834865bab signed it; sha256 10b1dc08aaaa1111 is the file")
    written = ["hash:10b1dc0834865bab", "hash:10b1dc08aaaa1111", "hash:10b1dc08"]
    assert A.stale_artifacts(row, written) == ["hash:10b1dc0834865bab"]
    alone = _row("kid 10b1dc0834865bab signed it")
    assert A.stale_artifacts(alone, written) == ["hash:10b1dc08", "hash:10b1dc0834865bab", "hash:10b1dc08aaaa1111"]


def test_the_context_is_of_the_occurrence_the_extractor_reads_not_of_the_first_one():
    """The same token after `kid` is a key id and after `sha256` a digest: the report shows the second.
    MUTANT: take the first occurrence of each named value; the context reads `kid `, and a reader concludes the
    rule failed. MUTANT: do not collapse whitespace; a wrapped line and an aligned column read as two contexts."""
    body = "kid 10b1dc0834865bab signed\n    sha256:   10b1dc0834865bab and digest\t09cb6def1234"
    # The third field is per row: `commit:` skips a token after `sha256`, but takes the same one after `kid`.
    assert A.digest_contexts(body) == [("10b1dc0834865bab", "34865bab signed sha256: ", True),
                                       ("09cb6def1234", "dc0834865bab and digest ", False)]
    assert [c[:2] for c in A.digest_contexts(body, width=8)] == [("10b1dc0834865bab", "sha256: "),
                                                                 ("09cb6def1234", " digest ")]
    assert [c[1] for c in A.digest_contexts(body, width=0)] == ["", ""]
    assert A.digest_contexts("sha256 09cb6def1234", width=24) == [("09cb6def1234", "sha256 ", False)]
    assert A.digest_contexts(None) == [] and A.digest_contexts("kid 10b1dc0834865bab") == []


def test_a_bare_sha_is_marked_as_the_commit_kinds_too_and_a_named_digest_is_not():
    """kxxx's production row after the first deploy: one SHA, written bare and after `commit`, became
    `commit:ea0a130`, `hash:ea0a130b8e64` and the key `hash:ea0a130b`. That is the design (089), and the report says
    how much of a length class it is. A token after `sha256` is skipped by `commit:`, so it is a digest alone.
    MUTANT: mark every digest; the sha256 citation counts as a commit twin. MUTANT: mark none; the SHA does not."""
    row = "BACK ONLINE: production serves ea0a130b8e64. This row cites commit ea0a130b8e64 as its own probe."
    assert [(value, twin) for value, _, twin in A.digest_contexts(row)] == [("ea0a130b8e64", True),
                                                                             ("ea0a130b8e64", True)]
    assert [(value, twin) for value, _, twin in A.digest_contexts("boot.bin sha256 09cb6def1234")] == [
        ("09cb6def1234", False)]
    report = A.summarize(A.digest_contexts(row) + A.digest_contexts("boot.bin sha256 09cb6def1234"))
    assert report["12"]["count"] == 3 and report["12"]["also_commit"] == 2 and report["12"]["distinct"] == 2


def test_the_report_shows_what_the_identifier_rule_keeps_out_as_well_as_what_is_read():
    """memory-bxxxxx on #166: since #163 a digest on the line after text ending in `peer:` is skipped, and a report
    of what is read cannot show a skipped digest, because it leaves no edge. The other side lists every hex token
    that would be a digest but for the identifier rule, with the text before it.

    MUTANT: list every hex token the extractor passes over; the nonce and the UUID are listed as identifiers.
    MUTANT: list the digests again; the two sides overlap and the report counts a token twice."""
    body = ('sha256sums from the peer:\n09cb6def1234  boot.bin, "signing_kid": "10b1dc0834865bab", nonce 0123abcd9, '
            "peer 610170d5, 550e8400-e29b-41d4-a716-446655440000, sha256 aaaa1111bbbb, digest 12345678, kid 87654321")
    read = [value for _, value in N.digest_positions(body)]
    kept_out = [value for _, value in N.identifier_positions(body)]
    assert read == ["aaaa1111bbbb", "12345678"]
    assert kept_out == ["09cb6def1234", "10b1dc0834865bab", "610170d5"]        # the first is a digest skipped in error
    assert not set(read) & set(kept_out)
    assert [(value, before) for value, before, _ in A.identifier_contexts(body)][:2] == [
        ("09cb6def1234", "a256sums from the peer: "), ("10b1dc0834865bab", 'ot.bin, "signing_kid": "')]
    assert N.identifier_positions(None) == [] and A.identifier_contexts("sha256 aaaa1111bbbb") == []


def test_digest_positions_are_the_digests_the_extractor_names():
    """One scan serves both: what the audit shows is what artifact_ref_fields writes.
    MUTANT: a second, looser scan in the audit; it reports a key id the extractor skips."""
    bodies = ['"signing_kid": "10b1dc0834865bab", sha256 09cb6def1234 and md5_of_peer=f8fe68b4c4cba197efa9c8bbd45f144e',
              "see https://example.test/09cb6def1234 and nonce 09cb6def1235, digest 12345678, peer 610170d5",
              "docs/a.md only", ""]
    for body in bodies:
        named = [ref for ref, field in N.artifact_ref_fields(body) if field == "body" and ref.startswith("hash:")]
        assert [f"hash:{value}" for _, value in N.digest_positions(body)] == named, body
    assert N.digest_positions(bodies[0]) == [(bodies[0].index("09cb6def1234"), "09cb6def1234"),
                                             (bodies[0].index("f8fe68b4"), "f8fe68b4c4cba197efa9c8bbd45f144e")]
    assert N.digest_positions(bodies[1]) == [(bodies[1].index("12345678"), "12345678")]
    assert N.digest_positions(None) == []


def test_the_report_stops_where_the_extractors_cap_stops():
    """Codex on #166. artifact_ref_fields writes at most 32 edges for a body, so the report of what a backfill would
    write must stop with it. A hash longer than eight characters takes two edges, so the 17th of 17 is not written;
    and file names that fill the cap leave no room for a digest after them.

    MUTANT: list every digest candidate; the 17th hash and the digest behind the file names are counted."""
    hashes = [f"{i:02d}ab{i:02d}cd{i:02d}ef" for i in range(17)]                 # distinct in their first eight
    body = " ".join("sha256 " + h for h in hashes)
    written = [ref for ref, field in N.artifact_ref_fields(body) if field == "body"]
    assert written == [f"hash:{h}" for h in hashes[:16]]                          # control: the cap falls here
    assert [value for _, value in N.digest_positions(body)] == hashes[:16]
    assert [value for value, _, _ in A.digest_contexts(body)] == hashes[:16]
    # a digest that IS written counts at each place the body names it, also after the cut
    assert [value for _, value in N.digest_positions(body + " and again " + hashes[0])] == hashes[:16] + [hashes[0]]

    def names(n: int) -> str:
        return " ".join(f"file{i:02d}.md" for i in range(n)) + " sha256 09cb6def1234"

    assert [value for _, value in N.digest_positions(names(30))] == ["09cb6def1234"]   # 30 + 2 edges: it fits
    assert N.artifact_ref_fields(names(31))[-1] == ("path:file30.md", "body")           # 31 + 2: it does not
    assert N.digest_positions(names(31)) == [] and A.digest_contexts(names(32)) == []
    assert A.summarize(A.digest_contexts(body))["12"]["count"] == 16


def test_the_summary_counts_by_length_most_frequent_first_and_keeps_ties_in_order():
    """MUTANT: sort lengths numerically; the largest class is no longer first. MUTANT: no tie-break; two runs of
    the report on the same rows differ. MUTANT: ignore `top`; a long tail floods the report."""
    found = [("09cb6def1234", "sha256 ", False), ("09cb6def1234", "digest ", False),
             ("09cb6def1235", "sha256 ", True), ("aaaa1112", "x ", False), ("bbbb2222cccc", "sha256 ", True),
             ("aaaa1111", "kid `", False)]                                             # the ties arrive unsorted
    report = A.summarize(found, top=2)
    assert list(report) == ["12", "8"]
    assert report["12"] == {"count": 4, "distinct": 3, "also_commit": 2,
                            "top_values": [{"value": "09cb6def1234", "count": 2},
                                           {"value": "09cb6def1235", "count": 1}],
                            "top_before": [{"before": "sha256 ", "count": 3}, {"before": "digest ", "count": 1}]}
    assert report["8"]["top_values"] == [{"value": "aaaa1111", "count": 1}, {"value": "aaaa1112", "count": 1}]
    assert report["8"]["top_before"] == [{"before": "kid `", "count": 1}, {"before": "x ", "count": 1}]
    assert A.summarize([], top=5) == {} and A.summarize(found, top=0)["8"]["top_values"] == []


def test_each_total_of_the_report_is_the_sum_of_its_own_breakdown():
    """memory-bxxxxx on #167: with `skipped_as_identifier` taken from the read tally every test passed, because the
    one assertion on it was `>= 1`, and the read total is at least 1 as well. Three digests are read here and one
    token is kept out, so the two totals cannot stand in for each other.

    MUTANT: take the skipped total from the read tally; 3 is not 1. MUTANT: give `digests` the skipped total; 1 is
    not 3. MUTANT: swap the two breakdowns; each total disagrees with the sum beside it."""
    read, skipped = A.Tally(), A.Tally()
    read.add(A.digest_contexts("sha256 09cb6def1234, the digest 09cb6def1234 again, and checksum aaaa1111"))
    skipped.add(A.identifier_contexts('"signing_kid": "10b1dc0834865bab"'))
    report = A.frequency_report(read, skipped, rows_read=2, rows_with=1, truncated=False, top=5)

    def breakdown(side: str) -> int:
        return sum(row["count"] for row in report[side].values())

    assert report["digests"] == breakdown("by_length") == 3
    assert report["skipped_as_identifier"] == breakdown("skipped_by_length") == 1
    assert list(report["by_length"]) == ["12", "8"] and list(report["skipped_by_length"]) == ["16"]
    assert (report["rows_read"], report["rows_with_a_digest"], report["truncated"]) == (2, 1, False)


def test_the_tally_keeps_counts_not_occurrences():
    """Codex on the audit: a row that names one digest a hundred times must cost one entry, or a large history
    exhausts the operator's memory before the report prints. The tally is fed row by row and reads each batch once.
    MUTANT: keep the occurrences and count at the end; a generator is spent before the report and the counts are 0."""
    tally = A.Tally()
    for _ in range(50):
        tally.add((value, "sha256 ", False) for value in ["09cb6def1234"] * 100)      # a generator: read once
    tally.add(iter([("09cb6def1235", "digest ", True)]))
    report = tally.report(top=5)
    assert tally.total == 5001 and report["12"]["count"] == 5001 and report["12"]["also_commit"] == 1
    assert report["12"]["distinct"] == 2
    assert report["12"]["top_values"] == [{"value": "09cb6def1234", "count": 5000},
                                          {"value": "09cb6def1235", "count": 1}]
    assert sum(len(c) for c in (tally._values[12], tally._before[12])) == 4           # entries, not occurrences
