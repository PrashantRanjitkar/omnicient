"""The PDF case report.

Template-built, so what is worth testing is what it is allowed to say: the
caveat on every page, decisions shown with their provenance, ruled-out
entities moved aside rather than dropped, and unpublished addresses masked
unless the reader explicitly asks.
"""

from __future__ import annotations

import pytest
from reportlab.platypus import Paragraph, Table

from app.config import ScoringConfig
from app.models.enums import EntityVerdict
from app.schemas.investigation import InvestigationExport
from app.services.report import _Report, render_report

HIDDEN = "alice.private@hidden.dev"


@pytest.fixture
def investigation(client) -> dict:
    """The demo dataset's investigation, as the API tests use it."""
    created = client.post(
        "/api/investigations",
        json={"identifier": "@alice_98", "platform": "Instagram", "auto_crawl": False},
    ).json()
    client.post(f"/api/investigations/{created['id']}/crawl", json={})
    return created


@pytest.fixture
def export(client, investigation) -> InvestigationExport:
    """The demo export, plus one address exposed by commit metadata."""
    body = client.get(f"/api/investigations/{investigation['id']}/export").json()
    template = body["entities"][0]
    account = next(e for e in body["entities"] if e["type"] == "ACCOUNT")
    exposed = {
        **template,
        "id": "exposed-email",
        "type": "EMAIL",
        "platform": "email",
        "identifier": HIDDEN,
        "name": "a***@hidden.dev",
        "is_seed": False,
        "analyst_verdict": "UNREVIEWED",
        "metadata": {"exposed_by": "commit_metadata"},
    }
    account["metadata"] = {
        **account["metadata"],
        "commit_sample": "12 commits across 2 repositories",
        "commit_time_zones": ["UTC+05:45 on 10 of 10 commits read"],
    }
    relationship = {
        **body["relationships"][0],
        "id": "commit-edge",
        "source_entity_id": account["id"],
        "target_entity_id": "exposed-email",
        "evidence_ids": [],
        # Stored text that names the address in full, to prove the report
        # masks what it prints and not only what it builds itself.
        "summary": f"commit author {HIDDEN}",
        "analyst_note": f"checked {HIDDEN} by hand",
        "analyst_status": "CONFIRMED",
    }
    body["entities"].append(exposed)
    body["relationships"].append(relationship)
    return InvestigationExport.model_validate(body)


def story_text(report: _Report) -> str:
    """Every word the report would print, as markup."""
    parts: list[str] = []

    def walk(flowable) -> None:
        if isinstance(flowable, Paragraph):
            parts.append(flowable.text)
        elif isinstance(flowable, Table):
            for row in flowable._cellvalues:
                for cell in row:
                    walk(cell)
        elif isinstance(flowable, list | tuple):
            for item in flowable:
                walk(item)
        elif hasattr(flowable, "_content"):
            walk(flowable._content)

    walk(report.build())
    return "\n".join(parts)


def test_the_endpoint_returns_a_pdf(client, investigation) -> None:
    response = client.get(
        f"/api/investigations/{investigation['id']}/export", params={"format": "pdf"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].endswith('-report.pdf"')
    assert response.content.startswith(b"%PDF")


def test_commit_addresses_are_printed_in_full_by_default(export) -> None:
    text = story_text(_Report(export, None, ScoringConfig()))

    assert HIDDEN in text
    # Stored text written while the address was masked on screen is
    # printed with the address it stands for.
    assert "a***@hidden.dev" not in text


def test_mask_hides_them_when_asked(export) -> None:
    text = story_text(_Report(export, None, ScoringConfig(), mask=True))

    assert HIDDEN not in text
    assert "a***@hidden.dev" in text


def test_commit_findings_are_reported_in_plain_words(export) -> None:
    text = story_text(_Report(export, None, ScoringConfig()))

    assert "UTC+05:45" in text
    assert "not shown on any profile" in text


def test_accounts_are_grouped_in_plain_words(export) -> None:
    text = story_text(_Report(export, None, ScoringConfig()))

    assert "The short answer" in text
    assert "Strongly linked" in text
    assert "Why we think it is linked" in text
    assert "One profile links directly to the other" in text


def test_no_scoring_jargon_reaches_the_reader(export) -> None:
    text = story_text(_Report(export, None, ScoringConfig()))

    for jargon in ("VERY_HIGH", "Points", "evidence weight", "Potential Alias", "confidence"):
        assert jargon not in text


def test_the_report_says_what_it_is_not(export) -> None:
    text = story_text(_Report(export, None, ScoringConfig()))

    assert "How to read this report" in text
    assert "evidence, not conclusions" in text
    assert "Demo data" in text


def test_ruled_out_entities_are_named_and_left_out(export) -> None:
    ruled = next(
        e for e in export.entities if not e.is_seed and e.type == "ACCOUNT" and e.resolved
    )
    ruled.analyst_verdict = EntityVerdict.DIFFERENT_IDENTITY
    ruled.analyst_note = "a namesake in another country"

    text = story_text(_Report(export, None, ScoringConfig()))

    assert "ruled out as a different person" in text
    assert "a namesake in another country" in text


def test_the_whole_document_renders(export) -> None:
    pdf = render_report(export, None, ScoringConfig())
    assert pdf.startswith(b"%PDF") and len(pdf) > 2000


def test_lookalike_handles_alone_never_count_as_linked(export) -> None:
    """mrbeast / mr-beast: an alias scored 90 for a dash. That is a name match."""
    from app.models.enums import RelationshipType

    report = _Report(export, None, ScoringConfig())
    assert all(
        r.relationship_type != RelationshipType.POTENTIAL_ALIAS for r in report.best.values()
    )


def test_a_profile_picture_is_cropped_to_a_circle() -> None:
    from io import BytesIO

    from PIL import Image as PILImage

    from app.services.report import AVATAR_PIXELS, circle_png

    source = BytesIO()
    PILImage.new("RGB", (300, 200), "red").save(source, "JPEG")
    png = circle_png(source.getvalue())

    assert png is not None
    picture = PILImage.open(BytesIO(png))
    assert picture.size == (AVATAR_PIXELS, AVATAR_PIXELS)
    assert picture.getpixel((0, 0))[3] == 0, "corners are transparent"
    assert picture.getpixel((64, 64))[3] == 255
    assert circle_png(b"not an image") is None


def test_rows_show_the_picture_or_an_initial(export) -> None:
    from io import BytesIO

    from PIL import Image as PILImage
    from reportlab.graphics.shapes import Drawing
    from reportlab.platypus import Image

    from app.services.report import circle_png

    account = next(e for e in export.entities if e.type == "ACCOUNT")
    raw = BytesIO()
    PILImage.new("RGB", (64, 64), "blue").save(raw, "PNG")
    account.avatar_url = "https://example.test/a.png"
    report = _Report(export, None, ScoringConfig(),
                     avatars={account.avatar_url: circle_png(raw.getvalue())})

    assert isinstance(report.avatar(account), Image)
    account.avatar_url = None
    assert isinstance(report.avatar(account), Drawing)
