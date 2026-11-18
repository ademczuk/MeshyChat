"""Merger race preflight and pre-review Antibody / FIX-FIRST surfacing CLI."""
from __future__ import annotations

from datetime import datetime, timezone
import argparse, json, math, os, re, subprocess, sys, time
from pathlib import Path
from typing import Any, Iterable

from . import cards, node, sendguard, wire

_HEX40_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_MSG_KEY_ARG_RE = re.compile(r"^msg/\S+$")
_HEX_TOKEN_RE = re.compile(r"(?<![\w-])([0-9a-fA-F]{7,40})(?![\w-])")
_HIST_PREFIX_RE = re.compile(r"\b(?:old|prior|prev(?:ious)?|superseded|stale|former|initial|original|replaced|reverted|was|from|before)(?:\s+(?:head|commit|sha|oid))?\s*[:=-]?\s*(?:https?://\S*/commit/)?$", re.IGNORECASE)
_HIST_SUFFIX_RE = re.compile(r"^\s*(?:->|=>|(?:was\s+)?(?:replaced|superseded)\b)", re.IGNORECASE)
_NON_HEAD_PREFIX_RE = re.compile(r"\b(?:origin/main|main|trunk|master|base(?:\s+(?:head|commit|sha|oid))?|merge-base|onto|parent|on\s+top\s+of|merged(?:\s+#?\d+)?\s+as|squash(?:-merged)?\s+as|inspected[_ -]?sha|tested[_ -]?sha|sha256|tree)\s*(?:[:=@-]|is)?\s*(?:https?://\S*/commit/)?$", re.IGNORECASE)
_SHORT_SHA_CUE_RE = re.compile(r"\b(?:head|commit|sha|oid|pin|pinned)\b", re.IGNORECASE)
_FIX_FIRST_RE = re.compile(r"\bfix[- ]first\b", re.IGNORECASE)
_PR_NUM_RE = re.compile(r"#(\d+)\b|\bPR\s*#?(\d+)\b|/pull/(\d+)\b", re.IGNORECASE)
_SENT_RE = re.compile(r"(?:[^!?;.\n]|\.(?!\s|$))+(?:[!?;\n]+|\.(?=\s|$)|$)")
_CONTRAST_SPLIT_RE = re.compile(r"\b(?:but|however|though|although|while|whereas)\b", re.IGNORECASE)
_SUBCLAUSE_SPLIT_RE = re.compile(r",|\b(?:and|then)\b(?=\s+(?:@[a-z0-9]|[a-z0-9][a-z0-9_.-]*\s+(?:is|are|was|were|will|has|have|am|did|does|do|won'?t|isn'?t|can|should)\b|i(?:'ll|'m|'ve)?\b))", re.IGNORECASE)
_LEADING_COND_RE = re.compile(r"^\s*(?:if|unless|whether|when|once|until)\b", re.IGNORECASE)
_COND_ANY_RE = re.compile(r"\b(?:if|unless|whether|when|once|until)\b", re.IGNORECASE)
_NEG_TOKEN_RE = re.compile(r"\b(?:not|never|no|neither|nor|isn'?t|aren'?t|wasn'?t|weren'?t|hasn'?t|haven'?t|won'?t|don'?t|doesn'?t|didn'?t|can'?t|cannot|shouldn'?t|mustn'?t)\b", re.IGNORECASE)
_PRE_MERGE_GUARD_RE = re.compile(
    r"(?:^\s*(?:after|before)\b|\b(?:after|before)\s+(?:(?:we|i|you|they|it|the\s+pr|pr\s*#?\d+|#\d+|[a-z0-9_.-]+)\s+)?(?:(?:is|are|was|were|gets?|got|be|been|being|has(?:\s+\w+){0,2}\s+been|have(?:\s+\w+){0,2}\s+been|will\s+be|can\s+be)\s+)?$"
    r"|\b(?:not|never|neither|nor|isn'?t|aren'?t|wasn'?t|weren'?t|hasn'?t|haven'?t|won'?t|don'?t|doesn'?t|didn'?t|can'?t|cannot|shouldn'?t|mustn'?t)\s+(?:(?:yet|now|currently|actually|still|ever|even|just|already|cleanly|going\s+to|planning\s+to|ready\s+to|able\s+to|be|been|being|to|a|an|the|any|sole|only|pinned|non-author|squash)\s+)*$"
    r"|\b(?:no|without)\s+(?:(?:further|actual|pinned|non-author|clean|new|active|squash)\s+)*$"
    r"|\b(?:hold(?:ing)?|paus(?:e|ed|ing)|defer(?:red|ring)?|cancel(?:led|ed|ling|ing)?|retract(?:ed|ing)?|withdr(?:ew|awn|awing|aw)|drop(?:ped|ping)?|abort(?:ed|ing)?|halt(?:ed|ing)?)\s+(?:(?:on|off|my|our|the|this|any|#\d+|pr\s*#?\d+)\s+)*$"
    r"|\b(?:nobody|no\s+one|none(?:\s+of\s+\w+)?|neither(?:\s+of\s+\w+)?)\s+(?:(?:is|was|has|will|can|should)\s+(?:now\s+|yet\s+)?)?$)",
    re.IGNORECASE,
)
_POST_MERGE_GUARD_RE = re.compile(r"^\s*(?:(?:claim|slot|lock|turn)\s+)?(?:(?:(?:for|of|on)\s+)?(?:#\d+|pr\s*#?\d+|\S+/pull/\d+)\s+)?(?:(?:is|are|was|were|be|been|has|have)\s+(?:not|never|on\s+hold|held|paused|deferred|cancel(?:led|ed)|retract(?:ed)?|withdr(?:ew|awn)|halted|aborted|blocked|off\b)|(?:isn'?t|aren'?t|wasn'?t|weren'?t|won'?t|can'?t|cannot|shouldn'?t|mustn'?t)\b)", re.IGNORECASE)
_AFFIRM_CANCEL_RE = re.compile(r"\b(?:(?:i|we)(?:'ve|'m|'re|'ll|\s+(?:have|am|are|will|just))?\s+)?(cancel(?:led|ed|ling|ing)?|retract(?:ed|ing)?|withdr(?:ew|awn|awing|aw)|drop(?:ped|ping)?|releas(?:ed|ing)?|yield(?:ed|ing)?)\s+(?:(?:my|our|the|this)\s+(?:(?:#\d+|pr\s*#?\d+)\s+)?(?:(?:squash[- ]?)?(?:merge|merging|merger|squash)(?:\s+(?:claim|slot|lock|turn))?|claim\b)|(?:(?:#\d+|pr\s*#?\d+)\s+)?(?:squash[- ]?)?(?:merge|merger|squash)\s+(?:claim|slot|lock|turn)\b)", re.IGNORECASE)
_NEG_FIRST_MERGE_RE = re.compile(r"\b(?:(?:i\s+am|i'?m|we\s+are|we'?re|i\s+will|we\s+will|i\s+do|we\s+do)\s+(?:no\s+longer|not\s+(?!yet\b))|(?:i|we)\s+(?:won'?t|don'?t|aren'?t|can'?t|cannot|never|no\s+longer)\s+)(?:going\s+to\s+|be\s+)?(?:squash[- ]?merg(?:e|ing)|merg(?:e|ing)|squash(?:ing)?)\b", re.IGNORECASE)
_TECH_MERGE_RE = re.compile(r"\b(?:git\s+merge[- ]\w+|merge-(?:tree|base)|merge\s+(?:conflict|conflicts|commit(?!\s+(?:https?://\S*/commit/)?[0-9a-fA-F]{7,40}\b)|commits\b(?!\s+[0-9a-fA-F]{7,40}\b)|gate|gates|queue|train|window|check|checks|preflight|guard)|merged\s+(?:tree|trees|with\s+(?:main|origin/main|trunk|base)))\b", re.IGNORECASE)
_TECH_VERB_PRE_RE = re.compile(r"\b(?:will|i'?ll|we'?ll|am|is|are|i'?m|we'?re|can|could|would|should|shall|may|might|must|please|pls|to|let'?s)\s+$", re.IGNORECASE)
_ANY_MERGE_WORD_RE = re.compile(r"\b(?:squash[- ]?merg(?:e|ed|ing)|merg(?:e|ed|ing)|merges|merger|squash(?:ed|ing|es)?)\b", re.IGNORECASE)
_PASSIVE_MERGED_RE = re.compile(r"\b(?:is|was|has(?:\s+\w+){0,2}\s+been|have(?:\s+\w+){0,2}\s+been)\s+(?:already\s+|now\s+|just\s+|cleanly\s+)?(?:squash[- ]?)?(?:merged|squashed)\b", re.IGNORECASE)
_ACTIVE_MERGED_RE = re.compile(r"\b(?:already\s+|just\s+)?(?:squash[- ]?)?(?:merged|squashed)\b", re.IGNORECASE)
_NON_CLAIMANT_ACTORS = frozenset({"i", "we", "you", "they", "he", "she", "it", "who", "whoever", "anyone", "anybody", "someone", "somebody", "nobody", "none", "no", "not", "never", "either", "both", "one", "this", "that", "which", "what", "when", "if", "once", "until", "unless", "whether", "pr", "pull", "branch", "head", "gate", "ci", "codex", "merger", "please", "ack", "ok", "okay"})
_GENERIC_DIRS = frozenset({"tools", "tests", "test", "apps", "app", "src", "lib", "bin", "scripts", "docs", "doc", "config", "configs", "common", "core", "utils", "helpers", "ci", "github", ".github", "workflows"})
_GENERIC_BASENAMES = frozenset({"__init__.py", "__main__.py", "cli.py", "main.py", "views.py", "models.py", "urls.py", "admin.py", "apps.py", "settings.py", "conftest.py", "utils.py", "helpers.py", "errors.py", "types.py", "constants.py", "config.py", "index.ts", "index.js", "readme.md", "changelog.md"})
_GENERIC_STEMS = frozenset({"__init__", "__main__", "cli", "main", "views", "models", "urls", "admin", "apps", "settings", "conftest", "utils", "helpers", "errors", "types", "constants", "config", "index", "readme", "changelog", "test", "tests", "node", "store", "wire", "files", "cards"})


def _parse_ts(val: Any) -> float | None:
    if isinstance(val, datetime):
        return (val if val.tzinfo else val.replace(tzinfo=timezone.utc)).timestamp()
    if isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(val):
        return float(val) / 1000.0 if val > 1e11 else float(val)
    if isinstance(val, str) and (raw := val.strip()):
        try:
            dt = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
        except ValueError:
            return None
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    return None


def _effective_ts(obj: Any) -> float | None:
    if not isinstance(obj, dict):
        return None
    vals = [t for src in (obj, obj.get("value")) if isinstance(src, dict)
            for f in ("edited_at", "updated_at", "recorded_at", "created_at", "time") if (t := _parse_ts(src.get(f))) is not None]
    return max(vals) if vals else None


def _pr_nums_in(text: str) -> set[int]:
    return {int(next(g for g in m.groups() if g)) for m in _PR_NUM_RE.finditer(text)}


def _scope_pr_sections(clean: str, pr: int) -> str:
    kept, pending, active_prs = [], [], None
    for seg in re.split(r"(?:[!?;\n]+|\.(?=\s|$))+", clean):
        if nums := _pr_nums_in(seg):
            if active_prs is None and pr in nums:
                kept.extend(pending)
            pending, active_prs = [], nums
            if pr in active_prs:
                kept.append(seg)
        elif active_prs is None:
            pending.append(seg)
        elif pr in active_prs:
            kept.append(seg)
    return "\n".join(kept or pending) if (kept or active_prs is None) else clean


def _extract_head_shas(clean: str) -> tuple[list[str], list[str], list[str]]:
    scrubbed, cur_full, short_raw, hist_shas = sendguard._MSG_OR_CARD_KEY_RE.sub(" ", clean), [], [], []
    add = lambda dst, s: dst.append(s) if s not in dst else None
    for m in _HEX_TOKEN_RE.finditer(scrubbed):
        sha = m.group(1).lower()
        clause_pre = re.split(r"(?:[!?;,\n]+|\.(?=\s|$))", scrubbed[: m.start()])[-1]
        if _NON_HEAD_PREFIX_RE.search(clause_pre[-60:]):
            continue
        local_pre = re.split(r"\b[0-9a-fA-F]{7,40}\b", clause_pre)[-1][-44:]
        if _HIST_PREFIX_RE.search(local_pre) or _HIST_SUFFIX_RE.search(scrubbed[m.end() : m.end() + 28]):
            add(hist_shas, sha)
        elif len(sha) == 40:
            add(cur_full, sha)
        elif (cued := bool(_SHORT_SHA_CUE_RE.search(clause_pre[-36:]))) or re.search(r"[a-f]", sha):
            short_raw.append((sha, cued))
    cur_short = [s for s, cued in short_raw if (cued or not cur_full) and not (len(cur_full) == 1 and cur_full[0].startswith(s))]
    return cur_full, list(dict.fromkeys(cur_short)), [s for s in hist_shas if s not in cur_full]


def _inspect_merge_prose(
    scope_text: str, *, pr: int, sender: str, seat_folded: str, has_merger_decl: bool,
    ts: float | None, key: str, active_claims: dict[str, tuple[float | None, str, str, str]], manual_checks: list[tuple[str, str]],
) -> str | None:
    """Fail-closed prose classifier: guarded/question/non-object merge mentions record check_by_hand."""
    pr_item, any_pr = rf"(?:#{pr}(?!\d)|\bPR\s*#?{pr}(?!\d)|\S*/pull/{pr}(?!\d))", r"(?:#\d+\b|\bPR\s*#?\d+\b|\S*/pull/\d+\b)"
    pr_obj = rf"(?:{any_pr}\s*(?:,|&|\band\b)\s*)*{pr_item}"
    pr_tok_re, topical_re = re.compile(rf"(?:{pr_item}|\b{pr}(?!\d))", re.I), re.compile(rf"^\s*(?:{pr_item}|\bon\s+{pr_item})\s*[:,]", re.I)
    aux_adv = r"(?:(?:now|just|also|cleanly|shortly|soon|immediately|next|going\s+to|go\s+ahead\s+and|be)\s+)*"
    v_merge = r"(?:squash[- ]?merg(?:e|ing)|squash\s+and\s+merge|merg(?:e|ing)|squash(?:ing)?)"
    decl_ask_re = re.compile(rf"^\s*(?:@[a-z0-9_.-]+\s*[,:-]?\s*)?(?:(?:please\s+(?:(?:re-?)?review\s+and\s+|verify\s+and\s+)?|(?:ready|good|ok|okay|cleared)\s+to\s+)(?:(?:run|execute|use)\s+)?(?:gh\s+pr\s+merge|squash[- ]?merge|squash\s+and\s+merge|merge|squash)\s+(?:{pr_obj}|\b{pr}\b))\b", re.I)
    sender_folded = sender.casefold()
    add_manual = lambda: manual_checks.append((key, sender or "unknown")) if sender_folded != seat_folded and all(k != key for k, _ in manual_checks) else None
    prose = "\n".join(ln for ln in scope_text.splitlines() if not sendguard._VERDICT_LINE_RE.match(ln))
    for sent_m in _SENT_RE.finditer(prose):
        if not (sent := sent_m.group(0).strip()):
            continue
        is_question, sent_topical = sent.endswith("?"), bool(topical_re.match(sent))
        for cclause in _CONTRAST_SPLIT_RE.split(sent):
            leading_cond = False
            for sub in _SUBCLAUSE_SPLIT_RE.split(cclause):
                if not (sub_s := sub.strip()):
                    continue
                leading_cond = leading_cond or bool(_LEADING_COND_RE.match(sub_s))
                if (sub_prs := _pr_nums_in(sub_s)) and pr not in sub_prs:
                    continue
                if sender_folded and sender_folded in active_claims and not is_question and not leading_cond and not _COND_ANY_RE.search(sub_s) and (
                    ((cm := _AFFIRM_CANCEL_RE.search(sub_s)) and not _NEG_TOKEN_RE.search(sub_s[: cm.start(1)]))
                    or ((nm := _NEG_FIRST_MERGE_RE.search(sub_s)) and not _NEG_TOKEN_RE.search(sub_s[: nm.start()]))
                ) and ts is not None and (prev_ts := active_claims[sender_folded][0]) is not None and ts > prev_ts:
                    active_claims.pop(sender_folded, None)
                sub_clean = _TECH_MERGE_RE.sub(lambda m: m.group(0) if _TECH_VERB_PRE_RE.search(sub_s[: m.start()]) else " ", sub_s)
                if not (mw := _ANY_MERGE_WORD_RE.search(sub_clean)) or re.fullmatch(r"(?:(?:#\d+|\bPR\s*#?\d+)\s*)?merger\s*:\s*[^;\n]+", sub_clean.strip(), re.I):
                    continue
                if is_question or leading_cond or _COND_ANY_RE.search(sub_clean) or _PRE_MERGE_GUARD_RE.search(sub_clean[: mw.start()]) or _POST_MERGE_GUARD_RE.search(sub_clean[mw.end() :]):
                    add_manual()
                    continue
                if _PASSIVE_MERGED_RE.search(sub_clean) or ((am := _ACTIVE_MERGED_RE.search(sub_clean)) and (
                    pr_tok_re.search(sub_clean[am.end() :]) or re.search(r"\b(?:i|we|[a-z0-9][a-z0-9_.-]{0,63})\s+$", sub_clean[: am.start()], re.I)
                )):
                    return "already_merged"
                topical = sent_topical or bool(topical_re.match(sub_clean))
                cmt_mid = r"(?:(?:the\s+)?commit(?:\s+(?:https?://\S*/commit/)?[0-9a-fA-F]{7,40})?\s+(?:for|on|of|in)\s+)?"
                obj_tail = rf"(?:\s+{cmt_mid}{pr_obj}|\s+it\b)" if topical else rf"\s+{cmt_mid}{pr_obj}"
                claim_v = rf"(?:{v_merge}{obj_tail}|(?:claim(?:ed|ing)?|tak(?:e|ing))\s+(?:the\s+)?(?:squash[- ]?)?merge(?:\s+(?:slot|lock|turn))?(?:\s+(?:for|on|of)\s+{pr_obj}{'|\\b' if topical else ''}))"
                actor_re = re.compile(rf"(?<![\w-])@?([a-z0-9][a-z0-9_.-]{{0,63}})(?:\s*:\s*{aux_adv}{claim_v}|\s+(?:(?:will|is|am|are|has|have)\s+{aux_adv}{claim_v}|is\s+(?:now\s+)?the\s+(?:sole\s+|only\s+)?merger(?:\s+(?:for|on|of)\s+{pr_obj}|\b)))", re.I)
                first_re = re.compile(rf"\b(?:i'?ll|i\s+will|i\s+am|i'?m|i\s+have|i'?ve|we'?ll|we\s+will|we\s+are|we'?re|we\s+have|we'?ve)\s+{aux_adv}{claim_v}", re.I)
                bare_re = re.compile(rf"(?:^\s*|(?:ack|ok|okay|on\s+it|{any_pr})\s*[,:-]?\s*){aux_adv}(?:(?:squash[- ]?merging|merging|squashing){obj_tail}|will\s+{aux_adv}{claim_v}|taking\s+(?:the\s+)?(?:squash[- ]?)?merge(?:\s+(?:for|on|of)\s+{pr_obj}{'|\\b' if topical else ''}))", re.I)
                actors = [m.group(1).strip() for m in actor_re.finditer(sub_clean) if m.group(1).strip().casefold() not in _NON_CLAIMANT_ACTORS and not m.group(1).strip().isdigit()]
                claimed_any = bool(actors)
                for actor in actors:
                    if actor.casefold() != seat_folded:
                        active_claims[actor.casefold()] = (ts, key, actor, sender)
                if not (actors and re.search(r"^[#@]?[a-z0-9][a-z0-9_.-]{0,63}\s*:", sub_clean, re.I)) and sender and (first_re.search(sub_clean) or bare_re.search(sub_clean)):
                    claimed_any = True
                    if sender_folded != seat_folded:
                        active_claims[sender_folded] = (ts, key, sender, sender)
                if not claimed_any and not (has_merger_decl and decl_ask_re.search(sub_clean)):
                    add_manual()
    return None


def _memo_thread_and_reply(val: Any) -> tuple[str | None, str | None]:
    if not isinstance(val, dict):
        return None, None
    c_th, l_th, c_rp, l_rp = val.get("thread_id"), val.get("thread"), val.get("in_reply_to"), val.get("reply_to")
    raw_th = c_th.strip() if isinstance(c_th, str) and c_th.strip() else (l_th.strip() if isinstance(l_th, str) and l_th.strip() else None)
    rp = c_rp.strip() if isinstance(c_rp, str) and c_rp.strip().startswith("msg/") else (l_rp.strip() if isinstance(l_rp, str) and l_rp.strip().startswith("msg/") else None)
    return raw_th, rp


def _supplement_unprojected_hits(
    state_path: Path, state: dict[str, Any], *, thread_id: str, cutoff: float, raw_hits: list[Any],
) -> tuple[list[dict[str, Any]] | None, str | None]:
    target, seen_keys, memos, cursor = thread_id.strip(), {h["key"] for h in raw_hits if isinstance(h, dict) and isinstance(h.get("key"), str)}, [], None
    since_iso = datetime.fromtimestamp(max(0.0, cutoff - 3600.0), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    for _ in range(50):
        try:
            res = node.invoke(state_path, state, skill="memory.query", args={"namespace": wire.MESH_NAMESPACE, "key_prefix": "msg/", "since": since_iso, "limit": 100, **({"cursor": cursor} if cursor else {})})
        except Exception as exc:
            return None, f"{type(exc).__name__}: {exc}"
        if not isinstance(res, dict) or not node.succeeded(res) or not isinstance(res["result"].get("results"), list):
            return None, "query_unverified_or_failed"
        memos.extend(m for m in res["result"]["results"] if isinstance(m, dict))
        if not (cursor := res["result"].get("next_cursor")):
            break
    else:
        return None, "query_window_truncated"
    by_key = {m["key"]: m for m in memos if isinstance(m.get("key"), str)}
    val_of = lambda doc: doc.get("_value") if isinstance(doc.get("_value"), dict) else (doc.get("value") if isinstance(doc.get("value"), dict) else doc)
    low_cache: dict[str, tuple[str | None, str | None]] = {}

    def _resolve_lowest_thread(k: str, visited: frozenset[str]) -> tuple[str | None, str | None]:
        if k in low_cache:
            return low_cache[k]
        if k in visited or len(visited) > 32:
            return (None, None) if k in visited else (None, "reply_chain_too_deep")
        hm, anc_head = by_key.get(k), None
        if hm is not None and type(hm.get("version")) is int and hm["version"] >= 1:
            head_ver = hm["version"]
        else:
            try:
                anc_head = node.read(state_path, state, key=k, include_value=True)
            except node.ReadFailed as exc:
                if exc.error.get("error") == node.E_READ_REFUSED and exc.error.get("error_code") in ("E_UNKNOWN_KEY", "MEMORY_REDACTED"):
                    low_cache[k] = (None, None)
                    return None, None
                return None, f"ancestor_read_failed:{exc.error.get('error_code') or exc.error.get('error', 'read_failed')}"
            except Exception as exc:
                return None, f"ancestor_read_failed:{type(exc).__name__}"
            if not isinstance(anc_head, dict) or anc_head.get("signature_verified") is not True or type(ahv := anc_head.get("version")) is not int or ahv < 1:
                return None, "ancestor_unverified"
            head_ver = ahv
        if head_ver > 32:
            return None, "ancestor_version_cap"
        next_vis = visited | {k}
        for v in range(1, head_ver + 1):
            if v == head_ver:
                v_val = val_of(anc_head) if anc_head is not None else hm.get("value")
            else:
                try:
                    anc_v = node.read(state_path, state, key=k, version=v, include_value=True)
                except node.ReadFailed as exc:
                    if exc.error.get("error") == node.E_READ_REFUSED and exc.error.get("error_code") in ("MEMORY_REDACTED", "E_UNKNOWN_KEY"):
                        continue
                    return None, f"ancestor_version_read_failed:{exc.error.get('error_code') or exc.error.get('error', 'read_failed')}"
                except Exception as exc:
                    return None, f"ancestor_version_read_failed:{type(exc).__name__}"
                if not isinstance(anc_v, dict) or anc_v.get("signature_verified") is not True:
                    return None, "ancestor_unverified"
                v_val = val_of(anc_v)
            raw_th, rp = _memo_thread_and_reply(v_val)
            if raw_th is not None:
                if not raw_th.startswith("redacted/"):
                    low_cache[k] = (raw_th, None)
                    return raw_th, None
                continue
            if rp is not None:
                inh_th, r_err = _resolve_lowest_thread(rp, next_vis)
                if r_err is not None or inh_th is not None:
                    if r_err is None:
                        low_cache[k] = (inh_th, None)
                    return inh_th, r_err
        low_cache[k] = (None, None)
        return None, None

    extra: list[dict[str, Any]] = []
    for m in reversed(memos):
        if not isinstance(key := m.get("key"), str) or not key.startswith("msg/"):
            continue
        raw_th, rp = _memo_thread_and_reply(m.get("value"))
        if raw_th is not None:
            eff_th = None if raw_th.startswith("redacted/") else raw_th
        elif rp is not None:
            eff_th, r_err = _resolve_lowest_thread(rp, frozenset({key}))
            if r_err is not None:
                return None, r_err
        else:
            eff_th = None
        if eff_th == target and key not in seen_keys:
            seen_keys.add(key)
            extra.append({"key": key, "recorded_at": m.get("created_at")})
    return extra, None


def check_merge_preflight(
    state_path: Path, state: dict[str, Any], *, pr: int, head: str, thread_id: str,
    own_seat: str | None = None, window_minutes: float = 30.0, limit: int | None = None,
    now: float | datetime | None = None, checked_by_hand: Iterable[str] = (),
) -> dict[str, Any]:
    """Check recent signed thread messages before running `gh pr merge --match-head-commit`."""
    seat, head_clean = str(own_seat or state.get("label") or "").strip(), str(head or "").strip()
    refuse = lambda code, reason, **extra: {"ok": False, "verdict": "MERGE PREFLIGHT REFUSED", "code": code,
                                            "reason": reason, "pr": pr, "head": head_clean, "thread_id": thread_id, "seat": seat, **extra}
    if not isinstance(pr, int) or isinstance(pr, bool) or pr <= 0:
        return refuse("invalid_pr", "--pr must be a positive integer", checked_messages=0)
    if not _HEX40_RE.fullmatch(head_clean):
        return refuse("invalid_head", "--head must be a 40-hex commit SHA", checked_messages=0)
    if not seat:
        return refuse("missing_seat", "own seat label is required (--seat or minted state label)", checked_messages=0)
    if isinstance(window_minutes, bool) or not isinstance(window_minutes, (int, float)) or not math.isfinite(window_minutes) or window_minutes <= 0:
        return refuse("invalid_window", "--window-minutes must be a positive finite number", checked_messages=0)
    cbh_keys: list[str] = []
    for raw_k in checked_by_hand:
        if not isinstance(raw_k, str) or not _MSG_KEY_ARG_RE.fullmatch(ks := raw_k.strip()):
            return refuse("invalid_checked_by_hand", "--checked-by-hand requires a msg/<key> identifier", checked_messages=0)
        if ks not in cbh_keys:
            cbh_keys.append(ks)
    cbh_set = set(cbh_keys)

    ref_now = t_now if (t_now := _parse_ts(now) if now is not None else None) is not None else time.time()
    cutoff = ref_now - float(window_minutes) * 60.0
    thread_ans = node.thread(state_path, state, thread_id=thread_id, limit=limit)
    if not isinstance(thread_ans, dict):
        return refuse("invalid_thread", "thread response is not a JSON object", checked_messages=0)
    if thread_ans.get("truncated"):
        return refuse("thread_truncated", "thread history was truncated before reaching the tail; omit limit or raise it", checked_messages=0)
    raw_hits = thread_ans.get("hits") if isinstance(thread_ans.get("hits"), list) else thread_ans.get("messages")
    if not isinstance(raw_hits, list):
        return refuse("invalid_thread", "thread response holds no list of hits", checked_messages=0)
    if "coverage" in thread_ans:
        cov = thread_ans.get("coverage")
        if (not isinstance(cov, dict) or type(cov.get("projected")) is not int or type(cov.get("total")) is not int
                or cov["projected"] < 0 or cov["total"] < 0 or cov["projected"] > cov["total"]):
            return refuse("incomplete_projection", "thread response carries invalid projection coverage metadata", checked_messages=0)
        if cov["projected"] < cov["total"]:
            extra_hits, sup_err = _supplement_unprojected_hits(state_path, state, thread_id=thread_id, cutoff=cutoff, raw_hits=raw_hits)
            if sup_err is not None:
                return refuse("incomplete_projection", f"thread projection is incomplete ({cov['projected']}/{cov['total']}) and authoritative query failed: {sup_err}",
                              coverage=cov, checked_messages=0)
            raw_hits = list(raw_hits) + extra_hits

    signed_msgs: list[tuple[float, float | None, int, dict[str, Any]]] = []
    for idx, hit in enumerate(raw_hits):
        if not isinstance(hit, dict) or not isinstance(key := hit.get("key"), str) or not key.strip():
            continue
        hit_ts = _effective_ts(hit)
        try:
            msg = node.read(state_path, state, key=key)
        except node.ReadFailed as exc:
            if hit_ts is not None and hit_ts < cutoff:
                continue
            return refuse("signature_unverified", f"failed to read/verify {key}: {exc.error.get('error', 'read_failed')}", key=key, checked_messages=len(signed_msgs))
        eff_ts = msg_ts if (msg_ts := _effective_ts(msg)) is not None else hit_ts
        if not isinstance(msg, dict) or msg.get("signature_verified") is not True:
            if hit_ts is not None and hit_ts < cutoff and eff_ts is not None and eff_ts < cutoff:
                continue
            return refuse("signature_unverified", f"message {key} in active window failed signature verification", key=key, checked_messages=len(signed_msgs))
        if eff_ts is not None and eff_ts < cutoff:
            continue
        signed_msgs.append((eff_ts if eff_ts is not None else (ref_now + 1e6), eff_ts, idx, msg))

    signed_msgs.sort(key=lambda item: (item[0], item[2]))
    head_folded, seat_folded = head_clean.lower(), seat.casefold()
    latest_merger: tuple[float | None, str, str, Any] | None = None
    latest_head_state: tuple[float | None, str, str, list[str]] | None = None
    active_claims: dict[str, tuple[float | None, str, str, str]] = {}
    manual_checks: list[tuple[str, str]] = []

    for _sort_ts, ts, _idx, msg in signed_msgs:
        key = str(msg.get("key") or "")
        payload = msg.get("value") if isinstance(msg.get("value"), dict) else msg
        sender = str(msg.get("from") or payload.get("from") or msg.get("author") or "").strip()
        clean = sendguard.strip_fences_and_quotes(payload.get("body") if isinstance(payload.get("body"), str) else str(msg.get("body") or ""))
        if (all_prs := _pr_nums_in(clean)) and pr not in all_prs:
            continue
        scope_text = _scope_pr_sections(clean, pr) if (all_prs - {pr}) else clean
        own_merger_decl = False
        if decls := sendguard.extract_merger_declarations(scope_text):
            uniq = list(dict.fromkeys(d.casefold() for d in decls))
            by_fold = {d.casefold(): d for d in reversed(decls)}
            d0 = uniq[0] if len(uniq) == 1 else ""
            valid_single = len(uniq) == 1 and bool(d0) and bool(sendguard._SEAT_LABEL_RE.fullmatch(d0)) and d0 not in sendguard._AMBIGUOUS_MERGERS
            own_merger_decl = valid_single and d0 == seat_folded
            new_m: tuple[float | None, str, str, Any] = (ts, "single", key, by_fold[d0]) if valid_single else (ts, "ambiguous", key, decls)
            if latest_merger is not None and latest_merger[0] == ts:
                if not (latest_merger[1] == "single" == new_m[1] and latest_merger[3].casefold() == new_m[3].casefold()):
                    prev_c = latest_merger[3] if isinstance(latest_merger[3], list) else [latest_merger[3]]
                    cur_c = new_m[3] if isinstance(new_m[3], list) else [new_m[3]]
                    latest_merger = (ts, "ambiguous", key, prev_c + [c for c in cur_c if c not in prev_c])
            else:
                latest_merger = new_m

        if _inspect_merge_prose(scope_text, pr=pr, sender=sender, seat_folded=seat_folded, has_merger_decl=own_merger_decl,
                                ts=ts, key=key, active_claims=active_claims, manual_checks=manual_checks) == "already_merged":
            return refuse("already_merged", f"PR #{pr} is already reported merged in {key} from {sender or 'unknown'}",
                          key=key, **({"from": sender} if sender else {}), checked_messages=len(signed_msgs))

        cur_full, cur_short, hist_shas = _extract_head_shas(scope_text)
        new_h = ((ts, "ambiguous", key, cur_full + cur_short) if (len(cur_full) > 1 or cur_short)
                 else (ts, "single", key, cur_full) if len(cur_full) == 1
                 else (ts, "invalidated", key, hist_shas)
                 if hist_shas and (latest_head_state is None or (latest_head_state[1] == "single" and any(latest_head_state[3][0].startswith(h) for h in hist_shas)))
                 else None)
        if new_h is not None:
            if latest_head_state is not None and latest_head_state[0] == ts:
                if not (latest_head_state[1] == "single" == new_h[1] and latest_head_state[3] == new_h[3]):
                    latest_head_state = (ts, "ambiguous", key, list(latest_head_state[3]) + [s for s in new_h[3] if s not in latest_head_state[3]])
            else:
                latest_head_state = new_h

    if active_claims:
        _c_ts, c_key, c_actor, c_sender = next(iter(active_claims.values()))
        via = f" (reported by {c_sender})" if c_sender and c_sender.casefold() != c_actor.casefold() else ""
        return refuse("claimed_by_other_seat", f"PR #{pr} merge is actively claimed in {c_key} by {c_actor}{via}",
                      key=c_key, **({"from": c_actor} if c_actor else {}), checked_messages=len(signed_msgs))
    if latest_merger is not None:
        _m_ts, m_kind, m_key, m_val = latest_merger
        if m_kind == "ambiguous":
            return refuse("ambiguous_merger", f"latest merger declaration in {m_key} is ambiguous or names multiple seats ({', '.join(m_val)})",
                          key=m_key, candidates=m_val, checked_messages=len(signed_msgs))
        if m_val.casefold() != seat_folded:
            return refuse("merger_assigned_to_other", f"latest merger assignment in {m_key} names `{m_val}`, not `{seat}`",
                          key=m_key, target=m_val, checked_messages=len(signed_msgs))
    if latest_head_state is not None:
        _h_ts, kind, h_key, h_shas = latest_head_state
        if kind == "ambiguous":
            return refuse("ambiguous_head", f"latest head reference in {h_key} names multiple or abbreviated candidate SHAs ({', '.join(h_shas)})",
                          key=h_key, candidate_heads=h_shas, checked_messages=len(signed_msgs))
        if kind == "invalidated" and any(head_folded.startswith(h) for h in h_shas):
            return refuse("stale_head", f"head SHA {head_folded} was marked historical/superseded in {h_key}",
                          key=h_key, thread_head=None, checked_messages=len(signed_msgs))
        if kind == "single" and h_shas[0] != head_folded:
            return refuse("stale_head", f"newer/latest head SHA in {h_key} ({h_shas[0]}) differs from --head {head_folded}",
                          key=h_key, thread_head=h_shas[0], checked_messages=len(signed_msgs))
    cleared_by_hand = [k for k, _ in manual_checks if k in cbh_set]
    if pending_manual := [(k, snd) for k, snd in manual_checks if k not in cbh_set]:
        c_key, c_snd = pending_manual[0]
        return refuse("check_by_hand", f"check by hand: {c_key} from {c_snd} mentions PR #{pr} merge prose (pass --checked-by-hand {c_key} after verifying)",
                      key=c_key, **({"from": c_snd} if c_snd else {}), checked_by_hand=cleared_by_hand, checked_messages=len(signed_msgs))
    note = f"; checked_by_hand={','.join(cleared_by_hand)}" if cleared_by_hand else ""
    return {"ok": True, "verdict": "MERGE PREFLIGHT PASS", "code": "ok",
            "reason": f"PR #{pr} head {head_folded} cleared across {len(signed_msgs)} signed message(s){note}",
            "pr": pr, "head": head_folded, "thread_id": thread_id, "seat": seat, "checked_by_hand": cleared_by_hand, "checked_messages": len(signed_msgs)}


def normalize_review_paths(paths: Iterable[str]) -> dict[str, list[str]]:
    """Extract posix paths and distinct basename/module search tokens, dropping generic tokens."""
    norm_paths, high_tokens, file_tokens, mod_tokens, tokens = [], [], [], [], []
    add = lambda dst, tok: dst.append(tok) if tok and tok not in dst else None
    for raw in paths:
        if not isinstance(raw, str) or not (p := re.sub(r"^(?:\./)+", "", raw.strip().replace("\\", "/").lstrip("/"))):
            continue
        add(norm_paths, p)
        parts = [seg for seg in p.split("/") if seg]
        base = parts[-1]
        stem = base.rsplit(".", 1)[0] if "." in base else base
        if base.lower() != "__init__.py":
            add(high_tokens, p)
            if len(parts) >= 2:
                add(high_tokens, f"{parts[-2]}/{base}")
        if base.lower() not in _GENERIC_BASENAMES and not base.lower().startswith("__init__."):
            add(file_tokens, base)
        if len(stem) >= 4 and stem.lower() not in _GENERIC_STEMS:
            add(file_tokens, stem)
        for seg in parts[:-1]:
            if len(seg) >= 4 and seg.lower() not in _GENERIC_DIRS:
                add(mod_tokens, seg)
    for t in high_tokens + file_tokens + mod_tokens:
        add(tokens, t)
    return {"paths": norm_paths, "tokens": tokens, "high_tokens": high_tokens, "file_tokens": file_tokens,
            "module_tokens": mod_tokens, "query_tokens": (file_tokens + high_tokens + mod_tokens)[:8]}


def _collect_git_diff_paths(repo: Path, base: str, head: str) -> list[str]:
    if not repo.is_dir():
        raise OSError(f"repository directory does not exist: {repo}")
    env, last_err = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}, ""
    for spec in (f"{base}...{head}", f"{base}..{head}"):
        proc = subprocess.run(["git", "--no-optional-locks", "-C", str(repo), "diff", "--name-only", spec],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=10.0)
        if proc.returncode == 0:
            return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
        last_err = (proc.stderr or proc.stdout or "").strip()
    raise OSError(last_err or f"git diff failed for {base}..{head} in {repo}")


def _match_tokens(text: str, norm: dict[str, list[str]]) -> tuple[int, list[str]]:
    score, matched = 0, []
    for weight, grp in ((4, norm["high_tokens"]), (3, norm["file_tokens"]), (1, norm["module_tokens"])):
        for tok in grp:
            if tok not in matched and re.search(rf"(?<![a-z0-9_]){re.escape(tok)}(?![a-z0-9_])", text, re.IGNORECASE):
                matched.append(tok)
                score += weight
    return score, matched


def check_review_preflight(
    state_path: Path, state: dict[str, Any], *, paths: Iterable[str],
    card_namespace: str = cards.DEFAULT_NAMESPACE, limit: int = 10,
) -> dict[str, Any]:
    """Query active Antibody/Constraint/Ruling/OpenItem cards and FIX FIRST notes for `paths`."""
    norm = normalize_review_paths(paths)
    unavail = lambda err: {"available": False, "ok": False, "paths": norm["paths"], "tokens": norm["tokens"],
                           "decision_cards": [], "fix_first_messages": [], "match_count": 0, "error": err}
    try:
        ns = cards.named_namespace(card_namespace)
    except node.Refused as exc:
        return unavail(f"Refused: {exc}")
    if not norm["paths"] or not norm["tokens"]:
        return {"available": True, "paths": norm["paths"], "tokens": norm["tokens"], "decision_cards": [], "fix_first_messages": [], "match_count": 0}
    token_query = " ".join(norm["query_tokens"]) or " ".join(norm["paths"][:4])
    try:
        try:
            card_ans = cards.search(state_path, state, token_query, ns=ns, k=20, rerank=False, types=("Antibody", "Constraint", "Ruling", "OpenItem"), status="active")
        except node.ServerSaid as exc:
            if exc.status != 422:
                raise
            card_ans = cards.search(state_path, state, token_query, ns=ns, k=20, rerank=False, types=("Constraint", "Ruling", "OpenItem"), status="active")
    except node.Refused as exc:
        return unavail(f"Refused: {exc}")
    mesh_ans = node.search(state_path, state, q=f"FIX FIRST {token_query}", k=15, rerank=False, ns="shared/mesh-chat")
    if not isinstance(mesh_ans, dict) or not isinstance(mesh_ans.get("hits"), list):
        return unavail("shared/mesh-chat search response holds no list of hits")
    if mesh_ans.get("namespace") not in (None, "shared/mesh-chat"):
        return unavail(f"asked for shared/mesh-chat and server answered for {mesh_ans.get('namespace')!r}")

    hit_text = lambda h: "\n".join(str(h[k]) for k in ("text", "body", "snippet", "title", "summary", "subject", "ref") if isinstance(h.get(k), str) and h.get(k))
    decision_cards, seen_cards, fix_first, seen_msgs = [], set(), [], set()
    for hit in card_ans.get("hits") or []:
        key = str(hit.get("key") or "").strip() if isinstance(hit, dict) else ""
        status = str(hit.get("status") or "active").strip().lower() if isinstance(hit, dict) else ""
        if key and key not in seen_cards and status in ("", "active") and (sc_m := _match_tokens(text := hit_text(hit), norm))[0] > 0:
            seen_cards.add(key)
            kp = cards.key_parts(key)
            decision_cards.append((sc_m[0], key, {"key": key, "type": hit.get("type") or "Card", "seat": hit.get("seat") or hit.get("author") or (kp[0] if kp else None),
                                                  "matched_tokens": sc_m[1], "snippet": " ".join(text.split())[:200], "recorded_at": hit.get("recorded_at") or hit.get("time")}))
    for hit in mesh_ans["hits"]:
        if not isinstance(hit, dict) or hit.get("namespace") not in (None, "shared/mesh-chat"):
            return unavail("a hit in shared/mesh-chat search is not a mail row")
        key = str(hit.get("key") or "").strip()
        if key and key not in seen_msgs and _FIX_FIRST_RE.search(text := hit_text(hit)) and (sc_m := _match_tokens(text, norm))[0] > 0:
            seen_msgs.add(key)
            fix_first.append((sc_m[0], key, {"key": key, "author": hit.get("author") or hit.get("from") or hit.get("seat"),
                                             "matched_tokens": sc_m[1], "snippet": " ".join(text.split())[:200], "recorded_at": hit.get("recorded_at")}))
    top_cards = [x[2] for x in sorted(decision_cards, key=lambda x: (-x[0], x[1]))[:limit]]
    top_msgs = [x[2] for x in sorted(fix_first, key=lambda x: (-x[0], x[1]))[:limit]]
    return {"available": True, "paths": norm["paths"], "tokens": norm["tokens"], "decision_cards": top_cards, "fix_first_messages": top_msgs, "match_count": len(top_cards) + len(top_msgs)}


def format_merge_preflight(report: dict[str, Any]) -> str:
    return f"MERGE PREFLIGHT PASS: {report.get('reason')}\n" if report.get("ok") else f"MERGE PREFLIGHT REFUSED [{report.get('code')}]: {report.get('reason')}\n"


def format_review_preflight(report: dict[str, Any]) -> str:
    if report.get("available") is False:
        return f"REVIEW PREFLIGHT UNAVAILABLE: {report.get('error') or 'unknown'}\n"
    d_cards, ff_msgs = report.get("decision_cards") or [], report.get("fix_first_messages") or []
    lines = [f"REVIEW PREFLIGHT: {len(d_cards)} matching Antibody/Constraint/Ruling/OpenItem card(s), {len(ff_msgs)} FIX FIRST note(s)"]
    lines.extend(f"  - CARD [{c.get('type')}] {c.get('key')} ({', '.join(c.get('matched_tokens') or [])}): {c.get('snippet')}" for c in d_cards)
    lines.extend(f"  - FIX FIRST {m.get('key')} from {m.get('author') or '?'} ({', '.join(m.get('matched_tokens') or [])}): {m.get('snippet')}" for m in ff_msgs)
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--base-url", default=os.environ.get("MESHY_BASE_URL"))
    common.add_argument("--state-file", type=Path, default=(Path(os.environ["MESHY_STATE_FILE"]) if os.environ.get("MESHY_STATE_FILE") else None))
    common.add_argument("--json", action="store_true")
    parser = argparse.ArgumentParser(prog="python -m tools.meshy_node.preflight")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_m, p_r = sub.add_parser("merge", parents=[common]), sub.add_parser("review", parents=[common])
    p_m.add_argument("--pr", type=int, required=True); p_m.add_argument("--head", required=True); p_m.add_argument("--thread", required=True)
    p_m.add_argument("--seat", default=None); p_m.add_argument("--window-minutes", type=float, default=30.0)
    p_m.add_argument("--checked-by-hand", action="append", default=[], metavar="MSG_KEY")
    p_r.add_argument("--path", action="append", default=[], dest="paths"); p_r.add_argument("--repo", type=Path, default=None)
    p_r.add_argument("--base", default="origin/main"); p_r.add_argument("--head", default="HEAD")
    p_r.add_argument("--card-namespace", default=cards.DEFAULT_NAMESPACE); p_r.add_argument("--limit", type=int, default=10)
    args = parser.parse_args(argv)
    if args.cmd == "merge" and not _HEX40_RE.fullmatch(str(args.head or "").strip()):
        rep = check_merge_preflight(Path("unused"), {"label": args.seat or "unknown"}, pr=args.pr, head=args.head, thread_id=args.thread, own_seat=args.seat, checked_by_hand=args.checked_by_hand)
        sys.stdout.write(json.dumps(rep) + "\n" if args.json else format_merge_preflight(rep))
        return 1
    if not args.base_url or args.state_file is None:
        sys.stderr.write("error: --base-url and --state-file are required\n")
        return 2
    try:
        state = node.load_state(args.state_file, node.normalize_base_url(args.base_url))
    except node.Refused as exc:
        sys.stderr.write(f"refused: {exc}\n")
        return 2
    try:
        if args.cmd == "merge":
            rep = check_merge_preflight(args.state_file, state, pr=args.pr, head=args.head, thread_id=args.thread, own_seat=args.seat, window_minutes=args.window_minutes, checked_by_hand=args.checked_by_hand)
            sys.stdout.write(json.dumps(rep) + "\n" if args.json else format_merge_preflight(rep))
            return 0 if rep["ok"] else 1
        if args.limit < 1 or (args.repo is None and not args.paths):
            sys.stderr.write("error: pass valid --limit >= 1 and --path or --repo\n")
            return 2
        paths = list(args.paths or []) + ([dp for dp in _collect_git_diff_paths(args.repo, args.base, args.head) if dp not in args.paths] if args.repo is not None else [])
        rep = check_review_preflight(args.state_file, state, paths=paths, card_namespace=args.card_namespace, limit=args.limit)
        sys.stdout.write(json.dumps(rep) + "\n") if args.json else (sys.stderr.write(f"error: {rep.get('error') or 'review preflight unavailable'}\n") if rep.get("available") is False else sys.stdout.write(format_review_preflight(rep)))
        return 0 if rep.get("available") is not False else 1
    except (node.ServerSaid, OSError, subprocess.SubprocessError) as exc:
        sys.stdout.write(json.dumps({"ok": False, "available": False, "error": f"{type(exc).__name__}: {exc}"}) + "\n") if args.json else sys.stderr.write(f"error: {type(exc).__name__}: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
