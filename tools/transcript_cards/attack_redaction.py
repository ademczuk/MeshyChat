"""Red-team the loader's secret detectors with planted, FAKE secrets of shapes an operator really types or pastes.

    python attack_redaction.py <loader folder>                    version 1 detectors alone
    python attack_redaction.py <loader folder> --proposed         plus detectors_proposed.PROPOSED
    python attack_redaction.py <loader folder> --proposed --cards <cards.jsonl>
                                                                  also: how many real cards each proposal would drop

Every value is assembled from parts at run time, so this file holds no secret-shaped literal. For each case it
prints which detectors fired. A control that nothing catches is a broken control (exit 1). With --proposed each
proposal is also red-proved: with it disabled, at least one case it alone catches must pass uncaught (exit 1 if a
proposal is redundant or dead). And every `clean` line, ordinary operator prose, must trip nothing (exit 1).

What fails the run (exit 1), with or without --proposed:
- a control or a shape under test that no detector catches (only an `accepted` shape may pass uncaught);
- a clean line that trips any detector, a standing one included;
- a detector no case depends on: switched off alone, every case is still caught by another, or it catches none.
  So taking any one detector out of the loader turns this file red, and a detector added without a case of its
  own does too (uxxx's take-one-out of 9 Oct: before this, 5 of 29 removals were noticed here);
- a seat case (SEAT_CASES) that the seat's own detectors miss, that is caught without the seat named, or that
  the loader's own detect() does not catch when the seat is named;
- two detectors under one name (the red-proof switches a rule off by name), the loader's own detect() giving
  other names than its table for any case or clean line, or a run with fewer cases than detectors.
A verdict is always exit 1 with the reason printed. A traceback is a crash of this file, not a verdict.
"""
import importlib.util
import json
import os
import sys

args = [a for a in sys.argv[1:] if not a.startswith("--")]
flags = [a for a in sys.argv[1:] if a.startswith("--")]
loader_dir = args[0] if args else "C:/Projects/meshy-test/transcript-cards"
cards_path = sys.argv[sys.argv.index("--cards") + 1] if "--cards" in sys.argv else None
if cards_path in args:
    args.remove(cards_path)
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("transcript_cards", os.path.join(loader_dir, "transcript_cards.py"))
tc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tc)
V1 = list(tc.DETECTORS)
PROPOSED = []
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if "--proposed" in flags:
    from detectors_proposed import PROPOSED
if "--followup" in flags:
    # For a loader that already holds the first proposals (1.1): swap the rules the follow-up replaces, and
    # treat both the swapped and the added rules as the proposals under test.
    import detectors_followup
    taken = set(detectors_followup.REPLACE) | {name for name, _ in detectors_followup.ADD}
    V1 = [(name, rx) for name, rx in V1 if name not in taken]      # a loader that already has them: test these
    PROPOSED = list(detectors_followup.REPLACE.items()) + list(detectors_followup.ADD)


def detect(text, disabled=()):
    return [name for name, rx in V1 + PROPOSED if name not in disabled and rx.search(text)]


MIX = "aB3dE5gH7jK9mN2pQ4rS6tU8vW0xY1zA"          # 32 characters: lower, upper, digits
HEX = "3f9a1c0b7e2d4a6f8c1b0e9d7a5f3c2b"          # 32 hex characters
SL = "/"
NL = chr(10)
DASH = "-" * 5
PWV = "Winter" + "2026x9"


def mix(n):
    return (MIX * (n // len(MIX) + 1))[:n]


def hexs(n):
    return (HEX * (n // len(HEX) + 1))[:n]


CASES = [
    # (name, text, expect)   "caught": a control version 1 must catch.  "open": a shape a later rule was written
    # for; the loader beside this file must catch it too.
    # "accepted": a shape no rule here catches, kept so the limit stays visible.
    ("control: vendor key", "use " + "gh" + "p_" + mix(36), "caught"),
    ("control: jwt", "token " + "ey" + "J" + mix(20) + "." + mix(24) + "." + mix(16), "caught"),
    ("control: named 12+ mixed", "API_KEY=" + mix(20), "caught"),
    ("control: url credentials", "postgresql://app:" + "pw" + mix(6) + "@db.internal:5432/x", "caught"),
    ("control: bare 40 mixed token", "the token is " + mix(40), "caught"),
    ("control: password phrase", "the password is " + "hunter" + "2", "caught"),
    ("control: secret in a url query", "https://example.test/cb?token=" + mix(40), "caught"),
    ("short password after a colon", "password: " + "hunter" + "2", "open"),
    ("short password after equals", "DB_PASSWORD=" + "s3cret", "open"),
    ("letters-only password after a colon", "password=" + "correcthorsebattery", "open"),
    ("passphrase of words", "passphrase: correct horse battery staple", "open"),
    ("German name", "Passwort: " + "geheim" + "1234567", "open"),
    ("pw abbreviation", "pw: " + PWV, "open"),
    ("bare pass name", "pass=" + "hunter" + "22", "open"),
    ("password as a block value on the next line", "db:\n  password:\n    " + PWV + "\n  user: app", "open"),
    ("password as a block value, a plain word", "db:\n  password:\n    " + "correcthorsebattery" + "\n  user: app", "open"),
    ("pasted form, value alone on the next line", "Username:\nvexor\nPassword:\n" + PWV + "\nthen press Return", "open"),
    ("redeem code in quotes, no long unbroken run",
     "printf '%s' '" + "-".join([MIX[0:6], MIX[6:15], MIX[15:23]]) + "_" + MIX[23:30] + "' | tool redeem", "open"),
    ("generated code of 27 characters in backticks",
     "use the code `" + MIX[0:8] + "-" + MIX[8:17] + "_" + MIX[17:25] + "` once", "open"),
    ("offer link on an address and port, capability in the path",
     "fetch http://192.168.50.30:64715" + SL + MIX[0:12] + "_" + MIX[12:26] + "-" + MIX[17:32], "open"),
    ("offer link, a 24-character capability before the file name",
     "fetch http://192.168.50.30:64715" + SL + MIX[0:11] + "_" + MIX[11:23] + SL + "boot-image-20260929-build-7.bin", "open"),
    ("password then for-clause, colon", "password for the box: " + PWV, "open"),
    ("password then for-clause, is", "the password for the box is " + PWV, "open"),
    ("pin", "PIN: 482913", "open"),
    ("one-time code", "2FA code 482913", "open"),
    ("recovery code", "recovery code: abcd-efgh-1234-wxyz", "open"),
    ("cli flag with a space, password", "mysql -u root --password " + PWV, "open"),
    ("cli flag with a space, token", "tool login --token " + hexs(40), "open"),
    ("cli -p flag", "sshpass -p " + PWV + " ssh host", "open"),
    ("token right after a slash (refresh token shape)", "refresh token 1" + SL + SL + "0g" + mix(40), "open"),
    ("authorization Token with hex", "Authorization: Token " + hexs(40), "open"),
    ("short Basic credentials", "Authorization: Basic " + "dXNlcjpwYXNz", "open"),
    ("service variable family, id", "BRAIN_SVC_CLIENT_ID=" + hexs(32) + ".access", "open"),
    ("service variable family, bare name", "BRAIN_SVC_AUD " + hexs(64), "open"),
    ("access header, client id", "CF-Access-Client-Id: " + hexs(32) + ".access", "open"),
    ("secret word then hex, no separator", "client secret " + hexs(64), "open"),
    ("private key word then hex, no separator", "private key " + hexs(64), "open"),
    ("webhook url, token in the path", "https://discord.com/api/webhooks/123456789012345678" + SL + mix(60), "open"),
    ("chat hook url, token in the path", "https://hooks.example.test/services/T0001/B0002" + SL + mix(24), "open"),
    ("bot token in a url path", "https://api.example.test/bot123456789:" + "AA" + mix(33) + "/sendMessage", "open"),
    ("named value in quotes with a space", 'password: "' + "Winter 2026 x9" + '"', "open"),
    ("env line, lower-case auth name", "auth=" + mix(20), "open"),
    ("cookie", "Cookie: session=" + mix(40), "open"),
    ("connection string password field", "Server=db;User Id=sa;Password=" + "Winter" + "2026;", "open"),
    ("private key block", "here is the key" + NL + DASH + "BEGIN OPENSSH " + "PRIVATE KEY" + DASH + NL
     + "b3BlbnNzaC1rZXk" + NL + DASH + "END OPENSSH " + "PRIVATE KEY" + DASH, "open"),
    ("bearer value outside an Authorization header",
     "curl -H 'X-Upstream: bearer " + hexs(40) + "' http://127.0.0.1:8080/x", "open"),
    ("cloud access key id, 20 characters", "the access key id is " + "AK" + "IA" + "Q3W7E9R2T5Y8U1I4", "open"),
    ("personal access token variable", "GITHUB_" + "PAT=" + mix(20), "open"),
    ("pairing code named by its words", "your pairing code " + mix(18), "open"),
    ("MAC address", "the interface en0 is " + ":".join(["3c", "22", "fb", "1a", "9e", "07"]), "open"),
    ("64 hex beside a hardware word", "the firmware hash is " + hexs(64), "open"),
    ("letters-only password after is", "the password for the box is " + "correcthorse", "accepted"),
    ("31-character mixed token, bare", "key " + mix(31) + " works", "accepted"),
    ("cloud secret with slashes, bare", "secret " + mix(14) + SL + mix(12) + "+" + mix(12), "accepted"),
    ("credentials as two bare words", "log in with vexor and " + PWV, "accepted"),
    ("pasted form, a plain word alone on the next line", "Password:\n" + "correcthorse" + "\nthen press Return", "accepted"),
]
# Ordinary operator prose about secrets, ports and flags. None of it is a secret; none of it may be dropped.
CLEAN = [
    "the password is stored in the keychain, do not print it",
    "rotate the token and tell me when it is done",
    "run pytest -p no:cacheprovider and check exit code 137",
    "pg_isready -h 127.0.0.1 -p 55447 says accepting connections",
    "ssh -p 22 host and close code 4401 means the token expired",
    "is the password field required, and which key id signed it?",
    "the signing key id is " + hexs(16) + " and the commit is " + hexs(40),
    "sha256 " + hexs(64) + " for boot.bin",
    "use --token <your token> or set GITHUB_TOKEN=$PAT in the shell",
    "password: <ask the operator>",
    "auth: bearer, and the secret lives in keys.txt",
    "the webhook docs are at https://example.test/docs/webhooks and hooks are optional",
    "PIN the dependency versions, then merge PR 163",
    "2FA is on for the account; the recovery code is in the safe",
    "peer 0447bf92-25fb-5230-b531-5a0d9c00d8c7 signed msg/1791098364506-memory-bxxxxx-286d76",
    "gate row: nextest_pass=true, clippy_ok=true, tests pass: 163 of 163",
    "password: none, the box uses a key; password_required=false",
    # pxx on loader 1.1: sudo's own prompt, pasted with the terminal output after it, is not a password
    "$ sudo systemctl restart meshy\nPassword:\nSorry, try again.\nPassword:\nJob for meshy.service failed",
    "    $ sudo ls /var/db\n    Password:\n    total 0\n    drwx------  2 root  wheel  64 Oct  8 09:00 x",
    "$ sudo journalctl -u meshy\nPassword:\n2026-10-04T08:05:01 meshy started, pid 4412",
    "reply in thread `transcripts-to-mesh-graph-20261004` under key 'node-awareness-20261003-followup'",
    "the class 'TranscriptCardsLoaderVersionOneDotOne2' and the file `second_scan_of_the_cards_output.py`",
    "see https://docs.example.test/document/d/" + mix(44) + "/edit and https://example.test/x/commit/" + hexs(40),
    "http://192.168.50.20:6310/v1/livez answers 200 and http://127.0.0.1:9090/-/ready is up",
    "get http://192.168.50.30:8080/releases/transcript-graph-recall-20261004/boot-image-20260929-build-7.bin",
    "open http://192.168.50.20:6310/v1/mesh/mentions?ref=hash:" + hexs(16) + " and /v1/mesh/search?ns=shared/decisions",
]
# A seat's own detectors (the loader's SEAT_DETECTORS, applied with --seat). Each case must be caught with the
# seat named and by nothing without it: caught without the seat, it would say nothing about the seat's rule.
SEAT_CASES = [
    # (name, text, seat)
    ("pxx: short hash beside a hardware word", "the dtb hash " + hexs(10) + " changed", "pxx"),
    ("pxx: upper-case UUID", "volume " + "-".join([hexs(8), hexs(4), hexs(4), hexs(4), hexs(12)]).upper()
     + " is the data volume", "pxx"),
    ("dxxxxx: 64 hex beside the word serial", "serial " + hexs(64) + " read from the box", "dxxxxx"),
    ("dxxxxx: 64 digits beside the word mac", "mac " + ("0123456789" * 7)[:64] + " from the label", "dxxxxx"),
    ("dxxxxx: short hash beside u-boot", "the u-boot env hash is " + hexs(10) + " after the flash", "dxxxxx"),
]
SEATS = dict(getattr(tc, "SEAT_DETECTORS", {}))


def seat_detect(text, seat, disabled=()):
    return [name for name, rx in SEATS.get(seat, []) if name not in disabled and rx.search(text)]


broken = 0
state_of = {}
for name, text, expect in CASES:
    fired = detect(text)
    state = "caught" if fired else "MISSED"
    if expect == "caught" and not fired:
        broken += 1
        state = "CONTROL BROKEN"
    state_of[name] = (state, fired, expect)
    print(f"{state:15} [{expect:8}] {name}: {', '.join(fired) or '-'}")
opened = [n for n, (_, _, e) in state_of.items() if e == "open"]
missed = [n for n in opened if state_of[n][0] == "MISSED"]
print(f"\n{len(CASES)} cases; {broken} broken control(s); {len(missed)} of {len(opened)} shapes under test not caught; "
      f"{sum(1 for n, (s, _, e) in state_of.items() if e == 'accepted' and s == 'MISSED')} accepted limit(s) still pass")

v1_names = {name for name, _ in V1}
v1_dirty = [(line, [n for n in detect(line) if n in v1_names]) for line in CLEAN]
v1_dirty = [(line, fired) for line, fired in v1_dirty if fired]
dirty = [(line, [n for n in detect(line) if n not in v1_names]) for line in CLEAN]
dirty = [(line, fired) for line, fired in dirty if fired]
print(f"clean operator prose, {len(CLEAN)} lines: version 1 trips on {len(v1_dirty)}, the proposals on {len(dirty)}")
for line, fired in v1_dirty:
    print("   version 1 false positive:", fired, "|", line[:70])
for line, fired in dirty:
    print("   PROPOSAL FALSE POSITIVE:", fired, "|", line[:70])

# Every detector must be needed here: switched off alone, at least one case that should be caught goes uncaught.
must_catch = [(c, text) for c, text, e in CASES if e != "accepted"]
# A detector is switched off by NAME, so two entries under one name would vouch for each other: a dead rule
# appended under a live rule's name would pass the red-proof below. Names are unique, per table.
all_names = [name for name, _ in V1 + PROPOSED]
repeated = sorted({n for n in all_names if all_names.count(n) > 1}
                  | {f"{seat}/{n}" for seat, rules in SEATS.items()
                     for n in [r[0] for r in rules] if [r[0] for r in rules].count(n) > 1})
for name in repeated:
    print("   DETECTOR NAME USED TWICE:", name)
# The cases above run against the loader's TABLE. The loader's own detect() must give the same names for every
# case and every clean line, or a loader whose detect() no longer searches the table would pass. Not compared
# in the old modes that swap rules in and out of the table.
disagree = []
if not PROPOSED:
    for label, text in [(c, text) for c, text, _ in CASES] + [("clean line", line) for line in CLEAN]:
        if tc.detect(text) != detect(text):
            disagree.append(label)
            print(f"   THE LOADER'S detect() DISAGREES WITH ITS TABLE on {label}: {tc.detect(text)} vs {detect(text)}")
empty = not V1 or len(must_catch) < len(V1) or not CLEAN
if empty:
    print(f"   TOO LITTLE TO JUDGE: {len(V1)} detectors, {len(must_catch)} cases that must be caught, {len(CLEAN)} clean lines")
unneeded = [name for name, _ in V1 + PROPOSED
            if not any(detect(text) and not detect(text, disabled=(name,)) for _, text in must_catch)]
print(f"red-proof of every detector: {len(V1 + PROPOSED) - len(unneeded)} of {len(V1 + PROPOSED)} are the sole "
      f"catcher of at least one case")
for name in unneeded:
    print("   NO CASE DEPENDS ON:", name)

seat_broken = 0
for name, text, seat in SEAT_CASES:
    own, without = seat_detect(text, seat), detect(text)
    # Through the loader's own detect() as well: the rules above are read from its table, and a loader that held
    # the table but no longer applied it for a named seat would pass a check that only reads the table.
    try:
        applied = [n for n in tc.detect(text, seat=seat) if n in own]
    except TypeError:                                  # a loader whose detect() takes no seat applies none
        applied = []
    ok = bool(own) and not without and applied == own and not tc.detect(text)
    seat_broken += not ok
    print(f"{'caught' if ok else 'SEAT CASE BROKEN':15} [seat    ] {name}: {', '.join(own) or '-'}"
          + (f" (and without the seat: {', '.join(without)})" if without else "")
          + ("" if applied == own else " (the loader's detect() does not apply it for this seat)"))
seat_unneeded = [f"{seat}/{name}" for seat, rules in sorted(SEATS.items()) for name, _ in rules
                 if not any(s == seat and seat_detect(text, seat) and not seat_detect(text, seat, disabled=(name,))
                            for _, text, s in SEAT_CASES)]
seat_dirty = [(seat, line, seat_detect(line, seat)) for seat in sorted(SEATS) for line in CLEAN if seat_detect(line, seat)]
print(f"seat detectors: {sum(len(r) for r in SEATS.values()) - len(seat_unneeded)} of "
      f"{sum(len(r) for r in SEATS.values())} are the sole catcher of a seat case; clean prose trips "
      f"{len(seat_dirty)} time(s)")
for name in seat_unneeded:
    print("   NO SEAT CASE DEPENDS ON:", name)
for seat, line, fired in seat_dirty:
    print(f"   SEAT FALSE POSITIVE ({seat}):", fired, "|", line[:70])

dead = 0
if PROPOSED:
    print("\nred-proof of each proposal (disabled, a case it alone catches must pass):")
    for name, _ in PROPOSED:
        alone = [c for c, text, _ in CASES if detect(text) and not detect(text, disabled=(name,))]
        print(f"   {name}: {'sole catcher of ' + str(len(alone)) + ' case(s)' if alone else 'REDUNDANT OR DEAD'}")
        dead += not alone
    if missed:
        print("still missed with the proposals:", missed)

if cards_path:
    cards = [json.loads(line) for line in open(cards_path, encoding="utf-8") if line.strip()]
    kept = getattr(tc, "_card_strings")
    print(f"\nreal cards: {len(cards)}. Cards each proposal would drop (id prefix, type, date; never text):")
    total = set()
    for name, rx in PROPOSED:
        hit = [c for c in cards if rx.search(kept(c))]
        total |= {c["id"] for c in hit}
        print(f"   {name}: {len(hit)}" + "".join(f"\n      {c['id'][:8]} {c['type']} {c['date']}" for c in hit[:6]))
    print(f"   all proposals together: {len(total)} of {len(cards)} cards")
sys.exit(1 if broken or missed or v1_dirty or dirty or dead or unneeded or seat_broken or seat_unneeded
         or seat_dirty or repeated or disagree or empty else 0)
