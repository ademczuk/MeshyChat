"""Unit tests for tools/meshy_node/antibodies.py."""
from __future__ import annotations

import hashlib
import json
import multiprocessing as mp
import subprocess
import sys
import time
from pathlib import Path
import pytest

from tools.meshy_node.antibodies import (
    CLAIM_TTL_S,
    DOUBT_AFTER,
    FUZZY_JACCARD_THRESHOLD,
    RESOLUTION_WINDOW_TURNS,
    SHORT_MESSAGE_MAX_CHARS,
    SUPPRESS_AFTER,
    AntibodyEntry,
    ClaimsStore,
    FixTrust,
    ResolutionTracker,
    benign_exit,
    collapse_paths,
    compute_fix_sig,
    error_signature,
    extract_error_code,
    has_runner_verdict,
    is_display_only_command,
    jaccard_similarity,
    looks_failed,
    main as antibodies_main,
    match_antibody,
    normalize_error,
    redact_sample,
    redact_text,
    split_shell_stages,
    strip_ansi,
    tokenize_error,
)


# ---- 1. Ordered Noise Normalizer & Fingerprinter

def test_strip_ansi_removes_csi_and_osc_sequences():
    raw = "\x1b[31m\x1b]0;window-title\x07ERROR:\x1b[0m failed \x1b]8;;https://example.com\x1b\\link\x1b]8;;\x1b\\"
    assert strip_ansi(raw) == "ERROR: failed link"


def test_normalize_uuid_and_hex_hash_before_iso_timestamp():
    # A 40-char SHA-1 hex digest containing "20261020" must become <hash> intact,
    # never split into <ts>... by timestamp regexes.
    digest_with_date = "20261020a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
    uuid_val = "550e8400-e29b-41d4-a716-446655440000"
    iso_val = "2026-10-20T07:31:00.123Z"
    msg = f"commit {digest_with_date} req {uuid_val} at {iso_val}"
    norm = normalize_error(msg)
    assert norm == "commit <hash> req <uuid> at <ts>"

    # Boundary: 31-char hex is not replaced as <hash>; 32-char hex is replaced as <hash>.
    hex_31 = "a" * 31
    hex_32 = "b" * 32
    assert normalize_error(f"short {hex_31} full {hex_32}") == f"short {hex_31} full <hash>"


def test_normalize_durations_pids_and_host_port_before_line_col():
    # 127.0.0.1:5434 must become <port>, NOT 127.0.0.1<pos>.
    # 2026-10-20T07:31:00Z must become <ts>, NOT 2026-10-20T07<pos>.
    # :42:10 must become <pos>.
    msg = (
        "2026-10-20T07:31:00Z pid 4321 PID=9876 connected to 127.0.0.1:5434 "
        "and db.internal.example:5432 in 123ms (4.5s) at module.py:42:10 line 99"
    )
    norm = normalize_error(msg)
    assert norm == (
        "<ts> <pid> <pid> connected to <port> and <port> in <ts> (<ts>) at module.py<pos> <pos>"
    )


def test_collapse_paths_replaces_temp_dirs_before_absolute_paths():
    msg = (
        r"win temp C:\Users\user\AppData\Local\Temp\pytest-of-vexor\run-1 "
        r"unix temp /tmp/mut-998877/check.py "
        r"token tmp-abc123xyz "
        r"win abs C:\Projects\MeshyChat\tools\meshy_node\cards.py:42:10 "
        r"unix abs /Users/alice/repos/meshychat/src/server.py:18"
    )
    norm = normalize_error(msg)
    assert norm == (
        "win temp <tmp> unix temp <tmp> token <tmp> "
        "win abs <path>/cards.py<pos> unix abs <path>/server.py<pos>"
    )
    # Idempotence check
    assert normalize_error(norm) == norm
    assert collapse_paths(collapse_paths(msg)) == collapse_paths(msg)


def test_error_signature_uses_nul_separator_and_12_hex_chars():
    norm = normalize_error("OperationalError: connection refused at 127.0.0.1:5434")
    sig = error_signature("Postgres", "OperationalError: connection refused at 127.0.0.1:9999")
    expected = hashlib.sha256(f"postgres\0{norm}".encode("utf-8")).hexdigest()[:12]
    assert sig == expected
    assert len(sig) == 12
    # Category boundary: "ab" + "\0" + "c" != "a" + "\0" + "bc"
    assert error_signature("ab", "c", is_normalized=True) != error_signature("a", "bc", is_normalized=True)


def test_redact_sample_redacts_before_truncation_and_collapses_paths_before_base64():
    # A secret token straddling the max_chars boundary must be redacted to <redacted> BEFORE slicing,
    # never sliced in half leaving a raw "sk-..." prefix in the truncated output.
    prefix = "x" * 44
    secret = "sk-ProjSecretTokenValue1234567890ABCDEF"
    raw = f"{prefix} {secret} trailing text"
    # At max_chars=58, naive truncation before redaction slices `secret` to `sk-ProjSecret` (10 chars after sk-)
    # which fails regex minimum length and leaks the secret prefix. Redacting first turns it into `<redacted>`.
    sample = redact_sample(raw, max_chars=58)
    assert "sk-" not in sample
    assert "<redacted>" in sample
    assert len(sample) <= 58
    assert "sk-" not in redact_sample(raw, max_chars=52)

    # A 45-char directory name in an absolute path must collapse to <path>/app.py first,
    # never get partially replaced by LONG_BASE64_RE as /Users/<redacted>/app.py.
    long_dir = "/Users/AdminDirectorySegmentWith45MixedChars12345678/app.py"
    redacted_dir = redact_text(f"error in {long_dir} with Bearer SecretBearerToken987654")
    assert redacted_dir == "error in <path>/app.py with Bearer <redacted>"
    # Quoted JSON key-value secrets ({"password": "swordfish123"}) must be redacted while keeping JSON delimiters
    json_secret = 'error: {"password": "swordfish123", "api_key": "secret_val_123"}'
    redacted_json = redact_sample(json_secret)
    assert "swordfish123" not in redacted_json
    assert "secret_val_123" not in redacted_json
    assert redacted_json == 'error: {"password": "<redacted>", "api_key": "<redacted>"}'
    assert redact_sample(redacted_json) == redacted_json

    # Quoted secrets containing spaces or punctuation ("abc defghi", "correct horse battery staple!")
    # must have their entire quoted contents redacted, and quoted keys with unquoted values must also redact.
    multiword_secret = 'error: {"password": "abc defghi", "secret": "correct horse battery staple!"}'
    redacted_multiword = redact_sample(multiword_secret)
    assert "abc" not in redacted_multiword
    assert "defghi" not in redacted_multiword
    assert "horse" not in redacted_multiword
    assert redacted_multiword == 'error: {"password": "<redacted>", "secret": "<redacted>"}'
    assert redact_sample(redacted_multiword) == redacted_multiword
    assert redact_sample('error: "password": swordfish123') == 'error: "password": <redacted>'

    # Idempotence check
    assert redact_sample(redact_sample(raw, max_chars=120), max_chars=120) == redact_sample(raw, max_chars=120)
    assert redact_sample(raw, max_chars=0) == ""

    # Long base64 mixed-class guard (M37, M38, M39): 40+ char tokens lacking upper, lower, or digit are preserved
    lower_digits_only = "z" * 35 + "123456"
    upper_digits_only = "Z" * 35 + "123456"
    letters_only = "Z" * 20 + "z" * 21
    assert redact_text(lower_digits_only) == lower_digits_only
    assert redact_text(upper_digits_only) == upper_digits_only
    assert redact_text(letters_only) == letters_only

    # Bounded input overflow (M43): when collapsed input > 4096 chars but redacts down to <= max_chars,
    # redact_sample must still append "..." to indicate the tail was truncated by bounded slicing.
    many_secrets = " ".join(["sk-" + ("A" * 200)] * 30)
    assert len(many_secrets) > 4096
    assert redact_sample(many_secrets, max_chars=400).endswith("...")
    assert redact_sample("abcdef", max_chars=3) == "abc"


def test_redact_sample_redacts_env_style_secrets_and_basic_auth():
    cases = {
        "env DB_PASSWORD": ("DB_PASSWORD=hunter2hunter2", "hunter2hunter2"),
        "env SECRET_KEY": ("SECRET_KEY=django-insecure-abc123", "django-insecure-abc123"),
        "env OPENAI_API_KEY": ("OPENAI_API_KEY=abcd1234efgh5678", "abcd1234efgh5678"),
        "env AWS_SECRET_ACCESS_KEY": ("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG", "wJalrXUtnFEMI/K7MDENG"),
        "env MESHY_TOKEN": ("MESHY_TOKEN=abcdef123456", "abcdef123456"),
        "Authorization Basic": ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA=="),
        "client_secret": ("client_secret=abcdef123456", "abcdef123456"),
        "access_token": ("access_token=abcdef123456", "abcdef123456"),
        "pgpassword": ("PGPASSWORD=abcdef123456 psql", "abcdef123456"),
        "short json password 1 char": ('{"password": "x"}', '"x"'),
        "short json password 5 chars": ('{"password": "abc12"}', "abc12"),
        "url empty user": ("https://:pw99@host.local/api", "pw99"),
        "url at in password": ("https://u:p@ssw0rd99@host.local/api", "ssw0rd99"),
        "url token as user": ("https://ghtokenSecret123@github.com/org/repo", "ghtokenSecret123"),
        "cli flag --password space": ("mysql --password mySecretPass1 -u root", "mySecretPass1"),
        "private_key": ("PRIVATE_KEY=privKeyVal987", "privKeyVal987"),
        "signing_key": ("SIGNING_KEY=signKeyVal654", "signKeyVal654"),
        "sk_live_ prefix": ("key is sk_live_1234567890abcdef", "sk_live_1234567890abcdef"),
        "comma in env value": ("DB_PASSWORD=abc,defghi", "defghi"),
        "semicolon in env value": ("DB_PASSWORD=abc;defghi", "defghi"),
        "cookie header": ("Cookie: sessionid=xyz987secret", "xyz987secret"),
    }
    for label, (raw, secret) in cases.items():
        redacted = redact_sample(raw)
        assert secret not in redacted, f"{label} leaked in {redacted!r}"
        assert "<redacted>" in redacted, f"{label} missing placeholder in {redacted!r}"

    # Authorization: Basic <token> must preserve the "Basic " scheme prefix (N6)
    assert redact_sample("Authorization: Basic dXNlcjpwYXNzd29yZA==") == "Authorization: Basic <redacted>"
    # Run the full benign corpus AND the full secret corpus (including N14 / t26 forms and PGPWD=) together
    benign_corpus = (
        "monkey=5",
        "turkey: 3",
        "hotkey: ctrl+s",
        "donkey=1",
        "turnkey: yes",
        "primary_key=True",
        "foreign_key: users.id",
        "cache_key = f'x:{n}'",
        "sort_key=created_at",
        "partition_key: tenant_id",
        "public_key=ssh-rsa",
        "max_tokens=1024",
        "num_tokens: 512",
        "min_tokens=10",
        "total_tokens: 2048",
        "tokenizer=bpe",
        "token_count=42",
        "api_key_name=OPENAI",
        "if session_id: return",
        "obj.secret_count: 3",
        "Cookie: required",
        "Authorization: header missing",
        "ssh://git@github.com/x/y",
        f"identifier {'z' * 42} ok",
        "camelcase AbcDefGhiJklMnoPqrStuVwxYzAbcDefGhiJklMnoP ok",
        "max_keys=5",
        "csrf_token = get_token(request)",
    )
    n14_secret_corpus = (
        ("PGPWD=pgSecret99", "pgSecret99"),
        ("pwd=myPwdSecret1", "myPwdSecret1"),
        ("passphrase=correctHorseBatteryStaple", "correctHorseBatteryStaple"),
        ('{"accessToken": "eyJhbGciOiJIUzI1NiJ9"}', "eyJhbGciOiJIUzI1NiJ9"),
        ('{"refreshToken": "rt_secret_987654"}', "rt_secret_987654"),
        ('{"clientSecret": "cs_secret_987654"}', "cs_secret_987654"),
        ("dbPassword=dbSecretValue123", "dbSecretValue123"),
        ("authToken: authTokVal987", "authTokVal987"),
        ("apiKey=apiKeyVal987", "apiKeyVal987"),
        ("secretKey=secKeyVal987", "secKeyVal987"),
        ("privateKey: privKeyVal987", "privKeyVal987"),
        ("OPENAI_KEY=oaKeyVal123456", "oaKeyVal123456"),
        ("STRIPE_KEY=rk_live_9876543210", "rk_live_9876543210"),
        ("LICENSE_KEY=LIC-SECRET-9988", "LIC-SECRET-9988"),
        ("DB_PASSWORD_PROD=prodPass9876", "prodPass9876"),
        ("SECRET_KEY_BASE=baseSecretHex9876", "baseSecretHex9876"),
        ("GITHUB_TOKEN_2=gho_secondTokenVal123", "gho_secondTokenVal123"),
        ("password1=firstPasswordVal", "firstPasswordVal"),
        ("DB_PASSWORD=1234", "1234"),
        ("token: 99999", "99999"),
        ("https://tok123@host.local/api", "tok123"),
        ("--password=cliEqPass123", "cliEqPass123"),
        ("credentials=credSecretVal99", "credSecretVal99"),
        ("machine h login bob password hunter2", "hunter2"),
        ("curl -u bob:hunter2pw https://example.com", "hunter2pw"),
        ("mysql -phunter2pw mydb", "hunter2pw"),
        ("KEY=bareKeySecret99", "bareKeySecret99"),
        ("auth=bareAuthSecret99", "bareAuthSecret99"),
        ("password=f'fstringSecret_{val}'", "fstringSecret_"),
        ("Set-Cookie: session=cookieVal99", "cookieVal99"),
    )
    assert redact_sample("Set-Cookie: session=cookieVal99") == "Set-Cookie: session=<redacted>"
    for benign in benign_corpus:
        assert redact_sample(benign) == benign, f"over-redacted benign input {benign!r} -> {redact_sample(benign)!r}"
    for raw_sec, secret_val in n14_secret_corpus:
        red_sec = redact_sample(raw_sec)
        assert secret_val not in red_sec, f"leaked {secret_val!r} in {red_sec!r} for {raw_sec!r}"
        assert "<redacted>" in red_sec, f"missing <redacted> in {red_sec!r} for {raw_sec!r}"
    # Combined message containing both benign items and secret items together
    combined = " | ".join(list(benign_corpus[:10]) + [raw for raw, _ in n14_secret_corpus])
    redacted_combined = redact_text(combined)
    for benign in benign_corpus[:10]:
        assert benign in redacted_combined, f"benign {benign!r} altered in combined text"
    for raw_sec, secret_val in n14_secret_corpus:
        assert secret_val not in redacted_combined, f"secret {secret_val!r} leaked in combined text ({raw_sec!r})"

    # Pre-redaction cap boundary after whitespace collapse must never slice a secret token in half (N10 / t19)
    ws_padded = (" " * 4080) + "sk-a1B2c3D4e5F6g7H8i9J0"
    assert "sk-" not in redact_sample(ws_padded)
    assert "<redacted>" in redact_sample(ws_padded)
    long_ws_padded = ("x " * 2045) + "ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4"
    assert "ghp_" not in redact_sample(long_ws_padded, max_chars=5000)

    # Bounded input that exceeded cap but collapsed after redaction still gets "..." suffix (M36)
    repeated_urls = " ".join(["https://user:secretpass1234567890@example.com/path"] * 120)
    capped_sample = redact_sample(repeated_urls, max_chars=400)
    assert capped_sample.endswith("...")
    assert len(capped_sample) <= 400


def test_normalize_error_and_redact_text_linear_time_on_50kb_inputs():
    # Regression test for quadratic backtracking in HOST_PORT_RE, LONG_BASE64_RE ('=' and '+' runs),
    # KV_*_SECRET_RE ('password'*6000 and 'key'*33333+'=x'), WIN_TEMP_PATH_RE ('C:/a'*12500),
    # URL_CRED_RE ('a://'+'b'*50000), COOKIE_HEADER_RE ('Cookie: a '*10000 and 'Cookie: '+'a'*100000),
    # and RUNNER_VERDICT_PATTERNS ('='*50000)
    for label, fn, payload in (
        ("HOST_PORT_RE dotted", normalize_error, "a." * 25_000),
        ("WIN_TEMP_PATH_RE norm", normalize_error, "C:/a" * 12_500),
        ("LONG_BASE64_RE alnum", redact_text, "abcdefghij0123456789abcdefghij0123456789 " * 1_250),
        ("LONG_BASE64_RE equals", redact_text, "=" * 50_000),
        ("LONG_BASE64_RE plus", redact_text, "+" * 50_000),
        ("KV_SECRET_RE password*6000", redact_sample, "password" * 6_000),
        ("KV_SECRET_RE key*33333+=x", redact_text, ("key" * 33_333) + "=x"),
        ("COOKIE_HEADER_RE 100KB words", redact_text, "Cookie: a " * 10_000),
        ("COOKIE_HEADER_RE 100KB single token", redact_text, "Cookie: " + ("a" * 100_000)),
        ("WIN_TEMP_PATH_RE redact", redact_sample, "C:/a" * 12_500),
        ("URL_CRED_RE scheme", redact_text, "a://" + ("b" * 50_000)),
        ("RUNNER_VERDICT equals", has_runner_verdict, "=" * 50_000),
        ("looks_failed equals", lambda s: looks_failed("pytest -q", s, exit_code=0), "=" * 50_000),
    ):
        t0 = time.perf_counter()
        out = fn(payload)
        elapsed = time.perf_counter() - t0
        assert elapsed < 1.0, f"{label} took {elapsed:.2f}s on ~50-100 KB input"
        assert isinstance(out, bool) or (isinstance(out, str) and len(out) > 0)


# ---- 2. Three-Tier Error Matcher

def test_match_antibody_tier1_exact_and_category_isolation():
    entries = [
        {
            "id": "AB-001",
            "category": "postgres",
            "message": "OperationalError: connection to 127.0.0.1:5434 refused (pid 1111)",
            "fix": "Ensure PostgreSQL 17 service is running on port 5434.",
        },
        {
            "id": "AB-002",
            "category": "pytest",
            "message": "OperationalError: connection to 127.0.0.1:5434 refused (pid 1111)",
            "fix": "Different category entry.",
        },
    ]
    res = match_antibody(
        "postgres",
        "OperationalError: connection to 127.0.0.1:5432 refused (pid 9999)",
        entries,
    )
    assert res is not None
    assert res.entry_id == "AB-001"
    assert res.tier == "exact"
    assert res.similarity == 1.0
    assert res["tier"] == "exact"
    assert res.get("entry_id") == "AB-001"

    # Unmatched category returns None
    assert match_antibody("git", "OperationalError: connection to 127.0.0.1:5432 refused (pid 9999)", entries) is None


def test_match_antibody_tier2_fuzzy_jaccard_boundary():
    # Entry has 5 distinct tokens: {"alpha", "bravo", "charlie", "delta", "echo"}
    entries = [
        AntibodyEntry.from_any({
            "id": "AB-010",
            "category": "build",
            "message": "alpha bravo charlie delta echo",
            "fix": "Run clean build.",
        })
    ]
    # Query sharing 3 of 5 tokens -> Jaccard = 3/5 = 0.60 (exact boundary >= 0.60 -> matches Tier 2)
    hit = match_antibody("build", "alpha bravo charlie", entries)
    assert hit is not None
    assert hit.entry_id == "AB-010"
    assert hit.tier == "fuzzy"
    assert hit.similarity == pytest.approx(0.60)

    # Query sharing 2 of 5 tokens -> Jaccard = 2/5 = 0.40 (< 0.60 -> does not match)
    miss = match_antibody("build", "alpha bravo", entries)
    assert miss is None


def test_match_antibody_tier3_short_message_error_code_and_length_boundary():
    entries = [
        {
            "id": "AB-020",
            "category": "mesh",
            "message": "E_UNKNOWN_KEY: key wiki/foo missing from store",
            "fix": "Prefix ontology key with wiki/.",
        },
        {
            "id": "AB-021",
            "category": "mesh",
            "message": "E_UNKNOWN_KEY: key card/bar not found in server index",
            "fix": "Import card key before reading.",
        },
    ]
    # Short message (< 80 chars) with low Jaccard (< 0.60) but matching error code E_UNKNOWN_KEY
    # and sharing "server" token with AB-021 as tie-breaker:
    query = "E_UNKNOWN_KEY: server rejected lookup"
    assert len(normalize_error(query)) < SHORT_MESSAGE_MAX_CHARS
    res = match_antibody("mesh", query, entries)
    assert res is not None
    assert res.tier == "code"
    assert res.code == "E_UNKNOWN_KEY"
    assert res.entry_id == "AB-021"

    # Boundary: normalized message of length == 80 must NOT trigger Tier 3 (requires len(norm) < 80)
    prefix = "E_UNKNOWN_KEY: "
    pad_len = SHORT_MESSAGE_MAX_CHARS - len(prefix)
    msg_len_80 = prefix + ("z" * pad_len)
    assert len(normalize_error(msg_len_80)) == SHORT_MESSAGE_MAX_CHARS
    assert match_antibody("mesh", msg_len_80, entries) is None

    msg_len_79 = prefix + ("z" * (pad_len - 1))
    assert len(normalize_error(msg_len_79)) == SHORT_MESSAGE_MAX_CHARS - 1
    hit_79 = match_antibody("mesh", msg_len_79, entries)
    assert hit_79 is not None and hit_79.tier == "code"

    # Empty query_code must NOT match an entry whose code is None/empty (M50)
    no_code_entries = [{"id": "AB-NOCODE", "category": "mesh", "message": "completely unrelated short text", "fix": "fix"}]
    assert match_antibody("mesh", "another short message with no code", no_code_entries) is None


def test_extract_error_code_variants():
    assert extract_error_code("failed with E_CARD_SUPERSEDES on write") == "E_CARD_SUPERSEDES"
    assert extract_error_code("npm ERR_PNPM_FETCH_404") == "ERR_PNPM_FETCH_404"
    assert extract_error_code("listen EADDRINUSE: port busy") == "EADDRINUSE"
    assert extract_error_code("psycopg.OperationalError: timeout") == "OperationalError"
    assert extract_error_code("fault code 0xC00000FD in mux") == "0xc00000fd"
    assert extract_error_code("server returned HTTP/1.1 503 Unavailable") == "HTTP_503"
    assert extract_error_code("all green") is None


# ---- 3. Display-Safe Failure & Test-Runner Verdict Classifier

def test_display_only_commands_ignore_file_content_unless_own_error():
    assert split_shell_stages("git log -n 5 && rg 'ERROR' src/ | head -n 20") == [
        "git log -n 5",
        "rg 'ERROR' src/",
        "head -n 20",
    ]
    assert is_display_only_command("git -C repo diff HEAD~1 | cat") is True
    assert is_display_only_command("Get-Content app.log | Select-String 'Traceback'") is True
    assert is_display_only_command("pytest -q | tail -n 20") is False
    assert is_display_only_command("") is False
    assert is_display_only_command("   ") is False
    assert is_display_only_command("echo $(rm -rf /tmp/x)") is False
    assert is_display_only_command("cat `whoami`") is False
    assert is_display_only_command("find . -name '*.tmp' -delete") is False
    assert is_display_only_command("find . -name '*.py' -exec rm {} +") is False
    assert is_display_only_command("find . -name '*.py'") is True

    # Display command printing error-shaped file content -> NOT treated as failure
    file_content_with_errors = (
        "=== FAILURES ===\n"
        "Traceback (most recent call last):\n"
        "ValueError: synthetic failure inside log file\n"
        "2 failed in 0.12s\n"
    )
    assert looks_failed("cat tests/fixtures/failure.log | head -n 30", file_content_with_errors, exit_code=0) is False

    # Display command emitting its OWN tool error -> treated as failure even if exit code was masked by pipe
    own_err_output = "cat: missing_file.log: No such file or directory\n"
    assert looks_failed("cat missing_file.log | head -n 10", own_err_output, exit_code=0) is True

    # Git display pipeline emitting unprefixed `fatal: ...` diagnostic -> treated as failure,
    # whereas non-Git display commands reading log lines starting with `fatal:` -> NOT treated as failure.
    git_fatal_output = "fatal: ambiguous argument 'missing': unknown revision or path not in the working tree.\n"
    assert looks_failed("git show missing | cat", git_fatal_output, exit_code=0) is True
    assert looks_failed("cat app.log", "fatal: database unavailable\n", exit_code=0) is False
    assert looks_failed("rg fatal app.log", "fatal: database unavailable\n", exit_code=0) is False
    assert looks_failed("cat README.md", "Normal documentation line without diagnostics.\n", exit_code=0) is False

    # Quoted shell metacharacters inside arguments of display commands must NOT split stages
    assert split_shell_stages("rg 'Error|Exception' worker.log | head -n 10") == [
        "rg 'Error|Exception' worker.log",
        "head -n 10",
    ]
    assert is_display_only_command("rg 'Error|Exception' worker.log") is True
    assert looks_failed("rg 'Error|Exception' worker.log", "RuntimeError: example\n", exit_code=0) is False
    assert looks_failed('grep -E "fatal:|Traceback" app.log', "Traceback (most recent call last):\n", exit_code=0) is False
    assert looks_failed("printf 'RuntimeError: example; ignored'", "RuntimeError: example; ignored\n", exit_code=0) is False


def test_benign_exit_exempts_exit_1_for_search_and_diff_commands():
    for cmd in ("grep -rn foo src/", "rg pattern tools/", "diff -u a.txt b.txt", "test -f x", "[ -d dir ]", "git diff --quiet", "git grep bar"):
        assert benign_exit(cmd, 1) is True
        assert benign_exit(cmd, 2) is False
        assert looks_failed(cmd, "", exit_code=1) is False
        assert looks_failed(cmd, "", exit_code=2) is True

    # Non-benign command with exit 1 is a failure
    assert benign_exit("pytest -q", 1) is False
    assert looks_failed("pytest -q", "", exit_code=1) is True

    # Chained commands where an earlier non-benign clause short-circuits before the final benign stage
    # must NOT be exempted as benign exit 1
    assert benign_exit("pytest -q && test -f report.xml", 1) is False
    assert looks_failed("pytest -q && test -f report.xml", "1 failed, 9 passed in 0.12s", exit_code=1) is True
    assert benign_exit("false && grep pat file.txt", 1) is False
    assert looks_failed("false && grep pat file.txt", "", exit_code=1) is True
    assert benign_exit("grep pat a.txt || grep pat b.txt", 1) is True
    assert looks_failed("grep pat a.txt || grep pat b.txt", "", exit_code=1) is False
    assert benign_exit("test -f x && echo yes", 1) is True
    assert looks_failed("test -f x && echo yes", "", exit_code=1) is False
    assert benign_exit("test -f x && pytest -q", 1) is False
    assert looks_failed("test -f x && pytest -q", "", exit_code=1) is True
    assert looks_failed("Get-Content missing.txt", "Get-Content : Cannot find path 'C:\\missing.txt' because it does not exist.", exit_code=0) is True
    assert looks_failed("Get-Content missing.txt", "Get-Content : Cannot find path 'C:\\missing.txt' because it does not exist.", exit_code=None) is True
    assert benign_exit("rg 'foo && bar' file.txt", 1) is True
    assert looks_failed("rg 'foo && bar' file.txt", "", exit_code=1) is False


def test_looks_failed_detects_test_runner_verdicts_when_exit_code_masked():
    assert has_runner_verdict("==================== FAILURES ====================") is True
    assert has_runner_verdict("3 failed, 12 passed in 1.40s") is True
    assert has_runner_verdict("test result: FAILED. 10 passed; 1 failed") is True
    assert has_runner_verdict("not ok 4 - should reject invalid token") is True
    assert has_runner_verdict("# fail 2") is True
    assert has_runner_verdict("15 passed, 0 failed in 0.50s") is False

    # Piped test runner where tail exits 0: still detected as failed!
    assert looks_failed("pytest -q | tail -n 5", "1 failed, 9 passed in 0.8s", exit_code=0) is True
    assert looks_failed("pytest -q | tail -n 5", "10 passed in 0.8s", exit_code=0) is False


# ---- 4. Per-Seat FixTrust (DOUBT_AFTER = 1, SUPPRESS_AFTER = 2)

def test_fix_trust_doubt_suppress_and_fix_sig_reset(tmp_path):
    state_file = tmp_path / "trust-state.json"
    trust = FixTrust(state_file)
    fix_v1 = "Set MESHY_TEMPLATE_DSN before running pytest."

    assert trust.status("AB-100", fix_v1) == "trusted"
    assert trust.should_inject("AB-100", fix_v1) is True
    assert trust.format_notice("AB-100", fix_v1) == fix_v1

    #Seeing before injecting in this turn does not increment recurred_after_inject
    trust.record_seen("sess-1", "AB-100", fix_v1)
    assert trust.status("AB-100", fix_v1) == "trusted"

    # Inject in sess-1, then error recurs once in the same turn -> DOUBT_AFTER (1) -> "doubted"
    trust.record_injected("sess-1", "AB-100", fix_v1)
    rec1 = trust.record_seen("sess-1", "AB-100", fix_v1)
    assert rec1["recurred_after_inject"] == DOUBT_AFTER
    assert trust.status("AB-100", fix_v1) == "doubted"
    assert trust.should_inject("AB-100", fix_v1) is True
    assert trust.format_notice("AB-100", fix_v1) == f"[VERIFY: this fix failed earlier on this machine] {fix_v1}"

    # Recurs a second time after inject with succeeded == 0 -> SUPPRESS_AFTER (2) -> "suppressed"
    rec2 = trust.record_seen("sess-1", "AB-100", fix_v1)
    assert rec2["recurred_after_inject"] == SUPPRESS_AFTER
    assert trust.status("AB-100", fix_v1) == "suppressed"
    assert trust.should_inject("AB-100", fix_v1) is False
    assert trust.format_notice("AB-100", fix_v1) is None

    # Reload from disk preserves suppression
    reloaded = FixTrust(state_file)
    assert reloaded.status("AB-100", fix_v1) == "suppressed"

    # Editing the fix text (fix_sig changes) resets counters, clears _injected_in_turn for that entry only, and restores "trusted"
    reloaded.record_injected("sess-1", "AB-100", fix_v1)
    reloaded.record_injected("sess-1", "AB-OTHER", fix_v1)
    fix_v2 = "Set both MESHY_TEMPLATE_DSN and MESHY_PG_BIN before running pytest."
    assert compute_fix_sig(fix_v1) != compute_fix_sig(fix_v2)
    assert reloaded.status("AB-100", fix_v2) == "trusted"
    assert ("sess-1", "AB-100") not in reloaded._injected_in_turn
    assert ("sess-1", "AB-OTHER") in reloaded._injected_in_turn
    # Calling status with fix=None must NOT reset an existing fix_sig or counters (M90)
    reloaded.record_injected("sess-1", "AB-100", fix_v2)
    assert reloaded.status("AB-100", None) == "trusted"
    assert reloaded.records["AB-100"]["injected"] == 1
    assert reloaded.records["AB-100"]["fix_sig"] == compute_fix_sig(fix_v2)

    # Backfill empty fix_sig when fix text is later supplied (M49)
    reloaded.record_injected("sess-1", "AB-EMPTY-SIG", None)
    assert reloaded.records["AB-EMPTY-SIG"]["fix_sig"] == ""
    reloaded.status("AB-EMPTY-SIG", fix_v2)
    assert reloaded.records["AB-EMPTY-SIG"]["fix_sig"] == compute_fix_sig(fix_v2)


def test_fix_trust_succeeded_protects_from_suppression_and_new_turn_clears_inject():
    trust = FixTrust()
    fix = "Run git worktree prune."
    trust.record_injected("sess-1", "AB-200", fix)
    trust.record_seen("sess-1", "AB-200", fix)
    trust.record_seen("sess-1", "AB-200", fix)
    assert trust.status("AB-200", fix) == "suppressed"

    # Once succeeded > 0, the fix is trusted even with recurred_after_inject >= 2
    trust.record_succeeded("AB-200", fix)
    assert trust.status("AB-200", fix) == "trusted"

    # new_turn prevents next turn's initial sighting from counting as recurrence before re-injection
    trust.record_injected("sess-2", "AB-201", fix)
    trust.new_turn("sess-2")
    trust.record_seen("sess-2", "AB-201", fix)
    assert trust.records["AB-201"]["recurred_after_inject"] == 0


# ---- 5. Turn-Scoped ResolutionTracker & Box-Local ClaimsStore

def test_resolution_tracker_succeeded_and_command_only_moved_past():
    tracker = ResolutionTracker()
    assert tracker.window_turns == RESOLUTION_WINDOW_TURNS

    # 1. Direct command success within 1 turn (turn 1 -> turn 2, delta == 1)
    assert tracker.record_failure("AB-001", "command:pytest -q", turn=1) is None
    res1 = tracker.record_success("command:pytest -q", turn=2)
    assert res1 is not None
    assert res1["entry_id"] == "AB-001"
    assert res1["reason"] == "succeeded"

    # 2. Window expiry boundary: turn 1 -> turn 3 (delta == 2 > 1) does NOT resolve
    tracker.record_failure("AB-002", "command:cargo test", turn=1)
    assert tracker.record_success("command:cargo test", turn=3) is None

    # 3. Command-only moved_past: same command fails within 1 turn with a DIFFERENT entry_id
    tracker.record_failure("AB-010", "command:pytest tests/test_api.py", turn=5)
    # Same entry_id on turn 5 does NOT trigger moved_past
    assert tracker.record_failure("AB-010", "command:pytest tests/test_api.py", turn=5) is None
    # Different entry_id on turn 6 (delta == 1) DOES trigger moved_past for AB-010
    moved = tracker.record_failure("AB-011", "command:pytest tests/test_api.py", turn=6)
    assert moved is not None
    assert moved["entry_id"] == "AB-010"
    assert moved["replaced_by"] == "AB-011"
    assert moved["reason"] == "moved_past"

    # 4. Non-Bash tool keys ("tool:Edit") must NEVER trigger moved_past when failing with a different entry_id,
    #    but DO resolve on direct success!
    tracker.record_failure("AB-020", "tool:Edit", turn=10)
    assert tracker.record_failure("AB-021", "tool:Edit", turn=10) is None
    tool_ok = tracker.record_success("tool:Edit", turn=11)
    assert tool_ok is not None
    assert tool_ok["entry_id"] == "AB-021"
    assert tool_ok["reason"] == "succeeded"


def test_claims_store_lease_ttl_hint_and_release(tmp_path):
    claims_path = tmp_path / "antibody-claims.json"
    store = ClaimsStore(claims_path)
    assert store.ttl_s == CLAIM_TTL_S

    # Seat A acquires claim at t=1000
    c1 = store.acquire_or_hint("abcdef123456", "txxxx", now=1000.0, entry_id="AB-007")
    assert c1["acquired"] is True
    assert c1["hint"] is None

    # Seat A re-checking at t=1060 still owns the claim
    c1_again = store.acquire_or_hint("abcdef123456", "txxxx", now=1060.0, entry_id="AB-007")
    assert c1_again["acquired"] is True
    assert c1_again["hint"] is None

    # Seat B hitting the same signature at t=1120 (elapsed = 120s = 2 min) gets claim hint
    store2 = ClaimsStore(claims_path)
    c2 = store2.acquire_or_hint("abcdef123456", "dxxxxx", now=1120.0, entry_id="AB-007")
    assert c2["acquired"] is False
    assert c2["agent"] == "txxxx"
    assert c2["hint"] == (
        "[antibody] AB-007: txxxx has been diagnosing this for 2 min. "
        "Its fix will be passed to you when it is recorded."
    )

    # Boundary at CLAIM_TTL_S (600s): at t=1599 (599s) still held by txxxx; at t=1600 (600s) expired!
    still_held = store2.acquire_or_hint("abcdef123456", "dxxxxx", now=1599.0, entry_id="AB-007")
    assert still_held["acquired"] is False
    expired_reclaim = store2.acquire_or_hint("abcdef123456", "dxxxxx", now=1600.0, entry_id="AB-007")
    assert expired_reclaim["acquired"] is True
    assert expired_reclaim["agent"] == "dxxxxx"

    # Wrong agent cannot release dxxxxx's claim; dxxxxx can release it
    assert store2.release("abcdef123456", agent="txxxx") is False
    assert store2.release("abcdef123456", agent="dxxxxx") is True
    assert store2.release("abcdef123456", agent="dxxxxx") is False


def test_claims_store_synchronizes_concurrent_instances_on_disk(tmp_path):
    claim_file = tmp_path / "shared-claims.json"
    # Construct both instances BEFORE either acquires a lease
    store1 = ClaimsStore(claim_file)
    store2 = ClaimsStore(claim_file)

    r1 = store1.acquire_or_hint("sig1", "dxxxxx", now=100.0, entry_id="AB-101")
    assert r1["acquired"] is True

    # store2 reloads disk state under lock before checking/granting sig1
    r2 = store2.acquire_or_hint("sig1", "txxxx", now=105.0, entry_id="AB-101")
    assert r2["acquired"] is False
    assert r2["agent"] == "dxxxxx"
    assert "dxxxxx has been diagnosing this" in r2["hint"]

    # When store2 acquires an unrelated sig2, sig1 is preserved on disk
    r3 = store2.acquire_or_hint("sig2", "txxxx", now=110.0, entry_id="AB-102")
    assert r3["acquired"] is True
    persisted = json.loads(claim_file.read_text(encoding="utf-8"))["claims"]
    assert set(persisted.keys()) == {"sig1", "sig2"}
    assert persisted["sig1"]["agent"] == "dxxxxx"
    assert persisted["sig2"]["agent"] == "txxxx"


def test_antibodies_cli_fingerprint_classify_and_scan_transcript(tmp_path, capsys):
    # 1. fingerprint subcommand
    rc_fp = antibodies_main([
        "fingerprint",
        "--category",
        "postgres",
        "--message",
        "OperationalError: connection refused at 127.0.0.1:5434 with token=SecretTokenValue123",
    ])
    assert rc_fp == 0
    fp_out = json.loads(capsys.readouterr().out)
    assert fp_out["category"] == "postgres"
    assert fp_out["code"] == "OperationalError"
    assert "<port>" in fp_out["normalized"]
    assert "SecretTokenValue123" not in fp_out["normalized"]
    assert "SecretTokenValue123" not in fp_out["redacted"]

    # 2. classify subcommand
    rc_cls = antibodies_main([
        "classify",
        "--command",
        "git show missing | cat",
        "--output",
        "fatal: ambiguous argument 'missing': unknown revision",
        "--exit-code",
        "0",
    ])
    assert rc_cls == 0
    cls_out = json.loads(capsys.readouterr().out)
    assert cls_out["display_only"] is True
    assert cls_out["looks_failed"] is True

    # 3. scan-transcript subcommand
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(
        json.dumps([
            {
                "id": "AB-001",
                "category": "postgres",
                "message": "OperationalError: connection refused at 127.0.0.1:5434",
                "fix": "Start PostgreSQL 17.",
            }
        ]),
        encoding="utf-8",
    )
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        "\n".join([
            json.dumps({
                "category": "postgres",
                "command": "pytest -q",
                "output": "OperationalError: connection refused at 127.0.0.1:5432",
                "exit_code": 1,
            }),
            json.dumps({
                "category": "git",
                "command": "git show missing | cat",
                "output": "fatal: bad revision 'missing'",
                "exit_code": 0,
            }),
            json.dumps({
                "category": "git",
                "command": "git diff",
                "output": "",
                "exit_code": 1,
            }),
            json.dumps({
                "category": "worker",
                "command": "python worker.py",
                "output": "starting worker",
                "stderr": "RuntimeError: worker failed to initialize",
            }),
        ]) + "\n",
        encoding="utf-8",
    )
    rc_scan = antibodies_main([
        "scan-transcript",
        "--transcript",
        str(transcript_path),
        "--catalog",
        str(catalog_path),
    ])
    assert rc_scan == 0
    scan_out = json.loads(capsys.readouterr().out)
    assert len(scan_out["matched"]) == 1
    assert scan_out["matched"][0]["entry_id"] == "AB-001"
    assert len(scan_out["uncatalogued"]) == 2
    by_cat = {u["category"]: u for u in scan_out["uncatalogued"]}
    assert set(by_cat.keys()) == {"git", "worker"}
    assert by_cat["worker"]["code"] == "RuntimeError"
    assert "RuntimeError: worker failed to initialize" in by_cat["worker"]["sample"]

    # 4. scan-transcript without --catalog (optional catalog, N19)
    rc_nocat = antibodies_main([
        "scan-transcript",
        "--transcript",
        str(transcript_path),
    ])
    assert rc_nocat == 0
    nocat_out = json.loads(capsys.readouterr().out)
    assert nocat_out["matched"] == []
    assert len(nocat_out["uncatalogued"]) == 3

    # 5. Subprocess invocation of `python -m tools.meshy_node.antibodies` (__main__ entrypoint, N15)
    repo_root = str(Path(__file__).resolve().parents[1])
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.meshy_node.antibodies",
            "fingerprint",
            "--category",
            "postgres",
            "--message",
            "DB_PASSWORD=superSecretSubproc123 at 127.0.0.1:5434",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    sub_out = json.loads(proc.stdout)
    assert sub_out["category"] == "postgres"
    assert "superSecretSubproc123" not in sub_out["normalized"]
    assert "superSecretSubproc123" not in sub_out["redacted"]
    assert "<redacted>" in sub_out["redacted"]


def _mp_fix_trust_worker(args: tuple[str, int]) -> int:
    path, idx = args
    trust = FixTrust(path)
    for k in range(5):
        trust.record_succeeded(f"entry_{idx}_{k}", "fix")
    return 5


def _mp_claims_worker(args: tuple[str, int]) -> bool:
    path, idx = args
    store = ClaimsStore(path)
    res = store.acquire_or_hint(f"sig_{idx:03d}", f"agent_{idx}", now=1000.0)
    return bool(res["acquired"])


def test_fix_trust_and_claims_store_fail_closed_and_multiprocess_concurrency(tmp_path):
    # 1. Cross-process turn persistence for FixTrust._injected_in_turn
    turn_file = tmp_path / "turn-trust.json"
    FixTrust(turn_file).record_injected("sess-mp", "AB-900", "fix text")
    rec_seen = FixTrust(turn_file).record_seen("sess-mp", "AB-900", "fix text")
    assert rec_seen["recurred_after_inject"] == 1

    # 2. Fail closed on truncated/corrupt or wrong-schema JSON for both ClaimsStore and FixTrust
    corrupt_claims = tmp_path / "corrupt-claims.json"
    ClaimsStore(corrupt_claims).acquire_or_hint("sigA", "txxxx", now=1000.0)
    corrupt_claims.write_text('{"claims": {"sigA": {"agent": "txxxx", ', encoding="utf-8")
    with pytest.raises(ValueError, match="Corrupt or unreadable ClaimsStore"):
        ClaimsStore(corrupt_claims)

    corrupt_claims.write_text('{"claims": {"sigA": "not-a-dict"}}', encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid claim record"):
        ClaimsStore(corrupt_claims)

    corrupt_claims.write_text('{"claims": "not-a-dict"}', encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid ClaimsStore schema"):
        ClaimsStore(corrupt_claims)

    corrupt_claims.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid ClaimsStore schema"):
        ClaimsStore(corrupt_claims)

    # Partial claim missing claimed_at or with NaN/Infinity/<=0/bool claimed_at must fail closed (N3 / F7 residual)
    for bad_claim_json in (
        '{"claims": {"sigA": {"agent": "txxxx"}}}',
        '{"claims": {"sigA": {"agent": "", "claimed_at": 100.0}}}',
        '{"claims": {"sigA": {"agent": 123, "claimed_at": 100.0}}}',
        '{"claims": {"sigA": {"agent": "txxxx", "claimed_at": NaN}}}',
        '{"claims": {"sigA": {"agent": "txxxx", "claimed_at": Infinity}}}',
        '{"claims": {"sigA": {"agent": "txxxx", "claimed_at": -Infinity}}}',
        '{"claims": {"sigA": {"agent": "txxxx", "claimed_at": 0}}}',
        '{"claims": {"sigA": {"agent": "txxxx", "claimed_at": -5.0}}}',
        '{"claims": {"sigA": {"agent": "txxxx", "claimed_at": true}}}',
        '{"claims": {"sigA": {"agent": "txxxx", "claimed_at": "100"}}}',
    ):
        corrupt_claims.write_text(bad_claim_json, encoding="utf-8")
        with pytest.raises(ValueError, match="missing or invalid 'claimed_at'"):
            ClaimsStore(corrupt_claims)

    # Valid fractional positive claimed_at (0.5) must be accepted (F7 residual)
    corrupt_claims.write_text('{"claims": {"sigA": {"agent": "txxxx", "claimed_at": 0.5}}}', encoding="utf-8")
    assert ClaimsStore(corrupt_claims).claims["sigA"]["claimed_at"] == 0.5

    corrupt_trust = tmp_path / "corrupt-trust.json"
    FixTrust(corrupt_trust).record_injected("s", "E1", "fix")
    corrupt_trust.write_text('{"records": {"E1": {"injected": 1', encoding="utf-8")
    with pytest.raises(ValueError, match="Corrupt or unreadable FixTrust"):
        FixTrust(corrupt_trust)

    for bad_trust_schema in (
        '{"records": "not-a-dict"}',
        "[]",
        '{"records": {}, "injected_in_turn": "not-a-list"}',
    ):
        corrupt_trust.write_text(bad_trust_schema, encoding="utf-8")
        with pytest.raises(ValueError, match="Invalid FixTrust state schema"):
            FixTrust(corrupt_trust)

    for bad_trust_record in (
        '{"records": {"E1": 123}}',
        '{"records": {"E1": {"injected": 1, "succeeded": 0}}}',
        '{"records": {"E1": {"injected": -1, "recurred_after_inject": 0, "succeeded": 0}}}',
        '{"records": {"E1": {"injected": true, "recurred_after_inject": 0, "succeeded": 0}}}',
        '{"records": {"E1": {"injected": "1", "recurred_after_inject": 0, "succeeded": 0}}}',
        '{"records": {"E1": {"injected": 1, "recurred_after_inject": 0, "succeeded": 0, "fix_sig": 999}}}',
    ):
        corrupt_trust.write_text(bad_trust_record, encoding="utf-8")
        with pytest.raises(ValueError, match="Invalid FixTrust record"):
            FixTrust(corrupt_trust)

    for bad_turn_pair in (
        '{"records": {}, "injected_in_turn": [123]}',
        '{"records": {}, "injected_in_turn": ["ab"]}',
        '{"records": {}, "injected_in_turn": ["bad-item"]}',
        '{"records": {}, "injected_in_turn": [["only-one"]]}',
        '{"records": {}, "injected_in_turn": [["", "E1"]]}',
        '{"records": {}, "injected_in_turn": [["s1", "  "]]}',
    ):
        corrupt_trust.write_text(bad_turn_pair, encoding="utf-8")
        with pytest.raises(ValueError, match="Invalid FixTrust injected_in_turn item"):
            FixTrust(corrupt_trust)

    # 3. 8-process x 5-write (40 total writes) concurrency test for both FixTrust and ClaimsStore
    mp_trust_file = str(tmp_path / "mp-trust.json")
    mp_claims_file = str(tmp_path / "mp-claims.json")
    with mp.Pool(8) as pool:
        trust_counts = pool.map(_mp_fix_trust_worker, [(mp_trust_file, i) for i in range(8)])
        claim_results = pool.map(_mp_claims_worker, [(mp_claims_file, i) for i in range(40)])

    assert sum(trust_counts) == 40
    persisted_trust = json.loads(open(mp_trust_file, encoding="utf-8").read())["records"]
    assert len(persisted_trust) == 40

    assert all(claim_results)
    persisted_claims = json.loads(open(mp_claims_file, encoding="utf-8").read())["claims"]
    assert len(persisted_claims) == 40


def test_file_lock_excludes_a_second_handle_on_this_os(tmp_path):
    """A1 (REFACTOR-QUEUE-2026-11): the lock dispatches on os.name, so it works on POSIX too (it imported msvcrt
    unconditionally before, and FixTrust/ClaimsStore with a path raised ModuleNotFoundError on Linux). One
    handle holds the lock; a second handle on the same file is refused with OSError until the first lets go."""
    from tools.meshy_node.antibodies import _lock_fd

    lock = tmp_path / "x.lock"
    with open(lock, "a+b") as first, open(lock, "a+b") as second:
        _lock_fd(first)
        with pytest.raises(OSError):
            _lock_fd(second)
        _lock_fd(first, unlock=True)
        _lock_fd(second)
        _lock_fd(second, unlock=True)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
