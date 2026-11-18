"""A PRIVATE transfer between two seats on one LAN: `offer` serves a file once, `pickup` fetches it, and the bytes never reach
the mesh server, a repository or any cloud store.

WHAT IT IS FOR. Some bytes must never be stored anywhere but the two seats: Apple firmware, per-unit calibration, MAC
addresses, and payloads built from them. Until 2026-10-05 seats moved them by hand: an ad-hoc one-shot HTTP server on the
LAN, its link pasted into a message, the sha256 copied beside it. Links expired or were spent four times and had to be
served again, and the hashes were typed by hand (one hand-typed pin was wrong). This is that method as a client feature.
CLIENT ONLY: the server stores one ordinary message (the offer) and, with `--receipt`, one ordinary reply, and never sees a
byte of the file. No server or schema change.

THE TWO COMMANDS.
  offer FILE --to LABEL --to-peer PEER_ID --bind IP [--port N] [--ttl S] [--thread T] [--subject S]
        copies the file once into a private snapshot and hashes that, listens on IP:port, sends ONE message to LABEL
        whose body carries the offer line below and a sentence for a person, serves the snapshot to exactly one complete
        signed GET of a random path, prints one JSON line per event (`offered`, `warning`, `refused`, `interrupted`,
        `served`, `expired`, `error`, `not_sent`, `stopped`) and exits 0 only when the file was served completely once.
  pickup --key K [--from-peer PEER_ID] [--out DIR] [--receipt]
        reads K through the signed `read` path, checks that `signed_by` matches `--from-peer` when given, takes its ONE
        offer line, downloads into a temporary file in DIR, checks
        the size and the sha256, and only then gives the file its plain name, never over an existing one (`_place`: a
        hard link, or where the filesystem has none a rename that refuses an existing name in the same step; with
        neither the pickup fails). With `--receipt` it answers K on the same thread (`in_reply_to` K): one line, picked
        up, the size, the sha256 verified.

THE LINE. One line of the body, and only one, compact JSON after the prefix, ASCII (json's escapes), every field required:
    meshy-offer/1 {"name":...,"size_bytes":...,"sha256":...,"url":"http://IP:PORT/TOKEN","expires":"...Z","for_peer":"<peer id>"}
`pickup` refuses a body with none or with several (it will not guess which), a field it does not know or a field twice, a
URL that is not `http://<IP>:<port>/<token>` (a `file:` or a host name is never fetched), and a name that could leave DIR,
mislead, or not be saved on Windows (`name_problem`, on every platform, before the fetch that would spend the offer).
`for_peer` is the peer whose key must sign the pickup (below); `expires` is UTC to the second.

THE TOKEN IS IN THE MESSAGE, AND THE MESSAGE IS ON THE SERVER. The URL's random path (secrets.token_urlsafe, 256 bits) is
stored in the message, so every reader of that message (its recipients, the server's operators, a backup of the mesh)
holds it for the window. What stands between them and the file:
  * SIGNED PICKUP. A GET is served only when it carries a signature over JCS {expires, nonce, offered_by, peer_id,
    purpose, token, url} (purpose `meshy-offer-pickup-v1`, so it is never the bytes of a sign-in, a DPoP proof or an
    envelope) by the key the recipient's peer publishes at /agents/<peer>/jwks.json, the key `read` already checks
    writers' signatures with. The offer reads that key ONCE, before anything is sent, and keeps it for the window (a key
    rotated meanwhile is not followed: offer again). `--to-peer` names the peer; a label is chosen by its writer and
    proves nothing, as for `rollcall --expect`. A seat's peer id is the `signed_by` that `read` prints for any message it
    sent. Each nonce is accepted once. Holding the token without the recipient's private key gets a 404.
    WHAT THE SIGNATURE BINDS, AND WHY. `url` is the offer line's url exactly as the line carries it (the address, the
    port and the token the request goes to) and `offered_by` is the peer that signed the offer message (`read`'s
    `signed_by`, which `pickup` requires); the offer verifies both against its OWN: the url it put in its line and its
    own peer id. So a signature is good for one offer at one address, made by one seat, and nowhere else. Without them
    a seat that can read the offer (a cc, a reader of the thread) could publish a decoy offer of its own, validly
    signed by its key, reusing the token, `expires` and `for_peer` with a url at its own server; a recipient that ran
    pickup on the decoy sent it a signature that opened the real offer when replayed there (Codex, round 5). Now that
    signature names the decoy's url and its author, and the real offer refuses it.
    THE MESH SERVER STAYS TRUSTED FOR WHO THE RECIPIENT IS. That key comes from the mesh server's answer (node.author_keys)
    and nothing pins it independently, so the signature keeps the file from a READER of the message who holds the token,
    not from the server itself: an operator who controls that JWKS answer can name a key of their own and sign the
    pickup. Pinning a key known out of band (a `--to-key`, or a local pin file of peers' keys) is a later item.
  * SINGLE USE. The first complete GET spends it. An interrupted one does not, so a pickup that lost its connection runs
    again; its new request carries a new nonce.
  * TTL. `--ttl` seconds, default 3600, at most MAX_TTL_S, counted from the moment pickups are answered, which is after
    the send was accepted, never before it. The line's `expires` is an UPPER BOUND, fixed before the send because the
    message carries it: the instant the send began plus the TTL plus SEND_ALLOWANCE_S (the send's polling budget,
    SEND_POLL_S, and one more round trip), rounded up to the second. The server closes at the TTL or at `expires`,
    whichever comes first, so nothing is served after the instant the message names. The send must END inside the
    allowance, less START_MARGIN_S (node.finishing_by bounds every request it makes, a sign-in and the result's JWKS
    included, and the send as a whole, the result's signature check after its last request included), so a send that
    was accepted always leaves the whole TTL: one that has not finished by then is `not_sent` (exit 1) and nothing is
    served, even when the server goes on to deliver the message (its link is then dead). Serving starts only while the
    whole TTL still fits before `expires`, checked at the instant it starts: when it no longer does (this process
    paused between the send's answer and serving) the accepted offer is `expired` with no `offered` line before it,
    nothing is ever answered, and its link is dead (exit 1). An advertised window always holds a whole serving one.
  * A LAN ADDRESS. `--bind` is ONE IP address of this host: a wildcard (0.0.0.0, ::), which listens on every interface,
    and a public address are refused unless `--any-interface`, and a wildcard then needs `--advertise IP`, the address
    the URL names. A host name is refused: it is resolved at bind time to whatever it resolves to then.
  * THE PICKER-UP IS NAMED. `served` prints the address that took the file; every refused request is a `refused` line
    with its address and the reason, so an attempt by anyone else is seen by the seat that made the offer.
What none of this does: the bytes cross the LAN as plain HTTP, so anyone who can watch that segment can read them (the
sha256 proves they arrived intact, not that nobody else saw them); and moving private bytes still needs the OPERATOR'S
APPROVAL FOR EACH TRANSFER. This makes a transfer safe to run, not authorised.

EVERYTHING THAT IS NOT THE ONE GET IS A 404, and none of them counts: another path, HEAD or any other method, a request
the server could not parse, a request that is not signed, or not by that peer's key, or not for this offer's url and
offering seat (a signature a decoy collected), a nonce seen before, a GET while another is in flight or after one
completed, and anything after `expires`. The 404 says nothing about which check
failed; the offer's own `refused` line does. Nothing is answered before the offer message was accepted.

THE FILE. FILE must be a regular file reached without a link: lstat says regular (and, on Windows, not a reparse point),
the open does not follow a link (O_NOFOLLOW where the platform has it; O_NONBLOCK, so a FIFO swapped in cannot hang it),
and the handle's (st_dev, st_ino) is the lstat's, as tools/backup/serve.py opens a dump. It is then copied ONCE, before
anything is sent, into a private snapshot (`Snapshot`: a temporary file created 0600 in a 0700 directory of its own), the
snapshot is hashed, and the snapshot is the only thing ever served: whatever happens to FILE afterwards (rewritten,
replaced, removed, even during the transfer) is never read again, so the bytes that cross the LAN are always the bytes
the line's sha256 names. A FILE whose size or modification time moved while it was copied is refused (exit 2). The
snapshot is removed when the offer ends, however it ends.

THE SEND. The offer message goes through `node.send` like any `send`, after the local-path guard (docshare: the URL is a
remote URL, not a path on this disk, so the guard lets it through) and the volume guard (sendguard, no override here).
The listening socket is opened before the send, so the URL can carry its port, but nothing on it is answered until the
send was accepted: a send that fails, is refused, raises or does not end in time closes the socket and nothing is
served. Once the send WAS accepted the recipient holds the offer, so it is served whatever else fails, unless its whole
TTL no longer fits before `expires` (THE TTL: `expired`, nothing answered, the send still counted): serving starts
first, the `offered` line (with the message key) is printed before any event of the server's, and a send ledger that
cannot be written (sendguard.record: a full disk, permissions) is a `warning` line, never a stop. Neither the line nor
the ledger is written before serving starts, so neither can shorten the window.

EXIT CODES (as cli.py's): offer 0 served once; 1 the server did not accept the offer message or the send did not end in
time, the window closed with nothing served or no longer fitted when serving was to start, the snapshot could not be
read back whole, or it was interrupted; 2 refused before anything was sent, or the mesh server could not be reached.
pickup 0 the file is in place (a receipt ledger that could not be written is a `warning` in the receipt); 1 any failure
after the read, one JSON object with a typed `error`, or a receipt that was not accepted (the file is kept, `picked_up`
true, and the object says so); 2 as every command.
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import socketserver
import stat
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import docshare, filenames, node, sendguard, wire

#: The prefix of the one machine-readable line. The `/1` is the format's version: a later format is a new prefix, never
#: a changed meaning of this one, so a pickup that does not know it finds no line and says so.
OFFER_PREFIX = "meshy-offer/1 "
#: The fields of the line, in the order `offer` writes them; `pickup` requires exactly these.
OFFER_FIELDS = ("name", "size_bytes", "sha256", "url", "expires", "for_peer")
#: The signed request's domain: JCS {expires, nonce, offered_by, peer_id, purpose, token, url}. A sign-in signs {aud,
#: nonce, peer_id, purpose} with purpose `meshy-signin-v1` (wire.signin_message), so these bytes are never one of those.
PICKUP_PURPOSE = "meshy-offer-pickup-v1"
PEER_HEADER = "Meshy-Pickup-Peer"
KID_HEADER = "Meshy-Pickup-Kid"
NONCE_HEADER = "Meshy-Pickup-Nonce"
SIGNATURE_HEADER = "Meshy-Pickup-Signature"
DEFAULT_TTL_S = 3600
MAX_TTL_S = 24 * 3600
TOKEN_BYTES = 32                 # secrets.token_urlsafe(32): 43 characters, 256 bits
NONCE_BYTES = 18
CHUNK = 1 << 20                  # streamed 1 MiB at a time, never read whole
STOP_UNWIND_S = 2.0              # how long stop() waits for a cut-off transfer to report itself
FETCH_TIMEOUT_S = 60.0           # per socket operation, not for the whole download
#: How long the offer's send polls for its call to finish (node.send's own default, passed explicitly), and how far past
#: the TTL the line's `expires` is put: that polling budget and one more round trip. The whole send must END within
#: SEND_ALLOWANCE_S - START_MARGIN_S of the instant `expires` is counted from (node.finishing_by: a sign-in, the invoke,
#: its polling, the result's JWKS, each request cut at that instant, and the whole send, its result check included, late
#: if it returns after it), and START_MARGIN_S covers the local steps from the send's answer to the server's start, so a
#: send that was accepted always leaves the whole TTL before `expires`; OfferServer.start checks it again as it starts.
#: The window itself starts when pickups are answered; `expires` only bounds it (THE TTL above).
SEND_POLL_S = 40.0
SEND_ALLOWANCE_S = int(SEND_POLL_S + node.HTTP_TIMEOUT_S)
START_MARGIN_S = 1.0
OFFER_TYPE = "offer"
RECEIPT_TYPE = "ack"

#: Why a pickup ended without the file in place: `error` in its one JSON object.
E_OFFER_UNVERIFIED = "E_OFFER_UNVERIFIED"                  # the message's author signature did not verify
E_OFFER_NOT_FOUND = "E_OFFER_NOT_FOUND"                    # the body carries no offer line
E_OFFER_AMBIGUOUS = "E_OFFER_AMBIGUOUS"                    # it carries more than one
E_OFFER_MALFORMED = "E_OFFER_MALFORMED"                    # the line is not the format above
E_OFFER_NAME_REFUSED = "E_OFFER_NAME_REFUSED"              # the name could leave --out, mislead, or not save on Windows
E_OFFER_NOT_FOR_THIS_PEER = "E_OFFER_NOT_FOR_THIS_PEER"    # `for_peer` is another peer: its pickup could not be signed
E_OFFER_WRONG_SENDER = "E_OFFER_WRONG_SENDER"              # `--from-peer` was given and the offer was signed by another
E_OFFER_EXISTS = "E_OFFER_EXISTS"                          # a file of that name is already in --out: never overwritten
E_OFFER_FETCH = "E_OFFER_FETCH"                            # the offer answered no file (404: spent, expired, refused)
E_OFFER_SIZE_MISMATCH = "E_OFFER_SIZE_MISMATCH"            # the bytes received are not `size_bytes` long
E_OFFER_HASH_MISMATCH = "E_OFFER_HASH_MISMATCH"            # their sha256 is not `sha256`
E_OFFER_WRITE = "E_OFFER_WRITE"                            # this disk refused them (--out not writable, full)
E_OFFER_PLACE_UNSUPPORTED = "E_OFFER_PLACE_UNSUPPORTED"    # --out has no hard links and no rename that refuses to replace

_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,128}", re.ASCII)
_NONCE_RE = re.compile(r"[A-Za-z0-9_-]{16,128}", re.ASCII)
_SHA256_RE = re.compile(r"[0-9a-f]{64}", re.ASCII)
_EXPIRES_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", re.ASCII)
_NOT_FOUND = b"404 Not Found\n"


# --------------------------------------------------------------------------- names and addresses

def name_problem(name) -> str | None:
    """Why `name` is refused as the file's name in --out, or None. The same rule on both sides: `offer` refuses to offer
    a file `pickup` would refuse to save. Refused: empty, `.` and `..`, any other leading `.` (a hidden name is the
    shape of a configuration file: an `.envrc` saved into a project folder is run by direnv), a path separator (`/`, and
    `\\`, which Windows reads as one), `:` (an alternate data stream on Windows: `a:b` writes a stream of `a`), a control
    or format character (Unicode category C*: a newline, an escape, a right-to-left override that shows a name other than
    the one saved), and more than 255 UTF-8 bytes.
    ON EVERY PLATFORM, ALSO WHAT WINDOWS CANNOT SAVE (tools/meshy_node/filenames.py): `< > " | ? *`, a Windows device
    name (CON, PRN, AUX, NUL, CONIN$, CONOUT$, COM0-COM9, LPT0-LPT9 and their superscript-digit forms, in any case,
    alone or before an extension: `nul.txt` opens the device; COM0 and LPT0 are refused on purpose, beyond what
    ntpath.isreserved lists, and the receiver keeps the same refusal: kxxx's ruling on #293 N2), a name that ends in
    `.` or a space (Windows drops them and saves another name), and more than 255 UTF-16 code units (one NTFS
    component; a character outside the BMP counts two). Before, `fw?.bin` passed: a
    Windows pickup fetched it, which spends the single-use offer, and only then failed to give it its name (Win32 error
    123) and removed the copy. The check runs before the fetch, so a name refused here never spends the offer."""
    if not isinstance(name, str) or not name:
        return "the name is empty"
    if name in (".", ".."):
        return f"the name {name!r} names a directory"
    if name.startswith("."):
        return f"the name {name!r} is hidden (a leading '.'), the shape of a configuration file a tool would read"
    for ch, what in (("/", "a path separator"), ("\\", "a path separator on Windows")):
        if ch in name:
            return f"the name {name!r} contains {what} ({ch!r})"
    if any(unicodedata.category(c).startswith("C") for c in name):
        return f"the name {name!r} contains a control or format character"
    problem = filenames.windows_name_problem(name)
    if problem:
        return problem
    return filenames.utf8_name_problem(name)


def _ip(text, flag: str):
    """`text` as an IP address, or Refused naming `flag`. A scoped IPv6 address (`fe80::1%en0`) is refused: the scope
    cannot be carried in the URL the recipient fetches."""
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        raise node.Refused(f"{flag} {text!r} is not an IP address (a name is resolved at bind time to whatever it "
                           "resolves to then); pass this host's LAN address, e.g. 192.168.50.10") from None
    if getattr(address, "scope_id", None):
        raise node.Refused(f"{flag} {text!r} is a scoped IPv6 address, which the URL cannot carry")
    return address


def _wildcard(address) -> bool:
    mapped = getattr(address, "ipv4_mapped", None)
    return address.is_unspecified or (mapped is not None and mapped.is_unspecified)


def check_bind(bind: str, *, any_interface: bool = False, advertise: str | None = None) -> tuple[str, str]:
    """(the address to listen on, the address the URL names), or Refused before anything is opened or sent.

    The listening address is half of what keeps the file on the LAN, so it must be ONE address of this host: a wildcard
    (0.0.0.0, ::, ::ffff:0.0.0.0) listens on every interface, a public (globally routable) address is reachable from
    outside the LAN, and both are refused unless `any_interface`. A wildcard has no address a recipient could fetch, so it
    also needs `advertise`, an address that is not a wildcard; with any other `bind` the URL names `bind` itself, and
    `advertise` is refused rather than silently ignored."""
    address = _ip(bind, "--bind")
    wild = _wildcard(address)
    if wild and not any_interface:
        raise node.Refused(f"--bind {bind} listens on every interface; pass this host's one LAN address (the private bytes "
                           "stay on the LAN only if the server does), or --any-interface with --advertise <that address>")
    if address.is_global and not any_interface:
        raise node.Refused(f"--bind {bind} is a public address, reachable from outside the LAN; pass this host's LAN "
                           "address, or --any-interface to listen there anyway")
    if not wild:
        if advertise is not None:
            raise node.Refused("--advertise is for a wildcard --bind (with --any-interface); the URL names --bind")
        return str(address), str(address)
    if advertise is None:
        raise node.Refused(f"--bind {bind} with --any-interface needs --advertise <the address the recipient fetches from>")
    shown = _ip(advertise, "--advertise")
    if _wildcard(shown):
        raise node.Refused(f"--advertise {advertise} is a wildcard; name the address the recipient reaches")
    if shown.version != address.version:
        # The listener's family comes from --bind (OfferServer): 0.0.0.0 never receives an IPv6 connection, and :: often
        # does not receive IPv4 either (a v6-only socket, the default on common Windows set-ups). A URL in the other
        # family would be an accepted offer with a dead link (GitHub Codex review of #124).
        raise node.Refused(f"--advertise {advertise} is IPv{shown.version} but --bind {bind} listens on IPv{address.version}; "
                           "advertise an address of the same family as the wildcard")
    return str(address), str(shown)


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host


# --------------------------------------------------------------------------- the file

def _is_regular(st) -> bool:
    """A regular file and not a reparse point: on Windows CPython's lstat says S_IFLNK only for a Win32 symlink, and
    another name-surrogate reparse point (a WSL symlink) comes back S_IFREG with the reparse attribute
    (tools/backup/serve.py is_listable)."""
    return (stat.S_ISREG(st.st_mode)
            and not getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def open_regular(path):
    """(binary file, its fstat): `path` open for reading, reached without a link, or Refused / OSError. The lstat and the
    open are two calls and the name can be replaced in between, so the open does not follow a link and the handle must be
    the regular file the lstat described, by (st_dev, st_ino)."""
    path = os.fspath(path)
    st = os.lstat(path)
    if not _is_regular(st):
        raise node.Refused(f"{path} is not a regular file: a link, a directory or a device is never offered")
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
             | getattr(os, "O_BINARY", 0))
    fd = os.open(path, flags)
    try:
        f = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    try:
        fst = os.fstat(f.fileno())
    except BaseException:
        f.close()
        raise
    if not _is_regular(fst) or (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
        f.close()
        raise node.Refused(f"{path} was replaced while it was being opened")
    return f, fst


def digest(f) -> tuple[int, str]:
    """(size, sha256 hex) of what is left to read in `f`, streamed."""
    h, n = hashlib.sha256(), 0
    for chunk in iter(lambda: f.read(CHUNK), b""):
        h.update(chunk)
        n += len(chunk)
    return n, h.hexdigest()


class Snapshot:
    """The bytes an offer serves: FILE (opened by `open_regular`, so never through a link) copied ONCE into a private
    temporary file, which is then hashed and is the ONLY thing the server reads. Rehashing FILE before the GET and as it
    streams would notice a change only after the changed bytes had crossed the LAN; a copy nobody else can write cannot
    change, so the bytes served are the bytes `sha256` names. The copy is a tempfile.TemporaryFile (created 0600 by
    mkstemp; on POSIX it has no name at all once created) inside a directory of its own (mkdtemp, 0700). A FILE whose
    size or modification time moved during the copy is Refused: the copy could hold half of each version. `close`
    removes the copy and its directory, and is safe twice. Refused or OSError, with nothing left behind."""

    def __init__(self, path):
        self.file = None
        src, st = open_regular(path)
        with src:
            self.dir = tempfile.mkdtemp(prefix="meshy-offer-")
            try:
                self.file = tempfile.TemporaryFile(dir=self.dir)
                copied = 0
                for chunk in iter(lambda: src.read(CHUNK), b""):
                    self.file.write(chunk)
                    copied += len(chunk)
                after = os.fstat(src.fileno())
                if (copied, after.st_size, after.st_mtime_ns) != (st.st_size, st.st_size, st.st_mtime_ns):
                    raise node.Refused(f"{os.fspath(path)} changed while it was being copied ({st.st_size} bytes at the "
                                       f"open, {copied} copied, {after.st_size} now); offer it when nothing writes it")
                self.file.seek(0)
                self.size, self.sha256 = digest(self.file)
            except BaseException:
                self.close()
                raise

    def close(self) -> None:
        if self.file is not None:
            self.file.close()
            self.file = None
        shutil.rmtree(self.dir, ignore_errors=True)

    def __enter__(self) -> Snapshot:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# --------------------------------------------------------------------------- the line and the bodies

def offer_line(meta: dict) -> str:
    """The one machine-readable line: the prefix, then compact ASCII JSON of OFFER_FIELDS in order. json escapes every
    control character, so the line is one line whatever the name holds."""
    return OFFER_PREFIX + json.dumps({k: meta[k] for k in OFFER_FIELDS}, separators=(",", ":"), ensure_ascii=True)


def offer_body(meta: dict) -> str:
    """The offer message's body: the line, then one sentence for a person reading the notice."""
    return (offer_line(meta) + "\n"
            f"{meta['name']} ({meta['size_bytes']} bytes, sha256 {meta['sha256']}) is offered to peer "
            f"{meta['for_peer']} for ONE download on the LAN, until {meta['expires']} at the latest; the bytes never "
            "pass through the mesh server. Fetch it with `python -m tools.meshy_node pickup --key <this message's key>`, "
            "which signs the request with your node's key and checks the size and the sha256 before the file gets its "
            "name.")


def receipt_body(name: str, size: int, sha256: str) -> str:
    """The `--receipt` reply: one line. Never the path it was saved to, which is a path on the recipient's disk."""
    return f"picked up {name}: {size} bytes, sha256 {sha256} verified"


class PickupFailed(Exception):
    """A pickup that ended without the file in place. `error` is the typed record; a temporary file is already gone."""

    def __init__(self, code: str, detail: str, **extra):
        self.error = {"error": code, "detail": detail, **extra}
        super().__init__(f"{code}: {detail}")


def _unique_keys(pairs):
    seen = {}
    for k, v in pairs:
        if k in seen:
            raise ValueError(f"the field {k!r} appears twice")
        seen[k] = v
    return seen


def _offer_url(url) -> tuple[str, str]:
    """(the URL to fetch, its token) for a line's `url`, or PickupFailed. Only `http://<IP>:<port>/<token>`: no other
    scheme (urllib would read a `file:` URL off this disk), no host name (nothing is resolved), no credentials, query or
    fragment. The URL fetched is rebuilt from the checked parts, never the text as it came."""
    def bad(why):
        return PickupFailed(E_OFFER_MALFORMED, f"the offer's url {why}; it is not fetched")
    if not isinstance(url, str):
        raise bad("is not a string")
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        raise bad("is not a URL") from None
    if parts.scheme != "http":
        raise bad("is not http://")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise bad("carries credentials, a query or a fragment")
    try:
        host = ipaddress.ip_address(parts.hostname or "")
    except ValueError:
        raise bad("does not name an IP address") from None
    if port is None or not parts.path.startswith("/") or not _TOKEN_RE.fullmatch(parts.path[1:]):
        raise bad("is not http://<IP>:<port>/<token>")
    token = parts.path[1:]
    return f"http://{_url_host(str(host))}:{port}/{token}", token


def parse_offer(body) -> tuple[dict, str, str]:
    """(the line's fields, the URL to fetch, its token) from a message body, or PickupFailed. Exactly one line of the
    body starts with OFFER_PREFIX: none is E_OFFER_NOT_FOUND and several are E_OFFER_AMBIGUOUS, because a pickup that
    picked one would be guessing which the sender meant. A quoted line (`> meshy-offer/1 ...`) is not an offer line."""
    lines = [ln.rstrip("\r") for ln in (body if isinstance(body, str) else "").split("\n")]
    found = [ln for ln in lines if ln.startswith(OFFER_PREFIX)]
    if not found:
        raise PickupFailed(E_OFFER_NOT_FOUND, f"the message carries no `{OFFER_PREFIX.strip()}` line: it is not an offer")
    if len(found) > 1:
        raise PickupFailed(E_OFFER_AMBIGUOUS, f"the message carries {len(found)} `{OFFER_PREFIX.strip()}` lines; an offer "
                                              "carries exactly one, and pickup does not guess which was meant")

    def bad(why):
        return PickupFailed(E_OFFER_MALFORMED, f"the offer line {why}")
    try:
        meta = json.loads(found[0][len(OFFER_PREFIX):], object_pairs_hook=_unique_keys)
    except (ValueError, RecursionError) as exc:
        raise bad(f"is not one JSON object ({type(exc).__name__}: {exc})") from None
    if not isinstance(meta, dict):
        raise bad("is not one JSON object")
    if set(meta) != set(OFFER_FIELDS):
        missing, extra = sorted(set(OFFER_FIELDS) - set(meta)), sorted(set(meta) - set(OFFER_FIELDS))
        raise bad(f"does not carry exactly {', '.join(OFFER_FIELDS)} (missing {missing}, unknown {extra})")
    why = name_problem(meta["name"])
    if why:
        raise PickupFailed(E_OFFER_NAME_REFUSED, f"{why}; the file is not fetched")
    size = meta["size_bytes"]
    if type(size) is not int or size < 0:
        raise bad("has a size_bytes that is not a whole number of bytes")
    if not isinstance(meta["sha256"], str) or not _SHA256_RE.fullmatch(meta["sha256"]):
        raise bad("has a sha256 that is not 64 lower-case hex digits")
    if not isinstance(meta["expires"], str) or not _EXPIRES_RE.fullmatch(meta["expires"]):
        raise bad("has an expires that is not YYYY-MM-DDTHH:MM:SSZ")
    try:
        if not isinstance(meta["for_peer"], str):
            raise ValueError
        meta["for_peer"] = str(uuid.UUID(meta["for_peer"]))
    except ValueError:
        raise bad("has a for_peer that is not a peer id") from None
    url, token = _offer_url(meta["url"])
    return meta, url, token


# --------------------------------------------------------------------------- the signed pickup

def pickup_message(*, url: str, offered_by: str, token: str, nonce: str, expires: str, peer_id: str) -> bytes:
    """The bytes a pickup signs: RFC 8785 of {expires, nonce, offered_by, peer_id, purpose, token, url}. `url` is the
    offer line's url as the line carries it and `offered_by` the peer that signed the offer message, so a signature
    opens that one offer at that one address and no other server it is shown to (SIGNED PICKUP: a decoy offer reusing
    the token at its own url collects a signature that names its url and its author). `expires` is the offer's own, as
    the line carries it, so a signature is bound to one offer's window and needs no clock agreement between the two
    seats; the nonce makes each request's signature new."""
    return wire.jcs({"expires": expires, "nonce": nonce, "offered_by": offered_by, "peer_id": peer_id,
                     "purpose": PICKUP_PURPOSE, "token": token, "url": url})


def pickup_headers(state: dict, *, url: str, offered_by: str, token: str, expires: str,
                   nonce: str | None = None) -> dict:
    """The four headers of a signed pickup of the offer at `url` that `offered_by` signed, signed with this node's key
    (the state file's seed) under its kid. The url and the offering peer are not sent: the offer checks the signature
    against its own."""
    peer = str(uuid.UUID(state["peer_id"]))
    nonce = nonce or secrets.token_urlsafe(NONCE_BYTES)
    signature = wire.sign(node._seed(state), pickup_message(url=url, offered_by=offered_by, token=token, nonce=nonce,
                                                            expires=expires, peer_id=peer))
    return {PEER_HEADER: peer, KID_HEADER: state["kid"], NONCE_HEADER: nonce, SIGNATURE_HEADER: wire.b64u(signature)}


def published_keys(jwks) -> dict[str, bytes]:
    """{kid: raw Ed25519 public key} from a peer's published JWKS (node.author_keys). An entry that is not an Ed25519 OKP
    key with a kid and a 32-byte `x` is skipped, never trusted."""
    out: dict[str, bytes] = {}
    keys = jwks.get("keys") if isinstance(jwks, dict) else None
    for k in keys if isinstance(keys, list) else []:
        if not (isinstance(k, dict) and k.get("kty") == "OKP" and k.get("crv") == "Ed25519"
                and isinstance(k.get("kid"), str) and isinstance(k.get("x"), str)):
            continue
        try:
            raw = wire.b64u_decode(k["x"])
        except (ValueError, TypeError):
            continue
        if len(raw) == 32:
            out[k["kid"]] = raw
    return out


def pickup_problem(headers, *, url: str, offered_by: str, token: str, expires: str, for_peer: str, keys: dict,
                   nonces: set) -> str | None:
    """Why a request is not a pickup signed by `for_peer` for THIS offer (`url`, the url its line carries, made by
    `offered_by`, the offering seat's own peer id), or None, and then its nonce is spent. NEVER RAISES on what the
    request carries: every header is the requester's."""
    try:
        return _pickup_problem(headers, url=url, offered_by=offered_by, token=token, expires=expires, for_peer=for_peer,
                               keys=keys, nonces=nonces)
    except Exception as exc:  # noqa: BLE001 - a verifier fails closed on malformed input
        return f"the signature could not be checked ({type(exc).__name__})"


def _pickup_problem(headers, *, url, offered_by, token, expires, for_peer, keys, nonces) -> str | None:
    peer, kid, nonce, sig = (headers.get(h) for h in (PEER_HEADER, KID_HEADER, NONCE_HEADER, SIGNATURE_HEADER))
    if not (peer and kid and nonce and sig):
        return "the request is not signed (pickup signs it with the recipient's node key)"
    try:
        signer = str(uuid.UUID(peer))
    except ValueError:
        return "the signer is not a peer id"
    if signer != for_peer:
        return f"signed as peer {signer}; the offer is for peer {for_peer}"
    if not _NONCE_RE.fullmatch(nonce):
        return "the nonce is malformed"
    if nonce in nonces:
        return "the nonce was used before: a replayed request"
    key = keys.get(kid)
    if key is None:
        return f"key {kid[:64]!r} is not one peer {for_peer} published when the offer was made"
    try:
        raw = wire.b64u_decode(sig)
    except (ValueError, TypeError):
        return "the signature is not base64url"
    if not wire.verify(key, pickup_message(url=url, offered_by=offered_by, token=token, nonce=nonce, expires=expires,
                                           peer_id=signer), raw):
        return ("the signature does not verify under that peer's key for this offer (its url, offering peer, token, "
                "expires)")
    nonces.add(nonce)
    return None


# --------------------------------------------------------------------------- the server

class _OfferHandler(BaseHTTPRequestHandler):
    """One request. GET of the token's path, signed by the recipient, inside the window: the file. Everything else: 404."""
    server_version = "meshy-offer"
    timeout = 60                      # a stalled client releases its thread
    # The stdlib answers a request line it cannot parse as HTTP/0.9 would: no status line. Every answer here has one.
    default_request_version = "HTTP/1.0"

    def version_string(self) -> str:
        return self.server_version    # not the Python version: it helps nobody fetching and tells a scanner what to try

    def log_message(self, format, *args) -> None:
        pass                          # the offer prints events, never an access log

    def parse_request(self) -> bool:
        self.raw_target = "-"
        if not super().parse_request():
            return False
        # The RAW target, not self.path: the stdlib rewrites a leading "//" in self.path (gh-87389).
        words = self.requestline.split()
        self.raw_target = words[1] if len(words) > 1 else "-"
        if self.command != "GET":
            self.not_found(f"method {self.command[:32]}: only one GET is served")
            return False
        return True

    def send_error(self, code, message=None, explain=None) -> None:
        # The answers the stdlib makes itself (400, 414, 431, 505) are a 404 like every other, and its default page,
        # which repeats the request line back, is never sent.
        self.not_found(f"a request that could not be parsed (HTTP {int(code)})")

    def do_GET(self) -> None:
        self.server.answer(self)

    def not_found(self, why: str | None) -> None:
        if why is not None:
            self.server.event({"event": "refused", "from": self.client_address[0], "why": why})
        self.close_connection = True
        try:
            self.send_response(HTTPStatus.NOT_FOUND)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(_NOT_FOUND)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(_NOT_FOUND)
        except OSError:
            pass                      # the requester went away; nothing was counted either way


class OfferServer(ThreadingHTTPServer):
    """The listening socket for one offer. Created (and bound, so the port is known) before the offer message is sent;
    answers nothing until `start`, which is also when its window of `ttl` seconds begins, and which starts nothing when
    that whole window no longer fits before `not_after`, the time.monotonic() instant of the line's `expires`. Serves
    `snapshot` and nothing else. `wait` blocks until the file was served, the offer failed, or the window closed.

    `url` is the url the offer line carries, `http://<url_host>:<the bound port>/<token>`, built here once the port is
    known and put in the line as it is; a pickup is served only when its signature names that url and `offered_by`, the
    offering seat's own peer id (SIGNED PICKUP)."""
    # HTTPServer turns SO_REUSEADDR on; on Windows that lets another socket bind the SAME address and port and take
    # connections meant for this one (tools/backup/serve.py). server_bind asks for exclusive use instead.
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, address: tuple[str, int], *, url_host: str, offered_by: str, snapshot: Snapshot, token: str,
                 expires: str, ttl: float, not_after: float, for_peer: str, keys: dict, emit):
        self.address_family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        super().__init__(address, _OfferHandler)
        self.url = f"http://{_url_host(url_host)}:{self.server_address[1]}/{token}"
        self.offered_by = offered_by
        self.snapshot, self.size, self.sha256 = snapshot, snapshot.size, snapshot.sha256
        self.token, self.expires, self.ttl, self.not_after = token, expires, ttl, not_after
        self.deadline = not_after          # until `start` sets the window; nothing is answered before it
        self.for_peer, self.keys, self._emit = for_peer, keys, emit
        self.nonces: set[str] = set()
        self.transfer = threading.Lock()   # one transfer at a time; a GET that cannot take it is a 404
        self.done = threading.Event()
        self.closed = False                # set when the window closes; no transfer starts after it
        self.outcome: str | None = None    # "served", "failed", "expired" or "stopped"; None while open
        self._state = threading.Lock()    # completion and expiry never wait for the transfer lock
        self._connection: socket.socket | None = None
        self._events = threading.Lock()
        self._checks = threading.Lock()    # the nonce set is read and written by one check at a time
        self._thread: threading.Thread | None = None

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # TCPServer's bind, not HTTPServer's, which reverse-resolves the address (socket.getfqdn) and waits on DNS.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def event(self, record: dict) -> None:
        with self._events:
            self._emit(record)

    def start(self, announce=None) -> bool:
        """Answer requests from now on, for `ttl` seconds from now, and True; or, when `ttl` seconds from now would
        run past `not_after` (the line's `expires`), start nothing, announce nothing and answer False: a window the
        message names must hold a whole serving one, never a shorter one or none (the process paused after the send's
        answer for longer than START_MARGIN_S). `announce()`, when given, runs once serving has begun and holds the
        event lock: what it prints comes before any event of the server's, and however long it takes (a stdout that
        blocks, a slow disk under the send ledger) the window is already set."""
        with self._events:
            now = time.monotonic()
            if now + self.ttl > self.not_after:
                return False
            self.deadline = now + self.ttl
            self._thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.1},
                                            name="meshy-offer", daemon=True)
            self._thread.start()
            if announce is not None:
                announce()
        return True

    def wait(self) -> str:
        """"served", "failed" or "expired". At the deadline close an active connection without waiting for its transfer
        lock: a pickup that began inside the window must also finish inside it."""
        remaining = self.deadline - time.monotonic()
        if remaining > 0:
            self.done.wait(remaining)
        with self._state:
            self.closed = True
            self.outcome = self.outcome or "expired"
            connection = self._connection
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass                      # the handler already closed it
        return self.outcome

    def stop(self) -> None:
        """Stop serving and close the socket. Safe before `start` (shutdown would wait forever for a loop that never ran)
        and twice. An active pickup is cut off FIRST, as at the deadline (GitHub Codex review of #124): the offer is
        marked closed and, unless it already finished, "stopped", so the transfer can no longer be counted as served;
        its connection is shut down; and the transfer is given a moment to unwind, so its "interrupted" event comes
        before the caller reports the stop. Request workers are daemon threads, so closing only the listening socket
        would have let a transfer in progress go on sending private bytes after the CLI said the offer was stopped."""
        with self._state:
            self.closed = True
            self.outcome = self.outcome or "stopped"
            connection = self._connection
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass                      # the handler already closed it
            if self.transfer.acquire(timeout=STOP_UNWIND_S):
                self.transfer.release()
        if self._thread is not None:
            self.shutdown()
            self._thread.join(timeout=5)
            self._thread = None
        self.server_close()

    def _closed_because(self) -> str | None:
        if self.outcome is not None:
            return "already served" if self.outcome == "served" else "the offer has ended"
        if self.closed or time.monotonic() >= self.deadline:
            return "the window has closed"
        return None

    def answer(self, handler: _OfferHandler) -> None:
        """The signature is checked BEFORE the transfer lock is tried, so only a request signed by the recipient can ever
        hold it: a reader of the message who has the token and not the key cannot make the real pickup meet "a transfer
        is in progress". The window and the outcome are checked again under the lock."""
        if not hmac.compare_digest(handler.raw_target.encode("utf-8"), ("/" + self.token).encode("ascii")):
            return handler.not_found("not the offer's path")
        why = self._closed_because()
        if why:
            return handler.not_found(why)
        with self._checks:
            why = pickup_problem(handler.headers, url=self.url, offered_by=self.offered_by, token=self.token,
                                 expires=self.expires, for_peer=self.for_peer, keys=self.keys, nonces=self.nonces)
        if why:
            return handler.not_found(why)
        if not self.transfer.acquire(blocking=False):
            return handler.not_found("a transfer is in progress")
        try:
            why = self._closed_because()
            if why:
                return handler.not_found(why)
            with self._state:
                self._connection = handler.connection
            self._send(handler)
        finally:
            with self._state:
                self._connection = None
            self.transfer.release()

    def _fail(self, why: str) -> None:
        with self._state:
            self.outcome = self.outcome or "failed"
        self.event({"event": "error", "detail": why + "; nothing more is served"})
        self.done.set()

    def _send(self, handler: _OfferHandler) -> None:
        """Stream the snapshot, never FILE (Snapshot). Under the transfer lock, so one reader of it at a time. The bytes
        are hashed as they go: a snapshot that could not be read back whole is not counted as served. Every write is
        bounded by the remaining window, including one that keeps making progress."""
        ip = handler.client_address[0]
        f = self.snapshot.file
        handler.close_connection = True
        sent, h = 0, hashlib.sha256()

        def write_timeout() -> None:
            remaining = self.deadline - time.monotonic()
            if self.closed or remaining <= 0:
                raise TimeoutError("the window closed mid-transfer")
            handler.connection.settimeout(min(handler.timeout, remaining))

        try:
            f.seek(0)
            handler.send_response(HTTPStatus.OK)
            handler.send_header("Content-Type", "application/octet-stream")
            handler.send_header("Content-Length", str(self.size))
            handler.send_header("Cache-Control", "no-store")
            write_timeout()
            handler.end_headers()
            while sent < self.size:
                chunk = f.read(min(CHUNK, self.size - sent))
                if not chunk:
                    break
                write_timeout()
                handler.wfile.write(chunk)
                h.update(chunk)
                sent += len(chunk)
            write_timeout()
            handler.wfile.flush()
            with self._state:
                if self.closed or time.monotonic() >= self.deadline:
                    raise TimeoutError("the window closed mid-transfer")
                if sent == self.size and h.hexdigest() == self.sha256:
                    self.outcome = "served"
        except (OSError, ValueError):     # ValueError: the snapshot was closed under a transfer (the offer stopping)
            with self._state:
                expired = self.closed or time.monotonic() >= self.deadline
                if expired:
                    self.closed = True
                    self.outcome = self.outcome or "expired"
                stopped = self.outcome == "stopped"
            if expired:
                try:
                    handler.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass                  # the deadline waiter or recipient already closed it
            self.event({"event": "interrupted", "from": ip, "bytes_sent": sent,
                        "detail": ("the offer was stopped mid-transfer; not counted as served" if stopped else
                                   "the window expired mid-transfer; not counted as served" if expired else
                                   "the connection failed mid-transfer; not counted, the offer stays open")})
            if expired:
                self.done.set()
            return None
        if sent != self.size or h.hexdigest() != self.sha256:
            return self._fail(f"the snapshot could not be read back whole ({sent} bytes went); not counted as served")
        self.event({"event": "served", "to_address": ip, "peer": self.for_peer, "bytes": sent, "sha256": self.sha256})
        self.done.set()
        return None


# --------------------------------------------------------------------------- the commands

def add_parsers(sub, common) -> None:
    o = sub.add_parser("offer", parents=[common],
                       help="PRIVATE bytes to one seat over the LAN: one signed one-shot download, announced in one "
                            "message; the bytes never reach the mesh server. Moving private bytes still needs the "
                            "operator's approval for each transfer")
    o.add_argument("file", type=Path, help="a regular file (a link is refused)")
    o.add_argument("--to", required=True, help="the recipient's label (one)")
    o.add_argument("--to-peer", required=True, metavar="PEER_ID",
                   help="the recipient's peer id (`read` prints it as `signed_by` for a message that seat sent): only a "
                        "pickup signed by the key this peer publishes is served")
    o.add_argument("--bind", required=True, metavar="IP",
                   help="this host's LAN address to listen on; a wildcard or a public address is refused unless "
                        "--any-interface")
    o.add_argument("--port", type=int, default=0, help="the port (default 0: any free one; the URL carries it)")
    o.add_argument("--ttl", type=int, default=DEFAULT_TTL_S,
                   help=f"seconds the offer stays open (default {DEFAULT_TTL_S}, at most {MAX_TTL_S})")
    o.add_argument("--thread", help="thread_id (default: offer/<first 12 hex of the sha256>)")
    o.add_argument("--subject", help="as `send --subject`: a slug, never prose")
    o.add_argument("--any-interface", action="store_true",
                   help="allow a wildcard or a public --bind (a wildcard then needs --advertise)")
    o.add_argument("--advertise", metavar="IP", help="with a wildcard --bind: the address the URL names")
    o.add_argument("--from", dest="sender", help="sender label (default: as `send`)")
    o.add_argument("--author-family", default="unknown")

    p = sub.add_parser("pickup", parents=[common],
                       help="fetch the file a `meshy-offer/1` message offers, check its size and sha256, and save it "
                            "under its name in --out (never over an existing file)")
    p.add_argument("--key", required=True, help="the msg/ key of the offer message")
    p.add_argument("--from-peer", metavar="PEER_ID", default=None,
                   help="require the offer message to be signed by this peer id (`read` prints it as `signed_by`); "
                        "an offer signed by any other peer is refused before the offer is contacted")
    p.add_argument("--out", type=Path, default=Path("."), help="the directory to save into (default: the current one)")
    p.add_argument("--receipt", action="store_true",
                   help="answer the offer (in_reply_to, same thread) with one line: picked up, size, sha256 verified")
    p.add_argument("--from", dest="sender", help="sender label for the receipt (default: as `send`)")
    p.add_argument("--author-family", default="unknown")


def _line(out, record: dict) -> None:
    """One event, one compact JSON line, flushed. A stdout that cannot be written must not stop a transfer in flight."""
    try:
        out.write(json.dumps(record) + "\n")
        out.flush()
    except (OSError, ValueError):
        pass


def _emit(out, record: dict) -> None:
    out.write(json.dumps(record, indent=2) + "\n")
    out.flush()


def _expiry(now: datetime, ttl: int) -> datetime:
    """`now` + `ttl`, rounded UP to the second: the line carries whole seconds, and rounding down would close the window
    before the instant it names."""
    at = now + timedelta(seconds=ttl)
    return at.replace(microsecond=0) + timedelta(seconds=1) if at.microsecond else at


def run_offer(args, state: dict, *, out=None) -> int:
    """`offer`. Everything that can be refused is refused before the socket opens or anything is sent (Refused, exit 2)."""
    out = out or sys.stdout
    node.require_peer(args.state_file, state)
    labels = [part.strip() for part in args.to.split(",") if part.strip()]
    if len(labels) != 1:
        raise node.Refused("--to names exactly one recipient: an offer is served once, to one peer")
    label = labels[0]
    try:
        for_peer = str(uuid.UUID(args.to_peer))
    except ValueError:
        raise node.Refused(f"--to-peer {args.to_peer!r} is not a peer id; `read --key <a message that seat sent>` "
                           "prints it as `signed_by`") from None
    bind, url_host = check_bind(args.bind, any_interface=args.any_interface, advertise=args.advertise)
    if not 0 <= args.port <= 65535:
        raise node.Refused(f"--port {args.port} is not a TCP port (0-65535)")
    if not 1 <= args.ttl <= MAX_TTL_S:
        raise node.Refused(f"--ttl must be 1 to {MAX_TTL_S} seconds, got {args.ttl}")
    why = wire.subject_problem(args.subject) if args.subject else None     # as `send` (backlog item 15)
    if why:
        raise node.Refused(f"--subject {args.subject!r} refused: {why}")
    path = Path(args.file)
    why = name_problem(path.name)
    if why:
        raise node.Refused(f"{why}; pickup would refuse it, so rename the file before offering it")
    try:
        snapshot = Snapshot(path)
    except OSError as exc:
        raise node.Refused(f"cannot read {path} or copy it into a private snapshot: {exc}") from None
    with snapshot:
        return _offer(args, state, out, label=label, for_peer=for_peer, bind=bind, url_host=url_host, name=path.name,
                      snapshot=snapshot)


def _offer(args, state: dict, out, *, label: str, for_peer: str, bind: str, url_host: str, name: str,
           snapshot: Snapshot) -> int:
    """The rest of `offer`, with the snapshot taken (run_offer removes it whatever this returns or raises).

    The window starts at `server.start`, after the send was accepted, and `expires` is set before the send to the TTL
    plus SEND_ALLOWANCE_S, so the instant the line names is never earlier than the one the server keeps (THE TTL). The
    send runs inside node.finishing_by(`clock` + SEND_ALLOWANCE_S - START_MARGIN_S): it has returned by then, its
    result checked, or it is `not_sent`, so a send that was accepted leaves the whole TTL before `expires`, and
    `server.start` refuses a window that no longer fits all the same (`expired`, nothing answered). Once serving began
    nothing stops the offer but its window, its one GET or an interrupt: the recipient already holds the message."""
    size, sha = snapshot.size, snapshot.sha256
    jwks, unavailable = node.author_keys(state["base_url"], for_peer)
    keys = published_keys(jwks)
    if not keys:
        raise node.Refused(f"peer {for_peer} publishes no Ed25519 key ({unavailable or 'its JWKS holds none'}), so no "
                           "pickup could be verified; nothing was sent")
    thread = args.thread or f"offer/{sha[:12]}"
    token = secrets.token_urlsafe(TOKEN_BYTES)
    now, clock = datetime.now(timezone.utc), time.monotonic()
    expires_at = _expiry(now, args.ttl + SEND_ALLOWANCE_S)
    expires = expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        server = OfferServer((bind, args.port), url_host=url_host, offered_by=str(uuid.UUID(state["peer_id"])),
                             snapshot=snapshot, token=token, expires=expires, ttl=args.ttl,
                             not_after=clock + (expires_at - now).total_seconds(), for_peer=for_peer, keys=keys,
                             emit=lambda record: _line(out, record))
    except OSError as exc:
        raise node.Refused(f"cannot listen on {bind} port {args.port}: {exc}; --bind must be one of this host's "
                           "addresses and the port free") from exc
    try:
        port = server.server_address[1]
        # The url the server checks every pickup's signature against, as it is: the line carries exactly that.
        meta = {"name": name, "size_bytes": size, "sha256": sha, "url": server.url, "expires": expires,
                "for_peer": for_peer}
        body = offer_body(meta)
        docshare.refuse_local_paths(body, node.Refused)
        body, subject = sendguard.admit(args.state_file, to=[label], cc=None, body=body, subject=args.subject,
                                        bulk_ack=None, refused=node.Refused, pair_loop=False)
        budget = SEND_ALLOWANCE_S - START_MARGIN_S
        try:
            with node.finishing_by(clock + budget):
                result = node.send(args.state_file, state, to=[label], thread_id=thread, body=body,
                                   msg_type=OFFER_TYPE, author_family=args.author_family, sender=args.sender,
                                   subject=subject, poll_timeout=SEND_POLL_S)
        except node.DeadlinePassed as exc:
            _line(out, {"event": "not_sent", "status": "unconfirmed", "error_message": str(exc),
                        "detail": f"the send did not end within {budget:g} s, so the offer could not keep its whole "
                                  "TTL before the expires its message names; the socket is closed and nothing was "
                                  "served. The server may still deliver the message: its link is dead. Offer again"})
            return 1
        if not node.succeeded(result):
            _line(out, {"event": "not_sent", "status": result.get("status"), "error_code": result.get("error_code"),
                        "error_message": result.get("error_message"),
                        "result_signature_verified": result.get("result_signature_verified"),
                        "detail": "the server did not accept the offer message; the socket is closed and nothing was "
                                  "served"})
            return 1
        key = str(result.get("key") or "")

        def record(then: str) -> None:
            try:
                sendguard.record(args.state_file, to=[label], cc=None, key=key)
            except Exception as exc:  # noqa: BLE001 - the offer was accepted: a ledger write must not strand it
                _line(out, {"event": "warning", "key": key,
                            "detail": f"the send ledger could not be written ({type(exc).__name__}: {exc}); this send "
                                      f"is not counted by the volume guard. {then}"})

        def announce() -> None:
            _line(out, {"event": "offered", "key": key, "to": label, "for_peer": for_peer, "name": name,
                        "size_bytes": size, "sha256": sha, "address": f"{_url_host(url_host)}:{port}", "bind": bind,
                        "ttl_s": args.ttl, "expires": expires, "thread": thread})
            record("The offer was accepted and is served")
        if not server.start(announce):
            _line(out, {"event": "expired", "key": key, "expires": expires,
                        "detail": f"the offer message was accepted, but serving could not start while the whole "
                                  f"{args.ttl} s TTL still fitted before the expires it names; nothing was served and "
                                  "the link is dead. Offer again if it is still wanted"})
            record("The offer message was accepted; nothing was served")
            return 1
        try:
            outcome = server.wait()
        except KeyboardInterrupt:
            server.stop()                 # cut off an active pickup before saying so
            if server.outcome == "served":
                _line(out, {"event": "stopped", "detail": "interrupted after the offer was served in full"})
                return 0
            _line(out, {"event": "stopped", "detail": "interrupted; a pickup in progress was cut off and nothing more "
                                                      "is served"})
            return 1
        if outcome == "expired":
            _line(out, {"event": "expired", "expires": expires,
                        "detail": "the window closed and nothing was served; the link is dead. Offer again if it is "
                                  "still wanted"})
        return 0 if outcome == "served" else 1
    finally:
        server.stop()


def _remove(path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _fetch(url: str, token: str, meta: dict, state: dict, sink, timeout: float, *, offered_by: str) -> tuple[int, str]:
    """(bytes written to `sink`, their sha256), reading at most `size_bytes` + 1 so an offer that sends more is caught
    without reading it all. No proxy (node._opener: the URL given is the URL reached, and the token reaches no proxy) and
    no redirect: the 3xx is the answer. The request is signed for the line's url as the line carries it and for
    `offered_by`, the peer that signed the offer message: only that offer, at that address, can verify it."""
    size = meta["size_bytes"]
    request = urllib.request.Request(url, method="GET",
                                     headers=pickup_headers(state, url=meta["url"], offered_by=offered_by, token=token,
                                                            expires=meta["expires"]))
    h, n = hashlib.sha256(), 0
    try:
        with node._opener(follow_redirects=False).open(request, timeout=timeout) as response:
            if response.status != 200:
                raise PickupFailed(E_OFFER_FETCH, f"the offer answered HTTP {response.status}",
                                   http_status=response.status)
            while True:
                chunk = response.read(min(CHUNK, size + 1 - n))
                if not chunk:
                    break
                n += len(chunk)
                if n > size:
                    raise PickupFailed(E_OFFER_SIZE_MISMATCH, f"the offer sent more than its {size} bytes")
                h.update(chunk)
                try:
                    sink.write(chunk)
                except OSError as exc:
                    raise PickupFailed(E_OFFER_WRITE, f"writing into --out failed: {type(exc).__name__}: {exc}") from None
    except urllib.error.HTTPError as exc:
        raise PickupFailed(E_OFFER_FETCH, f"the offer answered HTTP {exc.code}: it was already picked up, its window "
                                          "closed, or it refused this node's signed request (the offering seat's "
                                          "`refused` line says which)", http_status=exc.code) from None
    except (OSError, http.client.HTTPException) as exc:
        raise PickupFailed(E_OFFER_FETCH, f"the offer could not be fetched: {type(exc).__name__}: {exc}") from None
    return n, h.hexdigest()


#: A hard link refused with one of these means the filesystem has none (FAT and exFAT, some network shares; Windows
#: reports ERROR_INVALID_FUNCTION and ERROR_NOT_SUPPORTED as EINVAL): only then is a rename tried. Any other refusal
#: (EACCES, ENOSPC, EROFS) is this disk's and ends the pickup as E_OFFER_WRITE.
_NO_HARD_LINKS = frozenset({errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS}
                           | ({errno.EINVAL} if os.name == "nt" else set()))
#: A no-replace rename refused with one of these means this filesystem or kernel cannot do it.
_NO_RENAME_FLAG = frozenset({errno.EINVAL, errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP})
_RENAME_EXCL = 0x00000004          # macOS <stdio.h>, renamex_np(2)
_RENAME_NOREPLACE = 1              # Linux <linux/fs.h>, renameat2(2)
_AT_FDCWD = -100                   # Linux <fcntl.h>


def _noreplace_rename():
    """This platform's rename that refuses an existing target in the same step that would have replaced it, as
    f(src, dst) raising FileExistsError then (another OSError for anything else); None where there is none. Windows'
    os.rename (MoveFileExW without MOVEFILE_REPLACE_EXISTING) never replaces. macOS renamex_np with RENAME_EXCL and Linux
    renameat2 with RENAME_NOREPLACE (glibc 2.28) do it when asked, and os has neither, so they are called through ctypes.
    Never a check of the name and then a plain rename: a file that appears between the two is replaced."""
    if os.name == "nt":
        return os.rename
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError:
        return None
    if sys.platform == "darwin":
        fn = getattr(libc, "renamex_np", None)
        if fn is None:
            return None
        fn.argtypes, fn.restype = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint), ctypes.c_int

        def call(src, dst):
            return fn(os.fsencode(src), os.fsencode(dst), _RENAME_EXCL)
    elif sys.platform.startswith("linux"):
        fn = getattr(libc, "renameat2", None)
        if fn is None:
            return None
        fn.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        fn.restype = ctypes.c_int

        def call(src, dst):
            return fn(_AT_FDCWD, os.fsencode(src), _AT_FDCWD, os.fsencode(dst), _RENAME_NOREPLACE)
    else:
        return None

    def rename(src, dst) -> None:
        if call(src, dst) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), os.fspath(dst))      # EEXIST is FileExistsError (PEP 3151)
    return rename


def _place(tmp: str, target: Path) -> None:
    """Give the verified temporary file its name, never over an existing one, on every platform. A hard link is created
    or refused in one step (FileExistsError), and then the temporary name is dropped. Only where the filesystem has no
    hard links (_NO_HARD_LINKS): the temporary file is renamed by `_noreplace_rename`, which is also one step; where
    there is none, or this filesystem refuses its flag, E_OFFER_PLACE_UNSUPPORTED and the file gets no name. Any other
    OSError is raised as it is (pickup: E_OFFER_WRITE). The caller removes the temporary file on every failure."""
    exists = PickupFailed(E_OFFER_EXISTS, f"{target} appeared while the file was being fetched; pickup never "
                                          "overwrites, and the fetched copy was removed")
    unsupported = PickupFailed(E_OFFER_PLACE_UNSUPPORTED, f"{target.parent} has no hard links and no rename that refuses "
                                                          "an existing name, so the file cannot be given its name "
                                                          "without risking replacing one; the fetched copy was "
                                                          "removed. Pass an --out on another filesystem")
    try:
        os.link(tmp, target)
    except FileExistsError:
        raise exists from None
    except OSError as exc:
        if exc.errno not in _NO_HARD_LINKS:
            raise
        rename = _noreplace_rename()
        if rename is None:
            raise unsupported from None
        try:
            rename(tmp, target)
        except FileExistsError:
            raise exists from None
        except OSError as again:
            if again.errno in _NO_RENAME_FLAG:
                raise unsupported from None
            raise
        return
    _remove(tmp)


def pickup(message: dict, state: dict, *, out_dir: Path, timeout: float = FETCH_TIMEOUT_S,
           from_peer: str | None = None) -> dict:
    """Fetch the file `message` (a `node.read` answer) offers into `out_dir`, or PickupFailed with no temporary file left.
    Every check that needs no network runs before the offer is contacted, because a fetch can spend it."""
    if message.get("signature_verified") is not True:
        raise PickupFailed(E_OFFER_UNVERIFIED, "the message's author signature did not verify, so its url and sha256 are "
                                               "only as good as the server that returned them; nothing was fetched",
                           signature_error=message.get("signature_error"))
    try:
        offered_by = str(uuid.UUID(message.get("signed_by")))
    except (TypeError, ValueError, AttributeError):
        raise PickupFailed(E_OFFER_UNVERIFIED, "the message names no peer that signed it (`signed_by`), and the pickup "
                                               "is signed for the offering peer; nothing was fetched") from None
    if from_peer is not None:
        try:
            expected_peer = str(uuid.UUID(from_peer))
        except (TypeError, ValueError, AttributeError):
            raise node.Refused(f"--from-peer {from_peer!r} is not a peer id; `read --key <a message that seat sent>` "
                               "prints it as `signed_by`") from None
        if offered_by != expected_peer:
            raise PickupFailed(E_OFFER_WRONG_SENDER, f"the offer was signed by peer {offered_by}, not --from-peer "
                                                     f"{expected_peer}; nothing was fetched",
                               signed_by=offered_by, expected_peer=expected_peer)
    meta, url, token = parse_offer(message.get("body"))
    me = str(uuid.UUID(state["peer_id"]))
    if meta["for_peer"] != me:
        raise PickupFailed(E_OFFER_NOT_FOR_THIS_PEER, f"the offer is for peer {meta['for_peer']}, and this node is "
                                                      f"{me}: its signed request would be refused; nothing was fetched")
    target = Path(out_dir) / meta["name"]
    if os.path.lexists(target):
        raise PickupFailed(E_OFFER_EXISTS, f"{target} exists; pickup never overwrites. Move it or pass another --out "
                                           "and run pickup again: nothing was fetched, the offer is still open")
    try:
        fd, tmp = tempfile.mkstemp(prefix=".meshy-offer-", suffix=".part", dir=str(out_dir))
    except OSError as exc:
        raise PickupFailed(E_OFFER_WRITE, f"cannot create a temporary file in {out_dir}: {type(exc).__name__}: {exc}; "
                                          "nothing was fetched") from None
    try:
        try:
            with os.fdopen(fd, "wb") as sink:
                got, sha = _fetch(url, token, meta, state, sink, timeout, offered_by=offered_by)
                sink.flush()
                os.fsync(sink.fileno())
            if got != meta["size_bytes"]:
                raise PickupFailed(E_OFFER_SIZE_MISMATCH, f"received {got} bytes, the offer says {meta['size_bytes']}; "
                                                          "the fetched copy was removed")
            if not hmac.compare_digest(sha, meta["sha256"]):
                raise PickupFailed(E_OFFER_HASH_MISMATCH, f"received bytes with sha256 {sha}, the offer says "
                                                          f"{meta['sha256']}; the fetched copy was removed")
            _place(tmp, target)
        except OSError as exc:
            # _fetch turns every network failure into PickupFailed, so an OSError here is this disk's.
            raise PickupFailed(E_OFFER_WRITE, f"the file could not be written into {out_dir}: {type(exc).__name__}: "
                                              f"{exc}; the fetched copy was removed") from None
    except BaseException:
        _remove(tmp)
        raise
    host = urllib.parse.urlsplit(url).hostname
    return {"name": meta["name"], "path": os.path.abspath(target), "size_bytes": got, "sha256": sha,
            "sha256_verified": True, "from": message.get("from"), "signed_by": message.get("signed_by"),
            "offered_from": host}


def _receipt(args, state: dict, message: dict, picked: dict) -> tuple[dict, bool]:
    """Answer the offer: one line, to its `from`, on its thread, `in_reply_to` its key. The file is in place whatever
    this returns. Once the receipt was accepted the pickup is complete and spent, so a send ledger that cannot be written
    then (a full disk, permissions) is a `warning` in the receipt, never an error that would hide the completed pickup
    and invite a retry that can only meet an existing file and a spent offer."""
    to, thread = message.get("from"), message.get("thread_id")
    if not (isinstance(to, str) and to and isinstance(thread, str) and thread):
        return {"sent": False, "detail": "the offer names no sender label or thread to answer; the file is in place"}, False
    body = receipt_body(picked["name"], picked["size_bytes"], picked["sha256"])
    try:
        docshare.refuse_local_paths(body, node.Refused)
        body, subject = sendguard.admit(args.state_file, to=[to], cc=None, body=body, subject=None, bulk_ack=None,
                                        refused=node.Refused, pair_loop=False)
        result = node.send(args.state_file, state, to=[to], thread_id=thread, body=body, msg_type=RECEIPT_TYPE,
                           author_family=args.author_family, sender=args.sender, in_reply_to=args.key,
                           subject=subject)
    except (node.Refused, node.ServerSaid, wire.NotV1, OSError) as exc:
        return {"sent": False, "detail": f"{type(exc).__name__}: {exc}; the file is in place"}, False
    ok = node.succeeded(result)
    if ok:
        receipt = {"sent": True, "key": result.get("key"), "in_reply_to": args.key}
        try:
            sendguard.record(args.state_file, to=[to], cc=None, key=str(result.get("key") or ""))
        except Exception as exc:  # noqa: BLE001 - the receipt was accepted: a ledger write must not hide the pickup
            receipt["warning"] = (f"the send ledger could not be written ({type(exc).__name__}: {exc}); this receipt "
                                  "is not counted by the volume guard. It was accepted and the file is in place")
        return receipt, True
    return {"sent": False, "key": result.get("key"), "status": result.get("status"),
            "error_code": result.get("error_code"), "error_message": result.get("error_message"),
            "detail": "the server did not accept the receipt; the file is in place"}, False


def run_pickup(args, state: dict, *, out=None) -> int:
    """`pickup`: one JSON object, `picked_up` true or false."""
    out = out or sys.stdout
    node.require_peer(args.state_file, state)
    from_peer = getattr(args, "from_peer", None)
    if from_peer is not None:
        try:
            from_peer = str(uuid.UUID(from_peer))
        except (TypeError, ValueError, AttributeError):
            raise node.Refused(f"--from-peer {from_peer!r} is not a peer id; `read --key <a message that seat sent>` "
                               "prints it as `signed_by`") from None
    out_dir = Path(args.out)
    if not out_dir.is_dir():
        raise node.Refused(f"--out {out_dir} is not a directory")
    try:
        message = node.read(args.state_file, state, key=args.key)
    except node.ReadFailed as exc:
        _emit(out, {"picked_up": False, "key": args.key, **exc.error})
        return 1
    try:
        picked = pickup(message, state, out_dir=out_dir, from_peer=from_peer)
    except PickupFailed as exc:
        _emit(out, {"picked_up": False, "key": args.key, **exc.error})
        return 1
    summary = {"picked_up": True, "key": args.key, **picked}
    rc = 0
    if args.receipt:
        summary["receipt"], ok = _receipt(args, state, message, picked)
        rc = 0 if ok else 1
    _emit(out, summary)
    return rc
