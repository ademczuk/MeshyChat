"""antigravity_dialect.py - read Antigravity CLI transcript.jsonl files for transcript_cards.

Scope:
  - Extract operator-typed prompts from USER_EXPLICIT / USER_INPUT records
  - Extract answered questions (ask_question tool calls paired with validated human answers)
  - Distinguish visible assistant dialogue from hidden model thinking blocks
  - Drop all model internal reasoning (thinking scratchpads)
  - Drop all unauthenticated/arbitrary tool outputs (GENERIC)
  - Drop system harness messages, waiter wakeups, and status checks (SYSTEM_MESSAGE)
  - Drop subagent sessions (delegated execution under parent session)
  - Support cross-wake ask persistence and incremental correlation
"""

import json
import os
import re
import stat

ASK_TOOL = "ask_question"
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def preferred_antigravity_path(path):
    """Resolve <dir>/transcript.jsonl to sibling <dir>/transcript_full.jsonl when the full file exists as a
    regular non-symlink, non-reparse file. Accepts `str` and `os.PathLike[str]`; returns `path` unchanged otherwise."""
    orig = path
    if isinstance(path, os.PathLike):
        path = os.fspath(path)
    if not isinstance(path, str) or not path:
        return orig
    if os.path.basename(path) != "transcript.jsonl":
        return orig
    cand = os.path.join(os.path.dirname(path), "transcript_full.jsonl")
    try:
        st = os.lstat(cand)
    except OSError:
        return orig
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return orig
    if getattr(st, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT:
        return orig
    return cand

_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>(.*?)</USER_REQUEST>", re.DOTALL)
_METADATA_RE = re.compile(r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", re.DOTALL)
_SETTINGS_RE = re.compile(r"<USER_SETTINGS_CHANGE>.*?</USER_SETTINGS_CHANGE>", re.DOTALL)


def extract_user_text(content):
    """Extract clean operator text from an Antigravity USER_INPUT content field.
    Strips harness XML envelopes and metadata wrappers."""
    if not isinstance(content, str):
        return ""
    text = content
    # Strip metadata and settings blocks first
    text = _METADATA_RE.sub("", text)
    text = _SETTINGS_RE.sub("", text)
    # If wrapped in <USER_REQUEST>, extract the inner content
    m = _USER_REQUEST_RE.search(text)
    if m:
        text = m.group(1)
    return text.strip()


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


def file_state(path, original_path=None, open_asks=None):
    """Per-file reading state for an Antigravity transcript.jsonl, or None for any other file.
    Extracts conversation ID from directory hierarchy. Identifies subagent transcripts.
    Preserves open asks across capture wakes when provided."""
    identity = original_path if original_path else path
    if isinstance(identity, os.PathLike):
        identity = os.fspath(identity)
    if not isinstance(identity, str):
        return None
    basename = os.path.basename(identity)
    if basename not in ("transcript.jsonl", "transcript_full.jsonl"):
        return None
    first = first_record(path)
    if not isinstance(first, dict) or "step_index" not in first:
        return None

    # Determine conversation_id (layout first, then record conversation_id, then containing folder for card files)
    session = _session_from_path(identity)
    if not session:
        conv = first.get("conversation_id")
        if isinstance(conv, str) and conv.strip():
            session = conv.strip()
        else:
            dirs = _normalized_dir_parts(identity)
            if dirs and _is_session_segment(dirs[-1]):
                session = dirs[-1]
    if not session:
        return None

    # Subagent check: step 0 has source "SYSTEM" and sender=<id>, or "subagents" after the last "brain" segment
    content0 = first.get("content") or ""
    subagent = (first.get("source") == "SYSTEM" and "sender=" in content0) or is_subagent_path(identity)

    asks = dict(open_asks) if open_asks else {}
    return {"session": session, "subagent": subagent, "asks": asks, "last_ask_step": None, "refs": {}}


def card_ref(state, ref, created_at):
    """The record uuid of a card-producing record. The Antigravity CLI reuses a step_index after a restart or a
    rewind (one session holds two different operator prompts at step 18074, ten hours apart), so session plus
    step_index does not name one record. The FIRST record seen at an index keeps the plain ref, so every card id
    already written stays as it was (kxxx); a later record at that index with a different created_at gets
    the created_at appended and becomes a card of its own instead of being folded into the first one. The same
    record read twice (same created_at) keeps the plain ref. `state["refs"]` maps each ref seen to the created_at
    of its first record and is carried across windows and runs by the caller."""
    refs = state.setdefault("refs", {})
    first = refs.get(ref)
    if first is None:
        refs[ref] = created_at
        return ref
    if first == created_at:
        return ref
    return "%s:%s" % (ref, created_at)


_STRUCTURAL_PATH_PARTS = frozenset({"brain", "subagents", ".system_generated", "logs", ".", "..", ""})


def _is_session_segment(seg):
    if not isinstance(seg, str):
        return False
    s = seg.strip()
    if not s or s in _STRUCTURAL_PATH_PARTS:
        return False
    if len(s) == 2 and s[1] == ":":
        return False
    if s.lower().endswith(".jsonl"):
        return False
    return True


def _normalized_dir_parts(identity):
    if isinstance(identity, os.PathLike):
        identity = os.fspath(identity)
    if not isinstance(identity, str) or not identity.strip():
        return []
    parts = [p for p in os.path.normpath(identity.replace("\\", "/")).replace("\\", "/").split("/") if p]
    return parts[:-1] if (parts and parts[-1].lower().endswith(".jsonl")) else parts


def is_subagent_path(identity):
    """True when `"subagents"` appears in the transcript directory hierarchy, scoped strictly after the last
    `"brain"` segment whenever `"brain"` is present (txxxx INFO 1)."""
    dirs = _normalized_dir_parts(identity)
    if "brain" in dirs:
        last_brain = max(i for i, p in enumerate(dirs) if p == "brain")
        dirs = dirs[last_brain + 1:]
    return "subagents" in dirs


def _session_from_path(identity):
    dirs = _normalized_dir_parts(identity)
    if not dirs:
        return ""
    has_brain = "brain" in dirs
    scope = dirs
    if has_brain:
        last_brain = max(i for i, p in enumerate(dirs) if p == "brain")
        scope = dirs[last_brain + 1:]
    if "subagents" in scope:
        last_sub = max(i for i, p in enumerate(scope) if p == "subagents")
        if last_sub + 1 < len(scope) and _is_session_segment(scope[last_sub + 1]):
            return scope[last_sub + 1]
        return ""
    if has_brain:
        if scope and _is_session_segment(scope[0]):
            return scope[0]
        return ""
    for i, p in enumerate(dirs):
        if p == ".system_generated":
            if i >= 1 and _is_session_segment(dirs[i - 1]):
                return dirs[i - 1]
            return ""
    return ""


def _ag_rel_and_sessions(norm_path):
    rels, sids = set(), set()
    parts = [p for p in norm_path.split("/") if p and p != "."]
    parts_low = [p.lower() for p in parts]
    for i in range(len(parts) - 3):
        if parts_low[i + 1] == ".system_generated" and parts_low[i + 2] == "logs":
            cid = parts[i]
            sids.update((cid, cid.lower()))
            ag_rel = "/".join(parts[i:])
            rels.update((ag_rel, ag_rel.lower()))
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
    ag_rels, _ = _ag_rel_and_sessions(norm)
    forms.update(ag_rels)
    try:
        ab = os.path.abspath(raw)
        forms.add(ab.replace("\\", "/"))
        forms.add(os.path.normcase(ab).replace("\\", "/"))
        rp = os.path.realpath(ab)
        forms.add(rp.replace("\\", "/"))
        forms.add(os.path.normcase(rp).replace("\\", "/"))
        forms.update(_ag_rel_and_sessions(rp.replace("\\", "/"))[0])
    except OSError:
        ab, rp = None, None
    if base_dir:
        try:
            b_ab = os.path.abspath(base_dir)
            b_rp = os.path.realpath(base_dir)
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
    forms.update(f for f in _ag_rel_and_sessions(norm)[0] if f.count("/") >= min_slashes)
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


def _is_excluded(path, brain_dir, session, exclude_files):
    if not exclude_files:
        return False
    # The shared matcher first, so this dialect never disagrees with the loader about the same file and spec
    # (#354). This module's own rules below stay as additional matches.
    try:                       # late: transcript_cards imports this module
        from .transcript_cards import _matches_file_spec
    except ImportError:
        from transcript_cards import _matches_file_spec
    if _matches_file_spec(path, exclude_files, base_dir=brain_dir, session_id=session or None):
        return True
    # Resolved identities are matched exactly by the shared matcher only, never by this dialect's own rules.
    exclude_files = [e for e in exclude_files if not (isinstance(e, str) and e.startswith("\x00resolved:"))]
    target_forms = _canonical_path_forms(path, brain_dir)
    _, path_sids = _ag_rel_and_sessions(os.path.normpath(path).replace("\\", "/"))
    sess_low = session.lower() if session else ""
    target_sids = set(path_sids)
    if session:
        target_sids.update((session, sess_low))
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
        pat_forms = _pattern_path_forms(raw, brain_dir)
        if target_forms & pat_forms:
            return True
        is_path_typed = "/" in pat or "\\" in raw or pat_low.endswith(".jsonl")
        if not is_path_typed:
            _, pat_sids = _ag_rel_and_sessions(pat_norm)
            if target_sids and pat_sids and (target_sids & pat_sids):
                return True
            if session and (pat_norm == session or pat == session or pat_low == sess_low):
                return True
            if len(pat_norm) >= 4 and sess_low and sess_low.startswith(pat_low):
                return True
    return False


def select_files(brain_dir, cache=None, exclude_files=()):
    """(transcript.jsonl files, total files seen) under an Antigravity brain folder.
    Subagent transcripts are excluded."""
    cache = cache if cache is not None else {}
    picked, seen = [], 0
    for folder, _, names in sorted(os.walk(brain_dir)):
        for name in sorted(names):
            if name != "transcript.jsonl":
                continue
            seen += 1
            path = os.path.join(folder, name)
            if exclude_files:
                sess = _session_from_path(path)
                if not sess:
                    st_ex = file_state(path)
                    sess = (st_ex.get("session") or "") if st_ex else ""
                if _is_excluded(path, brain_dir, sess, exclude_files):
                    cache.pop(path, None)
                    continue
            if path not in cache:
                st = file_state(path)
                cache[path] = st is not None and not st.get("subagent")
            if cache[path]:
                picked.append(path)
    return picked, seen


def _is_valid_modal_answer(val, valid_opts, multi_select=False):
    """True when `val` selects at least one declared option from `valid_opts`.
    For single-select questions (`multi_select=False`), requires an exact single option match.
    For multi-select questions (`multi_select=True`), accepts an exact single option or a
    comma-joined multi-option selection (including options that themselves contain commas,
    plus optional trailing write-in text after the selected options), while rejecting any
    value that matches none of the options."""
    opts = [o for o in valid_opts if o]
    if val in opts:
        return True
    if not multi_select:
        return False
    by_len = sorted(opts, key=len, reverse=True)
    rest = val
    matched = 0
    while rest:
        if rest in opts:
            matched += 1
            break
        hit = next((o for o in by_len if rest.startswith(o + ", ")), None)
        if hit is None:
            break
        matched += 1
        rest = rest[len(hit) + 2:].strip()
    return matched >= 1


def classify(o, state):
    """What one Antigravity transcript record is for decision card extraction.
    Returns (descriptor, drops), where drops is None or a class name, and descriptor is:

      ("typed",   {"uuid", "time", "text", "has_attachment", "legacy"})
      ("goal",    {"uuid", "time", "text"})
      ("answers", {"call_id", "uuid", "time", "questions", "answers"})
    """
    if not isinstance(o, dict):
        return None, "not_a_record"

    step_index = o.get("step_index")
    source = o.get("source")
    rtype = o.get("type")
    created_at = o.get("created_at") or ""
    session = state.get("session") or ""
    if not session:
        conv = o.get("conversation_id")
        if isinstance(conv, str) and conv.strip():
            session = conv.strip()
            state["session"] = session
    ref = "antigravity:%s:%s" % (session, step_index if step_index is not None else "")

    if state.get("subagent"):
        return None, "antigravity_subagent_record"

    if source == "SYSTEM" or rtype == "SYSTEM_MESSAGE":
        return None, "antigravity_system_message"

    if source == "USER_EXPLICIT" and rtype == "USER_INPUT":
        text = extract_user_text(o.get("content"))
        if not text:
            return None, "antigravity_empty_prompt"

        # Check if this answers a buffered ask_question via strict exact match
        if state.get("asks"):
            for call_id, ask in list(state["asks"].items()):
                answers = {}
                for q in ask.get("questions") or []:
                    q_text = q.get("question") or q.get("title") or ""
                    chosen = None
                    for opt in q.get("options") or []:
                        opt_text = opt if isinstance(opt, str) else opt.get("label", "")
                        # Strict exact match only: never infer an option from a substring!
                        # e.g., 'Do not choose PostgreSQL; use SQLite.' must NOT match 'PostgreSQL'.
                        if text == opt_text or text == opt_text.strip():
                            chosen = opt_text
                            break
                    if chosen is not None:
                        answers[q_text] = chosen
                if answers and len(answers) == len(ask.get("questions") or []):
                    del state["asks"][call_id]
                    return ("answers", {"call_id": call_id, "uuid": card_ref(state, ref, created_at),
                                        "time": created_at, "questions": ask["questions"],
                                        "answers": answers}), None

        # Free-text user prompt (or prompt that did not select an option) is preserved as typed candidate
        if text.startswith("/goal"):
            return ("goal", {"uuid": card_ref(state, ref, created_at), "time": created_at, "text": text}), None
        return ("typed", {"uuid": card_ref(state, ref, created_at), "time": created_at, "text": text,
                          "has_attachment": False, "legacy": False}), None

    if rtype == "PLANNER_RESPONSE":
        # Check tool_calls for ask_question
        tcalls = o.get("tool_calls")
        if isinstance(tcalls, list):
            for tc in tcalls:
                if isinstance(tc, dict) and tc.get("name") == ASK_TOOL:
                    args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
                    raw_q = args.get("questions")
                    if isinstance(raw_q, str):
                        try:
                            raw_q = json.loads(raw_q)
                        except (ValueError, TypeError):
                            raw_q = None
                    if isinstance(raw_q, list) and raw_q:
                        call_id = "call-%s-%s" % (session, step_index)
                        state["asks"][call_id] = {
                            "step_index": step_index,
                            "call_id": call_id,
                            "uuid": ref,
                            "time": created_at,
                            "questions": raw_q
                        }
                        state["last_ask_step"] = step_index
                        return None, None
        return None, "antigravity_planner_response"

    if rtype == "GENERIC":
        # Tool execution result
        content = o.get("content") or ""
        # Strictly verify native correlation: must be the immediate successor of ask_question
        # and carry the native UI modal answer format (A1: <choice>, A2: <choice>)
        if state.get("asks"):
            for call_id, ask in list(state["asks"].items()):
                if step_index is not None and ask.get("step_index") is not None:
                    if step_index != ask["step_index"] + 1:
                        continue
                answers = {}
                lines = content.splitlines() if isinstance(content, str) else []
                for line in lines:
                    line = line.strip()
                    m = re.match(r"^A(\d+):\s*(.+)$", line)
                    if m:
                        idx = int(m.group(1)) - 1
                        val = m.group(2).strip()
                        questions = ask.get("questions") or []
                        if 0 <= idx < len(questions):
                            q = questions[idx]
                            q_text = q.get("question") or q.get("title") or ""
                            valid_opts = [opt if isinstance(opt, str) else opt.get("label", "")
                                          for opt in q.get("options") or []]
                            multi_select = bool(q.get("is_multi_select") or q.get("multiSelect"))
                            if _is_valid_modal_answer(val, valid_opts, multi_select):
                                answers[q_text] = val
                if answers and len(answers) == len(ask.get("questions") or []):
                    del state["asks"][call_id]
                    return ("answers", {"call_id": call_id, "uuid": card_ref(state, ref, created_at),
                                        "time": created_at, "questions": ask["questions"],
                                        "answers": answers}), None
        return None, "antigravity_tool_result"

    return None, "antigravity_bookkeeping"


def extract_context_event(o, state):
    """Extract a sanitized dialogue event for automatic full-context recovery.
    Maps visible assistant dialogue detail, operator instructions, and unfinished tasks
    while stripping thinking blocks, system harness messages, and raw tool noise.
    """
    if not isinstance(o, dict):
        return None, "not_a_record"
    if state.get("subagent"):
        return None, "subagent_record"

    source = o.get("source")
    rtype = o.get("type")
    created_at = o.get("created_at") or ""
    step_index = o.get("step_index")
    session = state.get("session") or ""
    if not session:
        conv = o.get("conversation_id")
        if isinstance(conv, str) and conv.strip():
            session = conv.strip()
            state["session"] = session
    ref = "antigravity:%s:%s" % (session, step_index if step_index is not None else "")

    if source == "SYSTEM" or rtype == "SYSTEM_MESSAGE":
        return None, "system_message"

    if source == "USER_EXPLICIT" and rtype == "USER_INPUT":
        text = extract_user_text(o.get("content"))
        if not text:
            return None, "empty_prompt"
        return {
            "type": "user_turn",
            "ref": ref,
            "session": session,
            "time": created_at,
            "text": text,
            "is_goal": text.startswith("/goal"),
        }, None

    if rtype == "PLANNER_RESPONSE":
        content = o.get("content")
        if isinstance(content, str) and content.strip():
            # Visible assistant text shown to operator; thinking scratchpad is completely excluded
            return {
                "type": "assistant_turn",
                "ref": ref,
                "session": session,
                "time": created_at,
                "text": content.strip(),
            }, None
        return None, "planner_without_content"

    if rtype == "GENERIC":
        return None, "tool_result"

    return None, "bookkeeping"
