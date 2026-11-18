"""Loader 1.19: every card carries `attribution` (gap register item 7), as the server checks it (#232).

What the loader emits, and the server rule each line answers (apps/cards/validation._check_attribution and
enforce_card):
  * a Candidate is the operator typing: speaker "operator", confirmation "typed";
  * a Ruling is the operator answering: speaker "operator", confirmation "answered" (the server refuses any other);
  * a Question is the seat's own model asking: speaker "agent", confirmation "asked" (the server refuses any other),
    and so source.first_hand is FALSE: on a card born with attribution the server refuses a first_hand that
    disagrees with it;
  * quoted_author is null, or "unknown" when the operator's text reads as a pasted document; first_hand then says
    false too. A typed prompt: 1000+ characters, 10+ lines or 2+ markdown headings. A free-text answer: 2+ headings
    or 5000+ characters only, because an answer labelled the operator's own words in exxxx's blind sample is 1,422
    characters over 25 lines;
  * extraction is "<dialect>/<kind>" in the server's alphabet, from the dialect the card was read in (the builders
    take it as an argument, "claude" by default).

The server's shape rules are mirrored here (SERVER_* below) because its module needs Django; the checks hold every
card this file makes to them.

Run:  python -m pytest tests/test_transcript_cards_attribution.py -q -p no:cacheprovider
"""
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "transcript_cards"))
import transcript_cards as tc  # noqa: E402

SEAT = "txxxx"
# apps/cards/validation.py at #232 (e33251cd): ATTRIBUTION_KEYS, SPEAKERS, CONFIRMATIONS, _EXTRACTION_RE.
SERVER_KEYS = {"speaker", "quoted_author", "extraction", "confirmation"}
SERVER_EXTRACTION = re.compile(r"^[a-z_]{1,31}/[a-z_]{1,32}$")
TWO_HEADINGS = "# Plan\nsome words\n## Steps\nmore words"


def server_accepts(card):
    """The server's attribution rules for one card, as a list of what it would refuse ([] = accepted)."""
    att, bad = card.get("attribution"), []
    if not isinstance(att, dict) or set(att) != SERVER_KEYS:
        return ["shape"]
    if att["speaker"] not in ("operator", "agent"):
        bad.append("speaker")
    if att["confirmation"] not in ("answered", "typed", "asked"):
        bad.append("confirmation")
    if att["quoted_author"] not in (None, "unknown"):
        bad.append("quoted_author")
    if not SERVER_EXTRACTION.fullmatch(att["extraction"]):
        bad.append("extraction")
    if card["type"] == "Ruling" and (att["confirmation"], att["speaker"]) != ("answered", "operator"):
        bad.append("ruling")
    if att["confirmation"] == "asked" and card["type"] != "Question":
        bad.append("asked")
    if card["type"] == "Question" and (att["confirmation"], att["speaker"]) != ("asked", "agent"):
        bad.append("question")
    first = att["speaker"] == "operator" and att["quoted_author"] is None
    if card["source"]["first_hand"] is not first:
        bad.append("first_hand")
    return bad


def _typed(uuid, text, ts):
    return {"type": "user", "uuid": uuid, "timestamp": ts, "sessionId": "S1", "promptSource": "typed",
            "origin": {"kind": "human"}, "parentUuid": "p-" + uuid, "message": {"role": "user", "content": text}}


def _answered(uuid, question, labels, answer, ts):
    qs = [{"question": question, "header": "h", "multiSelect": False,
           "options": [{"label": lab, "description": "what " + lab + " does"} for lab in labels]}]
    return {"type": "user", "uuid": uuid, "timestamp": ts, "sessionId": "S1", "sourceToolAssistantUUID": "a-" + uuid,
            "toolUseResult": {"questions": qs, "answers": {question: answer}, "annotations": {}},
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t-" + uuid,
                                                     "content": "answered"}]}}


def _lines(n, word="step"):
    return "\n".join("%s %d of the plan" % (word, i) for i in range(n))


PROMPTS = {   # uuid: (text, expected quoted_author)
    "short": ("Keep the cache local and do not add a server.", None),
    "c999": ("x" * 999, None),
    "c1000": ("x" * 1000, "unknown"),
    "l9": (_lines(9), None),
    "l10": (_lines(10), "unknown"),
    "h1": ("# Only one heading\nand some words", None),
    "h2": (TWO_HEADINGS, "unknown"),
}
ANSWERS = {   # uuid: (answer, expected quoted_author); "SQLite" is an offered label
    "label": ("SQLite", None),
    # The shape of the answer exxxx's sample labels the operator's own: about 1,422 characters, 25 lines, no heading.
    "own1422": ("\n".join(("point %d " % i).ljust(56, "w") for i in range(25)), None),
    "head2": (TWO_HEADINGS, "unknown"),
    "c4999": ("y" * 4999, None),
    "c5000": ("y" * 5000, "unknown"),
    "c30000": ("w" * 30000, "unknown"),
}


@pytest.fixture(scope="module")
def walked(tmp_path_factory):
    proj = tmp_path_factory.mktemp("proj")
    recs = [_typed("m", "echo %s-shell-ok" % SEAT, "2026-10-11T01:00:00.000Z")]
    for i, (uuid, (text, _)) in enumerate(PROMPTS.items()):
        recs.append(_typed(uuid, text, "2026-10-11T01:%02d:00.000Z" % (i + 1)))
    for i, (uuid, (answer, _)) in enumerate(ANSWERS.items()):
        recs.append(_answered(uuid, "Which store for the cache %s?" % uuid, ["SQLite", "Postgres"], answer,
                              "2026-10-11T02:%02d:00.000Z" % (i + 1)))
    with open(proj / "T.jsonl", "w", encoding="utf-8", newline="\n") as f:
        f.writelines(json.dumps(r) + "\n" for r in recs)
    out = tmp_path_factory.mktemp("out")
    report = tc.run(str(proj), SEAT, str(out))
    cards = [json.loads(line) for line in open(out / "cards.jsonl", encoding="utf-8")]
    by_uuid = {c["source"].get("transcript_uuid") or c["source"]["record_uuid"]: c for c in cards}
    return report, by_uuid


def test_the_version_is_1_22(walked):
    report, by_uuid = walked
    assert tc.LOADER_VERSION == "1.23" and report["loader_version"] == "1.23"
    assert {c["loader_version"] for c in by_uuid.values()} == {"1.23"}


@pytest.mark.parametrize("uuid", sorted(PROMPTS))
def test_a_typed_prompt_is_the_operator_typing(walked, uuid):
    card = walked[1][uuid]
    want = PROMPTS[uuid][1]
    assert card["type"] == "Candidate"
    assert card["attribution"] == {"speaker": "operator", "quoted_author": want, "extraction": "claude/typed_prompt",
                                   "confirmation": "typed"}
    assert card["source"]["first_hand"] is (want is None)
    assert server_accepts(card) == []


@pytest.mark.parametrize("uuid", sorted(ANSWERS))
def test_an_answer_is_the_operator_answering(walked, uuid):
    card = walked[1][uuid]
    want = ANSWERS[uuid][1]
    assert card["type"] == "Ruling"
    assert card["attribution"] == {"speaker": "operator", "quoted_author": want,
                                   "extraction": "claude/answered_question", "confirmation": "answered"}
    assert card["source"]["first_hand"] is (want is None)
    assert server_accepts(card) == []


def test_the_cut_does_not_hide_a_paste(walked):
    """The rule reads the FULL text, before the card is cut to fit: a 30,000-character answer is cut on the card and
    is still marked."""
    card = walked[1]["c30000"]
    assert card["truncated"] is True and len(card["answer"]) < 30000
    assert card["attribution"]["quoted_author"] == "unknown"


def test_a_label_only_answer_is_never_a_paste():
    """An answer that only names an offered option is the option's words, whatever the label's shape."""
    card, why = tc.answered_question_card(SEAT, "toolu_L", 0, "Pick one", [{"label": TWO_HEADINGS}], TWO_HEADINGS,
                                          "2026-10-11T03:00:00.000Z")
    assert why is None and card["attribution"]["quoted_author"] is None and card["source"]["first_hand"] is True


def test_a_note_can_carry_the_paste():
    card, why = tc.answered_question_card(SEAT, "toolu_N", 0, "Pick one", [{"label": "A"}], "A",
                                          "2026-10-11T03:00:00.000Z", notes=TWO_HEADINGS)
    assert why is None and card["attribution"]["quoted_author"] == "unknown" and card["source"]["first_hand"] is False


def test_an_asked_question_is_the_model_asking():
    card, why = tc.asked_question_card(SEAT, "toolu_Q", 0, TWO_HEADINGS + " Which store?", [{"label": "A"}],
                                       "2026-10-11T03:00:00.000Z")
    assert why is None
    # Never "unknown": a Question is the model's words, whatever their shape.
    assert card["attribution"] == {"speaker": "agent", "quoted_author": None, "extraction": "claude/question_hook",
                                   "confirmation": "asked"}
    assert card["source"]["first_hand"] is False
    assert server_accepts(card) == []


def test_a_hook_ruling_names_its_kind_and_dialect():
    card, why = tc.answered_question_card(SEAT, "toolu_H", 0, "Pick one", [{"label": "A"}], "A",
                                          "2026-10-11T03:00:00.000Z", kind="answered_question_hook")
    assert why is None and card["attribution"]["extraction"] == "claude/answered_question_hook"
    assert card["source"]["first_hand"] is True and server_accepts(card) == []
    card, _ = tc.answered_question_card(SEAT, "toolu_H", 0, "Pick one", [{"label": "A"}], "A",
                                        "2026-10-11T03:00:00.000Z", kind="answered_question_hook", dialect="kimi")
    assert card["attribution"]["extraction"] == "kimi/answered_question_hook" and server_accepts(card) == []
    card, _ = tc.answered_question_card(SEAT, "toolu_H", 0, "Pick one", [{"label": "A"}], "A",
                                        "2026-10-11T03:00:00.000Z", kind="answered_question_modal", dialect="antigravity")
    assert card["attribution"]["extraction"] == "antigravity/answered_question_modal" and server_accepts(card) == []
    card, _ = tc.asked_question_card(SEAT, "toolu_H", 0, "Pick one", [{"label": "A"}], "2026-10-11T03:00:00.000Z",
                                     dialect="codex")
    assert card["attribution"]["extraction"] == "codex/question_hook" and server_accepts(card) == []
    card, _ = tc.asked_question_card(SEAT, "toolu_H", 0, "Pick one", [{"label": "A"}], "2026-10-11T03:00:00.000Z",
                                     dialect="antigravity")
    assert card["attribution"]["extraction"] == "antigravity/question_hook" and server_accepts(card) == []


@pytest.mark.parametrize("dialect,kind,want", [
    ("claude", "typed_prompt", "claude/typed_prompt"),
    ("Codex", "Question-Codex", "codex/question_codex"),
    ("kimi", "answered question hook!", "kimi/answered_question_hook"),
    ("antigravity", "answered question modal!", "antigravity/answered_question_modal"),
    ("", "", "unknown/unknown"),
    ("d" * 40, "k" * 40, "d" * 31 + "/" + "k" * 32),
])
def test_extraction_is_always_in_the_servers_alphabet(dialect, kind, want):
    got = tc.extraction_of(dialect, kind)
    assert got == want and SERVER_EXTRACTION.fullmatch(got)


def test_read_file_and_approved_cards_carry_the_dialect():
    """The queue's path (read_file then approved_cards) builds the same attribution as run()."""
    recs = [_typed("q1", "Keep the cache local.", "2026-10-11T04:00:00.000Z"),
            _typed("q2", TWO_HEADINGS, "2026-10-11T04:01:00.000Z")]
    lines, off = [], 0
    for r in recs:
        b = (json.dumps(r) + "\n").encode("utf-8")
        lines.append((off, b))
        off += len(b)
    raws, _, _ = tc.read_file(lines, tc.new_checkpoint("claude", {}), seat=SEAT)
    assert {r["dialect"] for r in raws} == {"claude"}
    cards, drops = tc.approved_cards(raws, SEAT, "f.jsonl")
    got = {c["source"]["record_uuid"]: c["attribution"]["quoted_author"] for c in cards.values()}
    assert got == {"q1": None, "q2": "unknown"} and not drops
    assert all(server_accepts(c) == [] for c in cards.values())


def test_antigravity_read_file_and_approved_cards_carry_the_dialect():
    """Antigravity records read through read_file stamp dialect='antigravity' and attribution."""
    ask_call = [{
        "name": "ask_question",
        "args": {
            "questions": [{
                "question": "Which strategy?",
                "options": [{"label": "Rebase"}, {"label": "Merge"}],
                "is_multi_select": False
            }]
        }
    }]
    recs = [
        {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT",
         "created_at": "2026-10-11T04:00:00.000Z",
         "content": "<USER_REQUEST>\nKeep the cache local.\n</USER_REQUEST>"},
        {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE",
         "created_at": "2026-10-11T04:01:00.000Z",
         "tool_calls": ask_call},
        {"step_index": 2, "source": "USER_EXPLICIT", "type": "USER_INPUT",
         "created_at": "2026-10-11T04:02:00Z",
         "content": "<USER_REQUEST>\nRebase\n</USER_REQUEST>"},
    ]
    lines, off = [], 0
    for r in recs:
        b = (json.dumps(r) + "\n").encode("utf-8")
        lines.append((off, b))
        off += len(b)
    meta = {"session": "sess-att", "subagent": False}
    raws, _, _ = tc.read_file(lines, tc.new_checkpoint("antigravity", meta), seat=SEAT)
    assert len(raws) == 2
    assert {r["dialect"] for r in raws} == {"antigravity"}
    cards, drops = tc.approved_cards(raws, SEAT, "transcript.jsonl")
    assert not drops
    c_list = list(cards.values())
    cand = next(c for c in c_list if c["type"] == "Candidate")
    ruling = next(c for c in c_list if c["type"] == "Ruling")

    assert cand["attribution"]["extraction"] == "antigravity/typed_prompt"
    assert cand["attribution"]["confirmation"] == "typed"
    assert cand["source"]["first_hand"] is True
    assert server_accepts(cand) == []

    assert ruling["attribution"]["extraction"] == "antigravity/answered_question_modal"
    assert ruling["attribution"]["confirmation"] == "answered"
    assert ruling["source"]["first_hand"] is True
    assert ruling["free_text"] is False
    assert len(ruling["options"]) == 2
    assert ruling["answer"] == "Rebase"
    assert ruling["time"] == "2026-10-11T04:02:00.000Z"
    assert server_accepts(ruling) == []


def test_a_raw_with_no_dialect_is_refused_not_guessed():
    raw = {"type": "Candidate", "kept": {"kind": "typed_prompt", "text": "x"}, "extra": {}}
    with pytest.raises(KeyError):
        tc.attribution_of(raw)


def test_a_type_with_no_rule_is_refused_not_guessed():
    raw = {"type": "Decision", "dialect": "claude", "kept": {"kind": "typed_prompt", "text": "x"}, "extra": {}}
    with pytest.raises(KeyError):
        tc.attribution_of(raw)
