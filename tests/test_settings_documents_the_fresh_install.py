"""The settings module's environment table names what a fresh install needs.

WHY (end-to-end run of 2026-09-23, finding 3). The run's first attempt exported
MESHY_STATE_SECRET, because every other variable in meshychat/settings.py's table is
spelled MESHY_*. It did nothing: that name is only a Django SETTING, and the variable the
environment is read under is MESHYCHAT_STATE_SECRET (apps/dashboard/auth.py
`state_secret`, apps/grants/config.py). `/v1/auth/me` answered `csrf_token: null` and no
pairing code could be minted, and neither name appeared in the table or the README. The
bootstrap authority (MESHYCHAT_BOOTSTRAP_AUTHORITY_PEER_ID), without which no namespace can be
claimed, was undocumented too (divergence 024).

This is deliberately NOT "every variable is documented": 47 names read under apps/ are
absent from the table today (measured 2026-09-23), most of them rarely set.
It pins the ones a fresh install cannot work without, each of which must also actually be
read by the code (the control that the list is not stale).

Run:  pytest tests/test_settings_documents_the_fresh_install.py -q     (no database)
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FRESH_INSTALL = {
    "MESHYCHAT_STATE_SECRET": "the CSRF HMAC key; unset, no pairing code can be minted",
    "MESHYCHAT_BOOTSTRAP_AUTHORITY_PEER_ID": "the one peer allowed to namespace.claim",
    "MESHY_MODEL_PROVIDERS": "the model registry the embed job, search and the auditor use",
    "MESHY_EMBED_FORBIDDEN_ROWS": "the embed_policy gate; closed, nothing that exists embeds",
}


def _settings_doc() -> str:
    tree = ast.parse((ROOT / "meshychat" / "settings.py").read_text(encoding="utf-8"))
    doc = ast.get_docstring(tree)
    assert doc and "MESHY_DSN" in doc, "control: the environment table is where it was"
    return doc


def _names_read_by_code() -> set[str]:
    names: set[str] = set()
    for p in (ROOT / "apps").rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                names.add(node.value)
    return names


def test_every_fresh_install_variable_is_read_by_the_code():
    read = _names_read_by_code()
    stale = [n for n in FRESH_INSTALL if n not in read]
    assert not stale, f"no module under apps/ reads {stale}: this list has gone stale"


def test_every_fresh_install_variable_is_in_the_settings_table():
    doc = _settings_doc()
    missing = [n for n in FRESH_INSTALL if n not in doc]
    assert not missing, f"meshychat/settings.py's environment table does not name {missing}"


def test_the_setting_only_name_is_called_out():
    """MESHY_STATE_SECRET is the name an operator reaches for; the table must say that
    exporting it does nothing, beside the variable that works."""
    doc = _settings_doc()
    line = next((ln for ln in doc.splitlines() if "MESHY_STATE_SECRET" in ln), "")
    assert line, "the table does not mention MESHY_STATE_SECRET at all"
    assert "NOT" in line or "not" in line, line


#: The object-capability variables (B07 slice 4, divergence 040). A fresh install does not need
#: them (tokens are OFF unless MESHY_CAPABILITIES says on), but the table is where an operator
#: looks, and a variable it does not name is one they set under the wrong name, as
#: MESHY_STATE_SECRET was. Each must be in the table AND read by the code.
CAPABILITY = {
    "MESHY_CAPABILITIES": "tokens on or off; off by default, a malformed value refuses to load",
    "MESHY_CAPABILITY_ROOT_KEY_PATH": "a FILE holding the hex root private key",
    "MESHY_CAPABILITY_ROOT_KID": "the active key's integer id",
    "MESHY_CAPABILITY_ROOT_PUBKEYS": "the verify set, kid=hex;kid=hex",
    "MESHY_CAPABILITY_DEV_EPHEMERAL": "a dev-only ephemeral key, honoured only under DEBUG",
}


def test_every_capability_variable_is_read_and_documented():
    read, doc = _names_read_by_code(), _settings_doc()
    stale = [n for n in CAPABILITY if n not in read]
    assert not stale, f"no module under apps/ reads {stale}: this list has gone stale"
    missing = [n for n in CAPABILITY if n not in doc]
    assert not missing, f"meshychat/settings.py's environment table does not name {missing}"


def test_the_table_says_the_key_is_a_file_not_a_legacy_variable_and_how_to_repair_it():
    """The key is named by a path, never given in the environment, and never
    MESHYCHAT_BISCUIT_ROOT_SK (which makes issue_grant refuse every grant here); and a broken key
    stops every manage.py command, so the table carries the runbook line."""
    doc = _settings_doc()
    flat = " ".join(doc.split())
    assert "unset MESHY_CAPABILITIES" in flat, "the runbook line is not in the table"
    line = next((ln for ln in doc.splitlines() if "MESHYCHAT_BISCUIT_ROOT_SK" in ln), "")
    assert line and ("NOT" in line or "never" in line), line
    # CONTROL: the parser finds a line it should (the fresh-install table's own).
    assert "MESHYCHAT_STATE_SECRET" in flat


#: The transitional mesh switch (divergence 011, dated paragraph 2026-09-27). A fresh install does
#: not need it, and it is OFF unless set; production turns it on for the cutover and off once the
#: nodes send v1. An operator will look for it in the table, so it must be there and be read.
TRANSITIONAL = {
    "MESHY_MESH_ACCEPT_UNVERSIONED": "accept a mesh message with no schema_version; off unless on",
}


def test_every_transitional_variable_is_read_and_documented_as_transitional():
    read, doc = _names_read_by_code(), _settings_doc()
    stale = [n for n in TRANSITIONAL if n not in read]
    assert not stale, f"no module under apps/ reads {stale}: this list has gone stale"
    missing = [n for n in TRANSITIONAL if n not in doc]
    assert not missing, f"meshychat/settings.py's environment table does not name {missing}"
    flat = " ".join(doc.split())
    at = flat.index("MESHY_MESH_ACCEPT_UNVERSIONED")
    entry = flat[at:at + 700]
    assert "TRANSITIONAL" in entry and "OFF when unset" in entry, entry
    assert "tools.meshy_node" in entry and "011" in entry, entry
