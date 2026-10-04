"""What an account's public commits reveal that its profile does not.

Every public commit carries an author address and the UTC offset of the
machine that made it. GitHub's profile page shows neither, so most people do
not know they have published them. The adapter samples a handful of the
account's own commits and reports both - addresses as masked entities,
offsets as a distribution, never as a verdict about where someone lives.
"""

from __future__ import annotations

import httpx

from app.config import Settings
from app.models.enums import EntityType, EvidenceType
from app.services.crawler import Crawler
from app.services.discovery import COMMIT_METADATA, candidates_from_profile
from app.sources import SourceRegistry
from app.sources.base import ObservedProfile, SafeFetcher
from app.sources.github import GitHubAdapter, patch_offset
from app.utils.normalization import mask_email

USER = {
    "login": "alice",
    "type": "User",
    "html_url": "https://github.com/alice",
    "email": "alice@public.dev",
}


def commit(sha: str, login: str | None, email: str) -> dict:
    return {
        "sha": sha,
        "author": {"login": login} if login else None,
        "commit": {"author": {"email": email, "date": "2026-09-14T13:16:48Z"}},
    }


def github(
    *,
    repos: list[dict] | None = None,
    commits: dict[str, list[dict]] | None = None,
    offsets: dict[str, str] | None = None,
    commit_status: int = 200,
):
    """A fake GitHub; returns the handler and the list of URLs it served."""
    served: list[str] = []
    repos = repos if repos is not None else [
        {"name": "site", "fork": False, "size": 10, "pushed_at": "2026-09-14T13:16:48Z"},
        {"name": "theirs", "fork": True, "size": 99, "pushed_at": "2026-09-20T00:00:00Z"},
    ]
    commits = commits or {}
    offsets = offsets or {}

    def handler(request: httpx.Request) -> httpx.Response:
        served.append(str(request.url))
        path = request.url.path
        if path.endswith(".patch"):
            sha = path.rsplit("/", 1)[-1].removesuffix(".patch")
            return httpx.Response(
                200,
                text=f"From {sha} Mon Sep 17 00:00:00 2001\n"
                f"Date: Mon, 14 Sep 2026 15:16:48 {offsets.get(sha, '+0000')}\n",
            )
        if path.endswith("/repos"):
            body: object = repos
        elif path.endswith("/commits"):
            if commit_status != 200:
                return httpx.Response(commit_status)
            body = commits.get(path.split("/")[3], [])
        else:
            body = USER
        return httpx.Response(200, json=body, headers={"content-type": "application/json"})

    return handler, served


def fetcher(handler) -> SafeFetcher:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return SafeFetcher(Settings(respect_robots=False, request_delay=0), client=client)


async def look_up(handler) -> ObservedProfile:
    f = fetcher(handler)
    result = await GitHubAdapter(f).lookup("alice")
    await f.aclose()
    assert result.primary is not None
    return result.primary


# ---------------------------------------------------------------------------
# Time zones
# ---------------------------------------------------------------------------


def test_the_offset_is_read_from_the_patch_header() -> None:
    assert patch_offset("Date: Mon, 14 Sep 2026 15:16:48 +0545\n") == "+05:45"
    assert patch_offset("Date: Mon, 14 Sep 2026 15:16:48 -0700\n") == "-07:00"
    assert patch_offset("Date: Mon, 14 Sep 2026 15:16:48 +0000\n") == "+00:00"


def test_a_page_without_a_date_header_gives_no_offset() -> None:
    assert patch_offset("<html>Not Found</html>") is None
    assert patch_offset("") is None


async def test_offsets_are_reported_as_a_distribution_not_a_verdict() -> None:
    handler, _ = github(
        commits={"site": [commit(f"s{i}", "alice", "a@x.dev") for i in range(4)]},
        offsets={"s0": "+0545", "s1": "+0545", "s2": "+0545", "s3": "+0000"},
    )
    profile = await look_up(handler)

    assert profile.metadata["commit_time_zones"] == [
        "UTC+05:45 on 3 of 4 commits read",
        "UTC+00:00 on 1 of 4 commits read",
    ]
    assert profile.metadata["commit_sample"] == "4 commits across 1 repository"


# ---------------------------------------------------------------------------
# Whose commits count
# ---------------------------------------------------------------------------


async def test_only_commits_github_attributes_to_the_account_are_read() -> None:
    """A repository holds other people's commits; their addresses are theirs."""
    handler, served = github(
        commits={
            "site": [
                commit("mine", "alice", "alice@work.example.net"),
                commit("bob", "bob", "bob@elsewhere.net"),
                commit("anon", None, "stranger@nowhere.net"),
            ]
        }
    )
    profile = await look_up(handler)

    assert profile.commit_emails == ["alice@work.example.net"]
    patches = [url for url in served if url.endswith(".patch")]
    assert patches == ["https://github.com/alice/site/commit/mine.patch"]


async def test_forks_are_never_sampled() -> None:
    handler, served = github(commits={"theirs": [commit("t", "alice", "a@b.dev")]})
    await look_up(handler)

    assert not any("/repos/alice/theirs/" in url for url in served)


async def test_placeholder_and_robot_addresses_are_ignored() -> None:
    handler, _ = github(
        commits={
            "site": [
                commit("a", "alice", "123+alice@users.noreply.github.com"),
                commit("b", "alice", "alice@laptop.local"),
                commit("c", "alice", "root@localhost"),
                commit("d", "alice", "not-an-address"),
            ]
        }
    )
    profile = await look_up(handler)

    assert profile.commit_emails == []
    assert "commit_emails_found" not in profile.metadata


async def test_an_address_the_profile_publishes_is_not_an_exposure() -> None:
    handler, _ = github(
        commits={
            "site": [
                commit("a", "alice", "Alice@Public.dev"),
                commit("b", "alice", "alice@hidden.dev"),
            ]
        }
    )
    profile = await look_up(handler)

    assert profile.commit_emails == ["alice@hidden.dev"]


# ---------------------------------------------------------------------------
# Failing politely
# ---------------------------------------------------------------------------


async def test_a_rate_limit_costs_the_detail_not_the_profile() -> None:
    handler, _ = github(commit_status=429)
    profile = await look_up(handler)

    assert profile.identifier == "alice"
    assert profile.commit_emails == []
    assert profile.metadata["commit_sample"] == "Not sampled: GitHub answered RATE_LIMITED"


async def test_the_latest_push_to_an_own_repository_is_recorded_as_activity() -> None:
    handler, _ = github()
    profile = await look_up(handler)

    # The fork was pushed later, but pushing to a fork is not this account's
    # own work being updated - and it is excluded like everything else forked.
    assert profile.metadata["last_active"] == "2026-09-14T13:16:48Z"


# ---------------------------------------------------------------------------
# Masked from then on
# ---------------------------------------------------------------------------


def test_the_mask_keeps_the_first_letter_and_the_domain() -> None:
    assert mask_email("alice@example.com") == "a***@example.com"
    assert mask_email("nonsense") == "***"


def test_a_commit_address_becomes_a_masked_email_candidate() -> None:
    profile = ObservedProfile(
        platform="github", identifier="alice", commit_emails=["alice@hidden.dev"]
    )
    candidates = candidates_from_profile(
        profile, parent_key=("ACCOUNT", "github", "alice"), depth=1, from_seed=True
    )
    [email] = [c for c in candidates if c.entity_type is EntityType.EMAIL]

    assert email.identifier == "alice@hidden.dev", "kept in full for pivoting"
    assert email.display_name == "a***@hidden.dev"
    assert email.exposure == COMMIT_METADATA


async def test_the_address_never_appears_in_evidence_text() -> None:
    handler, _ = github(commits={"site": [commit("a", "alice", "alice@hidden.dev")]})
    f = fetcher(handler)
    registry = SourceRegistry()
    registry.register(GitHubAdapter(f))
    crawler = Crawler(registry, Settings(respect_robots=False, request_delay=0))

    outcome = await crawler.crawl("github", "alice", include_similarity=False)
    await f.aclose()

    email = outcome.entities[("EMAIL", "email", "alice@hidden.dev")]
    assert email.name == "a***@hidden.dev"
    assert email.exposure == COMMIT_METADATA

    written = [link.description for link in outcome.links] + [
        str(link.extracted_value) for link in outcome.links
    ] + [entity.discovered_via or "" for entity in outcome.entities.values()]
    assert not any("alice@hidden.dev" in text for text in written)

    [edge] = [link for link in outcome.links if link.target_key == email.key]
    assert edge.evidence_type is EvidenceType.SHARED_EMAIL
    assert "a***@hidden.dev" in edge.description


async def test_an_address_another_profile_publishes_is_not_masked() -> None:
    """Found in commits first, then listed on a public page: it is public."""
    from app.models.enums import DiscoveryMethod
    from app.services.crawler import CrawlOutcome, ObservedEntity

    outcome = CrawlOutcome()
    key = ("EMAIL", "email", "alice@hidden.dev")
    outcome.entities[key] = ObservedEntity(
        key=key, entity_type=EntityType.EMAIL, platform="email",
        identifier="alice@hidden.dev", name="a***@hidden.dev",
        method=DiscoveryMethod.DIRECT, exposure=COMMIT_METADATA,
    )
    gravatar = ("ACCOUNT", "gravatar", "alice")
    outcome.entities[gravatar] = ObservedEntity(
        key=gravatar, entity_type=EntityType.ACCOUNT, platform="gravatar",
        identifier="alice", name="@alice", resolved=True,
        profile=ObservedProfile(
            platform="gravatar", identifier="alice", emails=["Alice@Hidden.dev"]
        ),
    )

    Crawler._unmask_published(outcome)

    assert outcome.entities[key].exposure is None
    assert outcome.entities[key].name == "alice@hidden.dev"


async def test_an_empty_sample_says_why() -> None:
    """Commits whose address is not on the account are skipped - and said so."""
    handler, _ = github(commits={"site": [commit("a", None, "me@laptop.dev")]})
    profile = await look_up(handler)

    assert profile.commit_emails == []
    assert profile.metadata["commit_sample"].startswith(
        "None read: 1 recent commit in its own repositories, none linked"
    )
