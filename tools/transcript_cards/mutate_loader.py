"""Mutation check for tests/test_transcript_cards.py: each mutant of the loader must make the suite FAIL.
Run: python tools/transcript_cards/mutate_loader.py   (exit 0 = every mutant was caught).
Works on copies in a temp folder; the committed files are never changed."""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
# The checks live in the repo's tests folder; a copy beside this file is used when the folder is carried alone.
TEST_FILE = next((p for p in (os.path.join(HERE, "test_transcript_cards.py"),
                              os.path.join(HERE, os.pardir, os.pardir, "tests", "test_transcript_cards.py"))
                  if os.path.exists(p)), "")
KIMI_TEST_FILE = next((p for p in (os.path.join(HERE, "test_transcript_cards_kimi.py"),
                                   os.path.join(HERE, os.pardir, os.pardir, "tests",
                                                "test_transcript_cards_kimi.py"))
                       if os.path.exists(p)), "")
ANTIGRAVITY_TEST_FILE = next((p for p in (os.path.join(HERE, "test_transcript_cards_antigravity.py"),
                                         os.path.join(HERE, os.pardir, os.pardir, "tests",
                                                      "test_transcript_cards_antigravity.py"))
                             if os.path.exists(p)), "")
# The loader's files: a mutant names its target as an optional fourth element (default transcript_cards.py).
LOADER_FILES = ("transcript_cards.py", "kimi_dialect.py", "antigravity_dialect.py", "context_snapshot.py")
MUTANTS = [
    ("only AKIA counts as an AWS key id", "|(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}|", "|AKIA[0-9A-Z]{16}|"),
    ("a JWT with the empty payload e30 passes", r"\.(?:[A-Za-z0-9_-]{8,}|e30)\.", r"\.[A-Za-z0-9_-]{8,}\."),
    ("no re-screen of stored cards",
     "        fired = detect(_card_strings(cards[cid]), disabled_detectors, seat)\n        if fired:",
     "        fired = detect(_card_strings(cards[cid]), disabled_detectors, seat)\n        if False:"),
    ("a pasted answer is kept as a Ruling", "    if free_text and _PASTED_OUTPUT.search(answer):",
     "    if False:"),
    ("a pasted prompt is kept as a Candidate",
     '    if _PASTED_OUTPUT.search(stripped):\n        return [], "pasted_output"\n', ""),
    ("the arguments of /goal are dropped", '_SLASH_WITH_OPERATOR_TEXT = ("/goal",)', "_SLASH_WITH_OPERATOR_TEXT = ()"),
    ("every slash command's arguments are kept",
     "        if not (name and name.group(1) in _SLASH_WITH_OPERATOR_TEXT and args):",
     "        if not (name and args):"),
    ("a bare slash-command name is kept",
     '        if _BARE_SLASH.fullmatch(stripped):\n            return [], "slash_command_name"\n', ""),
    ("an answer pre-filled by the model is trusted", "        if any(i in prefilled for i in call_ids if i):",
     "        if False:"),
    ("pre-filled call ids are not carried to the next run", '    cursor["prefilled_asks"] = sorted(prefilled)',
     '    cursor["prefilled_asks"] = []'),
    ("seat detectors are ignored", "for name, rx in DETECTORS + SEAT_DETECTORS.get(seat, []) if",
     "for name, rx in DETECTORS if"),
    ("reading nothing exits 0", "a.project_dir, a.seat))" + chr(10) + "        return 3" + chr(10),
     "a.project_dir, a.seat))" + chr(10) + "        return 0" + chr(10)),
    ("free text is not flagged", '"extra": {"free_text": free_text, "long_free_text": free_text and len(answer) > 300}}, None',
     '"extra": {"free_text": False, "long_free_text": False}}, None'),
    # Both run() and approved_cards (the queue's path) take the id from _raw_identity, so one mutant reaches both;
    # T5 holds run() and T27 holds approved_cards, each red on its own.
    ("session id made part of the identity",
     '    return card_identity(raw["type"], raw["uuid"], raw["block_index"], raw.get("call_id"))',
     '    return card_identity(raw["type"], raw["uuid"] + raw["session"], raw["block_index"], raw.get("call_id"))'),
    ("content made part of the identity",
     '    return card_identity(raw["type"], raw["uuid"], raw["block_index"], raw.get("call_id"))',
     '    _c = sha256_text(json.dumps(raw["kept"], sort_keys=True, ensure_ascii=False))' + chr(10)
     + '    return card_identity(raw["type"], raw["uuid"] + _c, raw["block_index"], '
     '(raw.get("call_id") or "") + _c if raw.get("call_id") else None)'),
    ("a Ruling is named by its transcript record, not its question call",
     '    if card_type == "Ruling" and call_id:', "    if False:"),
    ("the question's place in the call is left out of a Ruling's id",
     'return sha256_text("v1|ask|%s|%d" % (call_id, block_index))[:32], call_id',
     'return sha256_text("v1|ask|%s" % call_id)[:32], call_id'),
    ("the transcript record's uuid is not kept on a Ruling",
     '    if ref != raw["uuid"]:\n        card["source"]["transcript_uuid"] = raw["uuid"]\n', ""),
    ("a store made under an earlier id rule is merged into",
     '                                              or cursor.get("id_rule") != ID_RULE)',
     "                                              or False)"),
    ("the later content wins a conflict",
     'if other and (existing["time"], existing["source"]["source_sha256"]) <= (raw["date"], source_sha):',
     'if other and False:'),
    ("an older-format store is merged into",
     'any(c.get("schema") != "card.v1" for c in cards.values())\n', "False\n"),
    ("repeated Candidates are not labelled", "    mark_repeats(cards.values())\n", ""),
    ("Rulings are grouped as repeats too", '        if c["type"] == "Candidate":\n            groups.setdefault(',
     '        if True:\n            groups.setdefault('),
    ("stale repeat labels are kept", "        for k in _REPEAT_FIELDS:\n            c.pop(k, None)\n", ""),
    ("the latest copy is the one that keeps the count", '        group.sort(key=lambda c: (c["time"], c["id"]))',
     '        group.sort(key=lambda c: (c["time"], c["id"]), reverse=True)'),
    ("a record with no time becomes a card", 'if raws and not raws[0]["date"]:', "if False:"),
    ("an offset time is kept as local time", '            t = t - offset if zone[0] == "+" else t + offset', "            pass"),
    ("a time is kept in the spelling it came in", '    date = utc_instant(o.get("timestamp"))',
     '    date = o.get("timestamp") if utc_instant(o.get("timestamp")) else ""'),
    ("a message from another seat is kept as an operator prompt",
     '    ("Another Claude session sent a message", "peer_message"),\n', ""),
    ("machine-typed text after the operator's words stays in the card",
     "        stripped = stripped[:machine.start()].strip()", "        stripped = stripped.strip()"),
    ("a prompt that is only machine-typed text is kept",
     "        if not stripped or whole in _NEVER_CUT:" + chr(10) + "            return [], whole" + chr(10),
     "        if whole in _NEVER_CUT:" + chr(10) + "            return [], whole" + chr(10)),
    ("the launcher's text in the middle of a prompt is not looked for",
     '_MACHINE_TYPED = re.compile(_WAKE_INJECTION.pattern + r"|MANDATORY session-start action[:,]")',
     "_MACHINE_TYPED = re.compile(_WAKE_INJECTION.pattern)"),
    ("naming the launcher prompt counts as the launcher typing",
     'r"|MANDATORY session-start action[:,]")', 'r"|MANDATORY session-start action")'),
    ("a cut prompt is not counted", '        cut_class = "machine_text_cut"', "        cut_class = None"),
    ("an oversize card is not cut", "    fit_to_limit(card)" + chr(10) + "    return card" + chr(10),
     "    return card" + chr(10)),
    ("a cut card is not marked truncated", "    if cut:" + chr(10) + '        card["truncated"] = True' + chr(10), ""),
    ("a prompt that opens with a seat header is kept as the operator's",
     "        if header and header.group(1) in seats:" + chr(10) + '            return [], "peer_message"' + chr(10), ""),
    ("any lower-case word after From is taken for a seat",
     "        if header and header.group(1) in seats:", "        if header:"),
    ("a seat header with a bracket or a clause is not seen",
     'r"(?: ' + chr(92) + '([^)' + chr(92) + 'n]{0,80}' + chr(92) + ')|,[^' + chr(92) + 'n:]{0,80})?:' + chr(92) + 's")',
     'r":' + chr(92) + 's")'),
    ("seat labels named on the command line are ignored",
     "    seats = KNOWN_SEATS | frozenset(s.strip().lower() for s in extra_seats if isinstance(s, str) and s.strip())",
     "    seats = KNOWN_SEATS"),
    ("a seat's report pasted by hand is kept as the operator's",
     '                return [], "peer_report_pasted"', "                pass"),
    ("any opening word and a colon is taken for a seat's report",
     "            if label in seats and label not in (own_names(seat) if own is None else own):",
     "            if label not in (own_names(seat) if own is None else own):"),
    ("a prompt addressed to this seat by its own name is dropped as a report",
     "            if label in seats and label not in (own_names(seat) if own is None else own):",
     "            if label in seats:"),
    ("a capitalised seat label is not seen",
     "            label = opener.group(1).lower()", "            label = opener.group(1)"),
    ("a pasted report needs a space after the colon",
     '{1,30})(?: ?' + chr(92) + '([^)' + chr(92) + 'n]{0,80}' + chr(92) + '))? ?:")',
     '{1,30})(?: ?' + chr(92) + '([^)' + chr(92) + 'n]{0,80}' + chr(92) + '))? ?:' + chr(92) + 's")'),
    ("a bracket after the seat label is not seen",
     '{1,30})(?: ?' + chr(92) + '([^)' + chr(92) + 'n]{0,80}' + chr(92) + '))? ?:")',
     '{1,30}) ?:")'),
    ("a seat's launcher name is not its own name",
     "    names = {seat} | set(SEAT_LAUNCHER_NAMES.get(seat, ()))", "    names = {seat}"),
    ("any hyphen part of a seat's label is taken for its own name",
     "    names = {seat} | set(SEAT_LAUNCHER_NAMES.get(seat, ()))", '    names = {seat} | set(seat.split("-"))'),
    ("a launcher name given as a marker is not the seat's own name",
     "    names |= {m.group(1) for m in (_MARKER_NAME.search(t) for t in markers if isinstance(t, str)) if m}" + chr(10), ""),
    ("names given as the seat's own are ignored",
     "    names |= {e.strip().lower() for e in extra if isinstance(e, str) and e.strip()}" + chr(10), ""),
    ("the seat's own names never reach the record reader",
     "    own = own_names(seat, extra_own, extra_markers)", "    own = own_names(seat)"),
    ("the seat's own names from the command line are ignored",
     "                 extra_markers=a.marker or (), extra_seats=a.seats or (), extra_own=a.own_name or (),",
     "                 extra_markers=a.marker or (), extra_seats=a.seats or (),"),
    ("jxxx is not on the roster", '"kxxx jxxx nxxxxx', '"kxxx nxxxxx'),
    ("a Question has the id of its Ruling",
     '        return sha256_text("v1|question|%s|%d" % (call_id, block_index))[:32], call_id',
     '        return sha256_text("v1|ask|%s|%d" % (call_id, block_index))[:32], call_id'),
    ("the builder skips the detectors for an answered question",
     "    fired = detect(_kept_strings(raw[" + chr(34) + "kept" + chr(34) + "]), disabled_detectors, seat)" + chr(10)
     + "    if fired:" + chr(10) + "        return None, " + chr(34) + "redaction:" + chr(34) + " + " + chr(34) + "," + chr(34)
     + ".join(sorted(fired))" + chr(10)
     + "    source_sha = sha256_text(json.dumps(raw[" + chr(34) + "kept" + chr(34) + "], sort_keys=True, ensure_ascii=False))" + chr(10)
     + "    cid, ref = card_identity(" + chr(34) + "Ruling" + chr(34),
     "    source_sha = sha256_text(json.dumps(raw[" + chr(34) + "kept" + chr(34) + "], sort_keys=True, ensure_ascii=False))" + chr(10)
     + "    cid, ref = card_identity(" + chr(34) + "Ruling" + chr(34)),
    ("the builder skips the detectors for an asked question",
     "    fired = detect(_kept_strings(raw[" + chr(34) + "kept" + chr(34) + "]), disabled_detectors, seat)" + chr(10)
     + "    if fired:" + chr(10) + "        return None, " + chr(34) + "redaction:" + chr(34) + " + " + chr(34) + "," + chr(34)
     + ".join(sorted(fired))" + chr(10)
     + "    source_sha = sha256_text(json.dumps(raw[" + chr(34) + "kept" + chr(34) + "], sort_keys=True, ensure_ascii=False))" + chr(10)
     + "    cid, ref = card_identity(" + chr(34) + "Question" + chr(34),
     "    source_sha = sha256_text(json.dumps(raw[" + chr(34) + "kept" + chr(34) + "], sort_keys=True, ensure_ascii=False))" + chr(10)
     + "    cid, ref = card_identity(" + chr(34) + "Question" + chr(34)),
    ("the builder takes an answer the model wrote",
     "    if prefilled:" + chr(10) + '        return None, "ruling_prefilled_by_model"' + chr(10), ""),
    ("the builder takes any spelling of a time",
     "    if not (isinstance(time, str) and _CARD_TIME.fullmatch(time) and utc_instant(time) == time):", "    if False:"),
    ("the builder takes a card with no call id",
     "    if not (isinstance(call_id, str) and call_id.strip()):", "    if False:"),
    ("a built Ruling loses the transcript record it was read from",
     "    record = transcript_uuid if isinstance(transcript_uuid, str) and transcript_uuid.strip() else call_id",
     "    record = call_id"),
    ("a built Ruling ignores the kind its caller names",
     "                           question if isinstance(question, str) else " + chr(34) + chr(34)
     + ", options, answer, note, bool(multi_select)," + chr(10) + "                           kind)",
     "                           question if isinstance(question, str) else " + chr(34) + chr(34)
     + ", options, answer, note, bool(multi_select))"),
    ("a Question is owned by the operator",
     '    return {"type": "Question", "owner": seat, "block_index": index, "kept": kept, "text": text,',
     '    return {"type": "Question", "owner": "operator", "block_index": index, "kept": kept, "text": text,'),
    ("a Question with no words is a card",
     "    if not question:" + chr(10) + '        return None, "question_empty"' + chr(10), ""),
    ("a Question loses its thread",
     '    extra = {"thread_id": thread_id.strip()} if isinstance(thread_id, str) and thread_id.strip() else {}',
     "    extra = {}"),
    ("screen_prose reads every string of a card",
     "    return detect(_card_strings(card), disabled_detectors, seat if seat is not None else card.get(" + chr(34) + "seat" + chr(34) + "))",
     "    return detect(json.dumps(card), disabled_detectors, seat if seat is not None else card.get(" + chr(34) + "seat" + chr(34) + "))"),
    ("screen_prose skips done_when",
     '    out = [card.get(k) or "" for k in ("text", "question", "answer", "notes", "done_when")]',
     '    out = [card.get(k) or "" for k in ("text", "question", "answer", "notes")]'),
    ("screen_prose forgets the card's own seat",
     "seat if seat is not None else card.get(" + chr(34) + "seat" + chr(34) + "))", "seat)"),
    ("what the operator types while a turn is running is dropped with the other attachments",
     '        if att.get("type") != "queued_command":', "        if True:"),
    ("a queued channel or peer message is taken for the operator's",
     '        if not old_shape and (att_origin is None or att_origin.get("kind") != "human"):',
     "        if not old_shape and att_origin is None:"),
    ("any queued record with no origin is taken for the old shape",
     '        old_shape = att_origin is None and mode == "prompt"', "        old_shape = att_origin is None"),
    ("an old-shape mid-turn message is dropped",
     '        old_shape = att_origin is None and mode == "prompt"', "        old_shape = False"),
    ("an old-shape mid-turn message is not marked as old",
     '             "origin": att_origin, "promptSource": "queued_command_legacy" if old_shape else "queued_command",',
     '             "origin": att_origin or {"kind": "human"}, "promptSource": "queued_command",'),
    ("a queued shell command is taken for a prompt",
     '        if mode not in (None, "prompt"):', "        if False:"),
    ("a wake line in an answer is kept as a Ruling", "    if free_text and _WAKE_INJECTION.search(answer):",
     "    if False:"),
    ("harness text is kept as an operator prompt", '    ("[SYSTEM NOTIFICATION", "harness_text"),\n', ""),
    ("a pasted block stays in the card", '        stripped = _PASTED_BLOCK.sub(" ", stripped).strip()',
     "        stripped = stripped.strip()"),
    ("a pasted block with no closing tag stays", r'.*?(?:</pasted_content\b[^>]*>|\Z)", re.S)', r'.*?</pasted_content\b[^>]*>", re.S)'),
    ("a prompt that is only a pasted block is kept", '        if not stripped:\n            return [], "pasted_content_only"\n',
     '        if not stripped:\n            stripped = "x"\n'),
    ("an image-only prompt is kept", '    if _IMAGE_ONLY.fullmatch(stripped):\n        return [], "attachment_only"\n', ""),
    ("an empty answer is a Ruling", "    if nothing_chosen and not note.strip():", "    if False:"),
    ("partial last line consumed",
     'if not line or not line.endswith(b"\\n"):',
     "if not line:"),
    ("tool results no longer dropped",
     'return [], "tool_result"',
     'return [{"type": "Candidate", "block_index": 0, "kept": {"kind": "typed_prompt", "text": json.dumps(content)}, '
     '"text": json.dumps(content), "truncated": False, "date": date, "session": session, "uuid": uuid, '
     '"extra": {"prompt_source": "x", "legacy_origin": False, "short": False, "has_attachment": False, "parent_uuid": None}}], None'),
    ("a typed label overrides a non-human origin", 'if origin is not None and kind != "human":',
     'if origin is not None and kind != "human" and source not in _OPERATOR_SOURCES:'),
    ("unlabelled records with an origin are dropped even when human",
     "    if origin is not None:" + chr(10) + "        legacy = False" + chr(10) + '    elif source == "queued_command_legacy":',
     "    if origin is not None and source is None:" + chr(10) + '        return [], "x"' + chr(10)
     + "    elif origin is not None:" + chr(10) + "        legacy = False" + chr(10) + '    elif source == "queued_command_legacy":'),
    ("the two-class code rule removed", "(len(letters) >= 16 and 0.25 <= upper_ratio <= 0.75) or flips >= 6",
     "(len(letters) >= 16 and 0.25 <= upper_ratio <= 0.75 and flips >= 1) or (flips >= 6 and upper_ratio > 0)"),
    ("rebuild keeps the old store", "    if rebuild or store_rebuilt:", "    if store_rebuilt:"),
    ("a transcript that only quotes the marker is picked",
     "if any(m in line and _marker_in_user_prompt(line, m) for m in markers):",
     "if any(m in line for m in markers):"),
    ("files not read is not reported", '"files_not_read": max(0, top_level - len(paths)),', '"files_not_read": 0,'),
    ("launcher prompt kept", '    ("MANDATORY session-start", "launcher_prompt"),\n', ""),
    ("redaction skipped for option descriptions",
     "                    out.extend(x for x in item.values() if isinstance(x, str))",
     "                    pass"),
    ("drop masks instead of dropping", "                cards.pop(cid, None)\n                continue",
     "                cards.pop(cid, None)"),
    ("cursor never reset on a replaced file", 'report["cursor_resets"] += 1', "pass"),
    ("state-named files accepted", 'r"(?i)(state|credential|secret|token|keys?\\.|\\.env|\\.pem|id_rsa|id_ed25519)"', 'r"(?i)(zzzzzz)"'),
    ("compaction summaries kept", '    if o.get("isCompactSummary"):\n        return [], "compaction_summary"\n', ""),
    ("sidechain records kept", '    if o.get("isSidechain"):\n        return [], "sidechain"\n', ""),
    # The Kimi dialect (kimi_dialect.py reads, transcript_cards.py builds)
    ("Kimi cron and trigger text is carded",
     '            if kind in ("system_trigger", "cron_job"):\n                return None, "kimi_" + kind',
     '            if False:\n                pass', "kimi_dialect.py"),
    ("a Kimi injection is carded",
     '            if kind == "injection":\n                return None, "kimi_injection_" + str(origin.get("variant"))',
     '            if False:\n                pass', "kimi_dialect.py"),
    ("a goal.create is not carded",
     '        return ("goal", {"uuid": goal_id, "time": when, "text": objective}), None',
     '        return None, "kimi_goal_dropped"', "kimi_dialect.py"),
    ("a no-origin Kimi prompt is not flagged legacy",
     '"legacy": kind is None', '"legacy": False', "kimi_dialect.py"),
    ("a disabled Kimi ask still rules",
     '            if result.get("isError") or (isinstance(output, str)\n'
     '                                         and output.startswith(ASK_TOOL + " is disabled")):',
     '            if False:', "kimi_dialect.py"),
    ("an id-less Kimi prompt is dropped",
     '            ref = "kimi:%s:%s" % (session, o.get("time"))', '            ref = None', "kimi_dialect.py"),
    ("a subagent wire is read as the operator's",
     '"subagent": os.path.basename(os.path.dirname(identity)) != "main"',
     '"subagent": False', "kimi_dialect.py"),
    ("Kimi epoch-ms is not converted to the server spelling",
     '    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (int(ms) % 1000)',
     '    return ""', "kimi_dialect.py"),
    ("a lone surrogate reaches a card unharmed",
     '    if not isinstance(s, str) or not any(0xD800 <= ord(c) <= 0xDFFF for c in s):',
     '    if True:'),
    ("a Kimi machine envelope is not cut",
     "machine_typed=_KIMI_MACHINE_TYPED", "machine_typed=_MACHINE_TYPED"),
    ("an unanswered Kimi question still rules",
     '            answer = payload["answers"].get(question)\n            if answer is None:',
     '            answer = payload["answers"].get(question)\n            if False:'),
    ("open Kimi asks are not persisted",
     '            cursor["kimi_open_asks"][path] = kimi["asks"]',
     '            cursor["kimi_open_asks"][path] = {}'),
    ("the Kimi path keeps the postbox's never-cut shapes",
     '_KIMI_MACHINE_TYPED = re.compile(_CODEX_MACHINE_TYPED.pattern +',
     '_KIMI_MACHINE_TYPED = re.compile('),
    ("a Kimi Ruling takes the call's time, not the answer's",
     '            return ("answers", {"call_id": call_id, "uuid": ev.get("uuid") or ask["uuid"], "time": when,',
     '            return ("answers", {"call_id": call_id, "uuid": ev.get("uuid") or ask["uuid"], "time": _time(ask["time"]),',
     "kimi_dialect.py"),
    ("a Kimi multi-select list answer is kept as JSON",
     '            if isinstance(answer, list):\n'
     '                # A multi-select answer arrives as a list of chosen labels; the card holds them joined,\n'
     '                # like the Claude walk\'s answers, so _label_only reads the same shape on every dialect.\n'
     '                answer = ", ".join(str(x) for x in answer)\n',
     ''),
    ("the dialect import fallback masks a real failure inside it",
     "    if exc.name is not None:", "    if False:"),
    ("a replaced Kimi wire file inherits the old open asks",
     "                if kimi is not None:\n"
     "                    # A replaced or truncated wire file is a new source generation: the old file's open asks\n"
     "                    # must not pair with this one's answers (codex-kxxx-cli's probe, txxxx's confirm).\n"
     "                    kimi[\"asks\"].clear()\n",
     ""),
    ("a frozen copy's metadata is read from the scratch path, not the original",
     "    identity = original_path if original_path else path", "    identity = path", "kimi_dialect.py"),
    ("option descriptions are never cut",
     "    if _fit_size(card) > FIT_BYTES and isinstance(options, list):",
     "    if False and isinstance(options, list):"),
    ("the floor of an option description is too high for four options to fit",
     "OPTION_FLOOR = 200 ", "OPTION_FLOOR = 2400 "),
    ("the cut writes into the option objects it was handed",
     '        options = card["options"] = [dict(o) if isinstance(o, dict) else o for o in options]' + chr(10), ""),
    ("a cut is measured with the file names and sessions",
     "    return _card_bytes(dict(card, source=dict(source, files=[], sessions=[])))", "    return _card_bytes(card)"),
    ("a cut option description does not say how long it was",
     ' + "' + chr(92) + 'n[cut: the full description was %d characters]" % len(d)', ""),
    ("a Ruling no cut can fit is returned by the builder",
     '    cid, ref = card_identity("Ruling", call_id, index, call_id)' + chr(10)
     + "    return _fits(_card_from_raw(raw, seat, cid, ref, source_sha, ()))",
     '    cid, ref = card_identity("Ruling", call_id, index, call_id)' + chr(10)
     + "    return _card_from_raw(raw, seat, cid, ref, source_sha, ()), None"),
    ("a Question no cut can fit is returned by the builder",
     '    cid, ref = card_identity("Question", call_id, index, call_id)' + chr(10)
     + "    return _fits(_card_from_raw(raw, seat, cid, ref, source_sha, ()))",
     '    cid, ref = card_identity("Question", call_id, index, call_id)' + chr(10)
     + "    return _card_from_raw(raw, seat, cid, ref, source_sha, ()), None"),
    ("a Question card carries no question and no options",
     '    elif raw["type"] == "Question":' + chr(10)
     + '        card.update({k: raw["kept"][k] for k in ("question", "options", "multi_select")})' + chr(10), ""),
    ("a card just under the server limit is refused as too large",
     "    if _card_bytes(card) > SERVER_VALUE_LIMIT:", "    if _card_bytes(card) > FIT_BYTES - 400:"),
    ("a negative index is taken by the builders",
     "    if type(index) is not int or index < 0:", "    if type(index) is not int:"),
    ("True is taken for an index by the builders",
     "    if type(index) is not int or index < 0:", "    if not isinstance(index, int) or index < 0:"),
    ("the transcript record handed to the builder is not stored",
     "    record = transcript_uuid if isinstance(transcript_uuid, str) and transcript_uuid.strip() else call_id",
     "    record = call_id"),
    ("the floor of an option description is zero", "OPTION_FLOOR = 200 ", "OPTION_FLOOR = 0 "),
    ("screen_prose raises on options that are not a list",
     "    for o in options if isinstance(options, (list, tuple)) else [options]:", "    for o in options or []:"),
    ("screen_prose reads only the label and the description of an option",
     "            out.extend(o.values())", '            out.extend([o.get("label") or "", o.get("description") or ""])'),
    # ---- the Codex dialect
    ("a Codex item of any kind is taken for typed",
     '                    and all(isinstance(k, str) and k.startswith("user.") for k in kinds))', "                    )"),
    ("a Codex item with a kind and no id is taken at once",
     '"done": own_kind and item_id is not None}', '"done": own_kind}'),
    ("the event after a Codex item does not mark it typed",
     '        return typed(ref, message, bool(payload.get("images") or payload.get("local_images")))',
     '        return [], "codex_user_event"'),
    ("a typed Codex item and its event are not told apart from an event alone",
     '        if same and before["done"]:' + chr(10)
     + '            return [], "codex_user_event"                  # its item was the card' + chr(10), ""),
    ("a Codex prompt with no item id is named without its session",
     '            ref = "codex:%s:%s" % (session, o.get("timestamp") or "")',
     '            ref = "codex::%s" % (o.get("timestamp") or "")'),
    ("a picture's wrapper is kept as the operator's words",
     "        words = [t for t in texts if not _CODEX_IMAGE_WRAP.fullmatch(t.strip())]", "        words = texts"),
    ("a picture on a Codex prompt is not noted",
     '        image = any(c.get("type") != "input_text" for c in parts)', "        image = False"),
    ("the Codex list of machine-typed text is not used",
     "        return cards_from_record(record, seat, (), seats, own, machine_typed=_CODEX_MACHINE_TYPED)",
     "        return cards_from_record(record, seat, (), seats, own)"),
    ("a Codex question call is not read",
     '    if kind == "response_item" and ptype == "function_call" and payload.get("name") in CODEX_ASK_TOOLS:',
     '    if kind == "response_item" and ptype == "function_call" and False:'),
    ("a Codex question that has only a title is not read",
     'call_id, qi, q.get("question") or q.get("title"), options,', 'call_id, qi, q.get("question"), options,'),
    ("the plain-string options of a Codex question are lost",
     '            options = [{"label": x} if isinstance(x, str) else x for x in (q.get("options") or [])]',
     '            options = q.get("options") or []'),
    ("a Codex exec run is read", 'isinstance(source, str) and source != "exec"', "isinstance(source, str)"),
    ("a Codex subagent thread is read", 'isinstance(source, str) and source != "exec"', 'source != "exec"'),
    ("a copy of a Claude Code session is read",
     '            and payload.get("originator") != "Claude Code")', "            )"),
    ("a file that is not a rollout file is read", 'name.startswith("rollout-") and ', ""),
    ("a rollout file is read as a Claude transcript",
     '    if first.get("type") != "session_meta":' + chr(10) + "        return None" + chr(10),
     "    if True:" + chr(10) + "        return None" + chr(10)),
    ("a run that ends on a Codex item does not wait for its event",
     '    cp["offset"] = held if held is not None else offset', '    cp["offset"] = offset'),
    ("a Codex folder with no interactive session exits 0",
     '% (report["files_top_level"], a.codex_sessions))' + chr(10) + "        return 3" + chr(10),
     '% (report["files_top_level"], a.codex_sessions))' + chr(10) + "        return 0" + chr(10)),
    ("both folders are taken at once",
     "    if [bool(a.project_dir), bool(a.codex_sessions), bool(a.kimi_sessions), bool(a.antigravity_brain)].count(True) != 1:",
     "    if False:"),
    ("the kxxx Codex seat is not on the roster", "codex-bxxxxx-cli codex-kxxx codex-kxxx-cli ", "codex-bxxxxx-cli "),
    ("a whole machine-typed prompt is not classed as a delivery",
     '    if "MeshyChat" in found or found.startswith(("Process this banked delivery", "Local postbox handling policy",'
     + chr(10) + '                                                 "Each further message below")):',
     "    if False:"),
    ("a tagged channel line is not classed", '    if found.startswith(("[STEERING]", "[MESSAGE]", "[RECALL]")):', "    if False:"),
    ("pattern: the first delivery template", 'r"|MeshyChat message for you ' + chr(92) + '(|MeshyChat postbox', 'r"|MeshyChat postbox'),
    ("pattern: the listener notice", '|MeshyChat postbox for "', '"'),
    ("pattern: the tag in front of a delivery",
     '    + r"|' + chr(92) + '[(?:STEERING|MESSAGE|RECALL)' + chr(92) + '] from ' + chr(92) + 'S"' + chr(10), ""),
    ("pattern: a tag only with a roster-shaped label",
     '] from ' + chr(92) + 'S"', '] from [a-z][a-z0-9_.-]{1,30} ' + chr(92) + '(peer "'),
    ("pattern: the batch instructions", 'r"|Process this banked delivery in one turn' + chr(92) + '.|Local', 'r"|Local'),
    ("pattern: the handling policy",
     '|Local postbox handling policy ' + chr(92) + '(not peer message content' + chr(92) + ')"', '"'),
    ("pattern: the launcher's orientation", 'r"|Session-start orientation, read |nimbalyst', 'r"|nimbalyst'),
    ("pattern: the nimbalyst instructions", '|nimbalyst://system-instructions"', '"'),
    ("pattern: the local wake fixture", 'r"|' + chr(92) + '[LOCAL-WAKE-FIXTURE |LOCAL_RESTART', 'r"|LOCAL_RESTART'),
    ("pattern: the restart receipt test", '|LOCAL_RESTART_RECEIPT_TEST |<realtime', '|<realtime'),
    ("pattern: a realtime delegation", '|<realtime_delegation>"', '"'),
    ("pattern: the native wake handoff", '    + r"|' + chr(92) + '[AGENTMESHY-NATIVE-WAKE ")', "    )"),
    ("only an item that is not yet a card holds the cursor",
     '            held = line_start if codex["item"] is not None else None',
     '            held = line_start if codex["item"] is not None and not codex["item"]["done"] else None'),
    ("an event takes the id of an item with other text",
     '        ref = before["id"] if same else None', '        ref = before["id"] if before is not None else None'),
    ("every file is read as a Codex rollout file",
     '    if first.get("type") != "session_meta":' + chr(10) + "        return None" + chr(10),
     "    if False:" + chr(10) + "        return None" + chr(10)),
    ("words in front of a postbox delivery are kept as the operator's",
     "        if not stripped or whole in _NEVER_CUT:", "        if not stripped:"),
    ("words in front of a tagged channel line are kept as the operator's",
     '_NEVER_CUT = ("postbox_delivery", "channel_line_typed")', '_NEVER_CUT = ("postbox_delivery",)'),
    ("words in front of an untagged delivery are kept as the operator's",
     '_NEVER_CUT = ("postbox_delivery", "channel_line_typed")', '_NEVER_CUT = ("channel_line_typed",)'),
    ("every machine-typed kind drops the whole record",
     "        if not stripped or whole in _NEVER_CUT:", "        if True:"),
    ("pattern: the paced postbox's batch header",
     'r"|' + chr(92) + 'd+ MeshyChat deliveries for |Each further', 'r"|Each further'),
    ("pattern: the same-commands line",
     '|Each further message below is read and answered with the two commands "', '"'),
    ("enroll returns antigravity",
     '    state = antigravity_dialect.file_state(path) if antigravity else None\n    if state is not None:\n        return "antigravity", {"session": state["session"], "subagent": state["subagent"]}',
     '    state = None'),
    ("read_file routes to antigravity raws",
     '        if antigravity is not None:\n            raws, drop_class = _antigravity_raws(o, antigravity, seat, seats, own)',
     '        if False:\n            raws, drop_class = _antigravity_raws(o, antigravity, seat, seats, own)'),
    ("open asks preserved in checkpoint for antigravity",
     'cp["open_asks"] = antigravity["asks"] if antigravity is not None else kimi["asks"] if kimi is not None else {}',
     'cp["open_asks"] = kimi["asks"] if kimi is not None else {}'),
    ("antigravity open asks persisted in cursor",
     '            cursor["antigravity_open_asks"][path] = antigravity["asks"]',
     '            cursor["antigravity_open_asks"][path] = {}'),
    ("antigravity options wrapped in dicts",
     'opts = [o if isinstance(o, dict) else {"label": str(o)} for o in q.get("options") or []]',
     'opts = [o if isinstance(o, dict) else str(o) for o in q.get("options") or []]'),
    ("antigravity Ruling time converted to utc_instant",
     'raw, cls = _ruling_raw(seat, utc_instant(payload["time"]), antigravity["session"]',
     'raw, cls = _ruling_raw(seat, payload["time"], antigravity["session"]'),
    ("antigravity multi-select modal answers dropped",
     "    return matched >= 1",
     "    return False",
     "antigravity_dialect.py"),
    ("antigravity modal answer matching none of the options accepted",
     "    return matched >= 1",
     "    return True",
     "antigravity_dialect.py"),
    ("antigravity single-select modal ask accepts compound answer",
     "    if not multi_select:\n        return False",
     "    if False:\n        return False",
     "antigravity_dialect.py"),
    ("a lone surrogate in a Question card's thread_id reaches the card unharmed",
     '    extra = {"thread_id": thread} if thread and _THREAD_RE.fullmatch(thread) else {}',
     '    extra = {"thread_id": thread} if thread else {}'),
    ("a lone surrogate in a Question card's session reaches the card unharmed",
     '    session = _no_lone_surrogates(session)\n    question = question.strip() if isinstance(question, str) else ""',
     '    question = question.strip() if isinstance(question, str) else ""'),
]


def _lay_out(work, sources, target=None, old="", new=""):
    """The loader's copies go in work/tools/transcript_cards, the repo's own layout, so a check that imports the loader
    as the package module tools.transcript_cards.transcript_cards (K21) imports the copy under test. Flat in work/,
    that import failed on every mutant, unmutated or not, and so counted every mutant as caught."""
    loader_dir = os.path.join(work, "tools", "transcript_cards")
    os.makedirs(loader_dir)
    for f, fsrc in sources.items():
        open(os.path.join(loader_dir, f), "w", encoding="utf-8", newline="\n").write(
            fsrc.replace(old, new) if f == target else fsrc)
    return loader_dir


def _baseline_failures(sources):
    """FAIL lines and exit codes of the suites over the UNMUTATED copies, in the mutants' own layout. Anything here
    means a mutant's red would prove nothing, so main() refuses before applying one."""
    work = tempfile.mkdtemp(prefix="tcmut-base-")
    try:
        loader_dir = _lay_out(work, sources)
        env = dict(os.environ, TRANSCRIPT_CARDS_DIR=loader_dir, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
        bad = []
        for test_file in (TEST_FILE, KIMI_TEST_FILE, ANTIGRAVITY_TEST_FILE):
            if not test_file:
                continue
            shutil.copy(test_file, work)
            p = subprocess.run([sys.executable, os.path.basename(test_file)], cwd=work, capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=600,
                               stdin=subprocess.DEVNULL, env=env)
            bad += [ln for ln in p.stdout.splitlines() if ln.startswith("FAIL ")]
            if p.returncode:
                bad.append("%s exited %d" % (os.path.basename(test_file), p.returncode))
        return bad
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    sources = {f: open(os.path.join(HERE, f), encoding="utf-8").read() for f in LOADER_FILES
               if os.path.exists(os.path.join(HERE, f))}
    base = _baseline_failures(sources)
    if base:
        print("BASELINE RED, no mutant applied: the unmutated loader fails %d check(s) here, first: %s"
              % (len(base), base[0][:120]))
        return 2
    survived = []
    for entry in MUTANTS:
        name, old, new = entry[0], entry[1], entry[2]
        target = entry[3] if len(entry) > 3 else "transcript_cards.py"
        if sources.get(target, "").count(old) != 1:
            print("BLIND  %s: the target text occurs %d times in %s, mutant not applied"
                  % (name, sources.get(target, "").count(old), target))
            survived.append(name)
            continue
        work = tempfile.mkdtemp(prefix="tcmut-")
        try:
            loader_dir = _lay_out(work, sources, target, old, new)
            shutil.copy(TEST_FILE, work)
            if KIMI_TEST_FILE:
                shutil.copy(KIMI_TEST_FILE, work)
            if ANTIGRAVITY_TEST_FILE:
                shutil.copy(ANTIGRAVITY_TEST_FILE, work)
            env = dict(os.environ, TRANSCRIPT_CARDS_DIR=loader_dir, PYTHONIOENCODING="utf-8",
                       PYTHONDONTWRITEBYTECODE="1")
            fails, crashed, rc = [], False, 0
            for test_file in (TEST_FILE, KIMI_TEST_FILE, ANTIGRAVITY_TEST_FILE):
                if not test_file:
                    continue
                p = subprocess.run([sys.executable, os.path.basename(test_file)], cwd=work, capture_output=True,
                                   text=True, encoding="utf-8", errors="replace", timeout=600,
                                   stdin=subprocess.DEVNULL, env=env)
                fails += [ln for ln in p.stdout.splitlines() if ln.startswith("FAIL ")]
                crashed = crashed or "Traceback" in (p.stdout + p.stderr)
                rc = rc or p.returncode
            if rc != 0 and fails:
                print("CAUGHT %s: %d failing check(s), first: %s" % (name, len(fails), fails[0][:90]))
            elif rc != 0 and crashed:
                print("CAUGHT %s: the suite crashed (non-zero exit, traceback)" % name)
            else:
                print("SURVIVED %s: suite exit %d with no failing check" % (name, rc))
                survived.append(name)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    print("\n%d mutants, %d survived" % (len(MUTANTS), len(survived)))
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
