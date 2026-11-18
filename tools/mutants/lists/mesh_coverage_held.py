"""A failed coverage count must not fail the read it rides on: each mutant must fail an assertion."""
G = "apps/mesh/graph.py"
T = ["tests/test_mesh_coverage_held.py"]

MUTANTS = [
    ("coverage:C1 the count keeps its parallel workers", G,
     'cur.execute("SET LOCAL max_parallel_workers_per_gather = 0")', 'pass', T),
    ("coverage:C2 the read keeps the count's setting", G,
     """cur.execute("SELECT set_config('max_parallel_workers_per_gather', %s, true)", (workers_were,))""", 'pass', T),
    ("coverage:C3 no savepoint, a failed count poisons the read", G,
     'with conn.transaction(), conn.cursor() as cur:', 'with conn.cursor() as cur:', T),
    ("coverage:C4 a failed count with nothing held returns nothing", G,
     '        if held is None:\n            raise\n', '        if held is None:\n            return (0, 0)\n', T),
    ("coverage:C5 a failed count is retried on every read", G,
     '            _coverage_held[key] = (_clock(), held[1])\n        return held[1]',
     '            pass\n        return held[1]', T),
    ("coverage:C6 any exception is taken for a database error", G,
     'except psycopg.Error as exc:', 'except Exception as exc:', T),
]
