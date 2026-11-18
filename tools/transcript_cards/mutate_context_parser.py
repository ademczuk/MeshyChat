"""Mutation check for tests/test_transcript_cards_context_parser.py: each mutant of context_parser.py must make the suite FAIL.
Run: python tools/transcript_cards/mutate_context_parser.py   (exit 0 = every mutant was caught, exit 1 = one survived or its anchor text went stale).
Works on copies in a temp folder; the committed files are never changed. A mutant whose anchor text no longer occurs exactly once is reported as SKIP and fails the run,
so a refactor cannot quietly turn a mutant into a no-op."""
import os, shutil, subprocess, sys, tempfile
nl = chr(10)
SRC = os.path.dirname(os.path.abspath(__file__))
TEST = os.path.join(os.path.dirname(os.path.dirname(SRC)), "tests", "test_transcript_cards_context_parser.py")
M = [
 ("analysis phase kept", 'or payload.get("phase") == "analysis"', 'or False'),
 ("secret screen off", '            if tc.detect(text):\n                return "secret"', '            if False:\n                return "secret"'),
 ("client screen off", 'if self.names.hits(text):', 'if False:'),
 ("budget off by one", '> MAX_VALUE_BYTES:\n            return self._omit(time, native_ref, "too_large", len(text.encode("utf-8")))', '>= MAX_VALUE_BYTES:\n            return self._omit(time, native_ref, "too_large", len(text.encode("utf-8")))'),
 ("omitted bytes = chars", 'len(text.encode("utf-8")))\n        self._push(value)', 'len(text))\n        self._push(value)'),
 ("removal counted as completion", 'if after[2] > before[2]:\n                    events.append(("ambiguous"', 'if False:\n                    events.append(("ambiguous"'),
 ("ambiguous never", 'if ta < tb:\n                if after[2] > before[2]:', 'if ta < tb:\n                if False:'),
 ("reorder emits", 'events += [("completed", key, None)] * max(0, after[2] - before[2])', 'events += [("completed", key, None)] * max(0, after[2] - before[2]) + ([("added", key, None)] if after != before and ta == tb and after[2] == before[2] else [])'),
 ("machine filter off", 'if role == "user" and self.drop_machine_typed and tc._CODEX_MACHINE_TYPED.search(part["text"]):', 'if False:'),
 ("manifest null for written", 'entries.append([row["ordinal"], row["text_sha256"]])', 'entries.append([row["ordinal"], None])'),
 ("native_ref raw id", 'value["native_ref"] = _hex32(native_ref)', 'value["native_ref"] = native_ref'),
 ("user text not user role", 'self._text(role, part["text"], time, payload.get("id"))', 'self._text("assistant", part["text"], time, payload.get("id"))'),
 ("name list not normalized", 'norm = _normalize_name(text)\n        return any(m in norm for m in self.markers)', 'return any(m in text for m in self.markers)'),
 ("short marker allowed", 'if len(norm) < 4:', 'if len(norm) < 1:'),
 ("state loses plan", '"ordinal": self.ordinal, "plan": [[s, list(c)] for s, c in self.plan.items()],', '"ordinal": self.ordinal, "plan": [],'),
 ("oversize screen off", '"reason": self._screen(clean)}', '"reason": None}'),
 ("hex32 not cleaned", 'return _sha(tc._no_lone_surrogates(text))[:32]', 'return _sha(text)[:32]'),
 ("state forgets machine filter", 'state["drop_machine_typed"], _state=state,', 'True, _state=state,'),
 ("denylist mismatch allowed", 'if parser.names.digest() != state["denylist_sha256"]:', 'if False:'),
 ("oversize never pruned", 'self.oversize = {k: v for k, v in self.oversize.items() if k in new}', 'pass'),
 ("short key untagged", 'return "t:" + step if len(step) <= MAX_STEP_CHARS else "h:" + _sha(step)', 'return step if len(step) <= MAX_STEP_CHARS else "h:" + _sha(step)'),
 ("oversize before screen", 'if big["reason"]:', 'if False:'),
 ("artifact: first block not last", 'self.final = {"time": time,', 'if self.final is None: self.final = {"time": time,'),
 ("artifact: analysis kept", 'if role not in ("user", "assistant") or payload.get("phase") == "analysis":', 'if role not in ("user", "assistant"):'),
 ("artifact: screen skipped", 'size = len(text.encode("utf-8"))' + chr(10) + '        reason = self._screen(text)', 'size = len(text.encode("utf-8"))' + chr(10) + '        reason = None'),
 ("artifact: no agent_id", 'value["text"], value["text_sha256"], value["agent_id"] = final["text"], _sha(final["text"]), self.src', 'value["text"], value["text_sha256"] = final["text"], _sha(final["text"])'),
 ("artifact: plan from subagent", 'and payload.get("name") == "update_plan" and not self.subagent:', 'and payload.get("name") == "update_plan":'),
 ("artifact: finish not idempotent", 'if not self.subagent or self.finished:' + chr(10) + '            return []', 'if not self.subagent:' + chr(10) + '            return []'),
 ("artifact: feed after finish", 'if self.finished:' + chr(10) + '            raise ValueError("this source is finished: it takes no more records")', 'if False:' + chr(10) + '            raise ValueError("x")'),
 ("artifact: main skips not sidechain", 'if side != self.subagent:' + chr(10) + '            return', 'if False:' + chr(10) + '            return'),
 ("artifact: no_final_text unreported", 'self.skipped.append(("no_final_text", None))', 'pass'),
 ("skipped record takes an ordinal", 'if not time:' + chr(10) + '            self.skipped.append(("no_time", native_ref))' + chr(10) + '            return' + chr(10) + '        reason = self._screen(text)', 'if not time:' + chr(10) + '            self.skipped.append(("no_time", native_ref))' + chr(10) + '            self.ordinal += 1' + chr(10) + '            return' + chr(10) + '        reason = self._screen(text)'),
 ("subagent prose emitted", 'if self.subagent:                         # a subagent source: only its last assistant block matters', 'if False:                         # mutated'),
 ("surrogate cleaning off (text)", 'text = tc._no_lone_surrogates(text)' + nl + '        if not text.strip():' + nl + '            return' + nl + '        if not time:' + nl + '            self.skipped.append(("no_time", native_ref))' + nl + '            return' + nl + '        reason = self._screen(text)', 'if not text.strip():' + nl + '            return' + nl + '        if not time:' + nl + '            self.skipped.append(("no_time", native_ref))' + nl + '            return' + nl + '        reason = self._screen(text)'),
 ("event_msg agent_message parsed", 'if kind != "response_item":', 'if kind == "event_msg" and not self.subagent and payload.get("type") == "agent_message":' + nl + '            return self._text("assistant", payload.get("message", ""), tc.utc_instant(rec.get("timestamp")), None)' + nl + '        if kind != "response_item":'),
 ("state version not bumped", 'STATE_VERSION = 8', 'STATE_VERSION = 1'),
 ("child falls back to parent id", 'native = rec.get("agentId")           # never the parent', 'native = rec.get("agentId") or rec.get("sessionId")           # never the parent'),
 ("envelope probe off", 'fits = len(serialize(probe)) <= MAX_VALUE_BYTES', 'fits = True'),
 ("state version 8 not bumped", 'STATE_VERSION = 8', 'STATE_VERSION = 7'),
 ("close_batch does nothing", 'if self.batch_count:' + nl + '            self.batch += 1', 'if False:' + nl + '            self.batch += 1'),
 ("close_batch closes empty", 'if self.batch_count:' + nl + '            self.batch += 1', 'if True:' + nl + '            self.batch += 1'),
 ("full batch never closes", 'if self.batch_count >= self.batch_items:', 'if False:'),
 ("batch lost from state", '"batch": self.batch, "batch_count": self.batch_count,', '"batch": 1, "batch_count": 0,'),
 ("cli cut ignored", 'if number - 1 in cuts:', 'if False:'),
 ("batch field constant", '"batch": self.batch, "ordinal": ordinal, "item": item}', '"batch": 1, "ordinal": ordinal, "item": item}'),
 ("final lost from state v3", '"final": self.final, "finished": self.finished,', '"final": None, "finished": self.finished,'),
 ("quarantine not persisted", '"quarantined": self.quarantined,', '"quarantined": False,'),
 ("unscreened namespace still screened", 'if not self.screens:' + nl + '            return None', 'if False:' + nl + '            return None'),
 ("every namespace unscreened", 'return namespace != UNSCREENED_NAMESPACE', 'return False'),
 ("namespace matched by prefix", 'return namespace != UNSCREENED_NAMESPACE', 'return not (namespace or "").startswith(UNSCREENED_NAMESPACE)'),
 ("namespace matched without case", 'return namespace != UNSCREENED_NAMESPACE', 'return (namespace or "").strip().lower() != UNSCREENED_NAMESPACE'),
 ("screens lost from state", '"screens": self.screens, "f3"', '"screens": True, "f3"'),
 ("screens not restored", 'self.screens = _state["screens"]', 'pass'),
 ("namespace mismatch allowed", 'if namespace is not None and screens_apply(namespace) != state["screens"]:', 'if False:'),
 ("cli namespace ignored", 'namespace=args.namespace, f3=args.f3)', 'namespace=None, f3=args.f3)'),
 ("quarantine flag never set", 'self.quarantined = True           # never assign', 'self.quarantined = False           # never assign'),
 ("quarantine ignored", 'if self.quarantined:' + nl + '            return                                    # decided', 'if False:' + nl + '            return                                    # decided'),
 ("state not validated", '        validate_state(state)' + nl, '        pass' + nl),
 ("ordinal may be negative", 'if not _is_int(state["ordinal"]):', 'if False:'),
 ("batch_count unbounded", 'not _is_int(state["batch_count"], 0, state["batch_items"] - 1)', 'False'),
 ("missing keys not refused", 'if missing:' + nl + '        raise', 'if False:' + nl + '        raise'),
 ("bool accepted as int", 'return type(v) is int and v >= low', 'return isinstance(v, int) and v >= low'),
 ("record not a dict", 'if not isinstance(record, dict):' + nl + '            self.skipped.append(("not_an_object", None))', 'if False:' + nl + '            self.skipped.append(("not_an_object", None))'),
 ("assistant text not type-checked", 'if isinstance(block.get("text"), str):' + nl + '                    self._text("assistant", block["text"], time, ref)', 'if True:' + nl + '                    self._text("assistant", block["text"], time, ref)'),
 ("plan not list not checked", 'if not isinstance(steps, list):', 'if False:'),
 ("message not dict not checked", 'if not isinstance(message, dict):' + nl + '            return', 'if False:' + nl + '            return'),
 ("todos not list not checked", 'if isinstance(todos, list):' + nl + '                    self._plan(', 'if True:' + nl + '                    self._plan('),
 ("final text not type-checked", 'isinstance(block.get("text"), str):' + nl + '                        self._final_candidate', 'True:' + nl + '                        self._final_candidate'),
 ("codex content not list-checked", 'for part in content if isinstance(content, list) else []:', 'for part in content or []:'),
 ("plan status not str-checked", 'status if isinstance(status, str) else ""', 'status'),
 ("plan falsy coerced to empty", 'steps = json.loads(payload.get("arguments") or "{}").get("plan")' + nl + '            except (ValueError, AttributeError, TypeError):' + nl + '                steps = None', 'steps = json.loads(payload.get("arguments") or "{}").get("plan") or []' + nl + '            except (ValueError, AttributeError, TypeError):' + nl + '                steps = None'),
 ("final keys not required", 'all(k in final for k in ("time", "ref", "bytes", "reason", "text"))', 'True'),
 ("oversize keys not required", '"bytes" in v and "reason" in v', 'True'),
 ("orphan hashed step allowed", 'r[0] not in over for r in plan):', 'False for r in plan):'),
 ("codex parts merged", 'for part in content if isinstance(content, list) else []:      # one item per text block, in order', 'for part in [{"type": "output_text", "text": "".join(q.get("text", "") for q in (content if isinstance(content, list) else []) if isinstance(q, dict))}]:      # merged'),
 ("antigravity thinking kept when content missing", 'content = rec.get("content")', 'content = rec.get("content") or rec.get("thinking")'),
 ("antigravity GENERIC tool result kept", 'elif source == "MODEL" and rtype == "PLANNER_RESPONSE":', 'elif source == "MODEL" and rtype in ("PLANNER_RESPONSE", "GENERIC"):'),
 ("antigravity SYSTEM_MESSAGE kept as user_text", 'if source == "USER_EXPLICIT" and rtype == "USER_INPUT":', 'if rtype in ("USER_INPUT", "SYSTEM_MESSAGE", "CHECKPOINT"):'),
 ("antigravity extract_user_text skipped", 'antigravity_dialect.extract_user_text(rec.get("content"))', '(rec.get("content") if isinstance(rec.get("content"), str) else "")'),
 ("antigravity machine filter off", 'if self.drop_machine_typed and tc._MACHINE_TYPED.search(text):' + nl + '                return', 'if False:' + nl + '                return'),
 ("antigravity step0 sender subagent detection off", 'if source == "SYSTEM" and isinstance(content0, str) and "sender=" in content0:' + nl + '                self.subagent = True', 'if False:' + nl + '                self.subagent = True'),
 ("antigravity late system wakeup flips main to subagent", 'if self.ordinal == 0 and self.final is None and (step_index is None or (type(step_index) is int and step_index == 0)):', 'if True:'),
 ("antigravity resume flips main to subagent on step0 sender", 'if self.ordinal == 0 and self.final is None', 'if not self.items and self.final is None'),
 ("antigravity subagent user prompt emitted", 'if self.subagent:' + nl + '                return' + nl + '            ref = self._ag_ref', 'if False:' + nl + '                return' + nl + '            ref = self._ag_ref'),
 ("antigravity rewind ref not disambiguated", 'return "%s:%s" % (base_ref, stamp)', 'return base_ref'),
 ("antigravity ag_hwm lost from state", '"ag_hwm": list(self.ag_hwm) if self.ag_hwm is not None else None,', '"ag_hwm": None,'),
 ("antigravity ag_hwm not restored from state", 'self.ag_hwm = tuple(_state["ag_hwm"]) if _state["ag_hwm"] is not None else None', 'self.ag_hwm = None'),
 ("antigravity ag_hwm not validated in state", 'bad.append("ag_hwm")', 'pass'),
 ("antigravity missing session_id not quarantined", 'if self.src is None:' + nl + '                self.quarantined = True' + nl + '                self.skipped.append(("no_session_id", None))' + nl + '                return' + nl + '            if self.subagent:', 'if False:' + nl + '                return' + nl + '            if self.subagent:'),
 ("antigravity context summary kept as user_text", 'if text.lstrip().startswith("<CONTEXT_SUMMARY>"):' + nl + '                return', 'if False:' + nl + '                return'),
 ("antigravity parse_file skips preferred_antigravity_path", 'read_path = preferred_antigravity_path(path)', 'read_path = path'),
 ("antigravity source_path session derivation skipped", 'derived = antigravity_dialect._session_from_path(raw_sp)', 'derived = "subagents"'),
 ("antigravity source_path subagents flag skipped", 'if antigravity_dialect.is_subagent_path(raw_sp):\n                    self.subagent = True', 'if False:\n                    self.subagent = True'),
 ("v6 claude/codex/kimi migration skipped", 'if (isinstance(state, dict) and state.get("v") == 6\n            and state.get("dialect") in ("codex", "claude", "kimi") and "ag_hwm" not in state):', 'if False:'),
 ("v6 kimi migration skipped", 'and state.get("dialect") in ("codex", "claude", "kimi") and "ag_hwm" not in state):', 'and state.get("dialect") in ("codex", "claude") and "ag_hwm" not in state):'),
 ("v6 antigravity checkpoint accepted", 'and state.get("dialect") in ("codex", "claude", "kimi") and "ag_hwm" not in state):', 'and state.get("dialect") in ("codex", "claude", "kimi", "antigravity") and "ag_hwm" not in state):'),
 ("kimi: dialect not registered", 'DIALECTS = ("codex", "claude", "antigravity", "kimi")', 'DIALECTS = ("codex", "claude", "antigravity")'),
 ("kimi: machine origin kept", 'if kind != "user" and kind is not None and self.drop_machine_typed:', 'if False and kind is not None and self.drop_machine_typed:'),
 ("kimi: machine-typed screen off", 'if self.drop_machine_typed and tc._KIMI_MACHINE_TYPED.search(text):', 'if False and tc._KIMI_MACHINE_TYPED.search(text):'),
 ("kimi: reminder parts kept", 'if text.lstrip().startswith("<system-reminder"):', 'if False and text.lstrip().startswith("<system-reminder"):'),
 ("kimi: non-user role kept", 'if role != "user":\n                self.skipped.append(("kimi_message_" + str(role), ref))', 'if False:\n                self.skipped.append(("kimi_message_" + str(role), ref))'),
 ("kimi: subagent dispatch prompt kept", 'if self.subagent:                         # the parent\'s dispatch prompt is not the subagent\'s report', 'if False:                             # mutated'),
 ("kimi: thinking becomes a loop event", 'if etype == "content.part" and ptype == "think":', 'if False and ptype == "think":'),
 ("kimi: any loop part emitted as text", 'if etype != "content.part" or ptype != "text" or not isinstance(part.get("text"), str):', 'if etype != "content.part":'),
 ("kimi: subagent text not buffered", 'if self.subagent:                         # a kimi subagent wire: only its last text part matters\n                self._final_candidate(text, time, ref)', 'if self.subagent:                         # a kimi subagent wire: only its last text part matters\n                self._text("assistant", text, time, ref)'),
 ("kimi: quarantine ignored", 'if self.quarantined:' + nl + '            return                                    # the first content record settled it', 'if False:' + nl + '            return                                    # mutated'),
 ("kimi: compaction not reported", 'self.skipped.append(("kimi_compaction", None))', 'pass'),
 ("kimi: undo not reported", 'self.skipped.append(("kimi_undo", None))', 'pass'),
 ("kimi: clear not reported", 'self.skipped.append(("kimi_clear", None))', 'pass'),
 ("kimi: bookkeeping not reported", 'self.skipped.append(("kimi_bookkeeping", None))', 'pass'),
 ("kimi: user text under an assistant role", 'for text in kept:\n                self._text("user", text, time, ref)', 'for text in kept:\n                self._text("assistant", text, time, ref)'),
 ("kimi: message time not converted", 'time = kimi_dialect._time(rec.get("time"))\n            if self.f3 and not self.subagent:\n                self._saw_time(time)\n            for text in kept:', 'time = ""\n            if self.f3 and not self.subagent:\n                self._saw_time(time)\n            for text in kept:'),
 ("kimi: part time not converted", 'time = kimi_dialect._time(rec.get("time"))\n            if self.f3 and not self.subagent:\n                self._saw_time(time)\n            if self.subagent:', 'time = ""\n            if self.f3 and not self.subagent:\n                self._saw_time(time)\n            if self.subagent:'),
 ("kimi: message path never quarantines", 'self.quarantined = True               # never assign an identity the path did not give', 'self.quarantined = False              # mutated'),
 ("kimi: loop path never quarantines", 'self.quarantined = True\n                self.skipped.append(("no_session_id", None))\n                return\n            time = kimi_dialect._time(rec.get("time"))\n            if self.f3 and not self.subagent:\n                self._saw_time(time)\n            if self.subagent:', 'self.quarantined = False\n                self.skipped.append(("no_session_id", None))\n                return\n            time = kimi_dialect._time(rec.get("time"))\n            if self.f3 and not self.subagent:\n                self._saw_time(time)\n            if self.subagent:'),
 ("kimi: session id not derived from path", 'elif session_id is None and k_session:', 'elif False and k_session:'),
 ("kimi: subagent mark dropped", 'self.subagent = True\n                    base = session_id if isinstance(session_id, str) and session_id.strip() else k_session', 'self.subagent = False\n                    base = session_id if isinstance(session_id, str) and session_id.strip() else k_session'),
 ("kimi: subagent keyed by the bare session", 'if base:\n                        session_id = base + "/agents/" + k_agent', 'if base:\n                        session_id = base'),
 ("kimi: cli session-id ignored", 'session_id=args.session_id, namespace=args.namespace, f3=args.f3)', 'namespace=args.namespace, f3=args.f3)'),
]
# Control first: the UNMUTATED copy, run the same way, must pass. Otherwise a harness that cannot import the module
# would "kill" every mutant and report a perfect score.
_d = tempfile.mkdtemp()
shutil.copytree(SRC, _d + "/tc", ignore=shutil.ignore_patterns("__pycache__", "*.log"))
_r = subprocess.run([sys.executable, TEST], capture_output=True, text=True,
                    env=dict(os.environ, TRANSCRIPT_CARDS_DIR=_d + "/tc"), timeout=600)
shutil.rmtree(_d, ignore_errors=True)
if "FAIL" in _r.stdout or _r.returncode != 0:
    print("CONTROL FAILED: the unmutated parser does not pass its own tests here; no mutant result means anything")
    sys.exit(2)
print("control ok: the unmutated parser passes")
base = open(SRC + "/context_parser.py", encoding="utf-8").read()
dead = 0
skipped = 0
for name, a, b in M:
    if base.count(a) != 1:
        print("SKIP (pattern count %d): %s" % (base.count(a), name)); skipped += 1; continue
    d = tempfile.mkdtemp()
    shutil.copytree(SRC, d + "/tc", ignore=shutil.ignore_patterns("__pycache__", "*.log"))
    open(d + "/tc/context_parser.py", "w", encoding="utf-8").write(base.replace(a, b))
    env = dict(os.environ, TRANSCRIPT_CARDS_DIR=d + "/tc")
    r = subprocess.run([sys.executable, TEST], capture_output=True, text=True, env=env, timeout=600)
    killed = "FAIL" in r.stdout or r.returncode != 0
    print(("KILLED " if killed else "SURVIVED ") + name)
    dead += killed
    shutil.rmtree(d, ignore_errors=True)
print("killed %d of %d" % (dead, len(M)))
sys.exit(0 if dead == len(M) and not skipped else 1)
