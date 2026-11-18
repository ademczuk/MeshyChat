"""Targeted boundary and security mutants for `tools/meshy_node/transcript_graph.py` and `sendguard.py`.

Run check:
    python tools/mutants/run.py --list meshy_node_transcript_graph --check
"""

TG = "tools/meshy_node/transcript_graph.py"
SG = "tools/meshy_node/sendguard.py"
T_TG = ["tests/test_meshy_node_transcript_graph.py"]

MUTANTS = [
    (
        "tg:M1 named_transcript_namespace allows blank or whitespace namespace",
        TG,
        "    if not ns or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in ns):\n"
        '        raise node.Refused("--namespace may not be blank or hold whitespace/control characters")',
        "    if False:\n"
        '        raise node.Refused("--namespace may not be blank or hold whitespace/control characters")',
        T_TG,
    ),
    (
        "tg:M2 search_transcripts skips server namespace verification",
        TG,
        '    if not isinstance(body, dict) or body.get("namespace") != ns_clean or not isinstance(body.get("hits"), list):',
        '    if not isinstance(body, dict) or not isinstance(body.get("hits"), list):',
        T_TG,
    ),
    (
        "tg:M3 search_transcripts drops _untrusted_transcript_evidence marker",
        TG,
        '        "namespace": ns_clean,\n'
        '        "_untrusted_transcript_evidence": True,\n'
        '        "evidence_warning": UNTRUSTED_BANNER,\n'
        '        "reading_hint": READ_HINT if annotated_hits else None,',
        '        "namespace": ns_clean,\n'
        '        "_untrusted_transcript_evidence": False,\n'
        '        "evidence_warning": UNTRUSTED_BANNER,\n'
        '        "reading_hint": READ_HINT if annotated_hits else None,',
        T_TG,
    ),
    (
        "tg:M4 _build_read_cmd omits 32-ordinal span cap on chunk read_cmd",
        TG,
        "        capped_last = min(last_ord, first_ord + MAX_READ_ORDINALS - 1)",
        "        capped_last = last_ord",
        T_TG,
    ),
    (
        "tg:M5 _resolve_read_keys relaxes 32-ordinal cap to 33",
        TG,
        "    if span > MAX_READ_ORDINALS:",
        "    if span > MAX_READ_ORDINALS + 1:",
        T_TG,
    ),
    (
        "tg:HZ read_transcript_items skips server result_signature_verified check",
        TG,
        '        if raw.get("result_signature_verified") is not True:',
        "        if False:",
        T_TG,
    ),
    (
        "tg:M7 read_transcript_items skips memo namespace and head verification",
        TG,
        '            or memo.get("namespace") != ns_clean\n'
        '            or memo.get("key") != target_key\n'
        '            or memo.get("is_head") is not True',
        "            or False",
        T_TG,
    ),
    (
        "tg:M8 read_transcript_items reports unverified author signature as verified",
        TG,
        '            "author_signature_verified": bool(verified),',
        '            "author_signature_verified": True,',
        T_TG,
    ),
    (
        "tg:M9 read_transcript_items crashes on E_UNKNOWN_KEY instead of recording missing",
        TG,
        '            if raw.get("error_code") == "E_UNKNOWN_KEY":',
        "            if False:",
        T_TG,
    ),
    (
        "tg:M10 format_transcript_read omits UNTRUSTED_BANNER header",
        TG,
        'def format_transcript_read(result: dict) -> str:\n'
        '    """Render `read_transcript_items` output with `UNTRUSTED_BANNER` and per-turn signature verification."""\n'
        "    lines = [UNTRUSTED_BANNER]",
        'def format_transcript_read(result: dict) -> str:\n'
        '    """Render `read_transcript_items` output with `UNTRUSTED_BANNER` and per-turn signature verification."""\n'
        '    lines = ["transcript items:"]',
        T_TG,
    ),
    (
        "tg:M11 fleet_transcript_consensus Question 24h age bound uses <= instead of <",
        TG,
        "            if age_s is not None and age_s < cards.QUESTION_MAX_AGE_S:",
        "            if age_s is not None and age_s <= cards.QUESTION_MAX_AGE_S:",
        T_TG,
    ),
    (
        "tg:M12 fleet_transcript_consensus Context staleness threshold uses >= instead of >",
        TG,
        "            is_stale = age_s is None or age_s > stale_threshold_s",
        "            is_stale = age_s is None or age_s >= stale_threshold_s",
        T_TG,
    ),
    (
        "tg:M13 _fetch_consensus_cards skips card namespace verification",
        TG,
        "                limit=cards.THREAD_LIMIT,\n"
        "                after=after,\n"
        "            ),\n"
        "        )\n"
        '        if not isinstance(page, dict) or page.get("namespace") != card_ns or not isinstance(page.get("cards"), list):',
        "                limit=cards.THREAD_LIMIT,\n"
        "                after=after,\n"
        "            ),\n"
        "        )\n"
        '        if not isinstance(page, dict) or not isinstance(page.get("cards"), list):',
        T_TG,
    ),
    (
        "tg:M14 sendguard.refuse_untrusted_transcript_paste disabled",
        SG,
        "    if _UNTRUSTED_TRANSCRIPT_SENTINEL in body or _UNTRUSTED_TRANSCRIPT_JSON_RE.search(body):",
        "    if False:",
        T_TG,
    ),
    (
        "tg:M15 sendguard.admit omits non-overridable transcript paste check before bulk_ack",
        SG,
        "    refuse_untrusted_transcript_paste(body, refused=refused)\n"
        "    try:\n"
        "        reason = clean_reason(bulk_ack)",
        "    try:\n"
        "        reason = clean_reason(bulk_ack)",
        T_TG,
    ),
    (
        "tg:M16 search_transcripts passes None card_filter when params list is empty",
        TG,
        "        card_filter=params,",
        "        card_filter=params or None,",
        T_TG,
    ),
    (
        "tg:M17 fleet_transcript_consensus drops Candidate-only recent seat discovery",
        TG,
        "    discovered_seats: set[str] = set(raw_seats) | set(latest_by_seat)",
        "    discovered_seats: set[str] = set(raw_seats)",
        T_TG,
    ),
    (
        "tg:M18 fleet_transcript_consensus card ingest staleness threshold uses >= instead of >",
        TG,
        "            c_stale: bool | None = c_age_s is None or c_age_s > ingest_stale_threshold_s",
        "            c_stale: bool | None = c_age_s is None or c_age_s >= ingest_stale_threshold_s",
        T_TG,
    ),
    (
        "tg:M19 _fetch_recent_card_activity skips card namespace verification",
        TG,
        "                    recorded_since=rec_since,\n"
        "                    limit=page_limit,\n"
        "                    after=after,\n"
        "                ),\n"
        "            )\n"
        '            if not isinstance(page, dict) or page.get("namespace") != card_ns or not isinstance(page.get("cards"), list):',
        "                    recorded_since=rec_since,\n"
        "                    limit=page_limit,\n"
        "                    after=after,\n"
        "                ),\n"
        "            )\n"
        '            if not isinstance(page, dict) or not isinstance(page.get("cards"), list):',
        T_TG,
    ),
    (
        "tg:M20 _card_written_dt uses event time instead of server written_at",
        TG,
        '    if "written_at" in card:\n        return _parse_iso_utc(card.get("written_at"))',
        '    if False:\n        return _parse_iso_utc(card.get("written_at"))',
        T_TG,
    ),
    (
        "tg:M21 _probe_transcript_index leaks raw transcript text in condensed_hits",
        TG,
        '        "read_cmd": h.get("read_cmd"),\n    }',
        '        "read_cmd": h.get("read_cmd"),\n        "excerpt": h.get("text"),\n    }',
        T_TG,
    ),
    (
        "tg:M22 fleet_transcript_consensus formats recorded_since with isoformat instead of cards.now_time",
        TG,
        "    recorded_since_iso = cards.now_time(current - timedelta(seconds=ingest_stale_threshold_s))",
        "    recorded_since_iso = (current - timedelta(seconds=ingest_stale_threshold_s)).isoformat()",
        T_TG,
    ),
    (
        "tg:M23 _coverage_summary ignores subset_at_least lower-bound semantics on ctxindex coverage",
        TG,
        '                prefix = ">=" if cov.get("subset_at_least") is True else ""',
        '                prefix = ""',
        T_TG,
    ),
    (
        "tg:M24 fleet_transcript_consensus skips seat-specific probe when recorded_since sweep is truncated",
        TG,
        "            if c_stale and recent_truncated:",
        "            if False:",
        T_TG,
    ),
    (
        "tg:M25 format_consensus_report truncates read_cmd in topic hits to 96 chars",
        TG,
        '            cmd_full = cards.short(h.get("read_cmd") or "", 512)',
        '            cmd_full = cards.short(h.get("read_cmd") or "", 96)',
        T_TG,
    ),
    (
        "tg:M26 _build_read_cmd omits --namespace on DEFAULT_TRANSCRIPT_NAMESPACE",
        TG,
        '        ns_arg = f" --namespace {shlex.quote(ns)}"',
        '        ns_arg = "" if ns == DEFAULT_TRANSCRIPT_NAMESPACE else f" --namespace {shlex.quote(ns)}"',
        T_TG,
    ),
    (
        "tg:M27 format_transcript_read omits task payload step/event rendering",
        TG,
        '        if row.get("item") == "task":',
        '        if False:',
        T_TG,
    ),
    (
        "tg:M28 format_transcript_search omits per-hit untrusted sentinel above quote",
        TG,
        '        lines.append(f"    {_item_untrusted_line(_search_hit_ref(hit, seat=seat, src=src))}")',
        '        pass',
        T_TG,
    ),
    (
        "tg:M29 format_transcript_read omits per-item untrusted sentinel above turn text",
        TG,
        '        lines.append(f"  {_item_untrusted_line(k)}")',
        '        pass',
        T_TG,
    ),
    (
        "tg:M30 read_transcript_items omits _untrusted_transcript_evidence from per-item JSON dict",
        TG,
        '        entry: dict[str, Any] = {\n            "_untrusted_transcript_evidence": True,\n            "key": target_key,',
        '        entry: dict[str, Any] = {\n            "key": target_key,',
        T_TG,
    ),
    (
        "tg:M31 _probe_transcript_index sends unbounded since=None on fleet session probe",
        TG,
        '            ns=transcript_ns, seat=seat_filter, since=eff_since, collapse="session",',
        '            ns=transcript_ns, seat=seat_filter, since=None, collapse="session",',
        T_TG,
    ),
    (
        "tg:M32 _probe_seat_latest_transcript sends unbounded since=None on initial seat probe",
        TG,
        "    res = _chunk_search(eff_since)",
        "    res = _chunk_search(None)",
        T_TG,
    ),
    (
        "tg:M33 _index_probe_since_iso drops MAX_FLEET_INDEX_PROBE_HOURS cap",
        TG,
        "    window_s = min(\n"
        "        max(DEFAULT_INDEX_PROBE_HOURS * 3600.0, transcript_stale_threshold_s),\n"
        "        MAX_FLEET_INDEX_PROBE_HOURS * 3600.0,\n"
        "    )",
        "    window_s = max(DEFAULT_INDEX_PROBE_HOURS * 3600.0, transcript_stale_threshold_s)",
        T_TG,
    ),
    (
        "tg:M34 _seat_probe_window_s drops MAX_SEAT_PROBE_HOURS cap",
        TG,
        "def _seat_probe_window_s(ingest_stale_threshold_s: float, transcript_stale_threshold_s: float) -> float:\n"
        "    return min(\n"
        "        max(DEFAULT_SEAT_PROBE_HOURS * 3600.0, ingest_stale_threshold_s, transcript_stale_threshold_s * 2.0),\n"
        "        MAX_SEAT_PROBE_HOURS * 3600.0,\n"
        "    )",
        "def _seat_probe_window_s(ingest_stale_threshold_s: float, transcript_stale_threshold_s: float) -> float:\n"
        "    return max(DEFAULT_SEAT_PROBE_HOURS * 3600.0, ingest_stale_threshold_s, transcript_stale_threshold_s * 2.0)",
        T_TG,
    ),
    (
        "tg:M35 _degradable_http_status fails to degrade on HTTP 500+",
        TG,
        "def _degradable_http_status(status: int) -> bool:\n"
        "    return status in _DEGRADABLE_CLIENT_STATUSES or status >= 500",
        "def _degradable_http_status(status: int) -> bool:\n"
        "    return status in _DEGRADABLE_CLIENT_STATUSES",
        T_TG,
    ),
    (
        "tg:M36 _degradable_http_status swallows HTTP 401 auth failure instead of failing closed",
        TG,
        "def _degradable_http_status(status: int) -> bool:\n"
        "    return status in _DEGRADABLE_CLIENT_STATUSES or status >= 500",
        "def _degradable_http_status(status: int) -> bool:\n"
        "    return status == 401 or status in _DEGRADABLE_CLIENT_STATUSES or status >= 500",
        T_TG,
    ),
    (
        "tg:M37 fleet_transcript_consensus emits false stale_card_ingestion when status=any walk failed",
        TG,
        "            if c_stale and recent_failed:\n                c_stale = None",
        "            if False:\n                c_stale = None",
        T_TG,
    ),
    (
        "tg:M38 fleet_transcript_consensus fails to trip circuit-breaker after per-seat search failure",
        TG,
        "            if tx_probe_err is not None:\n                can_probe_tx = False",
        "            if tx_probe_err is not None:\n                pass",
        T_TG,
    ),
    (
        "tg:M39 fleet_transcript_consensus probes dormant historical seats older than seat_probe_window_s",
        TG,
        "        elif can_probe_tx and not seat_dormant and tx_probes_used < MAX_SEAT_FRESHNESS_PROBES:",
        "        elif can_probe_tx and tx_probes_used < MAX_SEAT_FRESHNESS_PROBES:",
        T_TG,
    ),
    (
        "tg:M40 fleet_transcript_consensus omits cards_available outage from incomplete flag",
        TG,
        '    incomplete = (not cards_available) or (not bool(transcript_index.get("available")))',
        '    incomplete = not bool(transcript_index.get("available"))',
        T_TG,
    ),
    (
        "tg:M41 fleet_transcript_consensus drops transcript_lag when continuous seat ages past bounded probe window",
        TG,
        "        elif is_continuous:\n"
        "            window_proves_stale = tx_freshness_complete and seat_probe_window_s >= transcript_stale_threshold_s",
        "        elif False:\n"
        "            window_proves_stale = tx_freshness_complete and seat_probe_window_s >= transcript_stale_threshold_s",
        T_TG,
    ),
]
