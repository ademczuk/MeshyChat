"""Publish this token's advisory turn and optional scalar coverage observation."""
from __future__ import annotations

from . import node

#: The usage snapshot's fields (apps/peers/turn.validate_usage); anything else is refused here before it is sent,
#: at every level, so nothing off these lists (a transcript path inside a limit, say) ever leaves the seat.
USAGE_KEYS = frozenset({"as_of", "model", "context_used_pct", "limits"})
USAGE_LIMIT_KEYS = frozenset({"window", "used_pct", "resets_at", "source"})
USAGE_MAX_LIMITS = 4


def _number_or_none(value) -> bool:
    return value is None or (isinstance(value, (int, float)) and not isinstance(value, bool))


def _usage_refusal(usage) -> str | None:
    """Why this usage snapshot may not be sent, or None. Shapes and scalar types only; the server checks the values."""
    if not isinstance(usage, dict) or not set(usage) <= USAGE_KEYS or node._instant(usage.get("as_of")) is None:
        return "usage needs as_of with a timezone and only model, context_used_pct and limits"
    if not (usage.get("model") is None or isinstance(usage["model"], str)) or not _number_or_none(usage.get("context_used_pct")):
        return "usage model is text and context_used_pct a number"
    limits = usage.get("limits")
    if limits is None:
        return None
    if not isinstance(limits, list) or len(limits) > USAGE_MAX_LIMITS:
        return "usage limits is a list of at most %d windows" % USAGE_MAX_LIMITS
    for lim in limits:
        if (not isinstance(lim, dict) or not set(lim) <= USAGE_LIMIT_KEYS or not isinstance(lim.get("window"), str)
                or not isinstance(lim.get("source"), str) or not _number_or_none(lim.get("used_pct"))
                or not (lim.get("resets_at") is None or isinstance(lim["resets_at"], str))):
            return "each usage limit is window and source (text), optionally used_pct (a number) and resets_at (text)"
    return None


def publish(path, state, *, state_value, item_at, observation=None, usage=None):
    if state_value not in ("open", "closed") or node._instant(item_at) is None:
        raise node.Refused("turn needs --state open|closed and --item-at with a timezone")
    body = {"state": state_value, "item_at": item_at}
    if usage is not None:
        # as_of is when the seat OBSERVED these numbers, never the time of this report.
        why = _usage_refusal(usage)
        if why:
            raise node.Refused(why)
        body["usage"] = usage
    if observation is not None:
        if (not isinstance(observation, dict) or set(observation) != {"unsurfaced", "unsurfaced_oldest_at", "unsurfaced_since"}
                or observation["unsurfaced"] is not None and
                (type(observation["unsurfaced"]) is not int or not -1 <= observation["unsurfaced"] <= 2048)):
            raise node.Refused("unsurfaced needs a count (-1=incomplete) and optional oldest timestamp")
        count, oldest = observation["unsurfaced"], observation["unsurfaced_oldest_at"]
        if oldest is not None and (not isinstance(oldest, str) or len(oldest) > 64 or node._instant(oldest) is None):
            raise node.Refused("unsurfaced oldest value must be a timestamp, never key/text/path")
        if (count in (None, 0) and oldest is not None or
                count is not None and count > 0 and oldest is None):
            raise node.Refused("a pending count needs its oldest timestamp")
        since = observation["unsurfaced_since"]
        if ((count is None and since is not None) or
                (count is not None and (not isinstance(since, str) or len(since) > 64 or node._instant(since) is None))):
            raise node.Refused("unsurfaced since value must be a timestamp, never key/text/path")
        body.update(observation)
    node.require_peer(path, state)
    jwt = node.fresh_jwt(path, state)
    route = "/v1/peers/me/turn"
    status, answer = node.http_json(
        "POST", state["base_url"], route, body=body,
        headers=node._proved_headers(state, jwt, "POST", route))
    if status != 200 or not isinstance(answer, dict):
        raise node.ServerSaid(status, answer)
    return answer
