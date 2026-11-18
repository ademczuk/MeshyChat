"""A deleted transcript is not a conflict, and nothing else became one: each mutant must fail an assertion."""
W = "tools/postbox/context_worker.py"
T = ["tests/test_postbox_context_worker.py"]

MUTANTS = [
    ("missing:M1 a gone source still stops the seat", W,
     'if _gone_under_a_live_root(path, allowed_roots):', 'if False:', T),
    ("missing:M2 any stat failure counts as gone", W,
     '            except FileNotFoundError:\n                return True\n            except OSError:\n                break\n',
     '            except OSError:\n                return True\n', T),
    ("missing:M3 a file outside the roots counts as gone", W,
     '        if not inside:\n            continue\n        try:\n            root_st = os.lstat(root_abs)',
     '        try:\n            root_st = os.lstat(root_abs)', T),
    ("missing:M4 a root that has gone counts as live", W,
     '            root_st = os.lstat(root_abs)\n        except OSError:\n            continue\n',
     '            root_st = os.lstat(root_abs)\n        except OSError:\n            return True\n', T),
    ("missing:M5 a deleted pin still stops the seat", W,
     'if _enrolled_pin_is_gone(ledger_path, pin_path, seat, dialect, allowed_roots):', 'if False:', T),
    ("missing:HZ a pin never enrolled counts as gone", W,
     '    files = read_ledger(ledger_path)["files"]\n    for key in keys:\n',
     '    return _gone_under_a_live_root(requested, allowed_roots)\n    files = read_ledger(ledger_path)["files"]\n    for key in keys:\n', T),
    ("missing:M7 a link or file above the transcript reads as absent", W,
     '            if not stat.S_ISDIR(st.st_mode) or _is_reparse_or_link(st):\n                break\n    return False',
     '    return False', T),
    ("missing:M8 another seat's pin counts", W,
     'entry is not None and entry.get("seat") == seat and entry.get("dialect") == dialect',
     'entry is not None and entry.get("dialect") == dialect', T),
    ("missing:M9 another dialect's pin counts", W,
     'entry is not None and entry.get("seat") == seat and entry.get("dialect") == dialect',
     'entry is not None and entry.get("seat") == seat', T),
    ("missing:M10 the antigravity sibling key is not looked up", W,
     '        keys.add(os.path.normcase(os.path.join(os.path.dirname(requested), "transcript_full.jsonl")))\n',
     '        pass\n', T),
]
