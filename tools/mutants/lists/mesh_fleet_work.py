"""Step A fleet work facts: bounded reads, held results and explicit client states."""
W = "apps/mesh/work.py"
F = "tools/meshy_node/fleet.py"
SERVER = ["tests/test_mesh_fleet_work.py"]
CLIENT = ["tests/test_meshy_node_fleet_work.py"]
COST = ["tests/test_query_cost_bounds.py"]

MUTANTS = [
    ("fleet-work:W1 ask window includes old work", W,
     "ASK_DAYS = 14", "ASK_DAYS = 365", SERVER),
    ("fleet-work:W2 last send includes older keys", W,
     "SENT_DAYS = 7", "SENT_DAYS = 30", SERVER),
    ("fleet-work:W3 awaiting sample truncates counts", W,
     'states=("open", "active"), since=since, limit=None)',
     'states=("open", "active"), since=since, limit=1)', SERVER),
    ("fleet-work:W4 successful facts are never held", W,
     "_clock() - held[0] < HOLD_SECONDS", "False", SERVER),
    ("fleet-work:W5 held facts never expire", W,
     "_clock() - held[0] < HOLD_SECONDS", "True", SERVER),
    ("fleet-work:W6 work uses a heavy ten-label chunk", F,
     "WORK_CHUNK = 4", "WORK_CHUNK = 10", CLIENT),
    ("fleet-work:W7 no send becomes unknown", F,
     'sent=f"none/{sent_days}d" if no_send else',
     'sent=f"none/{sent_days}d" if False else', CLIENT),
    ("fleet-work:W8 unindexed writer scans old keys", W,
     "AND key >= %s AND key < 'msg/9'", "AND key <> %s AND key < 'msg/9'", COST),
    ("fleet-work:W9 non-head manifest changes indexed time", W,
     "ON sm.id = c.src_id AND sm.is_head ", "ON sm.id = c.src_id ", SERVER),
]
