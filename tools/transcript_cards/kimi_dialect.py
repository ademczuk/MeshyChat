"""The Kimi dialect's READING half for transcript_cards.py: which wire.jsonl files are a seat's, and what one
record of one is. This module builds NO card. The loader turns the descriptors returned here into cards with its
own functions (cards_from_record for a typed prompt, _ruling_raw for an answered question), so the detectors, the
size cut, the id rule and the drop classes exist once for every dialect.

A Kimi Code seat's transcripts are wire.jsonl files, one per agent of one session, under
<kimi home>/sessions/<working-dir>/session_<uuid>/agents/<agent>/wire.jsonl. A record is one typed JSON line with
a top-level `time` in epoch milliseconds. What carries the operator's words:

- context.append_message, message.role "user", message.origin.kind "user": a typed turn. origin.kind missing:
  the older format, still typed, flagged legacy by the loader. Every other origin is a machine rendering, dropped
  whole: cron_job (scheduled deliveries, text is a <cron-fire> envelope), system_trigger (goal continuations),
  injection (goal-objective reminders re-rendered every goal turn, todo reminders, permission changes, and the
  like), task and skill_activation. An envelope pasted into an otherwise typed line is cut by the loader from
  the machine's first word (MACHINE_TYPED).
- goal.create: the operator's /goal objective, verbatim and once per goal. The reminder injections that re-render
  it are machine text.
- context.append_loop_event, an AskUserQuestion tool.call: the seat's questions to the operator, buffered by
  toolCallId. Its tool.result names the same call, and the result's output is a JSON string {"answers":
  {<question text>: <chosen label or free text>}}: the record itself ties answer to question, so a Ruling is
  allowed (kxxx's bar for a dialect Ruling). A call the harness refused (auto permission mode: isError true, or
  output beginning "AskUserQuestion is disabled") never reached the operator and cards nothing. A call whose
  result has not been read stays open in state["asks"]; the loader persists it so a result appended after this
  run still ties.
- context.apply_compaction is model-written and never the operator's words (kxxx's rule for a dialect).

Everything else (fifty-odd record types on one seat: llm.*, turn.*, goal.update, cron.*, task.*,
token_counting.*, agent.*, subagent.*, config.update, mcp.*, ...) is harness bookkeeping, kimi_bookkeeping.
"""

import datetime
import json
import os

ASK_TOOL = "AskUserQuestion"


def _time(ms):
    """Epoch milliseconds to the one spelling the server takes (YYYY-MM-DDTHH:MM:SS.mmmZ), or ""."""
    if not isinstance(ms, (int, float)) or isinstance(ms, bool) or ms <= 0:
        return ""
    dt = datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (int(ms) % 1000)


def text_parts(content):
    """(text, has_attachment) of a wire message's content. Reminder parts are skipped, as the loader's own
    _typed_text skips them on the Claude path."""
    parts = content if isinstance(content, list) else [content]
    texts, image = [], False
    for c in parts:
        if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str):
            if not c["text"].lstrip().startswith("<system-reminder"):
                texts.append(c["text"])
        elif isinstance(c, str):
            texts.append(c)
        else:
            image = True
    return "\n".join(t for t in texts if t.strip()), image


def first_record(path):
    """The first JSON record of a file, or {} when unreadable."""
    try:
        with open(path, "rb") as f:
            for line in f:
                if line.strip():
                    o = json.loads(line.decode("utf-8", "replace"))
                    return o if isinstance(o, dict) else {}
    except (OSError, ValueError):
        return {}
    return {}


def wire_identity(path):
    """(session, subagent, agent) of a Kimi wire path, from the path alone: the session_<id> component nearest
    the file ("" when the path holds none), the agents/<dir> mark and that dir's name. A wire is a subagent's
    only when it sits under a literal agents/<dir> with a dir other than "main"; any other layout (a frozen or
    copied wire named f.jsonl in a spool folder, say) is a main wire, so an off-layout copy never silently runs
    in subagent mode. Nothing is read, so a frozen copy's LIVE original answers (file_state) and a path that is
    not on this box parses the same (the context parser's source_path, step 9)."""
    session = ""
    for comp in reversed(os.path.normpath(path).split(os.sep)):
        if comp.startswith("session_"):
            session = comp[len("session_"):]
            break
    agent = os.path.basename(os.path.dirname(path))
    grandparent = os.path.basename(os.path.dirname(os.path.dirname(path)))
    return session, grandparent == "agents" and agent != "main", agent


def file_state(path, original_path=None):
    """Per-file reading state for a Kimi wire.jsonl, or None for any other file. A wire file opens with a
    metadata record carrying a protocol version; nothing else does - so a file named wire.jsonl under
    agents/main but holding another dialect's records is NOT enrolled by the path alone.

    `path` is the file actually read (a frozen scratch copy for an enrollment, the live wire for the walk).
    `original_path` is the live wire's own path: the session slug and the main/subagent mark are derived from
    it, never from a scratch location (codex-kxxx-cli's frozen-copy finding; txxxx's split). The transcript
    walk passes neither and reads the live file, so its answers are unchanged."""
    first = first_record(path)
    if first.get("type") != "metadata" or "protocol_version" not in first:
        return None
    identity = original_path if original_path else path
    session, subagent, _agent = wire_identity(identity)
    return {"session": session, "subagent": subagent, "asks": {}}


def _session_index(parts, parts_low):
    """Index of the actual Kimi `session_<id>` directory component in `parts`, or None.

    Never matches ancestor directories above `sessions/` or outside the Kimi session slot.
    """
    if len(parts) >= 4 and parts_low[-1] == "wire.jsonl" and parts_low[-3] == "agents":
        cand = len(parts) - 4
        comp = parts_low[cand]
        return cand if comp.startswith("session_") and len(comp) > len("session_") else None
    if len(parts) >= 3 and parts_low[-2] == "agents":
        cand = len(parts) - 3
        comp = parts_low[cand]
        return cand if comp.startswith("session_") and len(comp) > len("session_") else None
    if len(parts) >= 2 and parts_low[-1] in ("agents", "context.jsonl"):
        cand = len(parts) - 2
        comp = parts_low[cand]
        return cand if comp.startswith("session_") and len(comp) > len("session_") else None
    start = 0
    for idx, p in enumerate(parts_low):
        if p == "sessions":
            start = idx + 1
    for idx in range(len(parts) - 1, start - 1, -1):
        comp = parts_low[idx]
        if comp.startswith("session_") and len(comp) > len("session_"):
            return idx
    return None


def _kimi_rel_and_sessions(norm_path):
    rels, sids = set(), set()
    parts = [p for p in norm_path.split("/") if p and p != "."]
    parts_low = [p.lower() for p in parts]
    idx = _session_index(parts, parts_low)
    if idx is not None:
        comp = parts[idx]
        sid = comp[len("session_"):]
        sids.update((sid, sid.lower(), comp, comp.lower()))
        s_rel = "/".join(parts[idx:])
        rels.update((s_rel, s_rel.lower()))
        if idx >= 1:
            w_rel = "/".join(parts[idx - 1:])
            rels.update((w_rel, w_rel.lower()))
    if len(parts) >= 4 and parts_low[-1] == "wire.jsonl" and parts_low[-3] == "agents":
        a_rel = "/".join(parts[-4:])
        rels.update((a_rel, a_rel.lower()))
    return rels, sids


def _canonical_path_forms(p, base_dir=None):
    forms = set()
    if not isinstance(p, str) or not p.strip():
        return forms
    raw = p.strip().replace("\\", "/")
    norm = os.path.normpath(raw).replace("\\", "/")
    if norm.startswith("./"):
        norm = norm[2:]
    bname = os.path.basename(norm)
    forms.add(raw)
    forms.add(norm)
    forms.add(os.path.normcase(norm).replace("\\", "/"))
    if bname:
        forms.add(bname)
        forms.add(os.path.normcase(bname))
    k_rels, _ = _kimi_rel_and_sessions(norm)
    forms.update(k_rels)
    try:
        ab = os.path.abspath(raw)
        forms.add(ab.replace("\\", "/"))
        forms.add(os.path.normcase(ab).replace("\\", "/"))
        rp = os.path.realpath(ab)
        forms.add(rp.replace("\\", "/"))
        forms.add(os.path.normcase(rp).replace("\\", "/"))
        forms.update(_kimi_rel_and_sessions(rp.replace("\\", "/"))[0])
    except OSError:
        ab, rp = None, None
    if base_dir:
        try:
            b_ab = os.path.abspath(base_dir)
            b_rp = os.path.realpath(b_ab)
            if ab:
                rel_ab = os.path.normpath(os.path.relpath(ab, b_ab)).replace("\\", "/")
                forms.add(rel_ab)
                forms.add(os.path.normcase(rel_ab).replace("\\", "/"))
            if rp:
                rel_rp = os.path.normpath(os.path.relpath(rp, b_rp)).replace("\\", "/")
                forms.add(rel_rp)
                forms.add(os.path.normcase(rel_rp).replace("\\", "/"))
        except (OSError, ValueError):
            pass
    forms.update(f.lower() for f in list(forms))
    return forms


def _pattern_path_forms(raw_pat, base_dir=None):
    forms = set()
    if not isinstance(raw_pat, str) or not raw_pat.strip():
        return forms
    raw = raw_pat.strip().replace("\\", "/")
    norm = os.path.normpath(raw).replace("\\", "/")
    if norm.startswith("./"):
        norm = norm[2:]
    min_slashes = norm.count("/")
    forms.add(raw)
    forms.add(norm)
    forms.add(os.path.normcase(norm).replace("\\", "/"))
    forms.update(f for f in _kimi_rel_and_sessions(norm)[0] if f.count("/") >= min_slashes)
    if "/" not in norm:
        forms.add(os.path.basename(norm))
        forms.add(os.path.normcase(os.path.basename(norm)))
    cand_inputs = [raw_pat.strip()]
    if base_dir and not os.path.isabs(raw_pat.strip()):
        cand_inputs.append(os.path.join(base_dir, raw_pat.strip()))
    b_ab = os.path.abspath(base_dir) if base_dir else None
    b_rp = os.path.realpath(base_dir) if base_dir else None
    for cand_in in cand_inputs:
        try:
            ab = os.path.abspath(cand_in)
            rp = os.path.realpath(cand_in)
            for p_cand in (ab, rp):
                forms.add(p_cand.replace("\\", "/"))
                forms.add(os.path.normcase(p_cand).replace("\\", "/"))
            if b_ab and b_rp:
                for rel_c in (os.path.relpath(ab, b_ab), os.path.relpath(rp, b_rp)):
                    rn = os.path.normpath(rel_c).replace("\\", "/")
                    forms.add(rn)
                    forms.add(os.path.normcase(rn).replace("\\", "/"))
        except (OSError, ValueError):
            pass
    forms.update(f.lower() for f in list(forms))
    return forms


def _is_excluded(path, sessions_dir, session, exclude_files):
    if not exclude_files:
        return False
    # The shared matcher first, so this dialect never disagrees with the loader about the same file and spec
    # (#354). This module's own rules below stay as additional matches.
    try:                       # late: transcript_cards imports this module
        from .transcript_cards import _matches_file_spec
    except ImportError:
        from transcript_cards import _matches_file_spec
    if _matches_file_spec(path, exclude_files, base_dir=sessions_dir, session_id=session or None):
        return True
    # Resolved identities are matched exactly by the shared matcher only, never by this dialect's own rules.
    exclude_files = [e for e in exclude_files if not (isinstance(e, str) and e.startswith("\x00resolved:"))]
    target_forms = _canonical_path_forms(path, sessions_dir)
    _, path_sids = _kimi_rel_and_sessions(os.path.normpath(path).replace("\\", "/"))
    session_dir = "session_" + session if session else ""
    sess_low = session.lower() if session else ""
    sdir_low = session_dir.lower() if session_dir else ""
    target_sids = set(path_sids)
    if session:
        target_sids.update((session, sess_low, session_dir, sdir_low))
    for raw in exclude_files:
        if not isinstance(raw, str):
            continue
        pat = raw.strip().replace("\\", "/")
        if not pat:
            continue
        pat_norm = os.path.normpath(pat).replace("\\", "/")
        if pat_norm.startswith("./"):
            pat_norm = pat_norm[2:]
        pat_low = pat_norm.lower()
        pat_forms = _pattern_path_forms(raw, sessions_dir)
        if target_forms & pat_forms:
            return True
        is_path_typed = "/" in pat or "\\" in raw or pat_low.endswith(".jsonl")
        if not is_path_typed:
            _, pat_sids = _kimi_rel_and_sessions(pat_norm)
            if target_sids and pat_sids and (target_sids & pat_sids):
                return True
            if session and (
                pat_norm in (session, session_dir)
                or pat in (session, session_dir)
                or pat_low in (sess_low, sdir_low)
            ):
                return True
            if len(pat_norm) >= 4:
                if sess_low and sess_low.startswith(pat_low):
                    return True
                if sdir_low and sdir_low.startswith(pat_low):
                    return True
    return False


def select_files(sessions_dir, cache=None, exclude_files=()):
    """(agents/main wire files, wire files seen) under a Kimi home's sessions folder, at any depth. A file's
    first record never changes, so the answer for a path is kept in `cache`. Subagent wires are not picked."""
    cache = cache if cache is not None else {}
    picked, seen = [], 0
    for folder, _, names in sorted(os.walk(sessions_dir)):
        for name in sorted(names):
            if name != "wire.jsonl":
                continue
            seen += 1
            path = os.path.join(folder, name)
            if exclude_files:
                session = ""
                for comp in reversed(os.path.normpath(path).split(os.sep)):
                    if comp.startswith("session_"):
                        session = comp[len("session_"):]
                        break
                if _is_excluded(path, sessions_dir, session, exclude_files):
                    cache.pop(path, None)
                    continue
            if path not in cache:
                cache[path] = file_state(path) is not None and os.path.basename(folder) == "main"
            if cache[path]:
                picked.append(path)
    return picked, seen


def classify(o, state):
    """What one wire record is. Returns (descriptor, drops), where drops is None, a class name, or a list of
    class names, and descriptor is None (nothing to build) or a (kind, payload) pair:

      ("typed",   {"uuid", "time", "text", "has_attachment", "legacy"}) - the operator's own typed turn
      ("goal",    {"uuid", "time", "text"})                             - a /goal objective, verbatim
      ("answers", {"call_id", "uuid", "time", "questions", "answers"})  - a result tied to its call

    An AskUserQuestion call returns (None, None): it is buffered in state["asks"] and its result decides.
    """
    if not isinstance(o, dict):
        return None, "not_a_record"
    rtype = o.get("type")
    session = state.get("session") or ""
    when = _time(o.get("time"))

    if rtype == "context.append_message":
        msg = o.get("message") if isinstance(o.get("message"), dict) else {}
        role = msg.get("role")
        if role != "user":
            return None, "kimi_message_" + str(role)
        if state.get("subagent"):
            return None, "kimi_subagent_prompt"
        origin = msg.get("origin") if isinstance(msg.get("origin"), dict) else {}
        kind = origin.get("kind")
        if kind != "user" and kind is not None:
            if kind == "injection":
                return None, "kimi_injection_" + str(origin.get("variant"))
            if kind in ("system_trigger", "cron_job"):
                return None, "kimi_" + kind
            return None, "kimi_origin_" + str(kind)
        text, image = text_parts(msg.get("content"))
        ref = msg.get("id") if isinstance(msg.get("id"), str) and msg.get("id") else None
        if ref is None:
            # An older wire wrote no message id. The session and the record's time name it:
            # two id-less prompts in one millisecond merge, which has not been seen.
            ref = "kimi:%s:%s" % (session, o.get("time"))
        return ("typed", {"uuid": ref, "time": when, "text": text, "has_attachment": image,
                          "legacy": kind is None}), None

    if rtype == "goal.create":
        # The operator's /goal objective, verbatim and once per goal (the reminder injections that re-render it
        # on every goal turn are machine text). a recorded decision standing as a typed /goal on a Claude seat.
        objective = o.get("objective")
        goal_id = o.get("goalId")
        if not (isinstance(objective, str) and objective.strip()):
            return None, "kimi_goal_empty"
        if not (isinstance(goal_id, str) and goal_id):
            return None, "kimi_goal_no_id"
        return ("goal", {"uuid": goal_id, "time": when, "text": objective}), None

    if rtype == "context.append_loop_event":
        ev = o.get("event") if isinstance(o.get("event"), dict) else {}
        etype = ev.get("type")
        if etype == "tool.call" and ev.get("name") == ASK_TOOL:
            call_id = ev.get("toolCallId")
            args = ev.get("args") if isinstance(ev.get("args"), dict) else {}
            questions = args.get("questions")
            if not (isinstance(questions, list) and isinstance(call_id, str) and call_id.strip()):
                return None, "kimi_question_unreadable"
            state["asks"][call_id] = {"time": o.get("time"), "questions": questions,
                                      "uuid": ev.get("uuid") or call_id}
            return None, None                       # buffered: the result decides Ruling, unanswered, or open
        if etype == "tool.result" and ev.get("toolCallId") in state["asks"]:
            call_id = ev["toolCallId"]
            ask = state["asks"].pop(call_id)
            result = ev.get("result") if isinstance(ev.get("result"), dict) else {}
            output = result.get("output")
            if result.get("isError") or (isinstance(output, str)
                                         and output.startswith(ASK_TOOL + " is disabled")):
                return None, "kimi_question_unanswered"
            try:
                answers = json.loads(output).get("answers") if isinstance(output, str) else None
            except ValueError:
                answers = None
            if not isinstance(answers, dict):
                return None, "kimi_answer_unreadable"
            return ("answers", {"call_id": call_id, "uuid": ev.get("uuid") or ask["uuid"], "time": when,
                                "questions": ask["questions"], "answers": answers}), None
        return None, "kimi_loop_event"

    if rtype == "context.apply_compaction":
        # Model-written (kxxx's rule for a dialect): never the operator's words.
        return None, "kimi_compaction"

    # Everything else in a wire file is harness bookkeeping (llm.*, turn.*, goal.update, cron.*, task.*,
    # token_counting.*, agent.*, subagent.*, config.update, mcp.*, and the like).
    return None, "kimi_bookkeeping"
