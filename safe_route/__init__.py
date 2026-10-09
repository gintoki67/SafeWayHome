"""OFFGRID Tech Ideate (Manipal University Jaipur / IEEE Student Branch) - Problem 1.

Safe Route Finder for Night Travel
----------------------------------
A data-driven routing system that *identifies*, *evaluates* and *compares* walking
routes using multiple public-safety parameters - street lighting, pedestrian activity,
road type and reported incidents - instead of distance or travel time alone.

Quick start::

    python -m safe_route --list
    python -m safe_route --from "Hawa Mahal" --to "Jaipur Junction" --time 23:30
"""

from .models import Incident, Node, RoadNetwork, Segment
from .routing import PathResult, dijkstra, yen_k_shortest
from .safety import PROFILES, SafetyModel, SafetyWeights

__all__ = [
    "Incident",
    "Node",
    "RoadNetwork",
    "Segment",
    "PathResult",
    "dijkstra",
    "yen_k_shortest",
    "SafetyModel",
    "SafetyWeights",
    "PROFILES",
]

__version__ = "1.0.0"
