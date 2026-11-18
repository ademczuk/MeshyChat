"""Mutants of apps/mesh/audit.py: the stale artifact edges, the digest contexts and the frequency report.

Each mutant name is `<set>:<id> <description>`; `<set>` is its mutation-set name. A mutant held
by more than one set is listed once under the first. The before and after texts are
literals, unchanged, except where a comment says the text was BLIND at 1a1ea9f and gives the
same mutation on today's code.
Run: python tools/mutants/run.py --list mesh_audit
"""

AUDIT = "apps/mesh/audit.py"

T_AUDIT = ["tests/test_mesh_audit.py"]
T_AUDIT_NORMALIZE = ["tests/test_mesh_audit.py", "tests/test_mesh_normalize.py"]
#: The database test behind the frequency report (mutate10 R5).
AUDIT_DB_TEST = ("tests/test_mesh_projection.py::test_the_audit_counts_the_digests_a_backfill_would_write_with_the_text_before_each")

# The helper texts are opaque immutable source material (_HZ_RE is one of them).
_M7_CTX = """    return [(value, _SPACE.sub(" ", body[max(0, start - 4 * width):start])[-width:], f"commit:{value[:7]}" in commits)
            for start, value in found]"""

MUTANTS = [
    ('mutate7:A1 every artifact edge is stale', AUDIT,
     "    return sorted({d for d in dsts if d.startswith(ARTIFACT_PREFIXES)} - current)",
     "    return sorted({d for d in dsts if d.startswith(ARTIFACT_PREFIXES)})",
     T_AUDIT),
    ('mutate7:A2 all mention kinds compared', AUDIT,
     "{d for d in dsts if d.startswith(ARTIFACT_PREFIXES)} - current",
     "set(dsts) - current",
     T_AUDIT),
    ('mutate7:A3 the embed gate closed', AUDIT,
     "normalize.project_row(row, mentions_allowed=True)",
     "normalize.project_row(row, mentions_allowed=False)",
     T_AUDIT),
    ('mutate7:A4 context of the first occurrence', AUDIT,
     _M7_CTX,
     _M7_CTX.replace("body[max(0, start - 4 * width):start]", "body[max(0, body.find(value) - 4 * width):body.find(value)]"),
     T_AUDIT),
    ('mutate7:A5 whitespace not collapsed', AUDIT,
     _M7_CTX,
     _M7_CTX.replace('_SPACE.sub(" ", body[max(0, start - 4 * width):start])[-width:]', 'body[max(0, start - width):start]'),
     T_AUDIT),
    ('mutate7:A5b slice before collapsing', AUDIT,
     _M7_CTX,
     _M7_CTX.replace('body[max(0, start - 4 * width):start])[-width:]', 'body[max(0, start - width):start])'),
     T_AUDIT),
    ('mutate7:A6 lengths in numeric order', AUDIT,
     "sorted(self._count, key=lambda n: (-self._count[n], n))",
     "sorted(self._count)",
     T_AUDIT),
    ('mutate7:A7 no tie-break', AUDIT,
     "sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:max(0, top)]",
     "counter.most_common()[:max(0, top)]",
     T_AUDIT),
    ('mutate7:A8 top ignored', AUDIT,
     "[:max(0, top)]",
     "",
     T_AUDIT),
    ('mutate7:A10 count of distinct values', AUDIT,
     '"count": self._count[length],',
     '"count": len(self._values[length]),',
     T_AUDIT),
    ('mutate7:A11 every digest a commit twin', AUDIT,
     _M7_CTX,
     _M7_CTX.replace('f"commit:{value[:7]}" in commits', 'True'),
     T_AUDIT),
    ('mutate7:A12 no digest a commit twin', AUDIT,
     _M7_CTX,
     _M7_CTX.replace('f"commit:{value[:7]}" in commits', 'False'),
     T_AUDIT),
    ('mutate7:A13 twin by the whole token', AUDIT,
     _M7_CTX,
     _M7_CTX.replace('f"commit:{value[:7]}" in commits', 'f"commit:{value}" in commits'),
     T_AUDIT),
    ('mutate7:A14 twins not counted', AUDIT,
     "            self._commit[length] += bool(also_commit)\n",
     "",
     T_AUDIT),
    ('mutate7:A15 total not counted', AUDIT,
     "            self.total += 1\n",
     "",
     T_AUDIT),
    ('mutate7:A16 occurrences kept, counted at the end', AUDIT,
     "    def add(self, found: Iterable[tuple[str, str, bool]]) -> None:\n        for value, before, also_commit in found:",
     "    def add(self, found: Iterable[tuple[str, str, bool]]) -> None:\n        self._kept = getattr(self, '_kept', []) + [found]\n        for value, before, also_commit in []:",
     T_AUDIT),
    ('mutate8:S7 identifier contexts read the digests', AUDIT,
     "    return _contexts(body, normalize.identifier_positions(body), width)",
     "    return _contexts(body, normalize.digest_positions(body), width)",
     T_AUDIT_NORMALIZE),
    ('mutate10:R1 skipped total from the read tally', AUDIT,
     '"skipped_as_identifier": skipped.total,',
     '"skipped_as_identifier": read.total,',
     T_AUDIT),
    ('mutate10:R2 digests total from the skipped tally', AUDIT,
     '"digests": read.total,',
     '"digests": skipped.total,',
     T_AUDIT),
    ('mutate10:R3 the two breakdowns swapped', AUDIT,
     '"by_length": read.report(top=top),\n            "skipped_as_identifier": skipped.total, "skipped_by_length": skipped.report(top=top)}',
     '"by_length": skipped.report(top=top),\n            "skipped_as_identifier": skipped.total, "skipped_by_length": read.report(top=top)}',
     T_AUDIT),
    ('mutate10:R4 rows_with reported as rows_read', AUDIT,
     '"rows_with_a_digest": rows_with,',
     '"rows_with_a_digest": rows_read,',
     T_AUDIT),
    # This mutation needs the database test, so it is named here.
    ('mutate10:R5 the call site swaps the tallies (needs the database test)', AUDIT,
     "frequency_report(tally, skipped,",
     "frequency_report(skipped, tally,",
     T_AUDIT + [AUDIT_DB_TEST]),
]
