"""Client-side filename checks shared by offers and attachments.

Callers check their own plain-name/wire constraints first. These additional
rules apply on every platform so a sender never publishes a name a Windows
recipient cannot save. They do not change the server's attachment schema.
"""

_WINDOWS_REFUSED_CHARS = ':<>"|?*'
_WINDOWS_DEVICES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"{port}{digit}" for port in ("COM", "LPT") for digit in "0123456789\u00b9\u00b2\u00b3"}
)

# One path component on NTFS and exFAT holds at most 255 UTF-16 code units. A character outside the
# Basic Multilingual Plane takes two, so the wire's 255-character limit is not enough on its own.
_WINDOWS_MAX_NAME_UNITS = 255


def utf8_name_problem(name: str) -> str | None:
    """Why an otherwise plain name cannot be one component of a POSIX file name, or None: encoded as the filesystem
    encoding writes it (UTF-8 with surrogateescape, so an undecodable byte U+DC80..U+DCFF is that one byte), it holds
    a lone surrogate no file name can hold, or it is longer than 255 bytes. Never raises."""
    try:
        size = len(name.encode("utf-8", "surrogateescape"))
    except UnicodeEncodeError:
        return "the name holds a lone surrogate, which no file name can hold"
    if size > 255:
        return "the name is longer than 255 UTF-8 bytes"
    return None


def surrogate_name_problem(name: str) -> str | None:
    """Why a name cannot go into a signed message, or None: a lone surrogate (an undecodable POSIX byte, or an
    unpaired UTF-16 unit in a Windows name) is not Unicode text, and the message's canonical JSON refuses it."""
    if any(0xD800 <= ord(ch) <= 0xDFFF for ch in name):
        return "the name holds a lone surrogate (an undecodable byte), which a signed message cannot carry"
    return None


def windows_name_problem(name: str) -> str | None:
    """Why an otherwise plain name cannot be saved as named on Windows, or None."""
    for ch in _WINDOWS_REFUSED_CHARS:
        if ch in name:
            if ch == ":":
                return f"the name {name!r} contains the alternate data stream separator on Windows ({ch!r})"
            return f"the name {name!r} contains {ch!r}, which Windows refuses in a file name"
    if name.split(".", 1)[0].rstrip(" ").upper() in _WINDOWS_DEVICES:
        return f"the name {name!r} is a Windows device name (with or without an extension it opens the device)"
    if name.endswith((".", " ")):
        return f"the name {name!r} ends in {name[-1]!r}, which Windows drops (it would save another name)"
    # Counted, not encoded: a lone surrogate (an undecodable POSIX byte) is one unit and must not raise.
    units = sum(2 if ord(ch) > 0xFFFF else 1 for ch in name)
    if units > _WINDOWS_MAX_NAME_UNITS:
        return (f"the name is {units} UTF-16 code units long; Windows allows {_WINDOWS_MAX_NAME_UNITS} "
                "in one file name")
    return None
