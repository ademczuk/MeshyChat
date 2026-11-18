"""Unit tests for tools/ci/pregate_check.py (DB-free union/pair pre-gate filter)."""
from __future__ import annotations
import ctypes, importlib.util, json, os, subprocess, sys, time
from pathlib import Path; import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1])); from tools.ci import pregate_check as pc  # noqa: E402
def _git(cwd: Path, *args: str) -> str: return subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@local.invalid", "-c", "core.autocrlf=false", *args], cwd=str(cwd), capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL).stdout.strip()
def _commit_files(repo: Path, base: str | None, files: dict[str, str | None], msg: str) -> str:
    (None if base is None else _git(repo, "checkout", "-q", "--detach", base)); [((repo / rel).unlink() if content is None else ((repo / rel).parent.mkdir(parents=True, exist_ok=True), (repo / rel).write_text(content, encoding="utf-8"))) for rel, content in files.items()]
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", msg); return _git(repo, "rev-parse", "HEAD")
def _init_repo(tmp_path: Path, initial_files: dict[str, str]) -> tuple[Path, str, Path]:
    (repo := tmp_path / "repo").mkdir(parents=True, exist_ok=True); _git(repo, "init", "-q"); return repo, _commit_files(repo, None, initial_files, "base"), tmp_path / "pregate-wt"
def test_clean_non_overlapping_union_and_cli_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo, base, wt = _init_repo(tmp_path, {"apps/__init__.py": "", "apps/math_ops.py": "def add(a: int, b: int) -> int:\n    return a + b\n", "tests/test_math_ops.py": "from apps.math_ops import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"})
    head_a = _commit_files(repo, base, {"apps/str_ops.py": "def shout(s: str) -> str:\n    return s.upper()\n", "tests/test_str_ops.py": "from apps.str_ops import shout\n\ndef test_shout():\n    assert shout('ok') == 'OK'\n"}, "head a")
    head_b = _commit_files(repo, base, {"apps/math_ops.py": "def add(a: int, b: int) -> int:\n    return a + b\n\ndef mul(a: int, b: int) -> int:\n    return a * b\n", "tests/test_math_ops.py": "from apps.math_ops import add, mul\n\ndef test_add():\n    assert add(2, 3) == 5\n\ndef test_mul():\n    assert mul(3, 4) == 12\n"}, "head b")
    _git(repo, "checkout", "-q", "--detach", base); refs_before = _git(repo, "show-ref")
    rc = pc.main(["--repo", str(repo), "--base", base, "--head", f"a={head_a}", "--head", f"b={head_b}", "--worktree-dir", str(wt), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == pc.EXIT_PASS == 0 and payload["status"] == "pass" and payload["tests_passed"] == 3 and "no collision found among the tests it could map" in payload["reason"]
    assert _git(repo, "status", "--porcelain") == "" and _git(repo, "rev-parse", "HEAD") == base and _git(repo, "show-ref") == refs_before
    mixed = pc.run_pregate(repo, base, [f"m={_commit_files(repo, base, {'apps/math_ops.py': 'def add(a: int, b: int) -> int:\n    return a + b + 0\n', 'apps/orphan.py': 'X = 1\n'}, 'mixed')}"], worktree_dir=wt)
    assert mixed["exit_code"] == pc.EXIT_INCONCLUSIVE == 3 and mixed["status"] == "unmapped" and mixed["tests_passed"] == 1 and "apps/orphan.py" in mixed["unmapped_files"]
def test_merge_conflict_returns_exit_2_and_identifies_conflicting_head(tmp_path: Path) -> None:
    repo, base, wt = _init_repo(tmp_path, {"pkg/calc.py": "VALUE = 1\n", "pkg/other.py": "FLAG = False\n"})
    head_a, head_b, head_c = _commit_files(repo, base, {"pkg/calc.py": "VALUE = 10\n"}, "a"), _commit_files(repo, base, {"pkg/other.py": "FLAG = True\n"}, "b"), _commit_files(repo, base, {"pkg/calc.py": "VALUE = 99\n"}, "c")
    res = pc.run_pregate(repo, base, [f"a={head_a}", f"b={head_b}", f"c={head_c}"], worktree_dir=wt)
    assert res["exit_code"] == pc.EXIT_CONFLICT == 2 and res["status"] == "merge_conflict" and res["conflict_head"] == "c" and res["conflicting_files"] == ["pkg/calc.py"] and [h["label"] for h in res["heads"]] == ["a", "b"]
def test_zero_file_overlap_pairwise_semantic_collision_caught(tmp_path: Path) -> None:
    repo, base, wt = _init_repo(tmp_path, {"apps/__init__.py": "", "tools/__init__.py": "", "apps/schema.py": "KINDS = frozenset({'dialogue', 'tool_legacy'})\n\ndef accept(kind: str) -> bool:\n    if kind not in KINDS:\n        raise ValueError(kind)\n    return True\n", "tests/test_schema.py": "from apps.schema import accept\n\ndef test_dialogue():\n    assert accept('dialogue') is True\n"})
    head_a = _commit_files(repo, base, {"apps/schema.py": "KINDS = frozenset({'dialogue'})\n\ndef accept(kind: str) -> bool:\n    if kind not in KINDS:\n        raise ValueError(kind)\n    return True\n"}, "head a")
    head_b = _commit_files(repo, base, {"tools/uploader.py": "from apps.schema import accept\n\ndef flush_legacy() -> bool:\n    return accept('tool_legacy')\n", "tests/test_uploader.py": "from tools.uploader import flush_legacy\n\ndef test_flush_legacy():\n    assert flush_legacy() is True\n"}, "head b")
    assert pc.run_pregate(repo, base, [f"a={head_a}"], worktree_dir=wt)["exit_code"] == 0 and pc.run_pregate(repo, base, [f"b={head_b}"], worktree_dir=wt)["exit_code"] == 0
    for res in (pc.run_pregate(repo, base, [f"a={head_a}", f"b={head_b}"], worktree_dir=wt), pc.run_pregate(repo, head_b, [f"a={head_a}"], worktree_dir=wt)):
        assert res["exit_code"] == pc.EXIT_FAILURE == 1 and res["status"] == "test_failure" and any("test_flush_legacy" in f for f in res["failed_tests"])
def test_db_dependent_tests_skipped_conftest_executed_and_dsn_poisoned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESHY_TEMPLATE_DSN", "postgresql://live:secret@127.0.0.1:5434/postgres"); monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "leaked.settings")
    repo, base, wt = _init_repo(tmp_path, {"apps/__init__.py": "", "apps/codec.py": "def encode_tag(x: str) -> str:\n    return x.strip().lower()\n", "tests/__init__.py": "", "tests/conftest.py": "import pytest\n@pytest.fixture\ndef prefix() -> str:\n    return 'ok:'\n", "tests/test_db_helper.py": "import psycopg\n\ndef seed():\n    return psycopg.connect('postgresql://127.0.0.1:5434/db')\n", "tests/test_transitive_db.py": "from apps.codec import encode_tag\nfrom tests.test_db_helper import seed\n\ndef test_with_db():\n    seed()\n    assert encode_tag('A') == 'a'\n", "tests/test_marked_db.py": "import pytest\nfrom apps.codec import encode_tag\n\n@pytest.mark.django_db\ndef test_orm():\n    assert encode_tag('B') == 'b'\n", "tests/test_pure_codec.py": "import os\nfrom apps.codec import encode_tag\n\ndef test_pure_and_isolated_env(prefix: str):\n    assert prefix + encode_tag('  Hi ') == 'ok:hi' and '127.0.0.1:1' in os.environ['DATABASE_URL']\n    assert 'MESHY_TEMPLATE_DSN' not in os.environ and 'DJANGO_SETTINGS_MODULE' not in os.environ\n"})
    res = pc.run_pregate(repo, base, [f"h={_commit_files(repo, base, {'apps/codec.py': 'def encode_tag(x: str) -> str:\n    return x.strip().lower()\n\nVERSION = 2\n'}, 'touch codec')}"], worktree_dir=wt)
    assert res["exit_code"] == pc.EXIT_INCONCLUSIVE == 3 and res["status"] == "partial" and res["tests_passed"] == 1 and res["selected_test_files"] == ["tests/test_pure_codec.py"] and set(res["skipped_db_tests"]) >= {"tests/test_transitive_db.py", "tests/test_marked_db.py"}
    assert pc.run_pregate(repo, base, [f"cf={_commit_files(repo, base, {'tests/conftest.py': 'import pytest\n@pytest.fixture\ndef prefix() -> str:\n    return \"bad:\"\\n'}, 'break conftest')}"], worktree_dir=wt)["exit_code"] == 1
    unmapped = pc.run_pregate(repo, base, [f"u={_commit_files(repo, base, {'data/blob.json': '{}'}, 'asset')}"], worktree_dir=wt)
    assert unmapped["exit_code"] == pc.EXIT_INCONCLUSIVE == 3 and unmapped["status"] == "unmapped" and "data/blob.json" in unmapped["unmapped_files"]
def test_unittest_non_test_prefix_and_selective_nested_autouse_conftest(tmp_path: Path) -> None:
    repo, base, wt = _init_repo(tmp_path, {"apps/__init__.py": "", "apps/codec.py": "def encode(x: str) -> str:\n    return x.upper()\n", "tests/__init__.py": "", "tests/conftest.py": "import psycopg, pytest\n@pytest.fixture\ndef db_conn():\n    return psycopg.connect('postgresql://127.0.0.1:5434/db')\n", "tests/unit/conftest.py": "import pytest\nfrom apps.codec import encode\n@pytest.fixture(autouse=True)\ndef verify_codec():\n    assert encode('ok') == 'OK'\n", "tests/unit/test_smoke.py": "def test_smoke():\n    assert 1 == 1\n", "tests/test_unittest_suite.py": "import unittest as ut, unittest.case\nfrom apps.codec import encode\nclass CodecChecks(ut.TestCase):\n    def test_upper(self):\n        self.assertEqual(encode('ab'), 'AB')\nclass CaseModChecks(unittest.case.TestCase):\n    def test_case_mod(self):\n        self.assertEqual(encode('cd'), 'CD')\n", "tests/test_dj_alias.py": "import django.test as dt\nfrom apps.codec import encode\nclass DjChecks(dt.TestCase):\n    def test_dj(self):\n        self.assertEqual(encode('ab'), 'AB')\n"})
    res_ut = pc.run_pregate(repo, base, [f"u={_commit_files(repo, base, {'apps/codec.py': 'def encode(x: str) -> str:\n    return \"CD\" if x == \"cd\" else (\"OK\" if x == \"ok\" else x)\n'}, 'break ut alias')}"], worktree_dir=wt)
    res_cm = pc.run_pregate(repo, base, [f"cm={_commit_files(repo, base, {'apps/codec.py': 'def encode(x: str) -> str:\n    return \"AB\" if x == \"ab\" else (\"OK\" if x == \"ok\" else x)\n'}, 'break unittest.case')}"], worktree_dir=wt)
    assert res_ut["exit_code"] == 1 and any("test_upper" in f for f in res_ut["failed_tests"]) and "tests/test_dj_alias.py" in res_ut["skipped_db_tests"] and res_cm["exit_code"] == 1 and any("test_case_mod" in f for f in res_cm["failed_tests"])
    assert pc.run_pregate(repo, base, [f"c={_commit_files(repo, base, {'apps/codec.py': 'def encode(x: str) -> str:\n    return x if x == \"ok\" else x.upper()\n'}, 'break nested pure autouse conftest')}"], worktree_dir=wt)["exit_code"] == 1
    ok_h = _commit_files(repo, base, {"apps/codec.py": "def encode(x: str) -> str:\n    return x.upper()\n\nV = 2\n"}, "clean")
    assert (r_ok := pc.run_pregate(repo, base, [f"g={ok_h}"], worktree_dir=wt))["exit_code"] == 3 and "tests/test_dj_alias.py" in r_ok["skipped_db_tests"]
    no_dj = _commit_files(repo, ok_h, {"tests/test_dj_alias.py": None}, "drop dj")
    assert pc.run_pregate(repo, no_dj, [f"g2={_commit_files(repo, no_dj, {'apps/codec.py': 'def encode(x: str) -> str:\n    return x.upper()\n\nV = 22\n'}, 'clean2')}"], worktree_dir=wt)["exit_code"] == 0
    db_auto = _commit_files(repo, ok_h, {"tests/conftest.py": "import psycopg, pytest\n@pytest.fixture(autouse=True)\ndef db_guard():\n    return None\n"}, "db autouse")
    assert pc.run_pregate(repo, db_auto, [f"p={_commit_files(repo, db_auto, {'apps/codec.py': 'def encode(x: str) -> str:\n    return x.upper()\n\nV = 3\n'}, 'touch under omitted db autouse')}"], worktree_dir=wt)["exit_code"] == 3
def test_fail_closed_syntax_error_bad_refs_and_inside_repo_worktree(tmp_path: Path) -> None:
    repo, base, wt = _init_repo(tmp_path, {"apps/bad.py": "X = 1\n"})
    syn = pc.run_pregate(repo, base, [f"bad={_commit_files(repo, base, {'apps/bad.py': 'def broken(:\n'}, 'syn')}"], worktree_dir=wt)
    assert syn["exit_code"] == 1 and syn["status"] == "test_failure" and "SyntaxError" in syn["reason"]
    for bad_wt in (repo / "nested-wt", tmp_path): assert pc.run_pregate(repo, base, [base], worktree_dir=bad_wt)["status"] == "error"
    other_repo, _, _ = _init_repo(tmp_path / "other", {"keep.txt": "precious\n"})
    assert pc.run_pregate(repo, base, [base], worktree_dir=other_repo)["status"] == "error" and (other_repo / "keep.txt").read_text(encoding="utf-8") == "precious\n"
    assert pc.run_pregate(repo, base, ["deadbeef" * 5], worktree_dir=wt)["status"] == "error" and pc.run_pregate(repo, base, [base], timeout=0.0, worktree_dir=wt)["status"] == "error"
    assert pc.main(["--unknown-flag"]) == pc.EXIT_FAILURE == 1 and pc.main(["--help"]) == pc.EXIT_PASS == 0
def test_relative_dynamic_renamed_deep_chain_and_missing_pytest_asyncio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    chain, id_wrap = {f"apps/l{i}.py": f"from apps.{'limits' if i == 1 else f'l{i-1}'} import threshold\n" for i in range(1, 6)}, "from .limits import threshold\n\ndef wrap() -> int:\n    return threshold()\n"
    repo, base, wt = _init_repo(tmp_path, {"apps/__init__.py": "", "apps/limits.py": "OTHER = 1\n\ndef _raw() -> int:\n    return 10\n\ndef threshold() -> int:\n    return _raw()\n", "apps/obsolete.py": "LEGACY = 7\n", "apps/a/__init__.py": "", "apps/a/limits.py": "OK = 1\ndef threshold() -> int:\n    return 5\n", "apps/a/wrapper.py": id_wrap, "apps/b/__init__.py": "", "apps/b/limits.py": "OK = 1\ndef threshold() -> int:\n    return 5\n", "apps/b/wrapper.py": id_wrap, **chain, "apps/views.py": "from .limits import (\n    threshold as quota,\n)\n\ndef _inner() -> int:\n    return quota()\n\ndef view_limit() -> int:\n    return _inner()\n", "apps/service.py": "import importlib\nPKG = 'apps'\nfrom apps.views import view_limit\n\ndef check_request(x: int) -> bool:\n    return importlib.import_module(PKG + '.limits').OTHER == 1 and x <= view_limit()\n", "tests/__init__.py": "", "tests/helpers.py": "from apps.l5 import threshold\n\ndef allowed(x: int) -> bool:\n    return x <= threshold()\n", "tests/fixtures/__init__.py": "", "tests/fixtures/support.py": "from apps.service import check_request\n\ndef check_nested(x: int) -> bool:\n    return check_request(x)\n", "tests/fixtures/db_support.py": "import psycopg\n\ndef connect_db():\n    return psycopg.connect('postgresql://127.0.0.1:5434/db')\n", "tests/test_via_helper.py": "from tests.helpers import allowed\n\ndef test_real_helper_wrapper():\n    assert allowed(5) is True\n", "tests/sub/test_nested.py": "from tests.fixtures.support import check_nested\n\ndef test_nested_helper_chain():\n    assert check_nested(5) is True\n", "tests/test_db_via_pkg_import.py": "from apps.limits import threshold\nfrom tests.fixtures import db_support\n\ndef test_db_pkg():\n    db_support.connect_db()\n    assert threshold() == 10\n", "tests/test_obsolete_consumer.py": "import apps.obsolete\n\ndef test_legacy():\n    assert apps.obsolete.LEGACY == 7\n", "tests/test_b_ok.py": "from apps.b.limits import OK\ndef test_ok():\n    assert OK == 1\n", "tests/test_b_wrap.py": "from apps.b.wrapper import wrap\ndef test_bw():\n    assert wrap() == 5\n"})
    res_rel = pc.run_pregate(repo, base, [f"rel={_commit_files(repo, base, {'apps/limits.py': 'OTHER = 1\n\ndef _raw() -> int:\n    return 0\n\ndef threshold() -> int:\n    return _raw()\n'}, 'break _raw')}"], worktree_dir=wt)
    res_ren = pc.run_pregate(repo, base, [f"ren={_commit_files(repo, base, {'apps/obsolete.py': None, 'apps/renamed.py': 'LEGACY = 7\n'}, 'rename')}"], worktree_dir=wt)
    res_b = pc.run_pregate(repo, base, [f"b={_commit_files(repo, base, {'apps/b/limits.py': 'OK = 1\ndef threshold() -> int:\n    return 0\n'}, 'break pkg b')}"], worktree_dir=wt)
    assert res_rel["exit_code"] == 1 and set(res_rel["selected_test_files"]) == {"tests/sub/test_nested.py", "tests/test_via_helper.py"} and "tests/test_db_via_pkg_import.py" in res_rel["skipped_db_tests"]
    assert res_ren["exit_code"] == 1 and "tests/test_obsolete_consumer.py" in res_ren["selected_test_files"] and res_b["exit_code"] == 1 and "tests/test_b_wrap.py" in res_b["selected_test_files"]
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: (_ for _ in ()).throw(ModuleNotFoundError("No module named 'pytest_asyncio'"))); assert pc._has_pytest_asyncio() is False
def test_file_loaders_unresolved_dyn_and_changed_test_module_helpers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rev_pipe = "from apps.entry import entry_limit\n" + "\n".join([f"def wrap_{i}() -> int:\n    return wrap_{i - 1}()\n" for i in range(7, 1, -1)] + ["def wrap_1() -> int:\n    return entry_limit()\n"])
    repo, base, wt = _init_repo(tmp_path, {"apps/__init__.py": "", "apps/limits.py": "OTHER = 1\ndef threshold() -> int:\n    return 10\n", "apps/default_impl.py": "def threshold() -> int:\n    return 10\n", "apps/entry.py": "from apps.limits import threshold\ndef entry_limit() -> int:\n    return threshold()\n", "apps/pipeline.py": rev_pipe, "apps/svc.py": "from apps.pipeline import wrap_7\ndef svc_ok() -> bool:\n    return wrap_7() == 10\n", "apps/spec_mod.py": "def spec_val() -> int:\n    return 7\n", "tests/__init__.py": "", "tests/test_static.py": "from apps.limits import OTHER\ndef test_other():\n    assert OTHER == 1\n", "tests/test_svc.py": "from apps.svc import svc_ok\ndef test_svc():\n    assert svc_ok()\n", "tests/test_loop_dyn.py": "import importlib\ndef _def_thr(mod=importlib.import_module('apps.' + 'limits')):\n    return mod.threshold()\ndef test_loop_after():\n    target = 'apps.default_impl'\n    for cand in ['limits']:\n        target = 'apps.' + cand\n    assert importlib.import_module(target).threshold() == 10 and _def_thr() == 10\n", "tests/test_def_only.py": "import importlib\ndef read_threshold(mod=importlib.import_module('apps.' + 'limits')):\n    return mod.threshold()\ndef test_def():\n    assert read_threshold() == 10\n", "tests/test_str_join.py": "import importlib\ndef test_sj():\n    parts = ('apps', 'limits')\n    assert importlib.import_module('apps.' + ''.join(['lim', 'its'])).threshold() == 10 and importlib.import_module('.'.join(parts)).threshold() == 10\n", "tests/test_fromlist_kw.py": "def test_fl_kw():\n    assert __import__('apps', fromlist=['limits']).limits.threshold() == 10\n", "tests/test_fromlist_pos.py": "def test_fl_pos():\n    assert __import__('apps', None, None, ('limits',)).limits.threshold() == 10\n", "tests/test_env_dyn.py": "import importlib, os\ndef test_env_impl():\n    assert importlib.import_module(os.getenv('PREGATE_IMPL') or 'apps.default_impl').threshold() == 10\n", "tests/test_runpath.py": "import os.path as osp, runpy\ndef test_rp():\n    assert runpy.run_path(osp.join('apps', 'limits.py'))['threshold']() == 10\n", "tests/test_specload.py": "import importlib.util\nfrom pathlib import Path\ndef test_sf():\n    sp = importlib.util.spec_from_file_location('m', Path(__file__).resolve().parents[1] / 'apps' / 'spec_mod.py')\n    mod = importlib.util.module_from_spec(sp); sp.loader.exec_module(mod)\n    assert mod.spec_val() == 7\n", "tests/test_support.py": "from apps.limits import threshold as base_t\ndef helper_limit() -> int:\n    return 20\ndef chained() -> int:\n    return base_t()\ndef test_smoke():\n    assert 1 == 1\n", "tests/test_behavior.py": "from tests.test_support import helper_limit\ndef test_beh():\n    assert helper_limit() == 20\n", "tests/test_twohop.py": "from tests.test_support import chained\ndef test_th():\n    assert chained() == 10\n"})
    rp_brk = pc.run_pregate(repo, base, [f"rp={_commit_files(repo, base, {'apps/limits.py': 'OTHER = 1\ndef threshold() -> int:\n    return 0\n'}, 'brk limits')}"], worktree_dir=wt)
    sp_brk = pc.run_pregate(repo, base, [f"sp={_commit_files(repo, base, {'apps/spec_mod.py': 'def spec_val() -> int:\n    return 0\n'}, 'brk spec')}"], worktree_dir=wt)
    ts_brk = pc.run_pregate(repo, base, [f"ts={_commit_files(repo, base, {'tests/test_support.py': 'from apps.limits import threshold as base_t\ndef helper_limit() -> int:\n    return 0\ndef chained() -> int:\n    return base_t()\ndef test_smoke():\n    assert 1 == 1\n'}, 'brk test helper')}"], worktree_dir=wt)
    assert rp_brk["exit_code"] == 1 and set(rp_brk["selected_test_files"]) >= {"tests/test_svc.py", "tests/test_loop_dyn.py", "tests/test_def_only.py", "tests/test_str_join.py", "tests/test_fromlist_kw.py", "tests/test_fromlist_pos.py", "tests/test_runpath.py", "tests/test_twohop.py"} and sp_brk["exit_code"] == 1 and "tests/test_specload.py" in sp_brk["selected_test_files"]
    env_only_base = _commit_files(repo, base, {"tests/test_svc.py": None, "tests/test_loop_dyn.py": None, "tests/test_def_only.py": None, "tests/test_str_join.py": None, "tests/test_fromlist_kw.py": None, "tests/test_fromlist_pos.py": None, "tests/test_runpath.py": None, "tests/test_twohop.py": None, "tests/test_support.py": None, "tests/test_behavior.py": None}, "env-only consumer")
    sj_only = _commit_files(repo, env_only_base, {"tests/test_only_sj.py": "import importlib\ndef test_only_sj():\n    assert importlib.import_module('apps.' + ''.join(['lim', 'its'])).threshold() == 10\n", "tests/test_caller_scope.py": "import importlib\ntarget = 'apps.' + 'default_impl'\nshadow_none = None\ndef load(name: str):\n    return importlib.import_module(name)\ndef load_two(mod_name: str):\n    target = 'apps.default_impl'\n    return load(mod_name)\ndef test_cs():\n    target = 'apps.' + 'limits'\n    shadow_none = 'limits'\n    assert load(target).threshold() == 10 and load_two('apps.' + shadow_none).threshold() == 10\n"}, "only str.join and caller scope")
    assert (sj_red := pc.run_pregate(repo, sj_only, [f"sj1={_commit_files(repo, sj_only, {'apps/limits.py': 'OTHER = 1\ndef threshold() -> int:\n    return 0\n'}, 'brk sj')}"], worktree_dir=wt))["exit_code"] == 1 and {"tests/test_only_sj.py", "tests/test_caller_scope.py"} <= set(sj_red["selected_test_files"]) and pc.run_pregate(repo, sj_only, [f"sj0={_commit_files(repo, sj_only, {'apps/spec_mod.py': 'def spec_val() -> int:\n    return 7\nV = 2\n'}, 'clean sj')}"], worktree_dir=wt)["exit_code"] == 0
    ext_h = _commit_files(repo, env_only_base, {"tests/ext_loader.py": "import importlib\ndef load(name: str):\n    return importlib.import_module(name)\ndef _warm():\n    return load('apps.default_impl')\n", "tests/test_ext_caller.py": "from tests.ext_loader import load\ndef test_ec():\n    assert load('apps.' + 'limits').threshold() == 10\n", "tests/test_alias_fn.py": "import importlib\ndef load(name: str):\n    return importlib.import_module(name)\ndef test_af():\n    load('apps.default_impl'); fn = load\n    assert fn('apps.' + 'limits').threshold() == 10\n"}, "cross-mod and aliased helper")
    assert (eh_red := pc.run_pregate(repo, ext_h, [f"eh1={_commit_files(repo, ext_h, {'apps/limits.py': 'OTHER = 1\ndef threshold() -> int:\n    return 0\n'}, 'brk eh')}"], worktree_dir=wt))["exit_code"] == 1 and {"tests/test_ext_caller.py", "tests/test_alias_fn.py"} <= set(eh_red["selected_test_files"])
    mj_only = _commit_files(repo, env_only_base, {"tests/test_mut_sj.py": "import importlib\ndef test_mut_sj():\n    ps = ['lim']\n    ps.append('its')\n    assert importlib.import_module('apps.' + ''.join(ps)).threshold() == 10\n", "tests/test_alias_sj.py": "import importlib\ndef test_alias_sj():\n    parts = ['default_impl']\n    alias = parts\n    alias[0] = 'limits'\n    assert importlib.import_module('apps.' + ''.join(parts)).threshold() == 10\n", "tests/test_diam_sj.py": "import importlib\ndef _bump(s: list[str]) -> None:\n    s[0] = 'limits'\ndef test_diam_sj():\n    b0 = ['default_impl']; a = b0; b = b0; a[0] = 'limits'\n    c = ['default_impl']; _bump(c)\n    assert importlib.import_module('apps.' + ''.join(b)).threshold() == 10 and importlib.import_module('apps.' + ''.join(c)).threshold() == 10\n"}, "mutated and aliased list str.join")
    assert (mj_red := pc.run_pregate(repo, mj_only, [f"mj1={_commit_files(repo, mj_only, {'apps/limits.py': 'OTHER = 1\ndef threshold() -> int:\n    return 0\n'}, 'brk mj')}"], worktree_dir=wt))["exit_code"] == 1 and {"tests/test_mut_sj.py", "tests/test_alias_sj.py", "tests/test_diam_sj.py"} <= set(mj_red["selected_test_files"])
    env_brk = _commit_files(repo, env_only_base, {"apps/limits.py": "OTHER = 1\ndef threshold() -> int:\n    return 0\n"}, "brk limits for env"); monkeypatch.delenv("PREGATE_IMPL", raising=False); assert pc.run_pregate(repo, env_only_base, [f"e0={env_brk}"], worktree_dir=wt)["exit_code"] == 0
    monkeypatch.setenv("PREGATE_IMPL", "apps.limits"); assert (env_red := pc.run_pregate(repo, env_only_base, [f"e1={env_brk}"], worktree_dir=wt))["exit_code"] == 1 and "tests/test_env_dyn.py" in env_red["selected_test_files"]; monkeypatch.delenv("PREGATE_IMPL")
    ke_base = _commit_files(repo, env_only_base, {"tests/env_helper.py": "import importlib, os\ndef load_thr() -> int:\n    return importlib.import_module(os.getenv('PREGATE_REVIEW_IMPL') or 'apps.default_impl').threshold()\n", "tests/test_key_env.py": "from tests.env_helper import load_thr\ndef test_ke(monkeypatch):\n    monkeypatch.setenv('PREGATE_REVIEW_IMPL', 'apps.' + 'limits')\n    assert load_thr() == 10\n"}, "key env")
    assert (ke_red := pc.run_pregate(repo, ke_base, [f"ke1={_commit_files(repo, ke_base, {'apps/limits.py': 'OTHER = 1\ndef threshold() -> int:\n    return 0\n'}, 'brk ke')}"], worktree_dir=wt))["exit_code"] == 1 and "tests/test_key_env.py" in ke_red["selected_test_files"]
    we_base = _commit_files(repo, ke_base, {"tests/test_wild_env.py": "import os\nfrom tests.env_helper import load_thr\ndef test_we():\n    d = {'PREGATE_REVIEW_IMPL': 'apps.' + 'limits'}\n    os.environ.update(d)\n    try:\n        assert load_thr() == 10\n    finally:\n        os.environ.pop('PREGATE_REVIEW_IMPL', None)\n"}, "wild env")
    assert (we_red := pc.run_pregate(repo, we_base, [f"we1={_commit_files(repo, we_base, {'apps/limits.py': 'OTHER = 1\ndef threshold() -> int:\n    return 0\n'}, 'brk we')}"], worktree_dir=wt))["exit_code"] == 1 and "tests/test_wild_env.py" in we_red["selected_test_files"] and pc.run_pregate(repo, we_base, [f"we2={_commit_files(repo, we_base, {'apps/spec_mod.py': 'def spec_val() -> int:\n    return 7\nV = 2\n'}, 'clean we')}"], worktree_dir=wt)["exit_code"] == 3
    assert ts_brk["exit_code"] == 1 and set(ts_brk["selected_test_files"]) >= {"tests/test_support.py", "tests/test_behavior.py"} and pc.run_pregate(repo, base, [f"ok={_commit_files(repo, base, {'tests/test_support.py': 'from apps.limits import threshold as base_t\ndef helper_limit() -> int:\n    return 20\ndef chained() -> int:\n    return base_t()\ndef test_smoke():\n    assert 2 == 2\n'}, 'ok test helper')}"], worktree_dir=wt)["exit_code"] == 0
    ud_base = _commit_files(repo, base, {"tests/test_udyn.py": "import runpy\ndef _p():\n    return 'apps/' + ''.join(['limits', '.py'])\ndef test_u():\n    assert runpy.run_path(_p())['OTHER'] == 1\n"}, "add udyn")
    assert pc.run_pregate(repo, ud_base, [f"u1={_commit_files(repo, ud_base, {'apps/spec_mod.py': 'def spec_val() -> int:\n    return 7\nV = 2\n'}, 'clean with udyn')}"], worktree_dir=wt)["exit_code"] == 3 and "tests/test_udyn.py" in (ud_red := pc.run_pregate(repo, ud_base, [f"u2={_commit_files(repo, ud_base, {'apps/limits.py': 'OTHER = 9\ndef threshold() -> int:\n    return 10\n', 'tests/test_static.py': 'def test_other():\n    assert True\n'}, 'brk udyn target')}"], worktree_dir=wt))["selected_test_files"] and ud_red["exit_code"] == 1
def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        if not (h := (k32 := ctypes.WinDLL("kernel32", use_last_error=True)).OpenProcess(0x00100000, False, pid)): return False
        alive = k32.WaitForSingleObject(h, 200) != 0; k32.CloseHandle(h); return alive
    if (st := Path(f"/proc/{pid}/stat")).is_file(): return not st.read_text(encoding="utf-8", errors="replace").rsplit(")", 1)[-1].lstrip().startswith("Z")
    try: os.kill(pid, 0); return True
    except OSError: return False
def test_package_init_dependencies_and_timeout_kills_grandchild_tree(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    hub_files = {"apps/__init__.py": "", "apps/boot.py": "def ok() -> bool:\n    return True\n", "apps/hub_dep.py": "def dep_limit() -> int:\n    return 10\n", "apps/feature/__init__.py": "from apps.boot import ok\nif not ok():\n    raise RuntimeError(1)\n", "apps/feature/codec.py": "def encode(s: str) -> str:\n    return s.upper()\n", "tests/test_codec.py": "from apps.feature.codec import encode\n\ndef test_encode():\n    assert encode('ok') == 'OK'\n", "apps/hub.py": "from apps.hub_dep import dep_limit\nVERSION = 'v1'\ndef scale(k: int):\n    return lambda fn: (lambda: fn() * k)\n\n@scale(2)\ndef threshold() -> int:\n    return 5\n\ndef run() -> int:\n    return 10\n\ndef allowed() -> bool:\n    return dep_limit() == 10\n\nKEEP, DROP = 1, 2\n", "tests/test_hub.py": "from apps.hub import VERSION, KEEP\ndef test_smoke():\n    assert KEEP == 1 and bool(VERSION)\n", "tests/test_hub_allowed.py": "from apps.hub import allowed\ndef test_ha():\n    assert allowed()\n", **{f"tests/test_hub_{i}.py": "from apps.hub import \\\n    threshold, DROP, run\n\ndef test_h():\n    assert threshold() == 10 and DROP == 2 and run() == 10\n" for i in range(7)}}
    repo, base, wt = _init_repo(tmp_path, hub_files)
    for brk in (_commit_files(repo, base, {"apps/feature/__init__.py": "raise RuntimeError(1)\n"}, "break init"), _commit_files(repo, base, {"apps/boot.py": "def ok() -> bool:\n    return False\n"}, "break boot"), _commit_files(repo, base, {"apps/hub.py": "from apps.hub_dep import dep_limit\nVERSION = 'v1'\ndef scale(k: int):\n    return lambda fn: (lambda: fn() * k)\n\n@scale(3)\ndef threshold() -> int:\n    return 5\n\ndef run() -> int:\n    return 10\n\ndef allowed() -> bool:\n    return dep_limit() == 10\n\nKEEP, DROP = 1, 2\n"}, "break decorator"), _commit_files(repo, base, {"apps/hub.py": "from apps.hub_dep import dep_limit\nVERSION = 'v1'\ndef scale(k: int):\n    return lambda fn: (lambda: fn() * k)\n\n@scale(2)\ndef threshold() -> int:\n    return 5\n\ndef run() -> int:\n    return 10\n\ndef allowed() -> bool:\n    return dep_limit() == 10\n\nKEEP = 1\n"}, "delete tuple target"), _commit_files(repo, base, {"apps/hub.py": "from apps.hub_dep import dep_limit\nVERSION = 'v1'\ndef scale(k: int):\n    return lambda fn: (lambda: fn() * k)\n\n@scale(2)\ndef threshold() -> int:\n    return 5\n\ndef run() -> int:\n    return 0\n\ndef allowed() -> bool:\n    return dep_limit() == 10\n\nKEEP, DROP = 1, 2\n"}, "break hub run()"), _commit_files(repo, base, {"apps/hub_dep.py": "def dep_limit() -> int:\n    return 0\n", "apps/hub.py": "from apps.hub_dep import dep_limit\nVERSION = 'allowed'\ndef scale(k: int):\n    return lambda fn: (lambda: fn() * k)\n\n@scale(2)\ndef threshold() -> int:\n    return 5\n\ndef run() -> int:\n    return 10\n\ndef allowed() -> bool:\n    return dep_limit() == 10\n\nKEEP, DROP = 1, 2\n"}, "break hub_dep + edit hub VERSION"), _commit_files(repo, base, {"apps/hub.py": "raise RuntimeError('import boom')\n"}, "hub top side effect")):
        assert pc.run_pregate(repo, base, [f"init={brk}"], worktree_dir=wt)["exit_code"] == 1
    res_to = pc.run_pregate(repo, base, [f"hang={_commit_files(repo, base, {'tests/test_hang.py': f'import subprocess, sys\nfrom pathlib import Path\ndef test_hang():\n    p = subprocess.Popen([sys.executable, \"-c\", \"import time; time.sleep(60)\"])\n    Path({str(pid_file)!r}).write_text(str(p.pid), encoding=\"utf-8\")\n    p.wait()\n'}, 'hang')}"], timeout=7.5, worktree_dir=wt)
    assert res_to["exit_code"] == pc.EXIT_FAILURE == 1 and res_to["status"] == "timeout" and pid_file.is_file(); gc_pid = int(pid_file.read_text(encoding="utf-8").strip())
    for _ in range(15):
        if not _pid_alive(gc_pid): break
        time.sleep(0.05)
    assert not _pid_alive(gc_pid)
def test_a_seat_gitconfig_with_diff_noprefix_cannot_turn_a_breaking_change_into_a_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # REFACTOR-QUEUE A2: GIT_CONFIG_NOSYSTEM does not keep out the seat's global gitconfig. With diff.noprefix on
    # there, "--- a/" never matched, the diff parsed to nothing, and a change that breaks a package's __init__ passed with exit 0.
    # The two scenarios that read diff signals, re-run under that seat config: before the fix one returned 0 (package init) and
    # the other 3 (loaders) where both must return 1. Each gets its own folder so the two repos do not meet.
    # kxxx's review of #645 added three more that did the same: colour codes around the headers, and other prefixes.
    # Each case first proves its setting is live: a plain git diff of a one-line change no longer prints the "--- a/" header the
    # parser reads (nxxxxx's review of #652). mnemonicPrefix is the exception and carries no such proof: on git 2.51.1 a diff
    # between two trees keeps a/ and b/ under it, so that case shows only that the pinned command line still parses. git older
    # than 2.45 does not know srcPrefix and dstPrefix, so there that case is not live either and its proof is not asked for.
    git_version = tuple(int(part) for part in _git(tmp_path, "--version").split()[2].split(".")[:2])
    for flags, live_since in (("[diff]\n\tnoprefix = true\n", (0, 0)), ("[diff]\n\tmnemonicPrefix = true\n", None), ("[color]\n\tui = always\n", (0, 0)),
                        ("[color]\n\tdiff = always\n", (0, 0)), ("[diff]\n\tsrcPrefix = x/\n\tdstPrefix = y/\n", (2, 45))):
        (gc := tmp_path / "seat.gitconfig").write_text(flags, encoding="utf-8"); monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gc))
        (ctl := tmp_path / f"control-{len(list(tmp_path.iterdir()))}").mkdir(); c_repo, c_base, _c_wt = _init_repo(ctl, {"apps/__init__.py": "", "apps/a.py": "X = 1\n"})
        c_raw = "\n" + _git(c_repo, "diff", "-U0", c_base, _commit_files(c_repo, c_base, {"apps/a.py": "X = 2\n"}, "one line"), "--", "*.py") + "\n"
        assert live_since is None or git_version < live_since or "\n--- a/apps/a.py\n" not in c_raw, (flags, c_raw)
        (one := tmp_path / f"init-{len(list(tmp_path.iterdir()))}").mkdir(); test_package_init_dependencies_and_timeout_kills_grandchild_tree(one)
        (two := tmp_path / f"loaders-{len(list(tmp_path.iterdir()))}").mkdir(); test_file_loaders_unresolved_dyn_and_changed_test_module_helpers(two, monkeypatch)
def test_a_seat_textconv_driver_cannot_reshape_the_patch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # REFACTOR-QUEUE A2, follow-up: a seat gitconfig can also name an attributes file and a textconv driver for *.py, and git diff
    # runs textconv unless it is told not to. The patch then describes converted text (here: each file's first line only), so a
    # change below line 1 has no header and no hunk. --no-textconv on the command line beats the seat's config.
    (ga := tmp_path / "seat.gitattributes").write_text("*.py diff=seatconv\n", encoding="utf-8")
    (gc := tmp_path / "seat.gitconfig").write_text('[core]\n\tattributesFile = %s\n[diff "seatconv"]\n\ttextconv = "f() { head -n 1 \\"$1\\"; }; f"\n' % ga.as_posix(), encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gc))
    # Control: the driver is live here. Without this the two scenarios below could pass because the stimulus never arrived.
    (ctl := tmp_path / "control").mkdir(); repo, base, _wt = _init_repo(ctl, {"apps/__init__.py": "", "apps/a.py": "X = 1\nY = 2\n"})
    head = _commit_files(repo, base, {"apps/a.py": "X = 1\nY = 3\n"}, "line two")
    assert "+Y = 3" not in _git(repo, "diff", "-U0", base, head, "--", "*.py") and "+Y = 3" in _git(repo, "diff", "--no-textconv", "-U0", base, head, "--", "*.py")
    (one := tmp_path / "init").mkdir(); test_package_init_dependencies_and_timeout_kills_grandchild_tree(one)
    (two := tmp_path / "loaders").mkdir(); test_file_loaders_unresolved_dyn_and_changed_test_module_helpers(two, monkeypatch)
def test_a_header_shaped_line_in_a_hunk_body_stays_body(tmp_path: Path) -> None:
    # nxxxxx's review of #652: the parser took "--- a/" and "+++ b/" for file headers wherever they stood. A docstring line
    # "++ b/fake.py" is added, the patch prints it as "+++ b/fake.py", and the change 28 lines below was booked to fake.py.
    base_impl = '"""Documentation.\n' + "".join(f"filler {i}\n" for i in range(24)) + '"""\n\ndef threshold():\n    return 5\n'
    repo, base, wt = _init_repo(tmp_path, {"apps/__init__.py": "", "apps/impl.py": base_impl, "apps/consumer.py": "from apps.impl import threshold\n\ndef value():\n    return threshold()\n", "tests/test_impl.py": "def test_benign():\n    assert True\n", "tests/test_consumer.py": "from apps.consumer import value\n\ndef test_consumer_value():\n    assert value() == 5\n"})
    head = _commit_files(repo, base, {"apps/impl.py": base_impl.replace("Documentation.\n", "Documentation.\n++ b/fake.py\n").replace("return 5", "return 6")}, "a header-shaped body line, then a break")
    # Control: the patch really carries the header-shaped line in a hunk body.
    assert "\n+++ b/fake.py\n" in "\n" + _git(repo, "diff", "-U0", base, head, "--", "*.py") + "\n"
    _old, new_lines, diff_syms, _defs = pc._parse_diff_signals(repo, base, _git(repo, "rev-parse", f"{head}^{{tree}}"), time.perf_counter() + 60)
    assert new_lines == {"apps/impl.py": {2, 30}} and set(diff_syms) <= {"apps/impl.py"}, (new_lines, diff_syms)
    # The whole run still selects the consumer's test and fails on it (it did before the fix too; kept so it stays so).
    res = pc.run_pregate(repo, base, [head], worktree_dir=wt)
    assert res["exit_code"] == 1 and res["status"] == "test_failure" and "tests/test_consumer.py" in res["selected_test_files"], res
def test_the_no_newline_marker_does_not_end_the_hunk_body(tmp_path: Path) -> None:
    # The "\ No newline at end of file" marker stands inside a hunk body and is not one of its counted lines. A parser that
    # lost its place there reads the next added line as a header and books the names after it to fake.py.
    n_repo, n_base, _n_wt = _init_repo(tmp_path, {"apps/__init__.py": "", "apps/tail.py": "A = 1\nB = 2"})
    n_head = _commit_files(n_repo, n_base, {"apps/tail.py": 'A = 1\nB = 2\n++ b/fake.py\nC = "tokenword"\n'}, "newline at the end, then a header-shaped line")
    assert "\n\\ No newline at end of file\n+B = 2\n+++ b/fake.py\n" in "\n" + _git(n_repo, "diff", "-U0", n_base, n_head, "--", "*.py") + "\n"
    _old, n_new, n_syms, _defs = pc._parse_diff_signals(n_repo, n_base, _git(n_repo, "rev-parse", f"{n_head}^{{tree}}"), time.perf_counter() + 60)
    assert n_new == {"apps/tail.py": {2, 3, 4}} and set(n_syms) == {"apps/tail.py"} and "tokenword" in n_syms["apps/tail.py"], (n_new, n_syms)
def test_a_form_feed_does_not_cut_a_hunk_body_line(tmp_path: Path) -> None:
    # A form feed inside one added line: splitlines() cut the line there and the rest read as a header of its own.
    f_repo, f_base, _f_wt = _init_repo(tmp_path, {"apps/__init__.py": "", "apps/ff.py": "A = 1\nB = 2\nC = 3\n"})
    f_head = _commit_files(f_repo, f_base, {"apps/ff.py": "# \x0c+++ b/fake.py\nA = 1\nB = 2\nC = 30\n"}, "a form feed, then a change")
    assert "+# \x0c+++ b/fake.py" in _git(f_repo, "diff", "-U0", f_base, f_head, "--", "*.py")
    _old, f_new, _syms, _defs = pc._parse_diff_signals(f_repo, f_base, _git(f_repo, "rev-parse", f"{f_head}^{{tree}}"), time.perf_counter() + 60)
    assert f_new == {"apps/ff.py": {1, 4}}, f_new
def test_a_seat_inter_hunk_context_cannot_put_unchanged_lines_into_the_patch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # With diff.interHunkContext a seat's git fuses hunks that stand close together and prints the unchanged lines between
    # them inside the hunk, so they counted as changed; with diff.suppressBlankEmpty a blank one prints as an empty line.
    # --inter-hunk-context=0 on the command line beats both.
    (gc := tmp_path / "seat.gitconfig").write_text("[diff]\n\tinterHunkContext = 3\n\tsuppressBlankEmpty = true\n", encoding="utf-8"); monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gc))
    repo, base, _wt = _init_repo(tmp_path, {"apps/__init__.py": "", "apps/a.py": "A = 1\nB = 2\n\nC = 3\nD = 4\n"})
    head = _commit_files(repo, base, {"apps/a.py": "A = 1\nB = 20\n\nC = 30\nD = 4\n"}, "two lines, a blank one between")
    # Control: the setting is live here. Unpinned, git prints one hunk of three lines where two hunks of one line are due.
    assert "@@ -2,3 +2,3 @@" in _git(repo, "diff", "-U0", base, head, "--", "*.py")
    old_lines, new_lines, _syms, _defs = pc._parse_diff_signals(repo, base, _git(repo, "rev-parse", f"{head}^{{tree}}"), time.perf_counter() + 60)
    assert old_lines == {"apps/a.py": {2, 4}} and new_lines == {"apps/a.py": {2, 4}}, (old_lines, new_lines)
def test_a_patch_the_parser_cannot_read_is_refused_not_read_as_no_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The guard behind A2: whatever shapes the patch next, a .py file whose content changed but whose header the parser never
    # saw refuses the run. Simulated by stripping the patch's header lines, as an unknown setting would; control first.
    repo, base, wt = _init_repo(tmp_path, {"apps/__init__.py": "", "apps/a.py": "X = 1\n", "tests/test_a.py": "from apps.a import X\n\ndef test_x():\n    assert X == 1\n"})
    head = _commit_files(repo, base, {"apps/a.py": "X = 1\nY = 2\n"}, "touch")
    ok = pc.run_pregate(repo, base, [head], worktree_dir=wt)
    assert ok["status"] != "error" and "no file header" not in ok["reason"], ok
    real = pc._git_text
    def headerless(cwd: Path, *args: str, **kw):
        p = real(cwd, *args, **kw)
        return p if "-U0" not in args else subprocess.CompletedProcess(p.args, p.returncode, "\n".join(ln for ln in p.stdout.splitlines() if not ln.startswith(("--- ", "+++ "))), p.stderr)
    monkeypatch.setattr(pc, "_git_text", headerless)
    res = pc.run_pregate(repo, base, [head], worktree_dir=wt)
    assert res["status"] == "error" and res["exit_code"] == pc.EXIT_FAILURE and "no file header for 1 changed .py file(s) (apps/a.py)" in res["reason"], res
def test_docs_only_unmapped_files_pass_through_but_other_unmapped_files_still_stop_the_run(tmp_path: Path) -> None:
    repo, base, wt = _init_repo(tmp_path, {"apps/__init__.py": "", "apps/math_ops.py": "def add(a: int, b: int) -> int:\n    return a + b\n", "tests/test_math_ops.py": "from apps.math_ops import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"})
    touch = {"apps/math_ops.py": "def add(a: int, b: int) -> int:\n    return a + b + 0\n"}
    docs = pc.run_pregate(repo, base, [f"d={_commit_files(repo, base, {**touch, 'docs/design/note.md': 'x\n', 'NOTES.md': 'y\n'}, 'code and docs')}"], worktree_dir=wt)
    assert docs["status"] != "unmapped" and docs["unmapped_files"] == [] and docs["doc_files"] == ["NOTES.md", "docs/design/note.md"] and docs["tests_passed"] == 1
    for other in ("sub/README.md", "data/blob.json", "scripts/run.sh"):
        res = pc.run_pregate(repo, base, [f"o={_commit_files(repo, base, {**touch, other: 'z\n'}, 'code and ' + other)}"], worktree_dir=wt)
        assert res["status"] == "unmapped" and res["exit_code"] == pc.EXIT_INCONCLUSIVE and res["unmapped_files"] == [other] and res["doc_files"] == [], other
def test_interpreter_without_the_repo_dependencies_is_refused_at_once_with_a_named_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, base, wt = _init_repo(tmp_path, {"apps/a.py": "X = 1\n"})
    head = _commit_files(repo, base, {"apps/a.py": "X = 2\n"}, "touch")
    ok = pc.run_pregate(repo, base, [head], worktree_dir=wt)
    assert ok["status"] != "error" and "cannot import" not in ok["reason"]
    monkeypatch.setattr(pc, "_REQUIRED_MODULES", ("django", "no_such_repo_dependency_xyz"))
    res = pc.run_pregate(repo, base, [head], worktree_dir=wt)
    assert res["status"] == "interpreter" and res["exit_code"] == pc.EXIT_INCONCLUSIVE != pc.EXIT_FAILURE and "no_such_repo_dependency_xyz" in res["reason"] and "repo's own python" in res["reason"] and res["tests_ran"] == 0 and res["elapsed_s"] < 5
def test_default_budget_is_sixty_seconds_for_the_function_and_the_command_line(monkeypatch: pytest.MonkeyPatch) -> None:
    import inspect
    assert inspect.signature(pc.run_pregate).parameters["timeout"].default == 60.0
    seen: dict[str, float] = {}
    monkeypatch.setattr(pc, "run_pregate", lambda **kw: seen.update(timeout=kw["timeout"]) or {"status": "pass", "exit_code": 0, "union_tree": None, "elapsed_s": 0.0, "reason": ""})
    assert pc.main(["--head", "HEAD"]) == 0 and seen["timeout"] == 60.0
    assert pc.main(["--head", "HEAD", "--timeout", "15"]) == 0 and seen["timeout"] == 15.0
def test_a_changed_doc_that_a_test_file_reads_is_not_a_pass_unless_that_test_ran(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reader = "from pathlib import Path\n\ndef test_guide():\n    assert Path(__file__).parents[1].joinpath('docs', 'GUIDE.md').read_text() != 'zero'\n"
    files = {"apps/__init__.py": "", "apps/math_ops.py": "def add(a: int, b: int) -> int:\n    return a + b\n", "docs/GUIDE.md": "one\n", "docs/STATUS.md": "s\n", "tests/test_math_ops.py": "from apps.math_ops import add\n\ndef test_add():\n    assert add(1, 2) == 3\n", "tests/test_guide.py": reader, "tests/sub/test_deep.py": "P = 'docs\\\\GUIDE.md'\n", "tests/helper.py": "G = 'GUIDE.md'\n", "tests/test_win.py": "A = r'docs\\a.md'\nB = 'docs\\\\b.md'\n", "tests/test_short.py": "C = 'c.md'\n"}
    repo, base, wt = _init_repo(tmp_path, files); touch = {"apps/math_ops.py": "def add(a: int, b: int) -> int:\n    return a + b + 0\n"}
    # the helper, on the union worktree of a real run: by file name, by path, by a Windows path; never a selected file, a non-test file or a short name
    ran = pc.run_pregate(repo, base, [f"r={_commit_files(repo, base, {**touch, 'docs/GUIDE.md': 'two\n'}, 'code and a doc a test reads')}"], worktree_dir=wt)
    assert ran["status"] == "partial" and ran["exit_code"] == pc.EXIT_INCONCLUSIVE and ran["doc_files"] == ["docs/GUIDE.md"] and "tests/test_guide.py" in ran["selected_test_files"] and ran["doc_reader_tests"] == ["tests/sub/test_deep.py"] and "tests/sub/test_deep.py" in ran["reason"]
    assert pc._doc_reader_tests(wt, ["docs/GUIDE.md"], []) == ["tests/sub/test_deep.py", "tests/test_guide.py"]
    assert pc._doc_reader_tests(wt, ["docs/GUIDE.md"], ["tests/test_guide.py"]) == ["tests/sub/test_deep.py"]
    assert pc._doc_reader_tests(wt, ["docs/STATUS.md"], []) == [] and pc._doc_reader_tests(wt, ["c.md"], []) == [] and pc._doc_reader_tests(wt, [], []) == []
    assert pc._doc_reader_tests(wt, ["docs/a.md"], []) == ["tests/test_win.py"] and pc._doc_reader_tests(wt, ["docs/b.md"], []) == ["tests/test_win.py"]  # a short file name is found by its Windows path, single or doubled
    # the verdict: a reader that did not run turns a pass into partial, exit 3, and is named
    monkeypatch.setattr(pc, "_doc_reader_tests", lambda wt_, docs, selected: ["tests/test_unrun_reader.py"] if docs else [])
    res = pc.run_pregate(repo, base, [f"r={_commit_files(repo, base, {**touch, 'docs/GUIDE.md': 'three\n'}, 'code and a doc again')}"], worktree_dir=wt)
    assert res["status"] == "partial" and res["exit_code"] == pc.EXIT_INCONCLUSIVE and res["doc_reader_tests"] == ["tests/test_unrun_reader.py"] and "tests/test_unrun_reader.py" in res["reason"] and "local_gate.py" in res["reason"]
    ok = pc.run_pregate(repo, base, [f"s={_commit_files(repo, base, touch, 'code only')}"], worktree_dir=wt)
    assert ok["status"] == "pass" and ok["doc_files"] == [] and ok["doc_reader_tests"] == []
def test_a_doc_reader_in_a_partly_selected_file_still_counts_as_not_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # kxxx a4af54 P2-1: selection is per test, so a file with one selected test of two did not run whole
    assert pc._whole_files(["tests/test_a.py::test_one"], {"tests/test_a.py": 2}) == [] and pc._whole_files(["tests/test_a.py::test_one", "tests/test_a.py::test_two"], {"tests/test_a.py": 2}) == ["tests/test_a.py"] and pc._whole_files([], {}) == []
    mix = "from pathlib import Path\nfrom apps.math_ops import add\n\ndef test_add():\n    assert add(1, 2) == 3\n\ndef test_guide():\n    assert Path(__file__).parents[1].joinpath('docs', 'GUIDE.md').read_text() != 'zero'\n"
    repo, base, wt = _init_repo(tmp_path, {"apps/__init__.py": "", "apps/math_ops.py": "def add(a: int, b: int) -> int:\n    return a + b\n", "docs/GUIDE.md": "one\n", "tests/test_mix.py": mix})
    change = {"apps/math_ops.py": "def add(a: int, b: int) -> int:\n    return a + b + 0\n", "docs/GUIDE.md": "two\n"}
    whole = pc.run_pregate(repo, base, [f"w={_commit_files(repo, base, change, 'code and doc')}"], worktree_dir=wt)
    assert whole["status"] == "pass" and whole["selected_test_files"] == ["tests/test_mix.py"] and whole["doc_reader_tests"] == []  # both tests ran, so the reader ran
    monkeypatch.setattr(pc, "_whole_files", lambda raw_nodes, total_by_rel: [])  # the same run, had only test_add been selected
    part = pc.run_pregate(repo, base, [f"p={_commit_files(repo, base, {**change, 'docs/GUIDE.md': 'three\n'}, 'code and doc again')}"], worktree_dir=wt)
    assert part["selected_test_files"] == ["tests/test_mix.py"] and part["doc_reader_tests"] == ["tests/test_mix.py"] and part["status"] == "partial" and part["exit_code"] == pc.EXIT_INCONCLUSIVE
if __name__ == "__main__": raise SystemExit(pytest.main([__file__]))
