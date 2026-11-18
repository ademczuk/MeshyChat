"""Three-part numeric version ordering.

THE BUG THIS FILE EXISTS TO NOT REPEAT (#410, fixed by PRs #412/#413): the comparison
used to be a STRING comparison, under which "10.0.0" sorts BELOW "9.0.0" because '1' <
'9'. A peer that released 9.0.0 could then never release 10.0.0 - the server answered
409 "version is not newer" for a version that plainly was. The parse below returns a
TUPLE OF INTEGERS and the comparison is on that tuple; `tests/test_skills_registry.py`
pins exactly the 9.0.0/10.0.0 pair.

TWO PARSERS, NOT ONE, and the distinction is load-bearing:

  * `parse_semver` validates what a CALLER supplied and is STRICT. "1.0" is rejected.
  * `parse_stored_semver` parses a version READ BACK FROM THE DATABASE and pads the
    legacy two-part form "1.0" -> "1.0.0". Applying caller-strictness to stored data
    would make those rows unreleasable, with an error naming a version string the caller
    never sent.

A stored version this cannot parse is still an error - that is a real data defect
rather than a legacy shape - and the message must name the STORED side (hxxxxx, #413),
or it blames the new version for a defect in the old row.
"""
from __future__ import annotations


class InvalidVersion(ValueError):
    """The error for an invalid version.

    `.version` is interpolated into the wire message verbatim, so it may hold a whole
    sentence rather than a version, such as the "stored current version ..." text
    `release_version` substitutes for a corrupt stored row.
    """

    def __init__(self, version: str):
        super().__init__(f"invalid version {version}: must be three dotted numerics (semver-lite)")
        self.version = version

    @property
    def message(self) -> str:
        return str(self)


def parse_semver(version) -> tuple[int, int, int]:
    """Parse exactly three dot-separated ASCII-digit runs.

    `str.isdigit()` is NOT used: it is true for Unicode digits such as U+0660 ARABIC-
    INDIC DIGIT ZERO, which `int()` then happily parses, so a version could round-trip
    through this function looking numeric while being a different string from an ASCII
    digit-only version. The check is spelled against the ASCII set.
    """
    if not isinstance(version, str):
        raise InvalidVersion(str(version))
    parts = version.split(".")
    if len(parts) != 3:
        raise InvalidVersion(version)
    out = []
    for p in parts:
        if not p or not all("0" <= c <= "9" for c in p):
            raise InvalidVersion(version)
        out.append(int(p))
    return (out[0], out[1], out[2])


def parse_stored_semver(version: str) -> tuple[int, int, int]:
    """Pad MAJOR.MINOR to MAJOR.MINOR.0 for stored values only."""
    if isinstance(version, str) and len(version.split(".")) == 2:
        return parse_semver(f"{version}.0")
    return parse_semver(version)


def validate_semver(version) -> None:
    """Raise InvalidVersion unless version has three numeric parts."""
    parse_semver(version)
