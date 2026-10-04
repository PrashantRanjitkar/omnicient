"""Graph service.

Turns the stored entities and relationships into the payload the React Flow
client renders.  The backend owns what the graph *is* - nodes, edges, evidence
counts, layout hints and summary statistics - and the frontend owns how it
looks.  That split is what lets the interface and the OSINT backend be built
in parallel against a fixed contract (section 47).

NetworkX does the structural work here (degree, layered layout).  It is a
processing library, not a store: the source of truth stays in Neo4j.
"""

from __future__ import annotations

import math
from collections import deque
from datetime import UTC, datetime

import networkx as nx

from ..models.entity import Entity
from ..models.enums import AnalystStatus, EntityType, EntityVerdict
from ..models.investigation import Investigation
from ..models.relationship import Relationship
from ..repository import Neo4jRepository
from ..schemas.graph import (
    GraphEdge,
    GraphGroup,
    GraphNode,
    GraphPosition,
    GraphResponse,
    GraphStats,
)
from ..utils.activity import activity_status, last_active
from ..utils.logging import get_logger
from ..utils.normalization import platform_label

logger = get_logger(__name__)

# Layout spacing in React Flow pixels.
# Card dimensions are measured, not assumed: a node renders about 185x109
# at zoom 1, so it is nearly twice as wide as it is tall. Spacing the levels
# as generously as the columns therefore looked wrong - the vertical gutter
# came out at 171px against 55px horizontally, and the tree read as three
# times airier down the screen than across it. Both gutters are now about
# the same, which is what makes it compact without crowding.
#: Vertical distance between one hop level and the next. 109 of that is card.
LEVEL_GAP = 170
#: Horizontal distance between neighbouring cards. 185 of that is card.
NODE_GAP = 225
#: Vertical distance between rows *within* a wrapped group of siblings. Less
#: than LEVEL_GAP on purpose, so a wrapped group reads as one block under its
#: parent rather than as extra hops. 109 of it is card.
GRID_ROW_GAP = 140
#: Siblings up to this many sit in a single row under their parent.
SINGLE_ROW_UP_TO = 6
#: A larger group of leaf siblings wraps into rows at most this wide. A bare
#: handle asks every source about itself, so the seed can have two dozen
#: children; laid out in one row they made a strip thousands of pixels wide
#: that the canvas could only fit by zooming out until nothing was legible.
#: Kept narrow because the canvas between the two side panels is roughly
#: square, and a wide drawing can only be fitted to it by shrinking it.
MAX_GRID_COLUMNS = 5
#: Once the blocks under one parent are wider than this, the next block
#: starts a new band underneath instead of extending the row sideways.
MAX_ROW_WIDTH = 1600
#: Entity types the canvas never draws. They are left out of the layout
#: entirely: laid out but invisible, eight organizations from one Facebook
#: Intro reserved eight columns of empty space and pushed the accounts that
#: *were* drawn nearly two thousand pixels apart.
UNDRAWN_TYPES = frozenset({EntityType.ORGANIZATION})
#: Relationships read directly off a page rather than inferred.
OBSERVED_TYPES = frozenset({"LINKS_TO", "REFERENCES"})
#: Below this score, an account found by searching for the handle has little
#: beyond the shared name: the handle match alone is worth 10, a matching
#: display name 5 more. Anything with a real signal - a photograph, a link,
#: a website - clears it.
NAME_ONLY_BELOW = 20
#: Name-only matches fold into one card once there are at least this many.
GROUP_AT_LEAST = 3


class GraphService:
    """Builds the investigation graph payload."""

    def __init__(self, repo: Neo4jRepository) -> None:
        self.repo = repo

    def build(self, investigation: Investigation) -> GraphResponse:
        """Assemble nodes, edges, layout and statistics for one investigation."""
        entities = self.repo.list_entities(investigation.id)
        relationships = self.repo.list_relationships(investigation.id)
        evidence_counts = self.repo.evidence_counts(investigation.id)

        graph = self._networkx_graph(entities, relationships)
        positions = self._layout(graph, entities)
        parents = self._spanning_tree(graph, entities)[2] if entities else {}
        best = self._best_confidence(relationships)

        # The compact layout folds a handle's name-only matches into one
        # card. It is laid out with a single member standing in for the
        # group, so the group takes exactly one card's room.
        group = self._name_only_group(entities, relationships, parents, best)
        compact = positions
        groups: list[GraphGroup] = []
        members: set[str] = set()
        if group is not None:
            seed_id, member_ids = group
            members = set(member_ids)
            stand_in = member_ids[0]
            kept = [e for e in entities if e.id not in members or e.id == stand_in]
            compact = self._layout(graph.subgraph(e.id for e in kept), kept)
            by_id = {e.id: e for e in entities}
            seed_entity = by_id[seed_id]
            groups.append(
                GraphGroup(
                    id=f"group:{seed_id}:name-only",
                    parent_id=seed_id,
                    handle=seed_entity.identifier,
                    member_ids=member_ids,
                    platforms=sorted({platform_label(by_id[m].platform) for m in member_ids}),
                    position=GraphPosition(**compact[stand_in]),
                )
            )

        nodes = [
            GraphNode(
                id=entity.id,
                type=EntityType(entity.type),
                platform=entity.platform,
                platform_name=platform_label(entity.platform),
                label=entity.name,
                identifier=entity.identifier,
                url=entity.url,
                display_name=entity.display_name,
                avatar_url=entity.avatar_url,
                is_seed=entity.is_seed,
                resolved=entity.resolved,
                depth=entity.depth,
                discovery_method=entity.discovery_method,
                degree=graph.degree(entity.id) if graph.has_node(entity.id) else 0,
                analyst_verdict=entity.analyst_verdict,
                analyst_note=entity.analyst_note,
                masked=bool((entity.meta or {}).get("exposed_by")),
                activity=activity_status(entity.meta),
                last_active=last_active(entity.meta),
                confidence_level=best.get(entity.id, (None, None))[0],
                confidence_score=best.get(entity.id, (None, None))[1],
                parent_id=parents.get(entity.id),
                position=GraphPosition(**positions[entity.id]),
                compact_position=(
                    None if entity.id in members else GraphPosition(**compact[entity.id])
                ),
                group_id=groups[0].id if entity.id in members else None,
            )
            for entity in entities
        ]

        edges = [
            GraphEdge(
                id=relationship.id,
                source=relationship.source_entity_id,
                target=relationship.target_entity_id,
                relationship_type=relationship.relationship_type,
                relationship_label=_relationship_label(relationship.relationship_type),
                confidence_score=relationship.confidence_score,
                confidence_level=relationship.confidence_level,
                analyst_status=relationship.analyst_status,
                origin=relationship.origin,
                evidence_count=evidence_counts.get(relationship.id, (0, 0))[0],
                contradiction_count=evidence_counts.get(relationship.id, (0, 0))[1],
                summary=relationship.summary,
            )
            for relationship in relationships
        ]

        seed = next((entity.id for entity in entities if entity.is_seed), None)
        response = GraphResponse(
            investigation_id=investigation.id,
            generated_at=datetime.now(UTC),
            seed_entity_id=seed,
            nodes=nodes,
            edges=edges,
            groups=groups,
            stats=self._stats(entities, relationships, evidence_counts),
        )
        logger.info(
            "graph_built investigation=%s nodes=%d edges=%d",
            investigation.id,
            len(nodes),
            len(edges),
        )
        return response

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _networkx_graph(
        entities: list[Entity], relationships: list[Relationship]
    ) -> nx.Graph:
        """Undirected view used for degree and layout calculations."""
        graph = nx.Graph()
        for entity in entities:
            graph.add_node(entity.id, depth=entity.depth, type=entity.type)
        for relationship in relationships:
            source = relationship.source_entity_id
            target = relationship.target_entity_id
            if not (graph.has_node(source) and graph.has_node(target)):
                continue
            # One edge per pair; remember whether any relationship between
            # them was read off a page, which is how the drawn tree follows
            # the path the crawl actually took.
            observed = str(relationship.relationship_type) in OBSERVED_TYPES
            if graph.has_edge(source, target):
                graph.edges[source, target]["observed"] |= observed
            else:
                graph.add_edge(
                    source,
                    target,
                    weight=max(relationship.confidence_score, 1.0),
                    observed=observed,
                )
        return graph

    @staticmethod
    def _name_only_group(
        entities: list[Entity],
        relationships: list[Relationship],
        parents: dict[str, str],
        best: dict[str, tuple[str, float]],
    ) -> tuple[str, list[str]] | None:
        """The accounts a bare-username seed found that have nothing but the name.

        Only for a seed that is a bare handle - an investigation started from
        an account or a URL did not search every source by name. A member has
        to hang directly off the handle, lead nowhere itself, score below
        NAME_ONLY_BELOW, and have no human decision on it: an account somebody
        confirmed, linked by hand or ruled on is never folded out of sight.
        """
        seed = next((e for e in entities if e.is_seed), None)
        if seed is None or str(seed.type) != EntityType.USERNAME:
            return None

        leads = set(parents.values())
        decided: set[str] = set()
        for relationship in relationships:
            if (
                relationship.analyst_status != AnalystStatus.UNREVIEWED
                or str(relationship.origin) == "ANALYST"
            ):
                decided.update(
                    (relationship.source_entity_id, relationship.target_entity_id)
                )

        member_ids = sorted(
            e.id
            for e in entities
            if parents.get(e.id) == seed.id
            and e.id not in leads
            and e.id not in decided
            and str(e.type) not in UNDRAWN_TYPES
            and str(e.analyst_verdict) == EntityVerdict.UNREVIEWED
            and best.get(e.id, (None, 0.0))[1] < NAME_ONLY_BELOW
        )
        if len(member_ids) < GROUP_AT_LEAST:
            return None
        return seed.id, member_ids

    @staticmethod
    def _spanning_tree(
        graph: nx.Graph, entities: list[Entity]
    ) -> tuple[nx.Graph, str, dict[str, str]]:
        """The tree the canvas is drawn as: each node and the one it hangs from.

        A node's parent is the neighbour it was first reached through from
        the seed, over the entities the canvas actually draws. The layout
        places cards by it, and the canvas uses it to decide which one line
        per card to draw by default - without that, an account found by name
        on twenty sites draws a line to every one of the others, and the
        picture becomes a web nobody can follow.
        """
        drawn_ids = {e.id for e in entities if str(e.type) not in UNDRAWN_TYPES}
        view = graph.subgraph(n for n in graph.nodes if n in drawn_ids)
        pool = [e for e in entities if e.id in drawn_ids] or entities

        def rank(node_id: str) -> tuple:
            degree = view.degree(node_id) if view.has_node(node_id) else 0
            return (-degree, node_id)

        root = next(
            (e.id for e in pool if e.is_seed),
            # No seed recorded: the busiest node is the closest thing to one.
            min((e.id for e in pool), key=rank),
        )

        def neighbours(node_id: str, observed_only: bool) -> list[str]:
            found = [
                other
                for other in view.neighbors(node_id)
                if not observed_only or view.edges[node_id, other].get("observed")
            ]
            return sorted(found, key=rank)

        # Two passes. First only links read off a page - "this profile links
        # to that site, which links to that account" - so the tree shows how
        # each card was actually reached. Then everything else, for accounts
        # no link leads to, attached to the nearest card already placed.
        #
        # A single shortest-path pass over every relationship hung a GitHub
        # account straight off the seed through a scored match, and drew the
        # link that really led to it - from the personal website - as a line
        # running sideways through the row.
        parent: dict[str, str] = {}
        if view.has_node(root):
            reached = [root]
            seen = {root}
            for observed_only in (True, False):
                queue = deque(reached)
                while queue:
                    node_id = queue.popleft()
                    for other in neighbours(node_id, observed_only):
                        if other in seen:
                            continue
                        seen.add(other)
                        parent[other] = node_id
                        reached.append(other)
                        queue.append(other)
        return view, root, parent

    @staticmethod
    def _layout(graph: nx.Graph, entities: list[Entity]) -> dict[str, dict[str, float]]:
        """Compact tree: the seed on top, what it led to underneath.

        Each node sits centred over what was reached through it, so a branch
        reads as one shape and depth reads down the screen. Two rules keep it
        compact:

        - Only what the canvas draws takes up space. Organizations are never
          drawn, so they are laid out separately where they cannot push the
          visible cards apart.
        - A parent with many leaf children wraps them into a block of rows,
          at most MAX_GRID_COLUMNS wide, instead of one endless strip.

        Levels are hop distance from the seed, not crawl depth: a bare handle
        asks every source about itself at depth zero, so depth alone would
        put the seed shoulder to shoulder with everything it found.
        """
        if not entities:
            return {}

        view, root, parent = GraphService._spanning_tree(graph, entities)
        drawn = [e for e in entities if str(e.type) not in UNDRAWN_TYPES]

        def rank(node_id: str) -> tuple:
            """Deterministic sibling order: busiest first, then by id."""
            degree = view.degree(node_id) if view.has_node(node_id) else 0
            return (-degree, node_id)

        children: dict[str, list[str]] = {}
        for node_id, mother in parent.items():
            children.setdefault(mother, []).append(node_id)
        for siblings in children.values():
            siblings.sort(key=rank)

        def grid(count: int) -> int:
            """How many columns a group of leaf siblings should use."""
            if count <= SINGLE_ROW_UP_TO:
                return count
            return min(MAX_GRID_COLUMNS, max(4, math.ceil(math.sqrt(count * 0.8))))

        def place(node_id: str) -> tuple[dict[str, tuple[float, float]], float]:
            """Lay out one subtree.

            Returns positions relative to the subtree (x from its leftmost
            card centre, y from its root) and its span: the distance between
            its leftmost and rightmost card centres.
            """
            kids = children.get(node_id, [])
            if not kids:
                return {node_id: (0.0, 0.0)}, 0.0

            blocks: list[tuple[dict[str, tuple[float, float]], float]] = []
            # Leaves first, nearest the parent, wrapped into a block if there
            # are many; then the branches. Put the other way round, the lines
            # to a large block of leaves had to cross every branch above it,
            # and on a bare-handle crawl that was two dozen lines through a
            # row of cards.
            leaves = [kid for kid in kids if not children.get(kid)]
            if leaves:
                columns = grid(len(leaves))
                span = (columns - 1) * NODE_GAP
                cells: dict[str, tuple[float, float]] = {}
                for index, leaf in enumerate(leaves):
                    row, column = divmod(index, columns)
                    in_row = min(columns, len(leaves) - row * columns)
                    # A short last row is centred rather than left-aligned.
                    inset = (columns - in_row) * NODE_GAP / 2
                    cells[leaf] = (inset + column * NODE_GAP, row * GRID_ROW_GAP)
                blocks.append((cells, span))
            for kid in kids:
                if children.get(kid):
                    blocks.append(place(kid))

            # Blocks go side by side until a band is too wide, then the next
            # band starts underneath the tallest block of the one above.
            bands: list[list[tuple[dict[str, tuple[float, float]], float]]] = [[]]
            width = -NODE_GAP
            for block in blocks:
                grow = block[1] + NODE_GAP
                if bands[-1] and width + grow > MAX_ROW_WIDTH:
                    bands.append([])
                    width = -NODE_GAP
                bands[-1].append(block)
                width += grow

            laid: dict[str, tuple[float, float]] = {}
            top = LEVEL_GAP
            total = 0.0
            for band in bands:
                band_span = sum(span for _, span in band) + NODE_GAP * (len(band) - 1)
                total = max(total, band_span)
            for band in bands:
                band_span = sum(span for _, span in band) + NODE_GAP * (len(band) - 1)
                # Each band is centred under the parent.
                cursor = (total - band_span) / 2
                deepest = 0.0
                for cells, span in band:
                    for member, (x, y) in cells.items():
                        laid[member] = (cursor + x, top + y)
                        deepest = max(deepest, y)
                    cursor += span + NODE_GAP
                top += deepest + LEVEL_GAP

            # The parent sits centred over everything reached through it.
            laid[node_id] = (total / 2, 0.0)
            return laid, total

        tree, _ = place(root)
        origin_x = tree[root][0]
        positions = {
            node_id: {"x": round(x - origin_x, 2), "y": round(y, 2)}
            for node_id, (x, y) in tree.items()
        }

        def park(ids: list[str]) -> None:
            """A centred grid underneath everything placed so far."""
            if not ids:
                return
            floor = max(point["y"] for point in positions.values())
            columns = grid(len(ids))
            for index, node_id in enumerate(ids):
                row, column = divmod(index, columns)
                in_row = min(columns, len(ids) - row * columns)
                positions[node_id] = {
                    "x": round((column - (in_row - 1) / 2) * NODE_GAP, 2),
                    "y": round(floor + LEVEL_GAP * 1.5 + row * GRID_ROW_GAP, 2),
                }

        # Drawn entities no relationship reaches are not part of the tree, and
        # hanging them off the root would draw a parentage that does not
        # exist. They sit in a grid underneath it instead.
        park(sorted((e.id for e in drawn if e.id not in positions), key=rank))
        # Undrawn ones still need a position in the payload; below everything,
        # where they can take no room from anything visible.
        park(sorted((e.id for e in entities if e.id not in positions), key=rank))
        return positions

    @staticmethod
    def _best_confidence(
        relationships: list[Relationship],
    ) -> dict[str, tuple[str, float]]:
        """Strongest association touching each node, for the node badge."""
        best: dict[str, tuple[str, float]] = {}
        for relationship in relationships:
            for node_id in (
                relationship.source_entity_id,
                relationship.target_entity_id,
            ):
                current = best.get(node_id)
                if current is None or relationship.confidence_score > current[1]:
                    best[node_id] = (
                        relationship.confidence_level,
                        relationship.confidence_score,
                    )
        return best

    @staticmethod
    def _stats(
        entities: list[Entity],
        relationships: list[Relationship],
        evidence_counts: dict[str, tuple[int, int]],
    ) -> GraphStats:
        stats = GraphStats(
            entities=len(entities),
            relationships=len(relationships),
            evidence=sum(total for total, _ in evidence_counts.values()),
            contradictions=sum(bad for _, bad in evidence_counts.values()),
            max_depth=max((entity.depth for entity in entities), default=0),
        )
        for entity in entities:
            stats.by_entity_type[entity.type] = stats.by_entity_type.get(entity.type, 0) + 1
        for relationship in relationships:
            key = relationship.relationship_type
            stats.by_relationship_type[key] = stats.by_relationship_type.get(key, 0) + 1
            level = relationship.confidence_level
            stats.by_confidence[level] = stats.by_confidence.get(level, 0) + 1
            status = relationship.analyst_status or AnalystStatus.UNREVIEWED
            stats.by_analyst_status[status] = stats.by_analyst_status.get(status, 0) + 1
        return stats


def _relationship_label(relationship_type: str) -> str:
    from ..models.enums import RELATIONSHIP_LABELS

    return RELATIONSHIP_LABELS.get(relationship_type, relationship_type)
