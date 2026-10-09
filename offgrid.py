"""Core graph data model for the Safe Route Finder (OFFGRID Problem 1).

The city is modelled as an undirected graph:

* :class:`Node`        - a junction / landmark with a name and planar coordinates (metres).
* :class:`Segment`     - a street or path between two nodes; carries every public-safety
                         attribute the routing engine needs.
* :class:`Incident`    - a reported incident with a location, a date (for time decay) and a
                         severity.
* :class:`RoadNetwork` - container with an adjacency index, a segment lookup index and a
                         small uniform spatial index used to find incidents near a segment.

Coordinates use a local planar system (x = east, y = north, in metres) so segment lengths
are simple Euclidean distances; :func:`safe_route.visualize.to_latlon` maps them onto real
Jaipur coordinates for map rendering.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Tuple

NodeID = str
Pair = Tuple[NodeID, NodeID]

#: Safety prior for each road type, from the point of view of somebody walking at night.
#: Wide, busy, well-lit streets score high; dark shortcuts and hostile highways score low.
ROAD_TYPE_SAFETY: Dict[str, float] = {
    "footpath": 0.80,   # dedicated walkways, usually lit and overlooked by buildings
    "local": 0.70,      # small residential streets
    "collector": 0.78,  # mid-size streets with shops and through traffic
    "arterial": 0.72,   # main roads: lit and busy, but fast traffic
    "highway": 0.35,    # no pedestrian infrastructure, fast traffic
    "alley": 0.25,      # the classic "short but dark" shortcut
}

#: Typical walking speed (km/h) per road type - alleys are slower (obstacles, bad surface).
WALK_SPEED_KPH: Dict[str, float] = {
    "footpath": 4.6,
    "local": 4.8,
    "collector": 4.9,
    "arterial": 5.0,
    "highway": 5.0,
    "alley": 3.4,
}


def clamp01(value: float) -> float:
    """Clamp *value* into the [0, 1] interval."""
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


def interpolate_hourly(profile: List[float], hour: float) -> float:
    """Linearly interpolate a 24-entry hourly profile at a fractional hour of day."""
    if len(profile) != 24:
        raise ValueError("hourly profiles must contain exactly 24 entries")
    h = hour % 24.0
    lo = int(math.floor(h)) % 24
    hi = (lo + 1) % 24
    frac = h - math.floor(h)
    return profile[lo] * (1.0 - frac) + profile[hi] * frac


def eyes_on_street(shops_open_until: int, hour: float) -> float:
    """Natural-surveillance score for *hour*, given when local shops close.

    While shops are open the score is 1.0.  After closing it decays linearly to 0 over two
    hours: people are still leaving, but the street empties quickly and stops being
    watched, which matters a lot for somebody walking home late.
    """
    if shops_open_until is None or shops_open_until <= 0:
        return 0.0
    h = hour % 24.0
    close = shops_open_until % 24.0
    hours_until_close = (close - h) % 24.0
    if hours_until_close <= 12.0:  # shops are still open
        return 1.0
    hours_since_close = (h - close) % 24.0
    return max(0.0, 1.0 - hours_since_close / 2.0)


@dataclass
class Node:
    """A junction or landmark of the street network."""

    id: NodeID
    name: str
    x: float
    y: float


@dataclass
class Segment:
    """One street/path between two nodes (undirected: usable both ways on foot)."""

    idx: int
    u: NodeID
    v: NodeID
    length_m: float
    road_type: str
    lighting: float = 0.5             # 0 = pitch black, 1 = fully and reliably lit
    hourly_pedestrian: List[float] = field(default_factory=lambda: [0.3] * 24)
    shops_open_until: int = 21        # 24h clock; 0 means "no shops around"
    incident_risk: float = 0.0        # 0..1, filled in by ``SafetyModel.bind``

    def other(self, node: NodeID) -> NodeID:
        """Return the endpoint of this segment that is not *node*."""
        if node == self.u:
            return self.v
        if node == self.v:
            return self.u
        raise KeyError(f"node {node!r} is not an endpoint of segment {self.u}-{self.v}")

    @property
    def key(self) -> Pair:
        """Ordered endpoint pair, usable as a dictionary / set key."""
        return (self.u, self.v) if self.u <= self.v else (self.v, self.u)


@dataclass
class Incident:
    """A reported incident near the network (location, date, severity, category)."""

    id: str
    x: float
    y: float
    date: date
    severity: int     # 1 (minor) .. 5 (very serious)
    kind: str = "incident"


class SpatialIndex:
    """Tiny uniform-grid index so "incidents near this point" stays fast."""

    def __init__(self, cell_size: float = 100.0) -> None:
        self.cell_size = float(cell_size)
        self._buckets: Dict[Tuple[int, int], List[Incident]] = {}

    def _cell(self, x: float, y: float) -> Tuple[int, int]:
        return (int(math.floor(x / self.cell_size)), int(math.floor(y / self.cell_size)))

    def insert(self, item: Incident) -> None:
        self._buckets.setdefault(self._cell(item.x, item.y), []).append(item)

    def query(self, x: float, y: float, radius: float) -> List[Incident]:
        """Return every incident within *radius* metres of (x, y)."""
        cx, cy = self._cell(x, y)
        reach = int(math.ceil(radius / self.cell_size))
        found: List[Incident] = []
        for gx in range(cx - reach, cx + reach + 1):
            for gy in range(cy - reach, cy + reach + 1):
                bucket = self._buckets.get((gx, gy))
                if not bucket:
                    continue
                for item in bucket:
                    if math.hypot(item.x - x, item.y - y) <= radius:
                        found.append(item)
        return found


class RoadNetwork:
    """An undirected street graph plus its incident history."""

    def __init__(self, name: str = "city") -> None:
        self.name = name
        self.nodes: Dict[NodeID, Node] = {}
        self.segments: List[Segment] = []
        self.incidents: List[Incident] = []
        self._adj: Dict[NodeID, List[Segment]] = {}
        self._by_pair: Dict[Pair, Segment] = {}
        self._incident_index = SpatialIndex()

    # ------------------------------------------------------------- build
    def add_node(self, node_id: NodeID, name: str, x: float, y: float) -> Node:
        """Add a junction; coordinates are metres in the local planar system."""
        if node_id in self.nodes:
            raise ValueError(f"duplicate node id {node_id!r}")
        node = Node(id=node_id, name=name, x=float(x), y=float(y))
        self.nodes[node_id] = node
        return node

    def add_segment(
        self,
        u: NodeID,
        v: NodeID,
        road_type: str,
        lighting: float = 0.5,
        hourly_pedestrian: Optional[List[float]] = None,
        shops_open_until: int = 21,
        length_m: Optional[float] = None,
    ) -> Segment:
        """Add a street segment between existing nodes *u* and *v*.

        ``hourly_pedestrian`` is a 24-entry list of relative footfall values (0..1).
        If ``length_m`` is omitted the Euclidean distance between the nodes is used.
        """
        if u not in self.nodes or v not in self.nodes:
            raise KeyError(f"segment {u}-{v} references an unknown node")
        if length_m is None:
            a, b = self.nodes[u], self.nodes[v]
            length_m = math.hypot(a.x - b.x, a.y - b.y)
        segment = Segment(
            idx=len(self.segments),
            u=u,
            v=v,
            length_m=float(length_m),
            road_type=road_type,
            lighting=clamp01(float(lighting)),
            hourly_pedestrian=[float(value) for value in (hourly_pedestrian or [0.3] * 24)],
            shops_open_until=int(shops_open_until),
        )
        self.segments.append(segment)
        return segment

    def add_incident(
        self,
        x: float,
        y: float,
        on: date,
        severity: int,
        kind: str = "incident",
        incident_id: Optional[str] = None,
    ) -> Incident:
        """Record a reported incident at (x, y) that happened on date *on*."""
        incident_id = incident_id or f"I{len(self.incidents) + 1:03d}"
        incident = Incident(
            id=incident_id,
            x=float(x),
            y=float(y),
            date=on,
            severity=int(severity),
            kind=str(kind),
        )
        self.incidents.append(incident)
        return incident

    def finalize(self) -> None:
        """(Re)build the derived indexes: adjacency, pair lookup, incident grid."""
        self._adj = {node_id: [] for node_id in self.nodes}
        self._by_pair = {}
        for segment in self.segments:
            self._adj[segment.u].append(segment)
            self._adj[segment.v].append(segment)
            self._by_pair[(segment.u, segment.v)] = segment
            self._by_pair[(segment.v, segment.u)] = segment
        self._incident_index = SpatialIndex()
        for incident in self.incidents:
            self._incident_index.insert(incident)

    # ------------------------------------------------------------- query
    def neighbors(self, node: NodeID) -> List[Segment]:
        """All segments touching *node*."""
        return self._adj.get(node, [])

    def segment_between(self, u: NodeID, v: NodeID) -> Optional[Segment]:
        """The segment connecting *u* and *v*, or ``None`` if there is none."""
        return self._by_pair.get((u, v))

    def incidents_near(self, x: float, y: float, radius: float) -> List[Incident]:
        """Incidents within *radius* metres of (x, y)."""
        return self._incident_index.query(x, y, radius)

    def label(self, segment: Segment) -> str:
        """Human-readable name of a segment, e.g. ``Hawa Mahal - Bari Chaupar``."""
        a = self.nodes[segment.u].name
        b = self.nodes[segment.v].name
        return f"{a} - {b}" if a <= b else f"{b} - {a}"

    def find(self, text: str) -> Optional[Node]:
        """Fuzzy location lookup: exact id/name first, then case-insensitive substring."""
        needle = (text or "").strip().lower()
        if not needle:
            return None
        for node in self.nodes.values():
            if node.id.lower() == needle or node.name.lower() == needle:
                return node
        for node in sorted(self.nodes.values(), key=lambda n: n.name):
            if needle in node.name.lower() or needle in node.id.lower():
                return node
        return None

    def suggestions(self, text: str, limit: int = 8) -> List[str]:
        """Location names that contain *text* (used for CLI error hints)."""
        needle = (text or "").strip().lower()
        return sorted(n.name for n in self.nodes.values() if needle in n.name.lower())[:limit]

    def stats(self) -> str:
        """One-line summary of the network size."""
        return (
            f"{len(self.nodes)} junctions, {len(self.segments)} segments, "
            f"{len(self.incidents)} incidents"
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"RoadNetwork(name={self.name!r}, {self.stats()})"
