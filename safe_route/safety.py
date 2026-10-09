"""The public-safety scoring model.

Every street segment receives a safety score in [0, 1] as a weighted blend of five
transparent, measurable sub-scores:

``lighting``        streetlight level - does the road stay lit all night?
``pedestrian``      footfall at the query hour - busy streets feel and *are* safer
``road_type``       prior for the road class (alley < highway < arterial < collector ...)
``incidents``       1 minus a recency/severity-weighted incident density in a buffer
``eyes_on_street``  natural surveillance - do nearby shops close soon after the query hour?

The score drives the engine in two ways:

1. **Route comparison** metrics (mean/worst-segment safety, unlit share, risk exposure).
2. **The edge cost** used by Dijkstra / Yen's K-shortest-paths::

       cost(segment)      = walk_minutes(segment) + lambda * risk_exposure(segment)
       risk_exposure(s)   = length_km(s) * (1 - safety(s))

   ``lambda`` (risk aversion, extra minutes per km of completely unsafe road) pins the
   score to a length unit: ``lambda = 0`` reproduces the plain fastest route, larger
   values keep detouring away from unsafe segments - exactly the trade-off the problem
   statement asks us to expose.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Callable, ClassVar, Dict, List, Optional, Tuple

from .models import (
    ROAD_TYPE_SAFETY,
    WALK_SPEED_KPH,
    RoadNetwork,
    Segment,
    clamp01,
    eyes_on_street,
    interpolate_hourly,
)

#: Segments scoring below this are reported as "unsafe stretches".
UNSAFE_THRESHOLD = 0.40

#: Segments with a lighting score at/above this count as "well lit".
LIT_THRESHOLD = 0.60


@dataclass
class SafetyWeights:
    """Relative importance of the five safety parameters (normalised when used)."""

    lighting: float = 0.30
    pedestrian: float = 0.25
    road_type: float = 0.15
    incidents: float = 0.20
    eyes_on_street: float = 0.10

    FIELDS: ClassVar[Tuple[str, ...]] = (
        "lighting", "pedestrian", "road_type", "incidents", "eyes_on_street",
    )

    def normalized(self) -> "SafetyWeights":
        """Return a copy whose weights sum to 1."""
        total = sum(getattr(self, name) for name in self.FIELDS)
        if total <= 0:
            raise ValueError("safety weights must sum to a positive value")
        return SafetyWeights(**{name: getattr(self, name) / total for name in self.FIELDS})

    def as_dict(self) -> Dict[str, float]:
        return {name: round(getattr(self, name), 3) for name in self.FIELDS}


#: Ready-made weight profiles, selectable from the CLI with ``--profile``.
PROFILES: Dict[str, SafetyWeights] = {
    # equal footing for every parameter
    "equal": SafetyWeights(0.20, 0.20, 0.20, 0.20, 0.20),
    # the default, balanced blend
    "balanced": SafetyWeights(0.30, 0.25, 0.15, 0.20, 0.10),
    # for "I just do not want to walk in the dark"
    "lighting_led": SafetyWeights(0.45, 0.20, 0.10, 0.20, 0.05),
    # for "I want streets with people around"
    "crowd_led": SafetyWeights(0.25, 0.45, 0.10, 0.10, 0.10),
    # for "avoid the streets where things keep happening"
    "incident_led": SafetyWeights(0.20, 0.20, 0.10, 0.40, 0.10),
}


class SafetyModel:
    """Scores every segment of a network, for any hour of the day."""

    def __init__(
        self,
        weights: Optional[SafetyWeights] = None,
        risk_aversion: float = 25.0,
        incident_buffer_m: float = 75.0,
        incident_half_life_days: float = 120.0,
        incident_cap: float = 3.0,
        reference_date: Optional[date] = None,
    ) -> None:
        """
        :param weights:             parameter weights (normalised automatically).
        :param risk_aversion:       lambda - extra minutes per km of fully unsafe road.
        :param incident_buffer_m:   how far from a segment an incident still counts.
        :param incident_half_life_days: older incidents count half as much every N days.
        :param incident_cap:        incident load that saturates the risk at 1.0.
        :param reference_date:      "today" for the time decay (defaults to today).
        """
        self.weights = (weights or SafetyWeights()).normalized()
        self.risk_aversion = float(risk_aversion)
        self.incident_buffer_m = float(incident_buffer_m)
        self.incident_half_life_days = float(incident_half_life_days)
        self.incident_cap = float(incident_cap)
        self.reference_date = reference_date or date.today()

    # ----------------------------------------------------------- prep
    def bind(self, network: RoadNetwork) -> None:
        """Pre-compute the incident risk of every segment (proximity + recency decay)."""
        for segment in network.segments:
            segment.incident_risk = self.segment_incident_risk(network, segment)

    def segment_incident_risk(self, network: RoadNetwork, segment: Segment) -> float:
        """Aggregate the incidents that lie within ``incident_buffer_m`` of *segment*.

        Each incident contributes ``severity/5 * 0.5^(age/half_life) * proximity`` where the
        proximity factor fades from 1.0 (incident on the segment) to 0.4 (edge of the
        buffer); the closest sampled point wins.  The sum is capped and scaled to [0, 1].
        """
        best_by_incident: Dict[str, float] = {}
        for point in self._sample_points(network, segment):
            for incident in network.incidents_near(point[0], point[1], self.incident_buffer_m):
                days_old = max((self.reference_date - incident.date).days, 0)
                decay = 0.5 ** (days_old / self.incident_half_life_days)
                severity = incident.severity / 5.0
                distance = math.hypot(incident.x - point[0], incident.y - point[1])
                proximity = 0.4 + 0.6 * (1.0 - distance / self.incident_buffer_m)
                contribution = severity * decay * proximity
                if contribution > best_by_incident.get(incident.id, 0.0):
                    best_by_incident[incident.id] = contribution
        return clamp01(sum(best_by_incident.values()) / self.incident_cap)

    @staticmethod
    def _sample_points(network: RoadNetwork, segment: Segment, step_m: float = 25.0) -> List[Tuple[float, float]]:
        """Points sampled along a segment (so incidents mid-street are caught too)."""
        a = network.nodes[segment.u]
        b = network.nodes[segment.v]
        steps = max(1, int(segment.length_m // step_m))
        points = [(a.x, a.y)]
        for i in range(1, steps + 1):
            t = i / steps
            points.append((a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t))
        return points

    # --------------------------------------------------------- scoring
    def segment_safety(self, network: RoadNetwork, segment: Segment, hour: float) -> float:
        """Blend the five sub-scores of *segment* for the given hour of day."""
        w = self.weights
        return clamp01(
            w.lighting * clamp01(segment.lighting)
            + w.pedestrian * clamp01(interpolate_hourly(segment.hourly_pedestrian, hour))
            + w.road_type * ROAD_TYPE_SAFETY.get(segment.road_type, 0.5)
            + w.incidents * (1.0 - clamp01(segment.incident_risk))
            + w.eyes_on_street * clamp01(eyes_on_street(segment.shops_open_until, hour))
        )

    def segment_components(self, network: RoadNetwork, segment: Segment, hour: float) -> Dict[str, float]:
        """The raw sub-scores of a segment - used for transparent reporting."""
        return {
            "lighting": clamp01(segment.lighting),
            "pedestrian": clamp01(interpolate_hourly(segment.hourly_pedestrian, hour)),
            "road_type": ROAD_TYPE_SAFETY.get(segment.road_type, 0.5),
            "incidents": 1.0 - clamp01(segment.incident_risk),
            "eyes_on_street": clamp01(eyes_on_street(segment.shops_open_until, hour)),
        }

    # ----------------------------------------------------------- costs
    def walk_minutes(self, segment: Segment, mode: str = "walk") -> float:
        """Minutes needed to walk (or cycle) along *segment*."""
        if mode == "walk":
            speed_kph = WALK_SPEED_KPH.get(segment.road_type, 4.8)
        elif mode == "bike":
            speed_kph = 13.0
        else:
            raise ValueError(f"unsupported travel mode {mode!r} (use 'walk' or 'bike')")
        return segment.length_m / 1000.0 / speed_kph * 60.0

    def segment_risk_exposure(self, network: RoadNetwork, segment: Segment, hour: float) -> float:
        """"km of unsafe road" carried by a segment - length weighted by (1 - safety)."""
        return (segment.length_m / 1000.0) * (1.0 - self.segment_safety(network, segment, hour))

    def cost_function(self, network: RoadNetwork, hour: float, risk_aversion: Optional[float] = None) -> Callable[[Segment], float]:
        """Build the edge-cost callable used by the shortest-path algorithms.

        ``cost = minutes + lambda * risk_exposure`` with ``lambda = self.risk_aversion``
        unless overridden per query.
        """
        lam = self.risk_aversion if risk_aversion is None else float(risk_aversion)

        def cost(segment: Segment) -> float:
            return self.walk_minutes(segment) + lam * self.segment_risk_exposure(network, segment, hour)

        return cost

    def describe(self) -> str:
        """One-line description of the active scoring configuration."""
        weights = ", ".join(f"{name} {getattr(self.weights, name):.2f}" for name in SafetyWeights.FIELDS)
        return (
            f"weights [{weights}], lambda={self.risk_aversion:.1f} min/km of unsafe road, "
            f"incident buffer {self.incident_buffer_m:.0f} m, half-life "
            f"{self.incident_half_life_days:.0f} d"
        )
