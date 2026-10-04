"""GitHub source adapter.

Reads the public user document GitHub publishes at ``api.github.com/users/{u}``.
That endpoint is the documented, anonymous interface for exactly this data, so
Omnicient uses it rather than scraping the profile page: it returns strictly
less information, it is stable, and it keeps a single request per lookup.

No token is sent.  Anonymous requests are rate limited by GitHub, and a rate
limit is reported as ``RATE_LIMITED`` and the investigation continues - it is
never evaded.

GitHub matters disproportionately in an identity investigation because
developers publish a personal site (``blog``) and often a company on the same
profile, which is exactly the kind of shared external attribute the
correlation engine can pivot on.

It also publishes something most people never think of as published: every
public commit carries the author's email address and the time zone their
machine was set to. :func:`GitHubAdapter.commit_activity` samples a few of the
account's own recent commits and reports both. Only commits GitHub itself
attributes to the account are read - GitHub does that only for an address on
the account - so a fork full of other people's work contributes nothing.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from email.utils import parsedate_tz
from typing import Any

from .base import (
    JsonProfileAdapter,
    LookupResult,
    ObservedProfile,
    SourceCategory,
    SourceError,
    activity_fields,
    enrich_profile,
)

#: How much of an account's history is sampled. Small on purpose: anonymous
#: callers get 60 API requests an hour, and a time zone that holds across ten
#: commits says what a thousand would.
REPOS_SAMPLED = 3
COMMITS_PER_REPO = 10
PATCHES_SAMPLED = 10
MAX_COMMIT_EMAILS = 3
#: The patch header holds the date; the diff after it is never needed.
PATCH_HEAD_BYTES = 2048

PATCH_DATE_RE = re.compile(r"^Date: (.+)$", re.MULTILINE)

#: Addresses that identify GitHub, a bot or a misconfigured machine rather
#: than a person.
NOT_PERSONAL_EMAIL_RE = re.compile(
    r"(noreply|no-reply|@localhost|\.local$|\.localdomain$|@example\.(com|org)$"
    r"|\[bot\]|^root@|\(none\))",
    re.IGNORECASE,
)
EMAIL_SHAPE_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.IGNORECASE)


@dataclass
class CommitActivity:
    """What a sample of an account's public commits revealed."""

    commits: int = 0
    #: Own repositories whose commits were asked for.
    repos_checked: int = 0
    #: Commits seen there that GitHub does not credit to the account.
    unattributed: int = 0
    repositories: list[str] = field(default_factory=list)
    emails: Counter[str] = field(default_factory=Counter)
    offsets: Counter[str] = field(default_factory=Counter)
    patches_read: int = 0
    last_pushed: str | None = None
    #: Why sampling stopped early, when it did.
    stopped: str | None = None
    requests: int = 0


class GitHubAdapter(JsonProfileAdapter):
    """Looks up publicly available GitHub profile information."""

    platform = "github"
    name = "GitHub"
    category = SourceCategory.DEV
    api_template = "https://api.github.com/users/{identifier}"
    url_template = "https://github.com/{identifier}"
    probe_present = "torvalds"
    # GitHub's documented versioned media type; a plain HTML Accept is refused
    # with 415.
    accept = "application/vnd.github+json"

    async def lookup(self, identifier: str) -> LookupResult:
        result = await super().lookup(identifier)
        profile = result.primary
        if profile is None:
            return result
        activity = await self.commit_activity(profile.identifier)
        result.pages_fetched += activity.requests
        apply_commit_activity(profile, activity)
        return result

    async def commit_activity(self, login: str) -> CommitActivity:
        """Sample the account's own recent commits.

        Three requests to the API (the repository list, then commits on the
        most recently pushed repositories), and up to ten ``.patch`` views on
        github.com: the API hands commit times back in UTC, and only the
        patch header keeps the offset the author's machine recorded.

        Every failure ends the sampling and is reported, never retried - a
        rate limit here costs the investigation some detail, not the profile.
        """
        activity = CommitActivity()
        headers = {"Accept": self.accept, **self.credentials()}
        try:
            listing = await self._json(
                f"https://api.github.com/users/{login}/repos"
                "?type=owner&sort=pushed&per_page=20",
                headers,
                activity,
            )
            repos = [
                repo
                for repo in (listing if isinstance(listing, list) else [])
                if isinstance(repo, dict)
                and not repo.get("fork")
                and repo.get("size")
                and repo.get("name")
            ]
            pushed = [repo["pushed_at"] for repo in repos if repo.get("pushed_at")]
            activity.last_pushed = max(pushed) if pushed else None

            shas: list[tuple[str, str]] = []
            for repo in repos[:REPOS_SAMPLED]:
                name = str(repo["name"])
                activity.repos_checked += 1
                commits = await self._json(
                    # Unfiltered on purpose: ``?author=`` hides commits GitHub
                    # cannot link to the account, and then an empty sample
                    # cannot say why it is empty. _authored_by does the
                    # filtering, and counts what it drops.
                    f"https://api.github.com/repos/{login}/{name}/commits"
                    f"?per_page={COMMITS_PER_REPO}",
                    headers,
                    activity,
                )
                counted = False
                for commit in commits if isinstance(commits, list) else []:
                    if not _authored_by(commit, login):
                        activity.unattributed += 1
                        continue
                    counted = True
                    activity.commits += 1
                    address = _personal_email(commit)
                    if address:
                        activity.emails[address] += 1
                    shas.append((name, str(commit.get("sha", ""))))
                if counted:
                    activity.repositories.append(name)

            for name, sha in shas[:PATCHES_SAMPLED]:
                if not sha:
                    continue
                fetched = await self.fetcher.get(
                    f"https://github.com/{login}/{name}/commit/{sha}.patch",
                    head_bytes=PATCH_HEAD_BYTES,
                )
                activity.requests += 0 if fetched.from_cache else 1
                offset = patch_offset(fetched.text)
                if offset:
                    activity.offsets[offset] += 1
                    activity.patches_read += 1
        except SourceError as exc:
            activity.stopped = str(exc.reason)
        return activity

    async def _json(
        self, url: str, headers: dict[str, str], activity: CommitActivity
    ) -> Any:
        import json

        fetched = await self.fetcher.get(url, headers=headers)
        activity.requests += 0 if fetched.from_cache else 1
        try:
            return json.loads(fetched.text)
        except ValueError:
            return None

    def parse_json(
        self, identifier: str, payload: Any, url: str
    ) -> ObservedProfile | None:
        if not isinstance(payload, dict) or payload.get("type") == "Organization":
            # Organizations are real, but they are not accounts an individual
            # identity can be correlated through.
            return None
        login = payload.get("login")
        if not login:
            return None

        links = [link for link in [_clean_url(payload.get("blog"))] if link]

        return enrich_profile(
            ObservedProfile(
                platform=self.platform,
                identifier=str(login),
                name=f"@{login}",
                url=payload.get("html_url") or url,
                display_name=payload.get("name") or None,
                bio=payload.get("bio") or None,
                avatar_url=payload.get("avatar_url") or None,
                location=payload.get("location") or None,
                # Only ever the address the user chose to publish.
                email=payload.get("email") or None,
                organization=_clean_company(payload.get("company")),
                external_links=links,
                source=self.platform,
                metadata={
                    key: payload[key]
                    for key in ("public_repos", "followers", "created_at", "twitter_username")
                    if payload.get(key) is not None
                },
            )
        )


def _clean_url(value: Any) -> str | None:
    """GitHub's ``blog`` field is free text: it may omit the scheme entirely."""
    from ..utils.normalization import normalize_url

    if not value or not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    return normalize_url(candidate)


def _clean_company(value: Any) -> str | None:
    """``company`` is free text and often an ``@org`` handle."""
    if not value or not isinstance(value, str):
        return None
    return value.strip().lstrip("@") or None


def _authored_by(commit: Any, login: str) -> bool:
    """True when GitHub attributes the commit to this account.

    The ``author`` object is GitHub's own link from the commit's address to an
    account, and it is only made for an address registered on that account.
    A commit on the account's own repository can still be somebody else's,
    so repository ownership alone is not enough.
    """
    if not isinstance(commit, dict):
        return False
    author = commit.get("author")
    return isinstance(author, dict) and str(author.get("login", "")).lower() == login.lower()


def _personal_email(commit: dict[str, Any]) -> str | None:
    """The commit's author address, unless it is a placeholder or a robot's."""
    details = (commit.get("commit") or {}).get("author") or {}
    address = str(details.get("email") or "").strip().lower()
    if not address or not EMAIL_SHAPE_RE.match(address):
        return None
    if NOT_PERSONAL_EMAIL_RE.search(address):
        return None
    return address


def patch_offset(text: str) -> str | None:
    """The UTC offset in a ``.patch`` header, as ``+05:45``."""
    match = PATCH_DATE_RE.search(text or "")
    if not match:
        return None
    parsed = parsedate_tz(match.group(1).strip())
    if parsed is None or parsed[9] is None:
        return None
    seconds = parsed[9]
    sign = "+" if seconds >= 0 else "-"
    hours, minutes = divmod(abs(seconds) // 60, 60)
    return f"{sign}{hours:02d}:{minutes:02d}"


def apply_commit_activity(profile: ObservedProfile, activity: CommitActivity) -> None:
    """Record what the commit sample showed on the profile it belongs to.

    Time zones go in as readable lines - they are an observation, shown as a
    distribution because people travel and CI machines run on UTC. Addresses
    go in ``commit_emails`` and nowhere else: they become their own entities,
    masked on screen, rather than plain text in the metadata panel.
    """
    profile.metadata.update(
        activity_fields(activity.last_pushed, "latest push to an own repository")
    )
    if activity.commits == 0:
        # Say why, so an empty section reads as "looked, nothing to read"
        # rather than as a feature that never ran.
        if activity.stopped:
            profile.metadata["commit_sample"] = (
                f"Not sampled: GitHub answered {activity.stopped}"
            )
        elif activity.unattributed:
            profile.metadata["commit_sample"] = (
                f"None read: {activity.unattributed} recent "
                f"commit{'s' if activity.unattributed != 1 else ''} in its own "
                "repositories, none linked to this account by GitHub (the author "
                "address is not on the account), so they could be anyone's"
            )
        elif activity.repos_checked:
            profile.metadata["commit_sample"] = (
                "None read: no recent commits in its own repositories"
            )
        else:
            profile.metadata["commit_sample"] = "None read: no public repositories of its own"
        return

    sampled = (
        f"{activity.commits} commit{'s' if activity.commits != 1 else ''} across "
        f"{len(activity.repositories)} "
        f"repositor{'ies' if len(activity.repositories) != 1 else 'y'}"
    )
    if activity.stopped:
        sampled += f" (stopped early: {activity.stopped})"
    profile.metadata["commit_sample"] = sampled
    if activity.offsets:
        profile.metadata["commit_time_zones"] = [
            f"UTC{offset} on {count} of {activity.patches_read} commits read"
            for offset, count in activity.offsets.most_common()
        ]

    published = {address.lower() for address in profile.emails}
    if profile.email:
        published.add(profile.email.lower())
    found = [address for address, _ in activity.emails.most_common(MAX_COMMIT_EMAILS)]
    # An address the profile already publishes is not an exposure; it is
    # listed with the published ones and shown in full.
    profile.emails += [a for a in found if a in published and a not in profile.emails]
    profile.commit_emails = [a for a in found if a not in published]
    if found:
        profile.metadata["commit_emails_found"] = (
            f"{len(found)} author address{'es' if len(found) != 1 else ''} "
            f"({len(profile.commit_emails)} not published on the profile)"
        )
