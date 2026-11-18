"""`meshy_node review-receipt`: is there a clean AI review of EXACTLY this head? Read only.

The merge rule (a recorded decision) needs a clean AI review on the exact head. That is GitHub's Codex review, or,
when its quota is out, a fallback review by a Codex helper that is not the author's (design AI-REVIEW-FALLBACK r1,
0358e2c1; the runner's note r0; a recorded decision as reported by the queue keeper). This module answers one
question for whoever merges. It writes nothing and decides nothing else: the local gate and the non-author GO stay
as they are.

THE CHECK FETCHES ITS OWN PROOF (pxx). A first version took the PR's comments and the signed messages from
files the caller assembled, and believed them: a hand-written file made it say CLEAN for a head nobody had
reviewed, by either route. Now `check` reads the PR and its comments from GitHub itself, refuses a head that is
not the PR's current head, and reads each message through the client's own verified read, taking the signer from
that read. The pure functions below still take plain data, for the tests; the command's file mode prints
NOT A MERGE CHECK and never exits 0.

The order, and each step fail-closed:
  1. GitHub's own review. The Codex connector keeps ONE summary comment per PR with a machine line
     `<!-- codex-review-state:v1 {...} -->`. It is clean for this head only when its code_review.commit_sha is this
     head in full, its status is "completed" and has_unresolved_findings is false (the real shapes, read from
     PRs 610, 624, 633, 635, 636 and 638 on Sun 15 Nov 2026: completed/false is clean, completed/true has findings,
     failed is no review). A review of another head says nothing about this one.
  2. Else a fallback receipt: a PR comment of the fixed shape
        AI-REVIEW: fallback; reviewer: <helper>; head: <40 hex>; P1: n; P2: n; P3: n; report sha256: <64 hex>; token: <16 hex>
     from a login on the poster list, AND a message SIGNED BY the peer of the session that runs that helper, whose
     first line carries the same reviewer, head, counts, report hash and token, AND the report itself, hashing to
     that value. Every seat posts to GitHub under one login, so the comment alone cannot say who reviewed; the
     signature is the proof and the comment is the visible record (pxx, oxxxxxx). A message's `from`
     is a label anyone can write; only the verified signer's peer id is compared. If comment and message disagree
     on any field the receipt is VOID and the PR waits: it is not "the stricter of the two". Clean means P1 0 and
     P2 0.
     The NEWEST receipt comment for the head is the one judged. A newer one that fails (an unknown reviewer, a
     missing message) makes the answer void even when an older one was good. That is meant: it fails closed. Do
     not "fix" it into picking the best receipt. Two DIFFERENT receipts at the same newest time (GitHub's times
     have one-second grain) are void too, and two different connector states at the same newest time are no
     review: an order nobody can know is never resolved in favour of the clean one.
  3. Else: no AI review of this head.
GitHub's own open findings on a head outrank a clean fallback receipt.
"""
from __future__ import annotations

import json
import re
import subprocess
from typing import Any, Callable

#: GitHub's Codex connector, as its login reads on its own comments (read from PR 638's comments, 15 Nov 2026).
CONNECTOR_LOGINS = frozenset({"chatgpt-codex-connector[bot]"})
#: Who may post a fallback receipt comment. One login today: every seat posts under it (oxxxxxx).
DEFAULT_POSTERS = frozenset({"sampleco"})
#: The Codex helpers that may give a fallback review: helper label -> (the session that runs it, that session's
#: peer id). The peer id is what a receipt's signed message is checked against (read from each session's own
#: signed messages on Sun 15 Nov 2026). A helper not listed here is not a reviewer.
HELPERS: dict[str, tuple[str, str]] = {
    "codex-pxx-cli": ("pxx", "717734fb-b4a3-5125-95a0-e21bbb0f7e8c"),
    "codex-kxxx-cli": ("kxxx", "a2b5be8f-aa4f-5634-8b28-97de57204a14"),
    "codex-bxxxxx-cli": ("memory-bxxxxx", "0447bf92-25fb-5230-b531-5a0d9c00d8c7"),
}
#: The PR author's seat, from the head branch's first path part (oxxxxxx: never from the GitHub login, which
#: every seat shares). A prefix not listed is UNKNOWN, and then no fallback receipt counts.
BRANCH_SEATS: dict[str, str] = {"kxxx": "kxxx", "pxx": "pxx", "bxxxxx": "memory-bxxxxx",
                                "memory-bxxxxx": "memory-bxxxxx"}
_AUTHOR_LINE_RE = re.compile(r"(?im)^[ \t>*_-]*author seat[*_]*[ \t]*:[*_ \t]*([a-z0-9][a-z0-9-]{0,39})[ \t*_.]*$")

_SHA_RE = re.compile(r"[0-9a-f]{40}")
_REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
_KEY_RE = re.compile(r"msg/[A-Za-z0-9][A-Za-z0-9._-]{0,120}")
_STATE_RE = re.compile(r"<!-- codex-review-state:v1 (\{.*?\}) -->", re.S)
_RECEIPT_RE = re.compile(
    r"AI-REVIEW: fallback; reviewer: (?P<reviewer>[a-z0-9][a-z0-9-]{0,39}); head: (?P<head>[0-9a-f]{40}); "
    r"P1: (?P<p1>\d{1,4}); P2: (?P<p2>\d{1,4}); P3: (?P<p3>\d{1,4}); report sha256: (?P<report>[0-9a-f]{64}); "
    r"token: (?P<token>[0-9a-f]{16})")
_FIELDS = ("reviewer", "head", "p1", "p2", "p3", "report", "token")
MAX_BODY = 200_000
MAX_KEYS = 20


def is_sha(value: Any) -> bool:
    return isinstance(value, str) and _SHA_RE.fullmatch(value) is not None


def _login(comment: Any) -> str | None:
    user = comment.get("user") if isinstance(comment, dict) else None
    login = user.get("login") if isinstance(user, dict) else None
    return login if isinstance(login, str) else None


def _body(comment: Any) -> str:
    body = comment.get("body") if isinstance(comment, dict) else None
    return body[:MAX_BODY] if isinstance(body, str) else ""


def _when(comment: Any) -> str:
    """The comment's last change: an edited comment counts by its edit, so a newer edit of an older comment is newer."""
    for key in ("updated_at", "created_at"):
        v = comment.get(key) if isinstance(comment, dict) else None
        if isinstance(v, str):
            return v
    return ""


def github_review(comments: Any, head: str) -> dict:
    """What GitHub's own Codex review says about `head`: {"state": clean | findings | ambiguous | none, "why": ...}.
    Only the connector's comments are read, and only its NEWEST state line: an older clean line for this head is
    not kept once the connector has written a newer state. `ambiguous` blocks like `findings`."""
    states = []
    for c in comments if isinstance(comments, list) else []:
        if _login(c) not in CONNECTOR_LOGINS:
            continue
        for raw in _STATE_RE.findall(_body(c)):
            states.append((_when(c), raw))
    if not states:
        return {"state": "none", "why": "GitHub's Codex connector has written no review state on this PR"}
    newest = max(at for at, _raw in states)
    tied = sorted({raw for at, raw in states if at == newest})
    if len(tied) == 1:
        return _one_state(tied[0], head)
    # GitHub's times have one-second grain: two DIFFERENT states at the newest time cannot be ordered (the Codex
    # review of 04287848). Each is judged. If they all say the same about this head, that is the answer. If they do
    # not, nobody can say which one stands, and "no review" would let a clean fallback receipt pass beside a state
    # that reports open findings (the Codex review of 1bff3551): the state is AMBIGUOUS, and it blocks.
    said = {_one_state(raw, head)["state"] for raw in tied}
    if len(said) == 1:
        return {"state": said.pop(), "why": "the connector's newest review states carry the same time and agree"}
    return {"state": "ambiguous", "why": "the connector's newest review states carry the same time and differ (%s); "
                                         "no review counts until GitHub's state is one" % ", ".join(sorted(said))}


def _one_state(raw: str, head: str) -> dict:
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return {"state": "none", "why": "the connector's newest review state is not readable"}
    review = data.get("code_review") if isinstance(data, dict) else None
    if not isinstance(review, dict):
        return {"state": "none", "why": "the connector's newest review state has no code review"}
    sha, status, open_ = review.get("commit_sha"), review.get("status"), review.get("has_unresolved_findings")
    if sha != head:
        return {"state": "none", "why": "the connector's newest review is of another head (%s)"
                % (sha[:12] if is_sha(sha) else "unreadable")}
    if status != "completed":
        return {"state": "none", "why": "the connector's review of this head did not complete (status %s)"
                % (status if isinstance(status, str) and re.fullmatch(r"[a-z_]{1,20}", status) else "unreadable")}
    if open_ is False:
        return {"state": "clean", "why": "GitHub Codex review of this head: completed, no unresolved finding"}
    if open_ is True:
        return {"state": "findings", "why": "GitHub Codex review of this head has unresolved findings"}
    return {"state": "none", "why": "the connector's review of this head does not say whether findings are open"}


def _receipt_fields(text: str) -> dict | None:
    """The fields of a receipt line, or None. The line must be the text's FIRST line, whole: a receipt quoted
    lower down in a comment or a message is not a receipt."""
    first = text.replace("\r\n", "\n").split("\n", 1)[0].strip()
    m = _RECEIPT_RE.fullmatch(first)
    return {k: m.group(k) for k in _FIELDS} if m else None


def author_seat_of(head_ref: Any, body: Any) -> str | None:
    """The PR author's seat: the head branch's first path part when BRANCH_SEATS knows it, else an "author seat:"
    line in the PR body naming a known seat or helper. Two sources that disagree are UNKNOWN (None)."""
    known = set(BRANCH_SEATS.values()) | set(HELPERS)
    from_ref = None
    if isinstance(head_ref, str) and "/" in head_ref:
        from_ref = BRANCH_SEATS.get(head_ref.split("/", 1)[0])
    lines = _AUTHOR_LINE_RE.findall(body[:MAX_BODY]) if isinstance(body, str) else []
    named = {n for n in lines}
    if len(named) > 1 or (named and not named <= known):
        return None
    from_body = next(iter(named), None)
    # The two sources are compared BY SESSION: a body that names a helper ("codex-kxxx-cli") on a branch of the
    # session that runs it ("kxxx/...") names one seat twice, not two seats (the Codex review of 1bff3551). The more
    # specific name is kept; fallback_receipt refuses the author's own session and its helper either way.
    if from_ref and from_body and from_ref != session_of(from_body):
        return None
    return from_body or from_ref


def session_of(seat: str, helpers: dict[str, tuple[str, str]] | None = None) -> str:
    """The session a seat name belongs to: a helper's own session, or the name itself."""
    helpers = HELPERS if helpers is None else helpers
    return helpers[seat][0] if seat in helpers else seat


def fallback_receipt(comments: Any, messages: Any, head: str, *, posters: frozenset = DEFAULT_POSTERS,
                     helpers: dict[str, tuple[str, str]] | None = None, author_seat: str | None = None,
                     report_sha256: str | None = None) -> dict:
    """The newest fallback receipt for `head`, checked: {"state": clean | findings | void | none, "why": ...}.

    comments: the PR's issue comments. messages: messages as the client's verified read returns them (key,
    signature_verified, signed_by, body). helpers: helper label -> (session, the session's peer id). author_seat:
    None is UNKNOWN, and then no fallback counts. report_sha256: the hash of the report file; without it a receipt
    is void, because the report is part of the proof."""
    helpers = HELPERS if helpers is None else helpers
    found = []
    for c in comments if isinstance(comments, list) else []:
        if _login(c) not in posters:
            continue
        f = _receipt_fields(_body(c))
        if f and f["head"] == head:
            found.append((_when(c), f))
    if not found:
        return {"state": "none", "why": "no fallback receipt comment names this head"}
    _at, want = max(found, key=lambda s: s[0])
    if len({tuple(f[k] for k in _FIELDS) for at, f in found if at == _at}) > 1:
        # the Codex review of 04287848: two receipts posted or edited within the same second have the same time, and
        # `max` then took whichever GitHub listed first, so an older clean receipt could mask a newer one with a P2.
        # Which is newer cannot be known from the times: the receipt is void and the PR waits for one clear receipt.
        return {"state": "void", "why": "two different receipts for this head carry the same newest time; which is "
                                        "newer cannot be known"}
    if author_seat is None:
        return {"state": "void", "why": "the PR's author seat is unknown, so no helper is known to be independent"}
    helper = helpers.get(want["reviewer"])
    if helper is None:
        return {"state": "void", "why": "the receipt's reviewer %s is not a known helper" % want["reviewer"]}
    session, peer = helper
    if session == session_of(author_seat, helpers) or want["reviewer"] == author_seat:
        return {"state": "void", "why": "the receipt's reviewer %s is the author's own helper" % want["reviewer"]}
    proof = None
    for m in messages if isinstance(messages, list) else []:
        if (not isinstance(m, dict) or m.get("signature_verified") is not True or m.get("signed_by") != peer
                or not isinstance(m.get("key"), str) or not m["key"].startswith("msg/")):
            continue
        f = _receipt_fields(m.get("body") if isinstance(m.get("body"), str) else "")
        if f and f["head"] == head and f["token"] == want["token"]:
            proof = f
            break
    if proof is None:
        return {"state": "void", "why": "no message signed by %s's peer carries this receipt (same head and token)"
                % session}
    differ = [k for k in _FIELDS if proof[k] != want[k]]
    if differ:
        return {"state": "void", "why": "the comment and the signed message disagree on %s" % ", ".join(differ)}
    if report_sha256 is None:
        return {"state": "void", "why": "the report was not given, so it cannot be checked against the receipt"}
    if report_sha256 != want["report"]:
        return {"state": "void", "why": "the report does not hash to the receipt's sha256"}
    counts = {k: int(want[k]) for k in ("p1", "p2", "p3")}
    base = {"reviewer": want["reviewer"], "session": session, "report_sha256": want["report"], **counts}
    if counts["p1"] == 0 and counts["p2"] == 0:
        return {"state": "clean", "why": "fallback review by %s: P1 0, P2 0" % want["reviewer"], **base}
    return {"state": "findings", "why": "fallback review by %s: P1 %d, P2 %d" % (
        want["reviewer"], counts["p1"], counts["p2"]), **base}


def verdict(comments: Any, messages: Any, head: Any, **kw) -> dict:
    """The one answer from data: {"clean": bool, "source": github | fallback | none, "why": ..., "github": ...,
    "fallback": ...}. `clean` is true only for exactly `head`. `check` is what a merger calls; this is its core."""
    if not is_sha(head):
        return {"clean": False, "source": "none", "why": "the head is not a full 40-hex sha", "github": None,
                "fallback": None}
    gh = github_review(comments, head)
    fb = fallback_receipt(comments, messages, head, **kw)
    out = {"head": head, "github": gh, "fallback": fb}
    if gh["state"] == "clean":
        return {"clean": True, "source": "github", "why": gh["why"], **out}
    if gh["state"] in ("findings", "ambiguous"):
        # GitHub's own review of this head has open findings, or its newest states cannot be told apart: no fallback
        # receipt outranks either (the stricter holds)
        return {"clean": False, "source": "github", "why": gh["why"], **out}
    if fb["state"] == "clean":
        return {"clean": True, "source": "fallback", "why": fb["why"], **out}
    if fb["state"] in ("findings", "void"):
        return {"clean": False, "source": "fallback", "why": fb["why"], **out}
    return {"clean": False, "source": "none", "why": "no AI review of this head: %s; %s" % (gh["why"], fb["why"]),
            **out}


# ------------------------------------------------------------------------------------------------ the live check

def _gh(args: list[str], timeout: float = 60.0) -> str:
    """One read-only `gh api` GET. Anything but exit 0 raises LookupError with gh's last line."""
    try:
        p = subprocess.run(["gh", "api", "-X", "GET", *args], capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LookupError("gh did not answer (%s)" % type(exc).__name__) from None
    if p.returncode != 0:
        raise LookupError("gh api failed (exit %d): %s" % (p.returncode,
                                                           (p.stderr.strip().splitlines() or ["no output"])[-1][:160]))
    return p.stdout


def gh_pr(repo: str, pr: int) -> dict:
    """The PR as GitHub has it now: {"head_sha", "head_ref", "body", "state"}."""
    data = json.loads(_gh(["repos/%s/pulls/%d" % (repo, pr)]))
    head = data.get("head") if isinstance(data, dict) else None
    if not isinstance(head, dict):
        raise LookupError("GitHub's answer for the PR has no head")
    return {"head_sha": head.get("sha"), "head_ref": head.get("ref"), "body": data.get("body"),
            "state": data.get("state")}


def gh_comments(repo: str, pr: int) -> list[dict]:
    """Every issue comment of the PR, all pages: login, times and body, one JSON object per line from gh."""
    out = _gh(["repos/%s/issues/%d/comments" % (repo, pr), "--paginate", "--jq",
               ".[] | {user: {login: .user.login}, created_at, updated_at, body}"], timeout=120.0)
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def check(repo: Any, pr: Any, head: Any, *, message_keys: list[str] | None = None,
          read_message: Callable[[str], dict] | None = None, report_sha256: str | None = None,
          fetch_pr: Callable[[str, int], dict] | None = None,
          fetch_comments: Callable[[str, int], list[dict]] | None = None) -> dict:
    """The merge check. It fetches the PR and its comments itself, refuses a head that is not the PR's head NOW,
    takes the author's seat from the PR, and reads each message key through `read_message` (the client's verified
    read). Any fetch that fails is "not clean" with the reason; nothing here is taken from the caller but the
    question (repo, PR, head), the message keys to look at, and the report's hash."""
    no = {"clean": False, "source": "none", "github": None, "fallback": None, "live": True}
    if not (isinstance(repo, str) and _REPO_RE.fullmatch(repo)) or type(pr) is not int or not 0 < pr < 10**7:
        return {**no, "why": "the repository or PR number is not valid"}
    if not is_sha(head):
        return {**no, "why": "the head is not a full 40-hex sha"}
    keys = list(message_keys or [])
    if len(keys) > MAX_KEYS or not all(isinstance(k, str) and _KEY_RE.fullmatch(k) for k in keys):
        return {**no, "head": head, "why": "a message key is not a msg/ key, or there are more than %d" % MAX_KEYS}
    fetch_pr, fetch_comments = fetch_pr or gh_pr, fetch_comments or gh_comments      # GitHub itself, unless a test
    try:
        info = fetch_pr(repo, pr)
        if not isinstance(info, dict) or info.get("head_sha") != head:
            now = info.get("head_sha") if isinstance(info, dict) else None
            return {**no, "head": head, "why": "the PR's head is %s now, not the head asked about"
                    % (now[:12] if is_sha(now) else "unreadable")}
        comments = fetch_comments(repo, pr)
    except (LookupError, ValueError, TypeError, KeyError, AttributeError) as exc:
        return {**no, "head": head, "why": "GitHub did not read: %s" % str(exc)[:160]}
    messages, unread = [], 0
    for key in keys:
        try:
            m = read_message(key) if read_message is not None else None
        except Exception:  # noqa: BLE001 - one message that does not read is one proof less, never a crash
            m = None
        if isinstance(m, dict):
            messages.append({"key": key, "signature_verified": m.get("signature_verified"),
                             "signed_by": m.get("signed_by"), "body": m.get("body")})
        else:
            unread += 1
    seat = author_seat_of(info.get("head_ref"), info.get("body"))
    v = verdict(comments, messages, head, author_seat=seat, report_sha256=report_sha256)
    return {**v, "live": True, "repo": repo, "pr": pr, "author_seat": seat, "messages_read": len(messages),
            "messages_unread": unread}


def render(v: dict) -> str:
    head = v.get("head") or "?"
    lines = []
    if v.get("live") is not True:
        lines.append("NOT A MERGE CHECK: files given. This run believed its input files and proves nothing.")
    lines += ["AI REVIEW %s for %s (%s)" % ("CLEAN" if v.get("clean") else "NOT CLEAN", head, v.get("source")),
              v.get("why") or ""]
    for name in ("github", "fallback"):
        part = v.get(name)
        if isinstance(part, dict):
            lines.append("  %s: %s (%s)" % (name, part.get("state"), part.get("why")))
    lines.append("This is the AI-review leg only. The local gate and a GO from someone other than the author are "
                 "separate, and a push to the PR makes this answer void.")
    return "\n".join(lines)
