"""Step B turn guards: each mutant must fail an assertion, not setup or a timeout."""
P = "tools/postbox/turnstate.py"
R = "tools/postbox/turn_reporter.py"
S = "apps/peers/turn.py"
F = "tools/meshy_node/fleet.py"
PARSER = ["tests/test_postbox_turnstate.py"]
REPORTER = ["tests/test_postbox_turn_reporter.py"]
SERVER = ["tests/test_peer_turn.py"]
CLIENT = ["tests/test_meshy_node_turn.py"]

MUTANTS = [
    ("turn:T1 aborted Codex turn stays open", P,
     'event in ("task_complete", "turn_aborted")', 'event in ("task_complete",)', PARSER),
    ("turn:T2 sidechain closes main turn", P,
     'or record.get("isSidechain")', 'or False', PARSER),
    ("turn:T3 interrupted Claude user remains open", P,
     '"closed" if interrupted else "open"', '"open" if interrupted else "open"', PARSER),
    ("turn:T4 marker-free growth invents closed", R,
     'state = "open"  # marker-free supported output',
     'state = "closed"  # marker-free supported output', REPORTER),
    ("turn:T5 refresh never repeats an unchanged report", R,
     'now - self.sent_at < REFRESH_SECONDS', 'True', REPORTER),
    ("turn:T6 failure retry ignores backoff", R,
     'if now < self.next_try:', 'if False:', REPORTER),
    ("turn:T7 child has no windowless flag", R,
     'creationflags=postbox_os.NO_WINDOW', 'creationflags=-1', REPORTER),
    ("turn:T8 rate allows a fifth report", S,
     'REPORTS_PER_MINUTE = 4', 'REPORTS_PER_MINUTE = 5', SERVER),
    ("turn:T9 reader retains stale report", S,
     "NOW() - INTERVAL '600 seconds'", "NOW() - INTERVAL '6000 seconds'", SERVER),
    ("turn:T10 older session reverts newer state", S,
     'EXCLUDED.turn_item_at >= mesh_presence.turn_item_at', 'TRUE', SERVER),
    ("turn:T11 client quiet boundary late", F,
     'TURN_QUIET_SECONDS = 300', 'TURN_QUIET_SECONDS = 301', CLIENT),
    ("turn:T12 client stale report reads idle", F,
     'TURN_REPORT_TTL_SECONDS = 600', 'TURN_REPORT_TTL_SECONDS = 601', CLIENT),
    ("turn:T13 active asks trigger idle alert", F,
     'owed.get("open", 0) > 0', 'owed.get("active", 0) > 0', CLIENT),
    ("turn:T14 publish leaks a local path", "tools/meshy_node/turn.py",
     'body = {"state": state_value, "item_at": item_at}',
     'body = {"state": state_value, "item_at": item_at, "path": str(path)}', CLIENT),
    ("turn:T15 turn report overwrites another presence writer", S,
     'turn_state = EXCLUDED.turn_state, turn_item_at',
     "state = 'active', turn_state = EXCLUDED.turn_state, turn_item_at", SERVER),
    ("turn:T16 Claude local commands become open turns", P,
     'return None, None\n        interrupted =', 'return "claude", "open"\n        interrupted =', PARSER),
    ("turn:T17 an unapplied report looks successful", R,
     'and answer.get("applied") is True', 'and True', REPORTER),
    ("turn:T18 meta prompts fail to open a model turn", P,
     'if typ == "user":\n        content =',
     'if typ == "user":\n        if record.get("isMeta") is True:\n            return None, None\n        content =', PARSER),
    ("turn:T19 a completed Codex child overrides its parent", R,
     'if isinstance(source, dict) and "subagent" in source:', 'if False:', REPORTER),
    ("turn:T20 an unseen new turn inherits the prior closed state", R,
     'state = "open"  # marker-free supported output',
     'state = previous[1] if previous and previous[1] else "open"  # marker-free supported output', REPORTER),
]
