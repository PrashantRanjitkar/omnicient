"""Graph service and Neo4j persistence: entities, duplicates, relationships, evidence."""

from __future__ import annotations

import pytest

from app.models.enums import EntityType, InvestigationStatus, RelationshipType
from app.schemas.investigation import InvestigationCreate
from app.services.graph import GraphService
from app.services.investigation import InvestigationService


@pytest.fixture
async def investigation(repo):
    """A completed demo investigation."""
    service = InvestigationService(repo)
    created = service.create(
        InvestigationCreate(identifier="@alice_98", platform="instagram", demo=True)
    )
    await service.run(created)
    return created


async def test_entities_are_created_for_the_seed_and_its_discoveries(
    repo, investigation
):
    entities = repo.list_entities(investigation.id)
    labels = {entity.label for entity in entities}

    assert investigation.status == InvestigationStatus.COMPLETED
    assert "instagram:alice_98" in labels
    assert "website:alice.dev" in labels
    assert "github:alice-security" in labels
    assert sum(entity.is_seed for entity in entities) == 1

    types = {entity.type for entity in entities}
    assert EntityType.ACCOUNT in types
    assert EntityType.WEBSITE in types


async def test_entities_carry_their_neo4j_type_label(repo, investigation):
    """An account is stored as ``(:Entity:Account)``, a website as ``(:Website)``.

    The documented model in section 7 is queried by label, so the labels have
    to be real - not just a ``type`` property.
    """
    accounts = repo.session.run(
        "MATCH (n:Account {investigation_id: $id}) RETURN count(n) AS total",
        id=investigation.id,
    ).single()["total"]
    websites = repo.session.run(
        "MATCH (n:Website {investigation_id: $id}) RETURN count(n) AS total",
        id=investigation.id,
    ).single()["total"]
    assert accounts > 0
    assert websites > 0


async def test_entities_are_not_duplicated_across_runs(repo, investigation):
    """Re-running discovery updates entities instead of duplicating them."""
    before = len(repo.list_entities(investigation.id))
    await InvestigationService(repo).run(investigation, reset=False)
    after = repo.list_entities(investigation.id)
    assert len(after) == before

    keys = [entity.key for entity in after]
    assert len(keys) == len(set(keys))


async def test_relationships_are_created_and_deduplicated(repo, investigation):
    before = len(repo.list_relationships(investigation.id))
    assert before > 0

    await InvestigationService(repo).run(investigation, reset=False)
    assert len(repo.list_relationships(investigation.id)) == before

    types = {
        relationship.relationship_type
        for relationship in repo.list_relationships(investigation.id)
    }
    assert RelationshipType.LINKS_TO in types
    assert RelationshipType.POTENTIAL_SAME_IDENTITY in types


async def test_every_relationship_has_evidence_attached(repo, investigation):
    relationships = repo.attach_evidence(repo.list_relationships(investigation.id))
    assert relationships
    for relationship in relationships:
        assert relationship.evidence, f"{relationship.id} has no evidence"
        assert relationship.evidence_ids
        for item in relationship.evidence:
            assert item.description
            assert item.investigation_id == investigation.id


async def test_snapshots_record_every_resolved_observation(repo, investigation):
    resolved = [
        entity for entity in repo.list_entities(investigation.id) if entity.resolved
    ]
    assert resolved
    for entity in resolved:
        assert repo.snapshots_for_entity(entity.id)


async def test_graph_payload_is_renderable(repo, investigation):
    graph = GraphService(repo).build(investigation)
    entities, relationships, evidence = repo.counts(investigation.id)

    assert graph.investigation_id == investigation.id
    assert graph.seed_entity_id
    assert len(graph.nodes) == entities
    assert len(graph.edges) == relationships

    node_ids = {node.id for node in graph.nodes}
    for edge in graph.edges:
        assert edge.source in node_ids and edge.target in node_ids
        assert edge.relationship_label
        assert edge.evidence_count >= 1

    seed = next(node for node in graph.nodes if node.is_seed)
    assert seed.depth == 0
    assert seed.position.y == 0
    # Deeper entities are laid out below their parents.
    assert max(node.position.y for node in graph.nodes) > 0
    assert graph.stats.entities == len(graph.nodes)
    assert graph.stats.evidence == evidence
    assert graph.stats.contradictions >= 1


async def test_a_website_seed_is_typed_as_a_website():
    """The seed's entity type follows its platform, not a default of ACCOUNT."""
    from app.services.discovery import seed_candidate

    assert seed_candidate("website", "alice.dev").entity_type == EntityType.WEBSITE
    assert seed_candidate("instagram", "alice_98").entity_type == EntityType.ACCOUNT


async def test_graph_layout_is_deterministic(repo, investigation):
    first = GraphService(repo).build(investigation)
    second = GraphService(repo).build(investigation)
    assert [(node.id, node.position.x, node.position.y) for node in first.nodes] == [
        (node.id, node.position.x, node.position.y) for node in second.nodes
    ]


async def test_analyst_decision_is_preserved_across_recrawl(repo, investigation):
    """A verdict an analyst already recorded must survive a re-run."""
    relationship = repo.list_relationships(investigation.id)[0]
    repo.set_analyst_status(relationship.id, "CONFIRMED", "Checked the evidence.")

    await InvestigationService(repo).run(investigation, reset=False)

    refreshed = repo.get_relationship(relationship.id)
    assert refreshed.analyst_status == "CONFIRMED"
    assert refreshed.analyst_note == "Checked the evidence."
    assert refreshed.reviewed_at is not None


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


async def test_the_seed_sits_at_the_top(repo) -> None:
    """An investigation hangs from the thing it started with."""
    from app.schemas.investigation import InvestigationCreate
    from app.services.graph import GraphService
    from app.services.investigation import InvestigationService

    service = InvestigationService(repo)
    created = service.create(
        InvestigationCreate(identifier="alice_98", platform="instagram", demo=True)
    )
    await service.run(created)

    graph = GraphService(repo).build(created)
    seed = next(node for node in graph.nodes if node.is_seed)

    assert (seed.position.x, seed.position.y) == (0.0, 0.0)


async def test_the_tree_grows_downward_from_the_seed(repo) -> None:
    """Hop distance reads down the screen, and only downward.

    The layout is a tree now rather than a ring, so the property worth
    holding is the one a tree promises: everything reached from the seed is
    below it, and a card is below the card it was reached through. A row that
    drifts upward would say the crawl went the other way.
    """
    from app.schemas.investigation import InvestigationCreate
    from app.services.graph import GraphService
    from app.services.investigation import InvestigationService

    service = InvestigationService(repo)
    created = service.create(
        InvestigationCreate(identifier="alice_98", platform="instagram", demo=True)
    )
    await service.run(created)

    graph = GraphService(repo).build(created)
    seed = next(node for node in graph.nodes if node.is_seed)
    by_id = {node.id: node for node in graph.nodes}

    assert all(node.position.y >= seed.position.y for node in graph.nodes)
    assert max(node.position.y for node in graph.nodes) > seed.position.y

    # Everything the seed touches hangs below it, strictly - the one
    # parent-child relationship the payload states plainly enough to check.
    kin = [
        by_id[edge.target if edge.source == seed.id else edge.source]
        for edge in graph.edges
        if seed.id in (edge.source, edge.target)
    ]
    assert kin, "the seed should be connected to something"
    for node in kin:
        assert node.position.y > seed.position.y, (
            f"{node.label} was reached from the seed but is drawn level with "
            "it or above it"
        )

    # And a parent sits over its children rather than off to one side.
    if len(kin) > 1:
        xs = [node.position.x for node in kin]
        assert min(xs) <= seed.position.x <= max(xs), (
            "the seed is not centred over what it found"
        )


async def test_no_two_cards_overlap(repo) -> None:
    """Cards are boxes, so the check has to be about boxes.

    This measured straight-line distance against a single threshold, which
    quietly assumed a square card. A node actually renders about 185x109 -
    nearly twice as wide as it is tall - so one number was simultaneously too
    slack horizontally and far too strict vertically, and the strict half was
    holding the levels three times further apart than the columns for no
    reason anyone had checked.
    """
    from app.schemas.investigation import InvestigationCreate
    from app.services.graph import GraphService
    from app.services.investigation import InvestigationService

    # The measured card, rounded up, so the assertion has a little margin.
    card_width, card_height = 200, 120

    service = InvestigationService(repo)
    created = service.create(
        InvestigationCreate(identifier="alice_98", platform="instagram", demo=True)
    )
    await service.run(created)

    graph = GraphService(repo).build(created)
    points = [(node.id, node.position.x, node.position.y) for node in graph.nodes]
    for index, (first, x1, y1) in enumerate(points):
        for second, x2, y2 in points[index + 1 :]:
            assert abs(x1 - x2) >= card_width or abs(y1 - y2) >= card_height, (
                f"{first} and {second} overlap: "
                f"{abs(x1 - x2):.0f}px apart across, {abs(y1 - y2):.0f}px down"
            )


def test_an_empty_investigation_lays_out_to_nothing() -> None:
    import networkx as nx

    from app.services.graph import GraphService

    assert GraphService._layout(nx.Graph(), []) == {}


# ---------------------------------------------------------------------------
# Compact layout
# ---------------------------------------------------------------------------


def _star(leaves: int, organizations: int = 0):
    """A seed with ``leaves`` accounts and ``organizations`` hanging off it."""
    import networkx as nx

    from app.models.entity import Entity

    seed = Entity("inv", "username", "seed", "seed", type=EntityType.USERNAME, is_seed=True)
    accounts = [Entity("inv", f"site{i}", f"a{i}", f"a{i}") for i in range(leaves)]
    orgs = [
        Entity("inv", "organization", f"o{i}", f"o{i}", type=EntityType.ORGANIZATION)
        for i in range(organizations)
    ]
    graph = nx.Graph()
    for entity in [seed, *accounts, *orgs]:
        graph.add_node(entity.id)
    for entity in [*accounts, *orgs]:
        graph.add_edge(seed.id, entity.id, weight=10.0, observed=False)
    return seed, accounts, orgs, graph


def _width(positions: dict, ids) -> float:
    xs = [positions[i]["x"] for i in ids]
    return max(xs) - min(xs)


def test_undrawn_organizations_take_no_room() -> None:
    """The canvas never draws organizations, so they must not space it out.

    Laid out like any other node, eight organizations from one Facebook
    Intro reserved eight columns of empty space and pushed the accounts that
    were drawn nearly two thousand pixels apart.
    """
    seed, accounts, _, graph = _star(leaves=4)
    plain = GraphService._layout(graph, [seed, *accounts])
    plain_width = _width(plain, [seed.id, *(a.id for a in accounts)])

    seed, accounts, orgs, graph = _star(leaves=4, organizations=8)
    crowded = GraphService._layout(graph, [seed, *accounts, *orgs])

    drawn = [seed.id, *(a.id for a in accounts)]
    assert _width(crowded, drawn) == plain_width
    # And they are still given a position, below everything that is drawn.
    lowest = max(crowded[i]["y"] for i in drawn)
    assert all(crowded[o.id]["y"] > lowest for o in orgs)


def test_a_wide_fan_out_wraps_instead_of_forming_a_strip() -> None:
    """A bare handle asks every source about itself, so the seed can have two
    dozen children. In one row they made a strip thousands of pixels wide
    that could only be fitted on screen by zooming out until nothing was
    legible."""
    from app.services.graph import MAX_GRID_COLUMNS, NODE_GAP

    seed, accounts, _, graph = _star(leaves=26)
    positions = GraphService._layout(graph, [seed, *accounts])

    assert _width(positions, (a.id for a in accounts)) <= (MAX_GRID_COLUMNS - 1) * NODE_GAP
    assert len({positions[a.id]["y"] for a in accounts}) > 1, "should wrap into rows"


async def test_the_tree_follows_the_links_the_crawl_followed(repo, investigation) -> None:
    """Each card hangs from the one it was actually found through.

    GitHub @alice-security was reached through the personal website the seed
    links to. A shortest-path tree over every relationship hung it straight
    off the seed through a scored match instead, and drew the link that
    really led to it as a line running sideways through the row.
    """
    graph = GraphService(repo).build(investigation)
    by_id = {node.id: node for node in graph.nodes}
    github = next(n for n in graph.nodes if n.platform == "github")
    seed = next(n for n in graph.nodes if n.is_seed)

    assert seed.parent_id is None
    assert github.parent_id is not None
    assert by_id[github.parent_id].type == "WEBSITE"
    assert by_id[github.parent_id].identifier == "alice.dev"


# ---------------------------------------------------------------------------
# Folding a handle's name-only matches
# ---------------------------------------------------------------------------


def _handle_search(scores: dict[str, float], seed_type=EntityType.USERNAME):
    """A handle seed and the accounts searching for it found, scored as given."""
    from app.models.entity import Entity
    from app.models.relationship import Relationship

    seed = Entity("inv", "username", "mrbeast", "mrbeast", type=seed_type, is_seed=True)
    accounts = {name: Entity("inv", name, "@mrbeast", "mrbeast") for name in scores}
    relationships = [
        Relationship("inv", seed.id, account.id, RelationshipType.USES_USERNAME,
                     confidence_score=scores[name])
        for name, account in accounts.items()
    ]
    entities = [seed, *accounts.values()]
    graph = GraphService._networkx_graph(entities, relationships)
    parents = GraphService._spanning_tree(graph, entities)[2]
    best = GraphService._best_confidence(relationships)
    return seed, accounts, entities, relationships, parents, best


def test_name_only_matches_of_a_handle_fold_into_one_group() -> None:
    seed, accounts, entities, relationships, parents, best = _handle_search(
        {"chess": 10, "steam": 10, "duolingo": 10, "github": 45, "youtube": 70}
    )

    group = GraphService._name_only_group(entities, relationships, parents, best)

    assert group is not None
    seed_id, members = group
    assert seed_id == seed.id
    # Only the ones with nothing but the name; the evidenced ones stay cards.
    assert set(members) == {accounts[n].id for n in ("chess", "steam", "duolingo")}


def test_too_few_name_only_matches_are_left_as_cards() -> None:
    _, _, entities, relationships, parents, best = _handle_search(
        {"chess": 10, "steam": 10, "github": 45}
    )

    assert GraphService._name_only_group(entities, relationships, parents, best) is None


def test_an_account_somebody_ruled_on_is_never_folded_away() -> None:
    """A decision an analyst made has to stay where they can see it."""
    _, accounts, entities, relationships, parents, best = _handle_search(
        {"chess": 10, "steam": 10, "duolingo": 10, "lichess": 10}
    )
    ruled = next(r for r in relationships if r.target_entity_id == accounts["chess"].id)
    ruled.analyst_status = "REJECTED"

    _, members = GraphService._name_only_group(entities, relationships, parents, best)

    assert accounts["chess"].id not in members


def test_only_a_bare_handle_seed_is_folded() -> None:
    """An investigation started from an account did not search by name."""
    _, _, entities, relationships, parents, best = _handle_search(
        {"chess": 10, "steam": 10, "duolingo": 10}, seed_type=EntityType.ACCOUNT
    )

    assert GraphService._name_only_group(entities, relationships, parents, best) is None
