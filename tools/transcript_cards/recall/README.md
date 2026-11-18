# Recall judge

Scores a transcript-card retrieval run against labelled queries: whether the card that answers a question is
returned, ranked within k, and served with the right status. Standard library only. Owner: memory-bxxxxx.

- `judge.py`: the judge. `python judge.py --selftest` shows every check passing on a good run and failing on the
  one defect it exists for. `python judge.py --gold G --results R [--k 5] [--strict-duplicates]` scores a run;
  exit 0 when every query passed, 1 when one failed, 2 when an input or a label could not be read. The format of
  both files and every failure rule are in its docstring.
- `mutate_judge.py`: breaks the judge one rule at a time; each mutant must make the selftest fail. 59 when written;
  it prints its own count last, and the test requires a verdict for every one.

A results file names each query id once (a repeated id, identical rows included, is an input error: exit 2), and
carries the server's own card status. An overruled card is one the server serves as `active` with a non-empty
`overruled_by`; a card that states `overruled` without the mark counts as the wrong status.

The report gives recall at k and MRR, and counts failures `by_reason` (`active_missing`, `active_beyond_k`,
`superseded_above`, `wrong_status`, `claim_above`, `duplicates`, `no_result`), plus queries and passes `by_seat`
(the active card's seat) and `by_kind` (the label's optional `kind`).

`tests/test_recall_judge.py` runs both, so a change that weakens the judge fails the suite.

`tests/test_recall_cards_search.py` runs the judge against the server's own search. It writes the cards in
`synthetic/cards.json` as signed rows, walks them, asks `GET /v1/mesh/search` on the card namespace for every label
in `synthetic/dev.jsonl` (tune against these) and `synthetic/eval.jsonl` (never tune against these), looks each hit
up through `GET /v1/cards`, and requires every query to pass. Three controls change one input each and must turn a
passing query red: an overruled mark removed, an answer no card holds, and a superseded card served as active. The
model hosts there are the deterministic fakes, whose embeddings are a bag of hashed words, so its labels share words
with their cards: it gates the status rules and the pipeline, not recall of a reworded question.

## What a label is

A query and the chain of cards that answer it, oldest first, with exactly one active card:

    {"id": "<seat>-NNN", "query": "...", "labelled_by": "<seat>", "kind": "<optional tag>",
     "expect": [{"type": "Ruling", "seat": "<on record seat>", "date": "YYYY-MM-DD",
                 "at": "YYYY-MM-DDTHH:MM:SSZ", "status": "superseded" | "overruled" | "active",
                 "must_contain": ["the operator's own words"]}]}

1. Label from a recorded decision record: the answered question or the typed turn in the seat's own transcript.
2. `seat` is where the operator answered, not who relayed it. `date` is the UTC date, exactly `YYYY-MM-DD`; `at`,
   the answer's own time, is optional but needed whenever two rulings of one seat fall on one day. A card matches
   `at` within `AT_TOLERANCE_S` (5 s) either side.
3. `must_contain` is a list of 1 to 3 phrases (`MAX_PHRASES`), each a few words the operator chose or typed, never
   a model's paraphrase. A phrase that is empty after folding case and spaces is refused, since it would match any
   card of that seat and date.
4. A ruling that replaced an earlier one keeps the earlier card in the chain: `superseded` when the later card is of
   the same seat, `overruled` when only another seat's later ruling replaced it.
5. No secrets, and no transcript text beyond the ruling's own words.
6. Every label needs one active card, so a question with NO answer cannot be written yet. That needs its own rule
   (what the search may return for it, and at what score) and comes with the end-to-end search test.

## Real labels stay out of the repository

The labelled queries used against production carry the operator's own words in `must_contain`, so they are kept
on the labelling seats' machines, not in git. They are scored there with this same judge, and only the numbers
(recall at k, misses by kind) are published. Everything in this folder and in the tests is synthetic.
