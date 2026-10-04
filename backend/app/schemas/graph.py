"""API schemas for the investigation graph.

This is the contract between the backend and the React Flow client: the
backend decides what the graph *is* (nodes, edges, evidence counts, layout
hints), the frontend decides how it looks.  The frontend never needs to know
how crawling or correlation work.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from ..models.enums import (
    AnalystStatus,
    ConfidenceLevel,
    DiscoveryMethod,
    EntityType,
    EntityVerdict,
    RelationshipOrigin,
    RelationshipType,
)
from ..utils.activity import ActivityStatus


class GraphPosition(BaseModel):
    """Layout hint in React Flow coordinates, computed with NetworkX."""

    x: float
    y: float


class GraphNode(BaseModel):
    """One entity, ready to render."""

    id: str
    type: EntityType
    platform: str
    platform_name: str
    label: str
    identifier: str
    url: str | None = None
    display_name: str | None = None
    avatar_url: str | None = None
    is_seed: bool = False
    resolved: bool = True
    depth: int = 0
    discovery_method: DiscoveryMethod = DiscoveryMethod.DIRECT
    degree: int = 0
    #: An analyst's ruling that this entity is somebody else. The canvas
    #: strikes it through rather than dropping it, and the filter panel can
    #: hide it - but the graph still ships it, because an entity the analyst
    #: cannot see is one they cannot un-rule.
    analyst_verdict: EntityVerdict = EntityVerdict.UNREVIEWED
    analyst_note: str | None = None
    #: The label is a masked value the person did not publish (a commit
    #: author address). ``identifier`` still holds it in full; the client
    #: shows it only when the analyst asks.
    masked: bool = False
    #: Whether the account has done anything in public lately. Context only:
    #: it never touches a score.
    activity: ActivityStatus = ActivityStatus.UNKNOWN
    last_active: datetime | None = None
    # Strongest association attached to this node, used for node badges.
    confidence_level: ConfidenceLevel | None = None
    confidence_score: float | None = None
    #: The node this one hangs from in the drawn tree - the neighbour it was
    #: first reached through from the seed. None for the seed and for
    #: anything no relationship reaches.
    parent_id: str | None = None
    #: Where the card goes with every entity shown.
    position: GraphPosition
    #: Where it goes when a handle's name-only matches are folded into one
    #: card. None for the folded members themselves; equal to ``position``
    #: when there is nothing to fold.
    compact_position: GraphPosition | None = None
    #: The folded group this entity belongs to, if any.
    group_id: str | None = None


class GraphEdge(BaseModel):
    """One relationship, ready to render."""

    id: str
    source: str
    target: str
    relationship_type: RelationshipType
    relationship_label: str
    confidence_score: float
    confidence_level: ConfidenceLevel
    analyst_status: AnalystStatus
    #: Whether a person drew this link or the engine derived it. The canvas
    #: must never draw the two identically.
    origin: RelationshipOrigin = RelationshipOrigin.ENGINE
    evidence_count: int = 0
    contradiction_count: int = 0
    summary: str | None = None


class GraphStats(BaseModel):
    """Counts the sidebar and filter panel render."""

    entities: int = 0
    relationships: int = 0
    evidence: int = 0
    contradictions: int = 0
    max_depth: int = 0
    by_entity_type: dict[str, int] = Field(default_factory=dict)
    by_relationship_type: dict[str, int] = Field(default_factory=dict)
    by_confidence: dict[str, int] = Field(default_factory=dict)
    by_analyst_status: dict[str, int] = Field(default_factory=dict)


class GraphGroup(BaseModel):
    """Accounts that share the searched handle and nothing else, as one card.

    A bare-username crawl asks every source for the handle, and on a common
    name most of what comes back is somebody else. Drawn one card each they
    dominate the canvas and read, from their shared line to the seed, as if
    they belonged together. Folded, they say what they are: accounts that
    use this name, with no other evidence yet.
    """

    id: str
    parent_id: str
    handle: str
    member_ids: list[str] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=list)
    position: GraphPosition


class GraphResponse(BaseModel):
    """The whole investigation graph."""

    investigation_id: str
    generated_at: datetime
    seed_entity_id: str | None = None
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    groups: list[GraphGroup] = Field(default_factory=list)
    stats: GraphStats = Field(default_factory=GraphStats)
