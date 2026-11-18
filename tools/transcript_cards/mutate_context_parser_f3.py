"""Mutation check for tests/test_transcript_cards_context_parser_f3.py: each mutant of context_parser.py's F3 rules must make
that suite FAIL. One mutant per rule agreed with txxxx on <thread> (6b8c71).
Run: python tools/transcript_cards/mutate_context_parser_f3.py   (exit 0 = every mutant was caught, exit 1 = one survived or its anchor text went
stale). Works on copies in a temp folder; the committed files are never changed. A mutant whose anchor no longer occurs exactly once is SKIP and
fails the run, so a refactor cannot quietly turn a mutant into a no-op."""
import os, shutil, subprocess, sys, tempfile
SRC = os.path.dirname(os.path.abspath(__file__))
TEST = os.path.join(os.path.dirname(os.path.dirname(SRC)), "tests", "test_transcript_cards_context_parser_f3.py")
M = [
 ("F3 on by default", 'source_path=None, f3=False):', 'source_path=None, f3=True):'),
 ("pairs by position, not id", '            if e[0] == tool_ref:\n                del self.tool_pending[i]', '            if True:\n                del self.tool_pending[i]'),
 ("duplicate result emitted again", 'if any(e[0] == tool_ref and e[1] == "p" for e in self.tool_seen):', 'if False:'),
 ("duplicate call kept", 'if any(e[0] == tool_ref for e in self.tool_pending):\n            return self.skipped.append(("tool_duplicate_call", native_ref))', 'if False:\n            return None'),
 ("flush ignores data age", 'or newest - called >= F3_FLUSH_AGE_S:', 'or True:'),
 ("flush off by one second", 'or newest - called >= F3_FLUSH_AGE_S:', 'or newest - called > F3_FLUSH_AGE_S:'),
 ("late result dropped", '        self._seen(tool_ref, "p")\n        self._exchange(tool_ref, None, None, result_text, "missing", status, time, native_ref)', '        self._seen(tool_ref, "p")'),
 ("flushed id counts as paired", '    def _flush_one(self, e):\n        self._seen(e[0], "f")', '    def _flush_one(self, e):\n        self._seen(e[0], "p")'),
 ("pending never capped", 'while self.tool_pending and len(serialize(self.tool_pending)) > F3_PENDING_BYTES:', 'while False:'),
 ("cap flushes the newest", 'self._flush_one(self.tool_pending.pop(0))', 'self._flush_one(self.tool_pending.pop())'),
 ("seen window unbounded", 'del self.tool_seen[:-F3_SEEN_IDS]', 'pass'),
 ("structured tool text encoded early", '        return value\n    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))', '        return value\n    return serialize(value).decode("utf-8")'),
 ("exchange not screened at all", 'reason = self._screen(*[t for t in (name, call_text, result_text) if t])', 'reason = None'),
 ("result not screened", 'reason = self._screen(*[t for t in (name, call_text, result_text) if t])', 'reason = self._screen(*[t for t in (name, call_text) if t])'),
 ("call not screened", 'reason = self._screen(*[t for t in (name, call_text, result_text) if t])', 'reason = self._screen(*[t for t in (name, result_text) if t])'),
 ("large exchange truncated", 'for n, text in enumerate(texts, 1):', 'for n, text in enumerate(texts[:1], 1):'),
 ("part cap ignored", 'if texts is None or len(texts) > min(F3_MAX_PARTS, room):', 'if texts is None:'),
 ("batch capacity ignored", 'if texts is None or len(texts) > min(F3_MAX_PARTS, room):', 'if texts is None or len(texts) > F3_MAX_PARTS:'),
 ("record room ignored", '        return self.batch_items - (len(self.items) - self._unit[0]) - self._unit[1]', '        return self.batch_items'),
 ("text blocks get no slot", '        return self.batch_items - (len(self.items) - self._unit[0]) - self._unit[1]', '        return self.batch_items - (len(self.items) - self._unit[0])'),
 ("a full record emits anyway", '        if room < 1:                       # not even an omission fits', '        if False:                       # not even an omission fits'),
 ("thinking past the room", '            if self._f3_room() < 1:\n                return self.skipped.append(("record_full", native_ref))\n            self._text(', '            self._text('),
 ("plan items get no slot", '                    reserve += len(events)', '                    pass'),
 ("plan reserve not chained", '                    plan, events = self._plan_diff(plan, [', '                    _p, events = self._plan_diff(plan, ['),
 ("codex plan items get no slot", '            self._unit[1] = self._codex_f1_reserve(payload, time)', '            pass'),
 ("codex plan reserve counts nothing", '                return len(self._plan_diff(self.plan, [', '                return 0 * len(self._plan_diff(self.plan, ['),
 ("feed never opens a unit", '        self._unit = [before, 0]', '        self._unit = None'),
 ("room counts from the source start", '        self._unit = [before, 0]', '        self._unit = [0, 0]'),
 ("split loses a character", '        pieces.append(text[start:start + low])\n        start += low', '        pieces.append(text[start:start + low])\n        start += low + (1 if low > 1 else 0)'),
 ("error result read as ok", 'status = "error" if is_error else "ok"', 'status = "ok"'),
 ("empty thinking kept", 'if isinstance(block.get("thinking"), str) and block["thinking"].strip():', 'if isinstance(block.get("thinking"), str):'),
 ("encrypted reasoning unreported", '            if not texts:                             # encrypted_content only: nothing readable to keep\n                self._excluded("encrypted_reasoning", ref)', '            if False:\n                pass'),
 ("meta not reported", 'return self._excluded("is_meta" if rec.get("isMeta") else "compact_summary", rec.get("uuid"))', 'return None'),
 ("F3 counts in every manifest", 'if any(row["item"] in ("tool_exchange", "thinking") for row in rows):', 'if True:'),
 ("call_missing not counted", 'counts["call_missing"] += row["call_status"] == "missing"', 'pass'),
 ("state loses pending calls", '"tool_pending": [list(e) for e in self.tool_pending],', '"tool_pending": [],'),
 ("state loses the data clock", '"newest_time": self.newest_time,', '"newest_time": "",'),
 ("f3 mismatch resumes", 'if _state["f3"] != f3:', 'if False:'),
 ("v7 upgrade mutates caller", '        upgraded = dict(state)\n        upgraded.update(v=8', '        upgraded = state\n        upgraded.update(v=8'),
 ("v7 upgrade misses antigravity", 'state.get("dialect") in ("codex", "claude", "antigravity", "kimi")', 'state.get("dialect") in ("codex", "claude", "kimi")'),
 ("pairing state allowed with F3 off", 'if state["f3"] is False and (state["tool_pending"] or state["tool_seen"] or state["f3_dialect"]):', 'if False:'),
 ("dialect slot unbounded", 'len(serialize(state["f3_dialect"])) <= F3_DIALECT_BYTES', 'True'),
 ("idle clock ignored", 'return wall is not None and wall - called >= F3_IDLE_FLUSH_S', 'return False'),
 ("idle clock flushes after 5 minutes", 'F3_IDLE_FLUSH_S = 24 * 3600', 'F3_IDLE_FLUSH_S = 300'),
 ("flush only= ignored", 'if (only is None or e[0] == only) and self._due(e, now, everything):', 'if self._due(e, now, everything):'),
 ("due_pending lists every call", 'due = [e[0] for e in self.tool_pending if self.f3 and self._due(e, now, everything)]', 'due = [e[0] for e in self.tool_pending]'),
 ("end of file leaves calls waiting", 'for ref in parser.due_pending(everything=True):', 'for ref in parser.due_pending():'),
 ("structured strings not screened", '            reason = self._screen(*_json_strings(call_text), *_json_strings(result_text))', '            reason = None'),
 ("structured input strings not screened", '            reason = self._screen(*_json_strings(call_text), *_json_strings(result_text))', '            reason = self._screen(*_json_strings(result_text))'),
 ("parse_file never rolls a split exchange back", '        if any(len(batches) > 1 for batches in spans.values()):', '        if False:'),
 ("parse_file rollback keeps the old batch", '            parser.close_batch()\n            step(parser)', '            step(parser)'),
 ("kimi tool call dropped", 'if etype == "tool.call":\n                    return self._tool_call', 'if etype == "tool.call":\n                    return None'),
 ("kimi result not paired", 'return self._tool_result(tool_id, _tool_text(result.get("output")), result.get("isError") is True,', 'return None'),
 ("kimi thinks from text", 'return self._thinking(thought, time, ref)', 'return self._thinking(part.get("text"), time, ref)'),
 ("kimi compaction misclassified", 'if self.f3 and not self.subagent:\n                return self._excluded("compact_summary", None)', 'if self.f3 and not self.subagent:\n                return self._excluded("kimi_compaction", None)'),
 ("kimi source identity unchecked", 'if self.src is None:\n                        self.quarantined = True\n                        self.skipped.append(("no_session_id", None))\n                        return\n                    time = kimi_dialect._time(rec.get("time"))\n                    if self.f3 and not self.subagent:\n                        self._saw_time(time)\n                    return self._thinking(thought, time, ref)', 'if False:\n                        pass\n                    time = kimi_dialect._time(rec.get("time"))\n                    if self.f3 and not self.subagent:\n                        self._saw_time(time)\n                    return self._thinking(thought, time, ref)'),
 ("kimi args use str dict", '_tool_text(ev.get("args"))', 'str(ev.get("args"))'),
 ("kimi result parent ref ignored", 'result_ref = ev.get("parentUuid") if isinstance(ev.get("parentUuid"), str) and ev.get("parentUuid") else tool_id', 'result_ref = tool_id'),
 ("kimi data clock not updated", 'def _kimi(self, rec):\n        if self.quarantined:', 'def _kimi(self, rec):\n        self._saw_time = lambda time: None\n        if self.quarantined:'),
 ("kimi time hoisted", 'rtype = rec.get("type")\n        if rtype == "context.append_message":', 'rtype = rec.get("type")\n        time = kimi_dialect._time(rec.get("time"))\n        if rtype == "context.append_message":'),
 ("tool names not bounded", 'if not isinstance(name, str) or not name or len(name) > 256:', 'if False:'),
 ("ag: validate_state skips _valid_ag_f3", 'elif state["dialect"] == "antigravity" and not _valid_ag_f3(state["f3_dialect"]):', 'elif False:'),
 ("ag: _valid_ag_f3 allows unknown keys", 'set(d) <= {"steps", "early", "res_seen", "res_hwm", "anon"}', 'True'),
 ("ag: _valid_ag_f3 skips steps check", '"steps" not in d or (isinstance(d["steps"], list)', 'True or (isinstance(d["steps"], list)'),
 ("ag: _valid_ag_f3 skips early check", '"early" not in d or (isinstance(d["early"], list)', 'True or (isinstance(d["early"], list)'),
 ("ag: _valid_ag_f3 skips res_seen check", '"res_seen" not in d or (isinstance(d["res_seen"], list)', 'True or (isinstance(d["res_seen"], list)'),
 ("ag: _valid_ag_f3 skips res_hwm check", '"res_hwm" not in d or (isinstance(d["res_hwm"], list) and len(d["res_hwm"]) == 4', 'True or (isinstance(d["res_hwm"], list) and len(d["res_hwm"]) == 4'),
 ("ag: _valid_ag_f3 skips anon check", '"anon" not in d or _is_int(d["anon"], 1)', 'True'),
 ("ag: truncated_fields ignored when valid list", 'if isinstance(tf, list) and all(isinstance(x, str) for x in tf):\n        return frozenset(tf)', 'if isinstance(tf, list) and all(isinstance(x, str) for x in tf):\n        return frozenset()'),
 ("ag: malformed truncated_fields fails open", 'return frozenset(("content", "thinking", "tool_calls"))', 'return frozenset()'),
 ("ag: result replay check uses dialogue ag_hwm", 'if res:\n            rid = _hex32(res_id) if res_id else ""', 'if False:\n            rid = ""'),
 ("ag: res_seen matches any step instead of identity", 'hit = next((x for x in seen if x[0] == step_index and x[1] == rid), None)', 'hit = seen[0] if seen else None'),
 ("ag: res_seen evicts fifo instead of min step", 'ev = min(seen, key=lambda x: x[0])', 'ev = seen[0]'),
 ("ag: res_seen eviction skips res_hwm fold", 'self._ag_fold_res_hwm(ev[0], ev[2])', 'pass'),
 ("ag: open pending call ignored past res_hwm", 'if ev_bound is None or bool(call_ns):', 'if ev_bound is None:'),
 ("ag: stale pre-rewind result steals open pending call", 'if call_ns and ns < call_ns:\n                bound_ns = call_ns', 'if False:\n                bound_ns = call_ns'),
 ("ag: open call lookup matches explicit sentinel step 0", 'else (st != 0 and st == step_index):', 'else (st == step_index):'),
 ("ag: step_index None skips open call timestamp check", 'if base_ref is None and not (call_ns and ns < call_ns):', 'if base_ref is None:'),
 ("ag: sub-ms rewind truncates open call timestamp to ms", 'self._ag_step_push(target_step, tid, p_ns)', 'self._ag_step_push(target_step, tid, _ag_utc_ns(time))'),
 ("ag: dialect cap skips folding res_seen first", 'if seen:\n                self._ag_evict_res_seen(seen)', 'if False:\n                self._ag_evict_res_seen(seen)'),
 ("ag: anon counter not persisted in f3_dialect", 'self.f3_dialect["anon"] = n', 'pass'),
 ("ag: dialect slot unbounded by F3_DIALECT_BYTES", 'while len(serialize(self.f3_dialect)) > F3_DIALECT_BYTES:', 'while False:'),
 ("ag: dialect cap drops early result without emitting", 'self._ag_emit_early(w)\n            elif steps:', 'pass\n            elif steps:'),
 ("ag: dialect cap evicts step without flushing pending call", 'self.flush_pending(everything=True, only=_hex32(tid))\n            else:\n                break', 'pass\n            else:\n                break'),
 ("ag: age flush leaves stale entry in steps", 'self._ag_drop_step_ref(e[0])', 'pass'),
 ("ag: explicit result id pops positional step", 'if explicit_id is not None:', 'if False:'),
 ("ag: explicit result id leaves matched id in steps", 'kept = [x for x in steps if x[1] != explicit_id]', 'kept = list(steps)'),
 ("ag: steps pops positionally instead of by step_index", 'if st == key:', 'if True:'),
 ("ag: early result ignored when planner arrives", 'if type(w[1]) is type(key) and w[1] == key:', 'if False:'),
 ("ag: explicit result id not buffered when early", 'if any(e[0] == _hex32(explicit_id) for e in self.tool_pending):', 'if True:'),
 ("ag: pre-rewind orphan steals rewound planner", 'if w[1] in keep_steps and (not min_time or not w[4] or min_time <= w[4]):', 'if w[1] in keep_steps:'),
 ("ag: early result filter truncates nanosecond timestamp to ms", 'self._ag_flush_open(owned, p_ns)', 'self._ag_flush_open(owned, _ag_utc_ns(time))'),
 ("ag: turn boundary flush_open steps disabled", 'for _st, tid, _cns in self.f3_dialect.pop("steps", None) or ():', 'for _st, tid, _cns in ():'),
 ("ag: due_pending omits early results", 'return due + [w[0] for w in self.f3_dialect.get("early", ())', 'return due + [w[0] for w in ()'),
 ("ag: flush_pending ignores only= on early results", 'if (only is None or w[0] == only) and self._due((None, None, None, tc.utc_instant(w[4]), None), now, everything):', 'if self._due((None, None, None, tc.utc_instant(w[4]), None), now, everything):'),
 ("ag: context summary skips open flush or exclusion", 'if text.lstrip().startswith("<CONTEXT_SUMMARY>"):\n            ref = self._ag_ref(step_index, raw_created) if time else self._ag_plain_ref(step_index)\n            if ref is _REPLAY:\n                return\n            self._saw_time(time)\n            self._ag_flush_open()\n            return self._excluded("compact_summary", ref)', 'if text.lstrip().startswith("<CONTEXT_SUMMARY>"):\n            return None'),
 ("ag: replayed context summary flushes live state", '            if ref is _REPLAY:\n                return\n            self._saw_time(time)', '            if ref is _REPLAY:\n                return self._ag_flush_open()\n            self._saw_time(time)'),
 ("ag: replayed user prompt flushes live state", 'if ref is _REPLAY:\n            return\n        self._saw_time(time)\n        if trunc:', 'if ref is _REPLAY:\n            return self._ag_flush_open()\n        self._saw_time(time)\n        if trunc:'),
 ("ag: truncated user prompt ignored when empty or emitted", 'if trunc:\n            self._ag_flush_open()\n            return self._excluded("truncated_fields", ref)', 'if False:\n            return None'),
 ("ag: user prompt reserves no slot before open flush", 'self._unit[1] = 1 if time else 0\n        self._ag_flush_open()', 'self._ag_flush_open()'),
 ("ag: no-time user prompt reserves phantom slot", 'self._unit[1] = 1 if time else 0', 'self._unit[1] = 1'),
 ("ag: planner missing session_id not quarantined", 'if not (has_prose or has_thinking or calls or trunc):\n            return\n        if self.src is None:\n            self.quarantined = True', 'if not (has_prose or has_thinking or calls or trunc):\n            return\n        if False:\n            self.quarantined = True'),
 ("ag: planner prose slot not reserved before f3 items", 'self._unit[1] = 1 if (has_prose and "content" not in trunc and time) else 0', 'self._unit[1] = 0'),
 ("ag: planner truncated thinking emitted", 'if "thinking" in trunc:\n            self._excluded("truncated_fields", ref)\n        elif has_thinking:', 'if False:\n            pass\n        elif has_thinking:'),
 ("ag: planner truncated tool_calls emitted", 'if "tool_calls" in trunc:\n            self._excluded("truncated_fields", ref)\n        elif calls:', 'if False:\n            pass\n        elif calls:'),
 ("ag: planner whitespace tool name kept instead of None", 'name = tc._no_lone_surrogates(raw_name).strip() if isinstance(raw_name, str) else None', 'name = tc._no_lone_surrogates(raw_name) if isinstance(raw_name, str) else None'),
 ("ag: planner duplicate call pushed onto steps", 'if not dup:\n                    w = self._ag_early_pop(ekey)', 'if True:\n                    w = self._ag_early_pop(ekey)'),
 ("ag: planner truncated content emitted as prose", 'if "content" in trunc:\n            self._excluded("truncated_fields", ref)\n        elif has_prose:', 'if False:\n            pass\n        elif has_prose:'),
 ("ag: system and checkpoint exclusions dropped", 'if source in ("SYSTEM", "SYSTEM_SDK") or rtype in (_AG_COMPACT_TYPES + _AG_META_TYPES):', 'if False:'),
 ("ag: compact types misclassified as is_meta", 'cls = "compact_summary" if rtype in _AG_COMPACT_TYPES else "is_meta"', 'cls = "is_meta"'),
 ("ag: tool result missing session_id not quarantined", 'if source != "MODEL" or rtype not in _AG_TOOL_RESULTS:\n            return\n        if self.src is None:\n            self.quarantined = True', 'if source != "MODEL" or rtype not in _AG_TOOL_RESULTS:\n            return\n        if False:\n            self.quarantined = True'),
 ("ag: truncated tool result paired instead of flushing call", 'if "content" in _ag_truncated(rec):\n            self._excluded("truncated_fields", ref)', 'if False:\n            self._excluded("truncated_fields", ref)'),
 ("ag: RUN_COMMAND nonzero exit_code ignored", 'or (rtype == "RUN_COMMAND" and type(rec.get("exit_code")) is int and rec.get("exit_code") != 0))', 'or False)'),
 ("ag: RUN_COMMAND bool exit_code treated as error", 'type(rec.get("exit_code")) is int', 'isinstance(rec.get("exit_code"), int)'),
 ("ag: subagent emits F3 items on planner response", 'if self.f3 and not self.subagent:\n                return self._ag_planner_f3', 'if self.f3:\n                return self._ag_planner_f3'),
 ("ag: subagent emits F3 items on tool result", 'elif self.f3 and not self.subagent:\n            self._ag_other_f3', 'elif self.f3:\n            self._ag_other_f3'),
]
_d = tempfile.mkdtemp()
shutil.copytree(SRC, _d + "/tc", ignore=shutil.ignore_patterns("__pycache__", "*.log"))
CMD = [sys.executable, "-m", "pytest", TEST, "-q", "-x", "--noconftest", "-p", "no:cacheprovider"]
_r = subprocess.run(CMD, capture_output=True, text=True, env=dict(os.environ, TRANSCRIPT_CARDS_DIR=_d + "/tc"), timeout=600)
shutil.rmtree(_d, ignore_errors=True)
if _r.returncode != 0:
    print("CONTROL FAILED: the unmutated parser does not pass its F3 tests here; no mutant result means anything")
    print(_r.stdout[-2000:])
    sys.exit(2)
print("control ok: the unmutated parser passes")
base = open(SRC + "/context_parser.py", encoding="utf-8").read()
dead = skipped = 0
for name, a, b in M:
    if base.count(a) != 1:
        print("SKIP (pattern count %d): %s" % (base.count(a), name)); skipped += 1; continue
    d = tempfile.mkdtemp()
    shutil.copytree(SRC, d + "/tc", ignore=shutil.ignore_patterns("__pycache__", "*.log"))
    open(d + "/tc/context_parser.py", "w", encoding="utf-8").write(base.replace(a, b))
    try:
        r = subprocess.run(CMD, capture_output=True, text=True, env=dict(os.environ, TRANSCRIPT_CARDS_DIR=d + "/tc"),
                           timeout=120)
        killed = r.returncode != 0
    except subprocess.TimeoutExpired:      # the control passes in about 2 s: a mutant that hangs the suite is caught
        killed = True
    print(("KILLED " if killed else "SURVIVED ") + name)
    dead += killed
    shutil.rmtree(d, ignore_errors=True)
print("killed %d of %d" % (dead, len(M)))
sys.exit(0 if dead == len(M) and not skipped else 1)
