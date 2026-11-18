"""Unit tests for Archive Custody & Provenance Verifier (`tools/meshy_node/custody.py`)
and `python -m tools.meshy_node verify-bundle`.

Run:
  .venv/Scripts/python.exe -m pytest tests/test_meshy_node_custody.py --noconftest -q -p no:cacheprovider
"""
from __future__ import annotations

import hashlib
import io
import json
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.meshy_node import cli, custody  # noqa: E402


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _build_tar_gz(path: Path, entries: list[tuple[str, bytes, int]]) -> str:
    """Create a `.tar.gz` archive with `(name, content_bytes, tar_type)` entries and return outer sha256."""
    with tarfile.open(path, mode="w:gz") as tf:
        for name, data, ttype in entries:
            info = tarfile.TarInfo(name=name)
            info.type = ttype
            if ttype == tarfile.REGTYPE:
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
            elif ttype in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                info.linkname = data.decode("utf-8")
                tf.addfile(info)
            else:
                info.size = 0
                tf.addfile(info)
    return _sha256_bytes(path.read_bytes())


def test_verify_bundle_clean_tar_gz_with_sha256sums_and_manifest(tmp_path):
    src_a = b"def solve(x):\n    return x + 42\n"
    src_b = b"assert solve(1) == 43\n"
    sha_a = _sha256_bytes(src_a)
    sha_b = _sha256_bytes(src_b)

    sha256sums = f"{sha_a}  pkg/safety.py\n{sha_b} *pkg/test_safety.py\n".encode("utf-8")
    manifest = json.dumps({
        "files": {
            "pkg/safety.py": sha_a,
            "pkg/test_safety.py": sha_b,
        }
    }).encode("utf-8")

    bundle = tmp_path / "bundle.tar.gz"
    outer_sha = _build_tar_gz(
        bundle,
        [
            ("pkg/safety.py", src_a, tarfile.REGTYPE),
            ("pkg/test_safety.py", src_b, tarfile.REGTYPE),
            ("SHA256SUMS", sha256sums, tarfile.REGTYPE),
            ("MANIFEST.json", manifest, tarfile.REGTYPE),
        ],
    )

    res = custody.verify_bundle(bundle, expected_sha256=outer_sha)
    assert res["ok"] is True
    assert res["error"] is None
    assert res["sha256"] == outer_sha
    assert res["manifest_found"] is True
    assert res["missing_from_manifest"] == []
    assert res["missing_from_archive"] == []
    assert res["digest_mismatches"] == []


def test_verify_bundle_refuses_outer_hash_mismatch(tmp_path):
    data = b"hello world\n"
    bundle = tmp_path / "simple.tar.gz"
    _build_tar_gz(bundle, [("hello.txt", data, tarfile.REGTYPE)])

    res = custody.verify_bundle(bundle, expected_sha256="0" * 64)
    assert res["ok"] is False
    assert res["error"] == custody.E_BUNDLE_HASH_MISMATCH


@pytest.mark.parametrize(
    "unsafe_name,ttype",
    [
        ("../escape.py", tarfile.REGTYPE),
        ("pkg/../../etc/passwd", tarfile.REGTYPE),
        ("/etc/passwd", tarfile.REGTYPE),
        ("C:/Windows/win.ini", tarfile.REGTYPE),
        ("pkg\\backslash.py", tarfile.REGTYPE),
        ("pkg/control\x07.py", tarfile.REGTYPE),
        ("pkg/symlink.py", tarfile.SYMTYPE),
        ("pkg/hardlink.py", tarfile.LNKTYPE),
        ("pkg/fifo.pipe", tarfile.FIFOTYPE),
    ],
)
def test_verify_bundle_refuses_unsafe_tar_members(tmp_path, unsafe_name, ttype):
    bundle = tmp_path / "unsafe.tar.gz"
    _build_tar_gz(bundle, [(unsafe_name, b"target", ttype)])

    res = custody.verify_bundle(bundle)
    assert res["ok"] is False
    assert custody.E_BUNDLE_UNSAFE_MEMBER in res["errors"]
    assert len(res["unsafe_members"]) == 1


def test_verify_bundle_refuses_case_folded_duplicate_paths(tmp_path):
    bundle = tmp_path / "collision.tar.gz"
    _build_tar_gz(
        bundle,
        [
            ("pkg/Handler.py", b"v1", tarfile.REGTYPE),
            ("pkg/handler.py", b"v2", tarfile.REGTYPE),
        ],
    )
    res = custody.verify_bundle(bundle)
    assert res["ok"] is False
    assert custody.E_BUNDLE_UNSAFE_MEMBER in res["errors"]
    assert "case-folded duplicate" in res["unsafe_members"][0]["reason"]


def test_verify_bundle_detects_manifest_mismatches_and_omissions(tmp_path):
    src_a = b"actual content\n"
    src_unlisted = b"unlisted file\n"
    wrong_sha = "f" * 64
    missing_sha = "e" * 64
    sha256sums = f"{wrong_sha}  pkg/a.py\n{missing_sha}  pkg/missing.py\n".encode("utf-8")

    bundle = tmp_path / "bad_manifest.tar.gz"
    _build_tar_gz(
        bundle,
        [
            ("pkg/a.py", src_a, tarfile.REGTYPE),
            ("pkg/unlisted.py", src_unlisted, tarfile.REGTYPE),
            ("SHA256SUMS", sha256sums, tarfile.REGTYPE),
        ],
    )
    res = custody.verify_bundle(bundle)
    assert res["ok"] is False
    assert res["error"] == custody.E_BUNDLE_MANIFEST_MISMATCH
    assert res["missing_from_archive"] == ["pkg/missing.py"]
    assert res["missing_from_manifest"] == ["pkg/unlisted.py"]
    assert len(res["digest_mismatches"]) == 1
    assert res["digest_mismatches"][0]["path"] == "pkg/a.py"


def test_verify_bundle_detects_stale_test_input_provenance(tmp_path):
    """Regression proof for PFVISION r5 post-mortem: test results recorded against pre-fix
    `safety.py` (`57de925b...`) while the archive ships updated `safety.py` (`7ea91905...`)
    must be flagged with `E_PROVENANCE_STALE_INPUT`.
    """
    current_safety = b"# post-fix safety.py\n"
    current_sha = _sha256_bytes(current_safety)
    stale_pre_fix_sha = "57de925b" + ("0" * 56)

    bundle = tmp_path / "r5.tar.gz"
    _build_tar_gz(
        bundle,
        [
            ("pfvision/safety.py", current_safety, tarfile.REGTYPE),
            ("SHA256SUMS", f"{current_sha}  pfvision/safety.py\n".encode("utf-8"), tarfile.REGTYPE),
        ],
    )

    # 1. Stale BEFORE/AFTER input map -> refused with E_PROVENANCE_STALE_INPUT
    stale_prov = {"input_digests": {"pfvision/safety.py": stale_pre_fix_sha}}
    res_stale = custody.verify_bundle(bundle, provenance_map=stale_prov)
    assert res_stale["ok"] is False
    assert res_stale["error"] == custody.E_PROVENANCE_STALE_INPUT
    assert res_stale["stale_inputs"][0]["recorded_sha256"] == stale_pre_fix_sha
    assert res_stale["stale_inputs"][0]["archive_sha256"] == current_sha

    # 2. Matching AFTER input map -> passes
    fresh_prov = {"before_after": {"AFTER": {"pfvision/safety.py": current_sha}}}
    res_fresh = custody.verify_bundle(bundle, provenance_map=fresh_prov)
    assert res_fresh["ok"] is True
    assert res_fresh["stale_inputs"] == []


def test_verify_bundle_zip_and_cli_integration(tmp_path, capsys):
    zpath = tmp_path / "bundle.zip"
    payload = b"zip member content\n"
    payload_sha = _sha256_bytes(payload)
    with zipfile.ZipFile(zpath, mode="w") as zf:
        zf.writestr("src/app.py", payload)
        zf.writestr("SHA256SUMS", f"{payload_sha}  src/app.py\n")

    outer_sha = _sha256_bytes(zpath.read_bytes())
    rc = cli.main(["verify-bundle", str(zpath), "--expected-sha256", outer_sha])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["member_count"] == 2


def test_verify_bundle_refuses_invalid_or_empty_manifest(tmp_path):
    src_a = b"actual content\n"
    bundle_bad_json = tmp_path / "bad_json.tar.gz"
    _build_tar_gz(
        bundle_bad_json,
        [
            ("pkg/a.py", src_a, tarfile.REGTYPE),
            ("MANIFEST.json", b"{not valid json", tarfile.REGTYPE),
        ],
    )
    res_bad = custody.verify_bundle(bundle_bad_json)
    assert res_bad["ok"] is False
    assert custody.E_MANIFEST_INVALID in res_bad["errors"]
    assert custody.E_BUNDLE_MANIFEST_MISMATCH in res_bad["errors"]
    assert len(res_bad["manifest_errors"]) == 1

    bundle_empty = tmp_path / "empty_sums.tar.gz"
    _build_tar_gz(
        bundle_empty,
        [
            ("pkg/a.py", src_a, tarfile.REGTYPE),
            ("SHA256SUMS", b"# comment only\n\n", tarfile.REGTYPE),
        ],
    )
    res_empty = custody.verify_bundle(bundle_empty)
    assert res_empty["ok"] is False
    assert custody.E_MANIFEST_INVALID in res_empty["errors"]
    assert custody.E_BUNDLE_MANIFEST_MISMATCH in res_empty["errors"]


def test_verify_bundle_reconciles_multiple_manifests_without_masking_conflicts(tmp_path):
    src_a = b"actual content\n"
    actual_sha = _sha256_bytes(src_a)
    wrong_sha = "f" * 64

    # 1. MANIFEST.json has wrong digest while SHA256SUMS (which sorts later) has actual_sha:
    # must fail rather than letting SHA256SUMS overwrite MANIFEST.json.
    bundle_conflict = tmp_path / "conflict.tar.gz"
    _build_tar_gz(
        bundle_conflict,
        [
            ("pkg/a.py", src_a, tarfile.REGTYPE),
            ("MANIFEST.json", json.dumps({"files": {"pkg/a.py": wrong_sha}}).encode("utf-8"), tarfile.REGTYPE),
            ("SHA256SUMS", f"{actual_sha}  pkg/a.py\n".encode("utf-8"), tarfile.REGTYPE),
        ],
    )
    res_conflict = custody.verify_bundle(bundle_conflict)
    assert res_conflict["ok"] is False
    assert custody.E_BUNDLE_MANIFEST_MISMATCH in res_conflict["errors"]
    assert len(res_conflict["digest_mismatches"]) >= 1
    assert any(m["manifest"] == "MANIFEST.json" and m["expected"] == wrong_sha for m in res_conflict["digest_mismatches"])

    # 2. Single top-level directory archive ('pkg/a.py' + root SHA256SUMS listing 'a.py') resolves cleanly,
    # whereas when a root file ('root.txt') also exists, 'x.py' in SHA256SUMS does NOT resolve to 'pkg/x.py' (F16).
    bundle_top = tmp_path / "single_top.tar.gz"
    _build_tar_gz(
        bundle_top,
        [
            ("pkg/a.py", src_a, tarfile.REGTYPE),
            ("SHA256SUMS", f"{actual_sha}  a.py\n".encode("utf-8"), tarfile.REGTYPE),
        ],
    )
    res_top = custody.verify_bundle(bundle_top)
    assert res_top["ok"] is True

    bundle_f16 = tmp_path / "mixed_root_f16.tar.gz"
    root_sha = _sha256_bytes(b"root\n")
    _build_tar_gz(
        bundle_f16,
        [
            ("root.txt", b"root\n", tarfile.REGTYPE),
            ("pkg/x.py", src_a, tarfile.REGTYPE),
            ("SHA256SUMS", f"{root_sha}  root.txt\n{actual_sha}  x.py\n".encode("utf-8"), tarfile.REGTYPE),
        ],
    )
    res_f16 = custody.verify_bundle(bundle_f16)
    assert res_f16["ok"] is False
    assert res_f16["missing_from_archive"] == ["x.py"]


def test_verify_bundle_nested_manifest_and_sha256sums_are_normal_members_not_parsed(tmp_path):
    # F15: Nested ext/manifest.json, vendor/MANIFEST.json {"name":"x"}, and vendor/SHA256SUMS
    # must NOT be parsed as bundle manifests and must be listed in the root manifest.
    a_bytes = b"print(1)\n"
    ext_bytes = b'{"manifest_version": 3, "name": "ext"}\n'
    vendor_m_bytes = b'{"name": "x"}\n'
    vendor_s_bytes = f"{'a' * 64}  other.c\n".encode("utf-8")

    sha_a = _sha256_bytes(a_bytes)
    sha_ext = _sha256_bytes(ext_bytes)
    sha_vm = _sha256_bytes(vendor_m_bytes)
    sha_vs = _sha256_bytes(vendor_s_bytes)

    # 1. When all nested files are listed in root SHA256SUMS -> ok=True (no false-positive E_MANIFEST_INVALID)
    listed_bundle = tmp_path / "nested_listed.tar.gz"
    sums_ok = (
        f"{sha_a}  a.py\n"
        f"{sha_ext}  ext/manifest.json\n"
        f"{sha_vm}  vendor/MANIFEST.json\n"
        f"{sha_vs}  vendor/SHA256SUMS\n"
    ).encode("utf-8")
    _build_tar_gz(
        listed_bundle,
        [
            ("a.py", a_bytes, tarfile.REGTYPE),
            ("ext/manifest.json", ext_bytes, tarfile.REGTYPE),
            ("vendor/MANIFEST.json", vendor_m_bytes, tarfile.REGTYPE),
            ("vendor/SHA256SUMS", vendor_s_bytes, tarfile.REGTYPE),
            ("SHA256SUMS", sums_ok, tarfile.REGTYPE),
        ],
    )
    res_ok = custody.verify_bundle(listed_bundle)
    assert res_ok["ok"] is True
    assert res_ok["manifest_errors"] == []
    assert res_ok["missing_from_manifest"] == []
    assert res_ok["missing_from_archive"] == []

    # 2. When nested files are omitted from root SHA256SUMS -> missing_from_manifest (not exempt)
    unlisted_bundle = tmp_path / "nested_unlisted.tar.gz"
    _build_tar_gz(
        unlisted_bundle,
        [
            ("a.py", a_bytes, tarfile.REGTYPE),
            ("ext/manifest.json", ext_bytes, tarfile.REGTYPE),
            ("vendor/MANIFEST.json", vendor_m_bytes, tarfile.REGTYPE),
            ("vendor/SHA256SUMS", vendor_s_bytes, tarfile.REGTYPE),
            ("SHA256SUMS", f"{sha_a}  a.py\n".encode("utf-8"), tarfile.REGTYPE),
        ],
    )
    res_unlisted = custody.verify_bundle(unlisted_bundle)
    assert res_unlisted["ok"] is False
    assert custody.E_BUNDLE_MANIFEST_MISMATCH in res_unlisted["errors"]
    assert res_unlisted["missing_from_manifest"] == [
        "ext/manifest.json",
        "vendor/MANIFEST.json",
        "vendor/SHA256SUMS",
    ]


def test_verify_bundle_refuses_unusable_or_malformed_provenance_maps(tmp_path):
    src = b"# safety.py\n"
    sha = _sha256_bytes(src)
    bundle = tmp_path / "prov_check.tar.gz"
    _build_tar_gz(
        bundle,
        [
            ("pfvision/safety.py", src, tarfile.REGTYPE),
            ("SHA256SUMS", f"{sha}  pfvision/safety.py\n".encode("utf-8"), tarfile.REGTYPE),
        ],
    )

    for bad_prov in (
        {"inputs": {"pfvision/safety.py": "not-a-digest"}},
        {"inputs": {"pfvision/safety.py": sha, "pfvision/other.py": "not-a-digest"}},
        {},
        {"inputs": {}},
    ):
        res = custody.verify_bundle(bundle, provenance_map=bad_prov)
        assert res["ok"] is False
        assert res["provenance_checked"] is False
        assert custody.E_PROVENANCE_INVALID in res["errors"]


def test_verify_bundle_scans_private_snapshot_immune_to_inplace_rewrite(tmp_path, monkeypatch):
    src_v1 = b"version 1 clean payload\n"
    sha_v1 = _sha256_bytes(src_v1)
    bundle = tmp_path / "live.tar.gz"
    outer_v1 = _build_tar_gz(
        bundle,
        [
            ("pkg/code.py", src_v1, tarfile.REGTYPE),
            ("SHA256SUMS", f"{sha_v1}  pkg/code.py\n".encode("utf-8"), tarfile.REGTYPE),
        ],
    )

    # Prepare different archive bytes that will overwrite `bundle` in place right after Snapshot is taken
    bundle_v2 = tmp_path / "swapped.tar.gz"
    src_v2 = b"version 2 mutated payload\n"
    sha_v2 = _sha256_bytes(src_v2)
    _build_tar_gz(
        bundle_v2,
        [
            ("pkg/code.py", src_v2, tarfile.REGTYPE),
            ("SHA256SUMS", f"{sha_v2}  pkg/code.py\n".encode("utf-8"), tarfile.REGTYPE),
        ],
    )
    v2_bytes = bundle_v2.read_bytes()

    orig_scan_tar = custody._scan_tar

    def _mutating_scan_tar(fileobj):
        # Rewrite the original path in place before scanning the stream passed to _scan_tar
        bundle.write_bytes(v2_bytes)
        return orig_scan_tar(fileobj)

    monkeypatch.setattr(custody, "_scan_tar", _mutating_scan_tar)

    res = custody.verify_bundle(bundle, expected_sha256=outer_v1)
    assert res["ok"] is True
    assert res["sha256"] == outer_v1
    assert res["members"]["pkg/code.py"]["sha256"] == sha_v1


@pytest.mark.parametrize(
    "entries",
    [
        [("a", b"file a", tarfile.REGTYPE), ("a/b.txt", b"nested b", tarfile.REGTYPE)],
        [("a/b.txt", b"nested b", tarfile.REGTYPE), ("a", b"file a", tarfile.REGTYPE)],
        [("A", b"file A", tarfile.REGTYPE), ("a/b.txt", b"nested b", tarfile.REGTYPE)],
        [("a/b.txt", b"nested b", tarfile.REGTYPE), ("A", b"file A", tarfile.REGTYPE)],
        [("a/", b"", tarfile.DIRTYPE), ("a", b"file a", tarfile.REGTYPE)],
    ],
)
def test_verify_bundle_refuses_file_vs_directory_collisions_tar_and_zip(tmp_path, entries):
    tar_bundle = tmp_path / "collide.tar.gz"
    _build_tar_gz(tar_bundle, entries)
    res_tar = custody.verify_bundle(tar_bundle)
    assert res_tar["ok"] is False
    assert custody.E_BUNDLE_UNSAFE_MEMBER in res_tar["errors"]
    assert any("file_dir_conflict_with:" in u["reason"] for u in res_tar["unsafe_members"])

    zip_bundle = tmp_path / "collide.zip"
    with zipfile.ZipFile(zip_bundle, mode="w") as zf:
        for name, data, ttype in entries:
            if ttype == tarfile.DIRTYPE:
                zf.writestr(name if name.endswith("/") else name + "/", b"")
            else:
                zf.writestr(name, data)
    res_zip = custody.verify_bundle(zip_bundle)
    assert res_zip["ok"] is False
    assert custody.E_BUNDLE_UNSAFE_MEMBER in res_zip["errors"]
    assert any("file_dir_conflict_with:" in u["reason"] for u in res_zip["unsafe_members"])


def test_verify_bundle_round3_concatenated_tar_orphan_zip_sparse_and_duplicate_json_keys(tmp_path):
    # 1. Concatenated tar archive (smuggling second member after first EOF block) -> E_BUNDLE_FORMAT
    b1 = io.BytesIO()
    with tarfile.open(fileobj=b1, mode="w") as tf:
        info = tarfile.TarInfo("safe.txt")
        info.size = 2
        tf.addfile(info, io.BytesIO(b"ok"))
    b2 = io.BytesIO()
    with tarfile.open(fileobj=b2, mode="w") as tf:
        info = tarfile.TarInfo("../evil.sh")
        info.size = 4
        tf.addfile(info, io.BytesIO(b"evil"))
    cat_tar = tmp_path / "cat.tar"
    cat_tar.write_bytes(b1.getvalue() + b2.getvalue())
    res_cat = custody.verify_bundle(cat_tar)
    assert res_cat["ok"] is False
    assert custody.E_BUNDLE_FORMAT in res_cat["errors"]

    # 2. Zip with orphan local file header before central directory -> E_BUNDLE_FORMAT
    z_clean = io.BytesIO()
    with zipfile.ZipFile(z_clean, mode="w") as zf:
        zf.writestr("safe.txt", b"ok")
    z_evil = io.BytesIO()
    with zipfile.ZipFile(z_evil, mode="w") as zf:
        zf.writestr("../evil.sh", b"evil")
    evil_raw = z_evil.getvalue()
    evil_local = evil_raw[: evil_raw.index(b"PK\x01\x02")]
    clean_raw = bytearray(z_clean.getvalue())
    cd_pos = clean_raw.index(b"PK\x01\x02")
    eocd_pos = clean_raw.rindex(b"PK\x05\x06")
    import struct as _struct
    _struct.pack_into("<I", clean_raw, cd_pos + 42, len(evil_local))
    _struct.pack_into("<I", clean_raw, eocd_pos + 16, cd_pos + len(evil_local))
    orphan_zip = tmp_path / "orphan.zip"
    orphan_zip.write_bytes(evil_local + bytes(clean_raw))
    res_orphan = custody.verify_bundle(orphan_zip)
    assert res_orphan["ok"] is False
    assert custody.E_BUNDLE_FORMAT in res_orphan["errors"]

    # 3. Duplicate keys in MANIFEST.json -> E_MANIFEST_INVALID
    dup_manifest_tar = tmp_path / "dup_manifest.tar.gz"
    sha_ok = _sha256_bytes(b"payload")
    _build_tar_gz(
        dup_manifest_tar,
        [
            ("a.txt", b"payload", tarfile.REGTYPE),
            ("MANIFEST.json", f'{{"a.txt": "{"0"*64}", "a.txt": "{sha_ok}"}}'.encode("utf-8"), tarfile.REGTYPE),
        ],
    )
    res_dup = custody.verify_bundle(dup_manifest_tar)
    assert res_dup["ok"] is False
    assert custody.E_MANIFEST_INVALID in res_dup["errors"]

    # 4. NFC vs NFD duplicate filenames -> E_BUNDLE_UNSAFE_MEMBER
    nfc_nfd_tar = tmp_path / "nfc_nfd.tar.gz"
    _build_tar_gz(
        nfc_nfd_tar,
        [
            ("caf\u00e9.txt", b"nfc", tarfile.REGTYPE),
            ("cafe\u0301.txt", b"nfd", tarfile.REGTYPE),
        ],
    )
    res_nfc = custody.verify_bundle(nfc_nfd_tar)
    assert res_nfc["ok"] is False
    assert custody.E_BUNDLE_UNSAFE_MEMBER in res_nfc["errors"]

    # 5. Provenance container with unexpected top-level file key -> E_PROVENANCE_INVALID
    clean_tar = tmp_path / "clean_prov.tar.gz"
    _build_tar_gz(clean_tar, [("a.py", b"payload", tarfile.REGTYPE)])
    res_prov = custody.verify_bundle(
        clean_tar,
        provenance_map={"inputs": {"a.py": sha_ok}, "b.py": "0" * 64},
    )
    assert res_prov["ok"] is False
    assert custody.E_PROVENANCE_INVALID in res_prov["errors"]

    # 6. GNU sparse tar member (PAX GNU.sparse.*, N4/F12) -> E_BUNDLE_FORMAT ("sparse tar member ... is forbidden")
    sparse_buf = io.BytesIO()
    with tarfile.open(fileobj=sparse_buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        s_info = tarfile.TarInfo("sparse.bin")
        s_info.size = 4
        s_info.pax_headers = {
            "GNU.sparse.major": "0",
            "GNU.sparse.minor": "1",
            "GNU.sparse.numblocks": "1",
            "GNU.sparse.map": "0,4",
            "GNU.sparse.size": "1073741824",
        }
        tf.addfile(s_info, io.BytesIO(b"data"))
    sparse_tar = tmp_path / "sparse.tar"
    sparse_tar.write_bytes(sparse_buf.getvalue())
    res_sparse = custody.verify_bundle(sparse_tar)
    assert res_sparse["ok"] is False
    assert custody.E_BUNDLE_FORMAT in res_sparse["errors"]
    assert "sparse tar member" in res_sparse["detail"]

    # 7. Multi-member .tgz, zip mid-gap bytes, and trailing bytes after zip EOCD (F9/N3) -> E_BUNDLE_FORMAT
    gz1 = tmp_path / "part1.tar.gz"
    gz2 = tmp_path / "part2.tar.gz"
    _build_tar_gz(gz1, [("a.txt", b"1", tarfile.REGTYPE)])
    _build_tar_gz(gz2, [("b.txt", b"2", tarfile.REGTYPE)])
    multi_tgz = tmp_path / "multi.tgz"
    multi_tgz.write_bytes(gz1.read_bytes() + gz2.read_bytes())
    assert custody.E_BUNDLE_FORMAT in custody.verify_bundle(multi_tgz)["errors"]

    trailing_zip = tmp_path / "trailing_eocd.zip"
    trailing_zip.write_bytes(z_clean.getvalue() + b"JUNK_AFTER_EOCD")
    assert custody.E_BUNDLE_FORMAT in custody.verify_bundle(trailing_zip)["errors"]

    # 8. Zip encrypted flag, unsupported compression method 99, and corrupt deflate stream (F10) -> E_BUNDLE_FORMAT
    enc_raw = bytearray(z_clean.getvalue())
    _struct.pack_into("<H", enc_raw, 6, 0x0001)
    _struct.pack_into("<H", enc_raw, enc_raw.index(b"PK\x01\x02") + 8, 0x0001)
    enc_zip = tmp_path / "enc.zip"
    enc_zip.write_bytes(bytes(enc_raw))
    assert custody.E_BUNDLE_FORMAT in custody.verify_bundle(enc_zip)["errors"]

    m99_raw = bytearray(z_clean.getvalue())
    _struct.pack_into("<H", m99_raw, 8, 99)
    _struct.pack_into("<H", m99_raw, m99_raw.index(b"PK\x01\x02") + 10, 99)
    m99_zip = tmp_path / "method99.zip"
    m99_zip.write_bytes(bytes(m99_raw))
    assert custody.E_BUNDLE_FORMAT in custody.verify_bundle(m99_zip)["errors"]

    # 9. Overlong path (>1024 chars / >64 segments, F11) and Windows/Unicode unsafe member names (F17)
    overlong_path = "/".join(["ab"] * 70) + "/file.txt"
    for bad_name in (
        overlong_path,
        "a/file.txt:evil",
        "CON",
        "sub/aux.txt",
        "trailing.",
        "trailing /a.txt",
        "line\u0085sep.txt",
        "bidi\u202e.txt",
        "zwsp\u200b.txt",
    ):
        norm_p, reason = custody.validate_member_path(bad_name)
        assert norm_p is None and reason is not None, f"expected {bad_name!r} to be refused"

    # Zip directory entry with S_IFLNK mode bits in external_attr (F17) -> E_BUNDLE_UNSAFE_MEMBER
    symlink_dir_zip = tmp_path / "symlink_dir.zip"
    with zipfile.ZipFile(symlink_dir_zip, mode="w") as zf:
        zinfo = zipfile.ZipInfo("d/")
        zinfo.external_attr = (0o120777 << 16) | 0x10
        zf.writestr(zinfo, b"")
    res_symdir = custody.verify_bundle(symlink_dir_zip)
    assert res_symdir["ok"] is False
    assert custody.E_BUNDLE_UNSAFE_MEMBER in res_symdir["errors"]

    # 10. Single-common-top-dir bundle root resolution when root SHA256SUMS is at archive root vs inside single top dir (F16)
    payload_bytes = b"verified root resolution content\n"
    payload_sha = hashlib.sha256(payload_bytes).hexdigest()
    root_sums_tar = tmp_path / "root_sums_with_topdir.tar.gz"
    _build_tar_gz(
        root_sums_tar,
        [
            ("SHA256SUMS", f"{payload_sha}  src/mod.py\n".encode("utf-8"), tarfile.REGTYPE),
            ("pkg-1.0/src/mod.py", payload_bytes, tarfile.REGTYPE),
        ],
    )
    res_f16 = custody.verify_bundle(root_sums_tar, provenance_map={"src/mod.py": payload_sha})
    assert res_f16["ok"] is True
    assert res_f16["errors"] == []

    # 11. Local ZIP directory header filename mismatch vs central directory (GitHub Codex r4366663832)
    dir_zip_buf = io.BytesIO()
    with zipfile.ZipFile(dir_zip_buf, mode="w") as zf:
        zinfo = zipfile.ZipInfo("safe/")
        zinfo.external_attr = (0o040755 << 16) | 0x10
        zf.writestr(zinfo, b"")
    dir_zip_raw = bytearray(dir_zip_buf.getvalue())
    # Replace local header filename "safe/" at offset 30..35 with "../x/" while keeping central directory "safe/"
    assert dir_zip_raw[30:35] == b"safe/"
    dir_zip_raw[30:35] = b"../x/"
    mismatch_dir_zip = tmp_path / "mismatch_dir.zip"
    mismatch_dir_zip.write_bytes(bytes(dir_zip_raw))
    res_mismatch_dir = custody.verify_bundle(mismatch_dir_zip)
    assert res_mismatch_dir["ok"] is False
    assert custody.E_BUNDLE_FORMAT in res_mismatch_dir["errors"]
    assert "does not match central directory name" in res_mismatch_dir["detail"]

    # 12. Unreferenced data / prefixed orphan local header before an empty ZIP central directory (GitHub Codex r4366759814)
    empty_zip_buf = io.BytesIO()
    with zipfile.ZipFile(empty_zip_buf, mode="w"):
        pass
    empty_eocd = empty_zip_buf.getvalue()
    assert len(empty_eocd) == 22
    clean_empty_zip = tmp_path / "clean_empty.zip"
    clean_empty_zip.write_bytes(empty_eocd)
    assert custody.E_BUNDLE_FORMAT not in custody.verify_bundle(clean_empty_zip)["errors"]

    orphan_buf = io.BytesIO()
    with zipfile.ZipFile(orphan_buf, mode="w") as zf:
        zf.writestr("../escape.txt", b"payload")
    orphan_raw = orphan_buf.getvalue()
    orphan_local_only = orphan_raw[: orphan_raw.index(b"PK\x01\x02")]
    for bad_empty_bytes in (
        b"junk" + orphan_local_only + empty_eocd,
        b"junk" + empty_eocd,
    ):
        bad_empty_zip = tmp_path / "bad_empty.zip"
        bad_empty_zip.write_bytes(bad_empty_bytes)
        res_bad_empty = custody.verify_bundle(bad_empty_zip)
        assert res_bad_empty["ok"] is False
        assert custody.E_BUNDLE_FORMAT in res_bad_empty["errors"]
        assert "empty central directory" in res_bad_empty["detail"]

    # 13. Compressed tar with concatenated second tar inside single gzip stream buffered ahead by tarfile (GitHub Codex r4369885468 / pxx 688ddd)
    import gzip

    def _ustar_bytes(name: str, data: bytes) -> bytes:
        b = io.BytesIO()
        with tarfile.open(fileobj=b, mode="w", format=tarfile.USTAR_FORMAT) as tf:
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        return b.getvalue()

    safe_ustar = _ustar_bytes("safe.txt", b"ok\n")[:2048]
    evil_ustar = _ustar_bytes("../escape.txt", b"x\n")
    ctrl_tgz = tmp_path / "control_safe_only.tar.gz"
    ctrl_tgz.write_bytes(gzip.compress(safe_ustar))
    assert custody.verify_bundle(ctrl_tgz)["ok"] is True

    hidden_tgz = tmp_path / "safe_plus_hidden.tar.gz"
    hidden_tgz.write_bytes(gzip.compress(safe_ustar + evil_ustar))
    res_hidden_tgz = custody.verify_bundle(hidden_tgz)
    assert res_hidden_tgz["ok"] is False
    assert custody.E_BUNDLE_FORMAT in res_hidden_tgz["errors"]
    assert "trailing non-zero data after tar end-of-archive marker" in res_hidden_tgz["detail"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
