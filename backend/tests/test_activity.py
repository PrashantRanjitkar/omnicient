"""Whether an account has done anything in public lately.

Dormancy is context, never evidence: it says nothing about who owns an
account, so it is kept out of scoring entirely. And because most sources
publish no activity date at all, "unknown" is a first-class answer - an
account that says nothing is never reported as dormant for it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.sources.gaming import ChessComAdapter, LichessAdapter
from app.sources.mastodon import MastodonAdapter
from app.utils.activity import ActivityStatus, activity_stamp, activity_status

NOW = datetime(2026, 10, 1, tzinfo=UTC)


def test_every_platform_format_reads_as_one_utc_timestamp() -> None:
    assert activity_stamp("2026-09-14T13:16:48Z") == "2026-09-14T13:16:48Z"
    assert activity_stamp("2026-09-14") == "2026-09-14T00:00:00Z"  # Mastodon
    assert activity_stamp(1789387200) == "2026-09-14T12:00:00Z"  # Chess.com, seconds
    assert activity_stamp(1789387200000) == "2026-09-14T12:00:00Z"  # Lichess, ms
    assert activity_stamp("2026-09-14T15:16:48+02:00") == "2026-09-14T13:16:48Z"


def test_unreadable_values_are_dropped_not_guessed() -> None:
    for value in (None, "", "yesterday", True, "2026-13-45"):
        assert activity_stamp(value) is None


def test_recent_activity_is_active() -> None:
    assert activity_status({"last_active": "2026-03-01"}, NOW) is ActivityStatus.ACTIVE


def test_two_years_of_silence_is_dormant() -> None:
    assert activity_status({"last_active": "2024-09-01"}, NOW) is ActivityStatus.DORMANT


def test_no_published_date_is_unknown_never_dormant() -> None:
    assert activity_status({}, NOW) is ActivityStatus.UNKNOWN
    assert activity_status(None, NOW) is ActivityStatus.UNKNOWN
    # A creation date is not activity: an account made in 2010 may post daily.
    assert activity_status({"created_at": "2010-01-01"}, NOW) is ActivityStatus.UNKNOWN


def test_mastodon_reports_its_latest_post() -> None:
    profile = MastodonAdapter().parse_json(
        "alice@mastodon.social",
        {"username": "alice", "url": "https://mastodon.social/@alice",
         "last_status_at": "2023-02-01"},
        "https://mastodon.social/@alice",
    )
    assert profile is not None
    assert profile.metadata["last_active"] == "2023-02-01T00:00:00Z"
    assert profile.metadata["last_active_basis"] == "date of the latest post"


def test_chess_com_reports_when_the_player_was_last_online() -> None:
    profile = ChessComAdapter().parse_json(
        "alice", {"username": "alice", "last_online": 1789387200}, "https://x"
    )
    assert profile is not None
    assert profile.metadata["last_active"] == "2026-09-14T12:00:00Z"


def test_lichess_reports_when_the_player_was_last_seen() -> None:
    profile = LichessAdapter().parse_json(
        "alice", {"username": "alice", "seenAt": 1789387200000}, "https://x"
    )
    assert profile is not None
    assert profile.metadata["last_active"] == "2026-09-14T12:00:00Z"


def test_a_source_without_a_date_adds_nothing() -> None:
    profile = LichessAdapter().parse_json("alice", {"username": "alice"}, "https://x")
    assert profile is not None
    assert "last_active" not in profile.metadata
