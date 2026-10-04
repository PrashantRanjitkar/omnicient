"""When an account last did something in public.

An account nobody has touched in years is worth knowing about: as context
("this profile is a fossil, its bio may be years out of date") and
defensively, because a dormant account under someone's name is an easy one to
take over or imitate. It says nothing about who owns the account, so it never
enters scoring.

Three states, and the third matters most. Most sources publish a creation
date at best, so for most accounts the honest answer is "unknown" - and an
account that publishes nothing is never reported as dormant for it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any

#: No public activity for this long reads as dormant.
DORMANT_AFTER = timedelta(days=730)


class ActivityStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DORMANT = "DORMANT"
    UNKNOWN = "UNKNOWN"


def activity_stamp(value: Any) -> str | None:
    """Normalise a platform's notion of "last active" to an ISO-8601 UTC string.

    Platforms disagree on the format: Mastodon sends a date, Chess.com Unix
    seconds, Lichess Unix milliseconds, GitHub an ISO timestamp. Anything
    unreadable is dropped rather than guessed at.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, int | float):
            seconds = value / 1000 if value > 10**11 else value
            moment = datetime.fromtimestamp(seconds, UTC)
        else:
            text = str(value).strip()
            if not text:
                return None
            if len(text) == 10:
                moment = datetime.combine(date.fromisoformat(text), datetime.min.time(), UTC)
            else:
                moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=UTC)
    except (ValueError, OverflowError, OSError):
        return None
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def last_active(metadata: dict[str, Any] | None) -> datetime | None:
    """The recorded last-activity moment, if a source published one."""
    stamp = activity_stamp((metadata or {}).get("last_active"))
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")) if stamp else None


def activity_status(
    metadata: dict[str, Any] | None, now: datetime | None = None
) -> ActivityStatus:
    moment = last_active(metadata)
    if moment is None:
        return ActivityStatus.UNKNOWN
    now = now or datetime.now(UTC)
    return ActivityStatus.DORMANT if now - moment > DORMANT_AFTER else ActivityStatus.ACTIVE
