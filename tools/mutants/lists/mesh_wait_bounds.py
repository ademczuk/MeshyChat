"""Mutants for apps/mesh/wait.py's wake paths, the controls for the three timing bounds that moved on 29 Oct.

Each of the three timing bounds in tests/test_mesh_wait_addressed.py and tests/test_mesh_wait_nudge.py was widened or
re-based after a loaded gate box failed it (main's gate 675924fb). These mutants remove the path each bound stands for,
so the widened bound is shown to still fail without it:
- W1 drops the park's gap re-read: a row committed between the first read and the registration then waits for the
  deadline (test_a_row_committed_between_the_first_read_and_the_registration_is_answered_at_once, < 5 s after the
  plant against a 20 s wait).
- W2 ignores a nudge: a woken waiter clears the event and waits on to its deadline
  (test_a_row_for_one_addressed_waiter_wakes_that_waiter_alone, < 6 s after the row against 20 s parks).
- W3 treats timeout=0 as the default wait (test_addressed_timeout_zero_reads_and_answers_at_once: the plain route's
  timeout=0 control then reads and answers 200, not 204).
- W4 and W5 (30 Oct) pin test_a_covered_waiter_does_not_poll_while_the_subscriber_listens after its waits grew from
  2 s to 5 s: W4 makes a covered waiter poll (its ceiling of 2 re-reads fails), W5 stops every poll (its control's
  floor of 4 fails).
"""
WAIT = "apps/mesh/wait.py"
T_NUDGE = ["tests/test_mesh_wait_nudge.py"]
T_ADDRESSED = ["tests/test_mesh_wait_addressed.py"]
MUTANTS = [
    ("mesh-wait:W1 the park skips its gap re-read", WAIT,
     "        wake.SUBSCRIBER.ensure_started()\n        found, ok = _reread(read)\n        if found is not None:\n            return found\n        while True:",
     "        wake.SUBSCRIBER.ensure_started()\n        found, ok = None, True\n        while True:",
     T_NUDGE),
    ("mesh-wait:W2 a nudge is cleared but never answered", WAIT,
     "            if nudge:\n                waiter.event.clear()\n",
     "            if nudge:\n                waiter.event.clear()\n                nudge = False\n",
     T_NUDGE),
    ("mesh-wait:W3 timeout=0 becomes the default wait", WAIT,
     "    wanted = min(DEFAULT_TIMEOUT_SECS if requested is None else requested, MAX_TIMEOUT_SECS)",
     "    wanted = min(DEFAULT_TIMEOUT_SECS if not requested else requested, MAX_TIMEOUT_SECS)",
     T_ADDRESSED),
    # test_a_covered_waiter_does_not_poll_while_the_subscriber_listens, its waits 2 s -> 5 s (30 Oct): W4 is its
    # ceiling (a covered waiter that polls re-reads about 50 times), W5 its control's floor (nobody polls).
    ("mesh-wait:W4 a covered waiter polls too", WAIT,
     "            tick = not alone and POLL_SECS < remaining",
     "            tick = POLL_SECS < remaining",
     T_NUDGE),
    ("mesh-wait:W5 no waiter polls", WAIT,
     "            tick = not alone and POLL_SECS < remaining",
     "            tick = False",
     T_NUDGE),
]
