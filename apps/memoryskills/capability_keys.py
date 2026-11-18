"""The capability root key store: a FILE, read once at startup (B07 slice 4, divergence 040).

One active Ed25519 keypair has its key id and public half available to verifiers, with a
dev-only ephemeral key behind an explicit flag (docs/design/biscuit-capabilities.md 2.2,
Q10, critique C1):

  * THE KEY IS A FILE, named by `MESHY_CAPABILITY_ROOT_KEY_PATH`; this module never reads
    `MESHYCHAT_BISCUIT_ROOT_SK` or takes key material from ANY environment variable. Setting
    that variable makes `issue_grant` refuse every skill grant
    (`apps/grants/config.py:biscuit_minting_enabled`). A raw key in the environment is
    inherited by every child process, and sharing one key across deployments would let a leak
    in either mint tokens accepted by both. A value of the PATH variable that is 64 hex digits
    is refused as key material; any value that names no readable file is refused too, and NO
    refusal repeats the value, whatever its encoding (review F1).
  * THE FILE HOLDS HEX, one 32-byte Ed25519 private key, optionally ending in one newline.
    Hex, not base64: the parser reads hex.
  * ROTATION BY KID FROM DAY ONE. `MESHY_CAPABILITY_ROOT_KID` is the active key's integer id
    (a u32, the width biscuit's `root_key_id` has); `MESHY_CAPABILITY_ROOT_PUBKEYS` is the
    verify set, `kid=hex;kid=hex`. The active kid must be in it with the active key's own
    public half. `provider(kid)` is what `Biscuit.from_base64` calls with a token's kid: an
    unknown kid, and a token with none, are refused.
  * FAIL CLOSED AT BOOT. With `MESHY_CAPABILITIES` on, a missing or invalid key, a verify set
    that lacks the active kid, and an `import biscuit_auth` that fails each raise
    `CapabilityKeyRefused`, which apps/memoryskills/apps.py turns into ImproperlyConfigured:
    the process does not start (V1 rule A4).
    With the flag OFF nothing here is parsed and `biscuit_auth` is not imported.
  * THE DEV KEY is read only under DEBUG: `MESHY_CAPABILITY_DEV_EPHEMERAL` gives a fresh key
    per process when no key file is named. Outside DEBUG the flag's name is never looked up,
    so it cannot turn anything on in a deploy.

THE KEY COMMAND. ready() refuses to start with the flag on and no key, so the key is made
outside Django:

    python -m apps.memoryskills.capability_keys create PATH --kid N

writes a new key to PATH exclusively (O_EXCL, owner-only mode from the first byte: 23ca808's
rule for key files; on Windows, where a mode sets no ACL, a protected ACL naming only the
creating user; a PATH that exists is refused and left as it was, and so is a PATH inside the
checkout, relative ones included, since the command runs from it) and prints the verify-set
entry, `MESHY_CAPABILITY_ROOT_PUBKEYS=N=<public hex>`. It never prints the private key. It
needs PyNaCl, not biscuit, so it also runs where biscuit-python cannot be installed.

Nothing here imports Django.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# The flag and its parser live in apps/core/capability_flag.py, which settings.py imports
# without loading this package (review F6); re-exported here, so the names stay one.
from apps.core.capability_flag import (  # noqa: F401
    ENABLE_ENV,
    FLAG_FALSE,
    FLAG_TRUE,
    CapabilityConfigError,
    enabled_from_env,
)

#: The variables, named once. tests/test_settings_documents_the_fresh_install.py holds that
#: settings.py's table documents each.
KEY_PATH_ENV = "MESHY_CAPABILITY_ROOT_KEY_PATH"
KID_ENV = "MESHY_CAPABILITY_ROOT_KID"
PUBKEYS_ENV = "MESHY_CAPABILITY_ROOT_PUBKEYS"
DEV_EPHEMERAL_ENV = "MESHY_CAPABILITY_DEV_EPHEMERAL"

#: biscuit's `root_key_id` is a u32 (format/schema.proto `optional uint32 rootKeyId`).
MAX_KID = 2 ** 32 - 1
#: The kid a dev ephemeral key takes when MESHY_CAPABILITY_ROOT_KID is unset.
DEV_KID = 0

RUNBOOK = ("unset MESHY_CAPABILITIES to run the server and manage.py commands while the key "
           "is repaired")

_HEX64 = re.compile(r"[0-9a-fA-F]{64}")
_KID_TEXT = re.compile(r"[0-9]+")


class CapabilityKeyRefused(ValueError):
    """The key store cannot be built. The message names the variable and the fault, and never
    repeats key material."""


# --------------------------------------------------------------------------- parsing

def public_of(private: bytes) -> bytes:
    """The Ed25519 public half of a 32-byte private key (RFC 8032 seed), by PyNaCl."""
    from nacl.signing import SigningKey
    return bytes(SigningKey(private).verify_key)


def _small_order(public: bytes) -> bool:
    from apps.crypto import primitives
    return public in primitives._SMALL_ORDER


def parse_kid(raw) -> int:
    """A u32 written in decimal digits and nothing else."""
    text = (raw or "").strip()
    if not text:
        raise CapabilityKeyRefused(f"{KID_ENV} is not set: it names the active key's id")
    if not _KID_TEXT.fullmatch(text) or int(text) > MAX_KID:
        raise CapabilityKeyRefused(
            f"{KID_ENV}={text!r} is not a key id: decimal digits only, 0 to {MAX_KID}")
    return int(text)


def parse_pubkeys(raw) -> dict[int, bytes]:
    """`kid=hex;kid=hex`, each hex a 32-byte Ed25519 public key that is not a small-order
    point, no kid twice, no empty entry."""
    text = (raw or "").strip()
    if not text:
        raise CapabilityKeyRefused(
            f"{PUBKEYS_ENV} is not set: it is the verify set, kid=hex;kid=hex, and must hold "
            "the active kid")
    out: dict[int, bytes] = {}
    for n, entry in enumerate(text.split(";"), start=1):
        kid_text, sep, hex_text = entry.strip().partition("=")
        if not sep:
            raise CapabilityKeyRefused(f"{PUBKEYS_ENV}: entry {n} is not kid=hex")
        try:
            kid = parse_kid(kid_text)
        except CapabilityKeyRefused:
            raise CapabilityKeyRefused(
                f"{PUBKEYS_ENV}: entry {n}'s kid is not decimal digits (0 to {MAX_KID})"
            ) from None
        hex_text = hex_text.strip()
        if not _HEX64.fullmatch(hex_text):
            raise CapabilityKeyRefused(
                f"{PUBKEYS_ENV}: the key of kid {kid} is not 64 hex digits (32 bytes)")
        public = bytes.fromhex(hex_text)
        if _small_order(public):
            raise CapabilityKeyRefused(
                f"{PUBKEYS_ENV}: the key of kid {kid} is a small-order point, never a key")
        if kid in out:
            raise CapabilityKeyRefused(f"{PUBKEYS_ENV}: kid {kid} appears twice")
        out[kid] = public
    return out


def read_key_file(path) -> bytes:
    """The 32-byte private key in the file at `path`: 64 hex digits, optionally followed by
    one newline (LF or CRLF), and nothing else.

    NO REFUSAL REPEATS THE VARIABLE'S VALUE (review F1). The 64-hex check below recognizes
    one form of pasted key; other encodings fall through to a file that does not exist, and
    startup puts this message on stderr. So every refusal names the variable, the fault and
    the value's LENGTH, and never the value: one rule, not a guess about which values are
    keys."""
    text = (path or "").strip()
    if not text:
        raise CapabilityKeyRefused(
            f"{KEY_PATH_ENV} is not set: it names the FILE that holds the root private key "
            f"(make one with: python -m apps.memoryskills.capability_keys create PATH)")
    if _HEX64.fullmatch(text):
        raise CapabilityKeyRefused(
            f"{KEY_PATH_ENV} holds what looks like key material, not a path: the key must be "
            "in a file the variable names, never in the environment. Move it to a file, and "
            "treat the value as exposed")
    try:
        raw = Path(text).read_bytes()
    except OSError as exc:
        why = f": {exc.strerror}" if exc.strerror else ""
        raise CapabilityKeyRefused(
            f"{KEY_PATH_ENV} does not name a readable file ({type(exc).__name__}{why}). Its "
            f"value ({len(text)} characters) is not repeated here: if it is the key itself, "
            "move the key to a file and treat the value as exposed") from None
    body = raw[:-2] if raw.endswith(b"\r\n") else raw[:-1] if raw.endswith(b"\n") else raw
    if len(body) != 64 or not _HEX64.fullmatch(body.decode("latin-1")):
        raise CapabilityKeyRefused(
            f"{KEY_PATH_ENV} names a file that is not one hex Ed25519 private key (64 hex "
            f"digits, optionally one newline; read {len(raw)} bytes)")
    return bytes.fromhex(body.decode("ascii"))


# --------------------------------------------------------------------------- the store

class RootKeyStore:
    """The active root key and the verify set, built once. biscuit objects are made here,
    which is the first `import biscuit_auth` of the process."""

    __slots__ = ("active_kid", "_private", "_verify", "_public_keys", "_private_key")

    def __init__(self, private: bytes, active_kid: int, verify: dict[int, bytes]):
        try:
            import biscuit_auth
        except ImportError as exc:
            raise CapabilityKeyRefused(
                f"biscuit_auth could not be imported ({exc}); biscuit-python 0.4.0 installs on "
                "CPython 3.13 only (divergence 040)") from None
        if active_kid not in verify:
            raise CapabilityKeyRefused(
                f"{PUBKEYS_ENV} does not hold the active kid {active_kid} ({KID_ENV}): tokens "
                "this server mints would not verify")
        public = public_of(private)
        if verify[active_kid] != public:
            raise CapabilityKeyRefused(
                f"{PUBKEYS_ENV} holds another key under the active kid {active_kid}: it is not "
                f"the public half of the key in {KEY_PATH_ENV}")
        alg = biscuit_auth.Algorithm.Ed25519
        private_key = biscuit_auth.PrivateKey.from_bytes(private, alg)
        derived = bytes(biscuit_auth.KeyPair.from_private_key(private_key).public_key.to_bytes())
        if derived != public:
            raise CapabilityKeyRefused(
                f"{KEY_PATH_ENV}: biscuit derives another public key from this private key")
        self.active_kid = active_kid
        self._private = private
        self._verify = dict(verify)
        self._public_keys = {kid: biscuit_auth.PublicKey.from_bytes(pk, alg)
                             for kid, pk in verify.items()}
        self._private_key = private_key

    def __repr__(self) -> str:
        return f"RootKeyStore(active_kid={self.active_kid}, kids={sorted(self._verify)})"

    @property
    def kids(self) -> frozenset[int]:
        return frozenset(self._verify)

    def public_key_bytes(self, kid: int) -> bytes:
        return self._verify[kid]

    def provider(self, kid):
        """biscuit's root key provider: the verify key for a token's kid. A token without a
        kid (None), and a kid outside the verify set, are refused."""
        if type(kid) is not int or kid not in self._public_keys:
            raise ValueError("unknown root key id")
        return self._public_keys[kid]

    def root_private_key(self):
        """The active biscuit PrivateKey, for the mint (slice 5) and the tests' tokens."""
        return self._private_key

    def _private_bytes(self) -> bytes:
        return self._private


def load(*, enabled: bool, key_path, kid, pubkeys, debug: bool, env) -> RootKeyStore | None:
    """The store for these settings, or None when tokens are off. Raises CapabilityKeyRefused.

    `env` is read for ONE name, the dev flag, and only when `debug` is true."""
    if not enabled:
        return None
    if (key_path or "").strip():
        private = read_key_file(key_path)
        active_kid = parse_kid(kid)
        verify = parse_pubkeys(pubkeys)
    elif debug and (env.get(DEV_EPHEMERAL_ENV) or "").strip().lower() in FLAG_TRUE:
        private = os.urandom(32)
        active_kid = parse_kid(kid) if (kid or "").strip() else DEV_KID
        verify = parse_pubkeys(pubkeys) if (pubkeys or "").strip() else {}
        if active_kid in verify:
            raise CapabilityKeyRefused(
                f"{PUBKEYS_ENV} already names kid {active_kid}, the dev ephemeral key's")
        verify[active_kid] = public_of(private)
    else:
        raise CapabilityKeyRefused(
            f"{KEY_PATH_ENV} is not set: it names the FILE that holds the root private key "
            f"(make one with: python -m apps.memoryskills.capability_keys create PATH)")
    return RootKeyStore(private, active_kid, verify)


_STORE: RootKeyStore | None = None


def install(store: RootKeyStore | None) -> RootKeyStore | None:
    """Make `store` the process's store; returns the one it replaced. ready() calls it once;
    tests call it to put a store in place and to restore the previous one."""
    global _STORE
    previous, _STORE = _STORE, store
    return previous


def current() -> RootKeyStore | None:
    """The process's store, or None: tokens are off, or no store was loaded."""
    return _STORE


def load_at_startup(settings, env=None) -> RootKeyStore | None:
    """apps/memoryskills/apps.py ready(): build the store from the settings and install it."""
    store = load(enabled=settings.MESHY_CAPABILITIES,
                 key_path=settings.MESHY_CAPABILITY_ROOT_KEY_PATH,
                 kid=settings.MESHY_CAPABILITY_ROOT_KID,
                 pubkeys=settings.MESHY_CAPABILITY_ROOT_PUBKEYS,
                 debug=bool(settings.DEBUG), env=os.environ if env is None else env)
    install(store)
    return store


# --------------------------------------------------------------------------- the key file

#: Windows: a DACL that is PROTECTED (nothing inherited from the folder) and holds one entry,
#: full control for the SID filled in. SDDL, so the rule reads as one line.
_OWNER_ONLY_SDDL = "D:P(A;;FA;;;{sid})"


def _windows_open_new_owner_only(path: Path) -> int:
    """CreateFileW(CREATE_NEW) with _OWNER_ONLY_SDDL for the creating user's SID: the file
    exists owner-only from the moment it exists, and an existing path is FileExistsError
    (ERROR_FILE_EXISTS). Returns a C runtime descriptor for writing."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                    ("bInheritHandle", wintypes.BOOL)]

    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     ctypes.POINTER(SecurityAttributes), wintypes.DWORD,
                                     wintypes.DWORD, wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.HANDLE)]
    advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                             wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p,
                                                ctypes.POINTER(ctypes.c_void_p)]
    convert_sddl = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert_sddl.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
                             ctypes.c_void_p]

    def failed():
        return ctypes.WinError(ctypes.get_last_error())

    token_query, token_user, sddl_revision_1 = 0x0008, 1, 1
    generic_write, create_new, attribute_normal = 0x40000000, 1, 0x80

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), token_query,
                                     ctypes.byref(token)):
        raise failed()
    try:
        size = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, token_user, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, token_user, buffer, size,
                                            ctypes.byref(size)):
            raise failed()
    finally:
        kernel32.CloseHandle(token)
    sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]  # TOKEN_USER.User.Sid
    sid_text = ctypes.c_void_p()
    if not advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid_text)):
        raise failed()
    try:
        sid = ctypes.wstring_at(sid_text.value)
    finally:
        kernel32.LocalFree(sid_text)

    descriptor = ctypes.c_void_p()
    if not convert_sddl(_OWNER_ONLY_SDDL.format(sid=sid), sddl_revision_1,
                        ctypes.byref(descriptor), None):
        raise failed()
    try:
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor.value,
                                        False)
        handle = kernel32.CreateFileW(str(path), generic_write, 0, ctypes.byref(attributes),
                                      create_new, attribute_normal, None)
        if handle is None or handle == ctypes.c_void_p(-1).value:
            raise failed()
    finally:
        kernel32.LocalFree(descriptor)
    try:
        return msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
    except BaseException:
        kernel32.CloseHandle(handle)
        raise


def _open_new_owner_only(path: Path) -> int:
    """A write descriptor on a NEW file, owner-only from creation; FileExistsError if `path`
    exists. POSIX: one os.open, O_EXCL and mode 0o600 (23ca808). Windows: that mode sets no
    ACL and the file would take its folder's inherited entries (review F4), so the file is
    created with an explicit protected ACL instead."""
    if os.name == "nt":
        return _windows_open_new_owner_only(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    return os.open(str(path), flags, 0o600)


#: The checkout this module runs from (apps/memoryskills/ is two levels below it).
CHECKOUT = Path(__file__).resolve().parents[2]


def _inside_checkout(path: Path) -> bool:
    """`path`, symlinks resolved and case folded where the platform folds it, is the
    checkout or below it."""
    target = os.path.normcase(os.path.realpath(path))
    root = os.path.normcase(os.path.realpath(CHECKOUT))
    return target == root or target.startswith(root.rstrip(os.sep) + os.sep)


def create_key_file(path) -> str:
    """Write a NEW private key to `path` and return its public half in hex.

    Never inside the checkout (Q10, review F5): the command runs from it, so a relative PATH
    would put the key in the working tree, one `git add -A` from a commit, and an ignored
    directory there is still copied with the tree. Refused before anything is created.
    Creation is the test (O_EXCL, or CREATE_NEW on Windows), and the file is owner-only from
    the first byte: mode 0o600 on POSIX (23ca808), a protected ACL naming only the creating
    user on Windows (review F4). A path that exists is refused and left alone. A write that
    fails removes the file it created: nothing else held that key yet."""
    path = Path(path)
    if _inside_checkout(path):
        raise CapabilityKeyRefused(
            f"{path} is inside the checkout ({CHECKOUT}); the root key never lives in the "
            "repository's working tree (Q10). Name a path outside it")
    path.parent.mkdir(parents=True, exist_ok=True)
    private = os.urandom(32)
    try:
        fd = _open_new_owner_only(path)
    except FileExistsError:
        raise CapabilityKeyRefused(f"{path} exists; refusing to overwrite a key") from None
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(private.hex().encode("ascii") + b"\n")
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return public_of(private).hex()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m apps.memoryskills.capability_keys",
        description="Create the capability root key file (divergence 040).")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="write a new private key to PATH, exclusively")
    create.add_argument("path")
    create.add_argument("--kid", required=True, help="the key's integer id")
    args = parser.parse_args(argv)
    try:
        kid = parse_kid(args.kid)
        public_hex = create_key_file(args.path)
    except CapabilityKeyRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(f"wrote a new root private key to {args.path} (kid {kid}); its public half:")
    print(f"{PUBKEYS_ENV}={kid}={public_hex}")
    print(f"then set {KEY_PATH_ENV}={args.path} and {KID_ENV}={kid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
