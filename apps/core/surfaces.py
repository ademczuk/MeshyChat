"""`MESHY_SURFACES`: which OPTIONAL route surfaces this server mounts (divergence 054).

WHY. Production's access log from the cutover (2026-09-27 11:43Z) to 2026-09-29 09:48Z holds
141,505 requests. Mesh mail, peers and agent cards, calls, sign-in and the WebSocket carry all
of them but a handful of probes. Seven mounted surfaces carried none, or two probes: the
dashboard, the HTTP grants routes, the skills registry, files, the guard's scans, the board and
the summaries. They were reachable by every host that can reach the server, and 60 of the 82
unticked findings of docs/REVIEW-FIX-QUEUE.md are in five of them. A route nobody calls is only
a way in. Operator ruling 2026-09-29 (ASDLC D-2026-09-29-U21): each goes behind a setting that
is OFF unless the deployment names it. The code and its tests stay.

WHAT IS NOT OPTIONAL, and is always mounted: health, livez and metrics; authapi (sign-in,
onboarding), modelhost, peers, calls, a2a (agent cards), mesh, meshsem, memoryskills, and the
guard's 410 for its old prefix. Turning a surface off unmounts its ROUTES only: the package
stays importable, and what the core calls in-process (grants through the call pipeline, the
guard through a write) is unchanged.

THE VALUE. Unset or blank: none. `all`: every one. Otherwise a comma-separated list of the
names below. An unknown name refuses to load, as MESHY_CAPABILITIES does on a value it cannot
read: a typo must not read as "off".

This module imports nothing of the project (apps/core/capability_flag.py says why).
"""
from __future__ import annotations

ENV = "MESHY_SURFACES"

#: name -> the urls modules it mounts, in the order meshychat/urls.py always mounted them.
OPTIONAL: dict[str, tuple[str, ...]] = {
    "grants": ("apps.grants.urls",),
    "skillsreg": ("apps.skillsreg.urls",),
    "files": ("apps.files.urls",),
    "dashboard": ("apps.dashboard.urls",),
    "meshguard": ("apps.meshguard.urls",),
    "board": ("apps.board.urls",),
    "meshsum": ("apps.meshsum.urls",),
}

ALL = "all"


class SurfacesConfigError(ValueError):
    """MESHY_SURFACES names a surface that does not exist. Raised while settings.py loads."""


def from_env(env) -> frozenset[str]:
    """The optional surfaces the deployment turned on. meshychat/settings.py calls this once."""
    raw = env.get(ENV)
    if raw is None or not raw.strip():
        return frozenset()
    names = [n.strip().lower() for n in raw.split(",") if n.strip()]
    if names == [ALL]:
        return frozenset(OPTIONAL)
    unknown = sorted(n for n in names if n not in OPTIONAL)
    if unknown:
        raise SurfacesConfigError(
            f"{ENV}={raw!r} names {', '.join(unknown)}, which is not a surface. Known: "
            f"{', '.join(OPTIONAL)}; or '{ALL}' alone; unset mounts none of them.")
    return frozenset(names)


def modules(enabled) -> list[str]:
    """The urls modules of the enabled surfaces, in OPTIONAL's order."""
    return [m for name, mods in OPTIONAL.items() if name in enabled for m in mods]


__all__ = ["ALL", "ENV", "OPTIONAL", "SurfacesConfigError", "from_env", "modules"]
