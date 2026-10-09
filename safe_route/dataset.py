"""City datasets for the routing engine.

* :func:`build_demo_city` - a deterministic, Jaipur-inspired synthetic street network
  (seeded RNG, so runs are reproducible) with realistic spatial structure: a lit, busy
  old-city core, darker outskirts, and corner-cutting alleys that are *shorter* but much
  less safe - exactly the trade-off the problem statement describes.
* :func:`load_city` - load a real city from a JSON file (nodes / segments / incidents),
  so the same engine can run on an OpenStreetMap extract, MCGM/municipal data, etc.

Custom JSON schema (all distances in metres, planar x/y coordinates)::

    {
      "name": "My city",
      "nodes":      [{"id": "n1", "name": "Hawa Mahal", "x": 0, "y": 0}, ...],
      "segments":   [{"u": "n1", "v": "n2", "road_type": "collector",
                      "lighting": 0.8, "shops_open_until": 22,
                      "hourly_pedestrian": [24 floats 0..1]}],
      "incidents":  [{"x": 40, "y": 20, "date": "2026-01-15", "severity": 4,
                      "kind": "poor lighting"}]
    }
"""

from __future__ import annotations

import json
import math
import random
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Tuple

from .models import RoadNetwork

#: Demo grid geometry and generation parameters.
GRID_COLS = 7
GRID_ROWS = 6
GRID_SPACING_M = 170.0
SEED = 20260214
MISSING_LINK_FRACTION = 0.10   # fraction of grid edges omitted -> irregular street pattern
ALLEY_PROBABILITY = 0.30       # chance that a corner cuts with a dark alley
INCIDENT_COUNT = 26

#: Relative hourly footfall curve (index = hour of day). Main streets stay lively until
#: well past midnight while residential lanes empty out; this is what makes a "busy
#: street" a safe street at 2 a.m.
NIGHT_CURVE: List[float] = [
    0.20, 0.14, 0.10, 0.07, 0.06, 0.08, 0.14, 0.24, 0.36, 0.50, 0.58, 0.62,  # 00-11
    0.66, 0.62, 0.60, 0.64, 0.70, 0.78, 0.86, 0.80, 0.70, 0.62, 0.52, 0.34,  # 12-23
]

#: 42 named Jaipur localities for the 7x6 demo grid.
CITY_NAMES: List[str] = [
    "Hawa Mahal", "Bari Chaupar", "Tripolia Bazaar", "Chandpole", "Johari Bazaar",
    "Nehru Bazaar", "MI Road", "Surajpol", "Kishanpole", "Gangauri", "Jaipur Junction",
    "Statue Circle", "Ajmeri Gate", "Sanganeri Gate", "Ghat Gate", "Zorawar Singh Gate",
    "New Gate", "Pink City Square", "Sindhi Camp", "Brahmpuri", "Amer Road",
    "Bani Park", "Civil Lines", "Adarsh Nagar", "Raja Park", "Jawahar Circle",
    "Tonk Road", "C-Scheme", "Malviya Nagar", "Shastri Nagar", "JLN Marg",
    "Subhash Nagar", "Vidyadhar Nagar", "Jhotwara", "Khatipura", "Ramgarh Mod",
    "Devi Marg", "Jagatpura", "Mansarovar", "Durgapura", "Pratap Nagar", "Sitapura",
]

INCIDENT_KINDS: List[str] = [
    "poor lighting", "snatching", "harassment", "unlit stretch",
    "accident", "suspicious activity",
]


def _is_connected(net: RoadNetwork) -> bool:
    """True if every junction can be reached from any other one (iterative DFS)."""
    if not net.nodes:
        return False
    start = next(iter(net.nodes))
    seen = {start}
    stack = [start]
    while stack:
        current = stack.pop()
        for segment in net.neighbors(current):
            neighbour = segment.other(current)
            if neighbour not in seen:
                seen.add(neighbour)
                stack.append(neighbour)
    return len(seen) == len(net.nodes)


def build_demo_city(seed: int = SEED) -> RoadNetwork:
    """Build the deterministic demo network around Jaipur's old city."""
    rng = random.Random(seed)
    net = RoadNetwork(name="Demo city (Jaipur-inspired synthetic street grid)")

    # ---------------------------------------------------------------- nodes
    cell: Dict[str, Tuple[int, int]] = {}
    for r in range(GRID_ROWS):
        for c in range(GRID_COLS):
            node_id = f"n{r:02d}_{c:02d}"
            x = c * GRID_SPACING_M + rng.uniform(-20.0, 20.0)
            y = r * GRID_SPACING_M + rng.uniform(-20.0, 20.0)
            net.add_node(node_id, CITY_NAMES[r * GRID_COLS + c], x, y)
            cell[node_id] = (r, c)

    center_r, center_c = (GRID_ROWS - 1) / 2.0, (GRID_COLS - 1) / 2.0

    def centrality(node_id: str) -> float:
        """Distance (in grid cells) from the lit, busy city centre."""
        r, c = cell[node_id]
        return math.hypot(c - center_c, r - center_r)

    # ------------------------------------------------- regular street grid
    grid_edges: List[Tuple[str, str]] = []
    for r in range(GRID_ROWS):
        for c in range(GRID_COLS):
            if c + 1 < GRID_COLS:
                grid_edges.append((f"n{r:02d}_{c:02d}", f"n{r:02d}_{c + 1:02d}"))
            if r + 1 < GRID_ROWS:
                grid_edges.append((f"n{r:02d}_{c:02d}", f"n{r + 1:02d}_{c:02d}"))

    degree = {node_id: 0 for node_id in cell}
    for u, v in grid_edges:
        degree[u] += 1
        degree[v] += 1

    kept: List[Tuple[str, str]] = []
    dropped: List[Tuple[str, str]] = []
    for u, v in grid_edges:
        # only remove an edge if both endpoints keep degree > 2, so nothing starves
        if rng.random() < MISSING_LINK_FRACTION and degree[u] > 2 and degree[v] > 2:
            degree[u] -= 1
            degree[v] -= 1
            dropped.append((u, v))
            continue
        kept.append((u, v))

    def add_grid_street(u: str, v: str) -> None:
        """Give a regular grid edge realistic lighting / footfall / closing times."""
        r, c = cell[u]
        if (r == GRID_ROWS // 2 or c == GRID_COLS // 2) and rng.random() < 0.6:
            road_type = "arterial"
        else:
            road_type = rng.choices(
                ["collector", "local", "footpath"], weights=[35, 45, 20]
            )[0]
        d = max(centrality(u), centrality(v))
        lighting = min(0.99, max(0.08, 0.95 - 0.15 * d + rng.uniform(-0.10, 0.10)))
        if road_type == "arterial":
            lighting = min(0.99, lighting + 0.10)
        footfall = min(1.0, max(0.05, 1.00 - 0.13 * d + rng.uniform(-0.10, 0.10)))
        # late-night eateries / 24h outlets cluster on the main roads
        if road_type == "arterial" and rng.random() < 0.4:
            shops = int(rng.choice([24, 2, 3]))
        else:
            shops = int(rng.choice([20, 21, 21, 22, 22, 23]))
        net.add_segment(
            u, v, road_type,
            lighting=lighting,
            hourly_pedestrian=[round(footfall * value, 3) for value in NIGHT_CURVE],
            shops_open_until=shops,
        )

    for u, v in kept:
        add_grid_street(u, v)

    # ------------------------------------------------------- dark shortcuts
    alley_edges: List[Tuple[str, str]] = []
    for r in range(GRID_ROWS - 1):
        for c in range(GRID_COLS - 1):
            if rng.random() < ALLEY_PROBABILITY:
                alley_edges.append((f"n{r:02d}_{c:02d}", f"n{r + 1:02d}_{c + 1:02d}"))

    for u, v in alley_edges:
        d = max(centrality(u), centrality(v))
        lighting = rng.uniform(0.06, 0.10 + 0.14 * max(0.0, 3.0 - d))
        footfall = rng.uniform(0.02, 0.12)
        a, b = net.nodes[u], net.nodes[v]
        length_m = 0.62 * math.hypot(a.x - b.x, a.y - b.y)  # shortcuts *are* shorter
        net.add_segment(
            u, v, "alley",
            lighting=lighting,
            hourly_pedestrian=[round(footfall * value, 3) for value in NIGHT_CURVE],
            shops_open_until=0,
            length_m=length_m,
        )

    # Keep the network routable: if dropping links fragmented it, add some back.
    net.finalize()
    for u, v in dropped:
        if _is_connected(net):
            break
        add_grid_street(u, v)
        net.finalize()

    # ---------------------------------------------------------- incidents
    hot_zones = [(1, 4), (4, 1), (0, 5), (5, 0)]  # dark corners of the grid
    for i in range(1, INCIDENT_COUNT + 1):
        if rng.random() < 0.65:
            hr, hc = rng.choice(hot_zones)
            x = (hc + rng.uniform(-0.8, 0.8)) * GRID_SPACING_M
            y = (hr + rng.uniform(-0.8, 0.8)) * GRID_SPACING_M
        else:
            x = rng.uniform(0.0, (GRID_COLS - 1) * GRID_SPACING_M)
            y = rng.uniform(0.0, (GRID_ROWS - 1) * GRID_SPACING_M)
        day = date.today() - timedelta(days=rng.randint(1, 240))
        severity = rng.choices([1, 2, 3, 4, 5], weights=[15, 25, 30, 20, 10])[0]
        net.add_incident(
            x, y, day, severity,
            INCIDENT_KINDS[rng.randrange(len(INCIDENT_KINDS))],
            incident_id=f"I{i:02d}",
        )

    net.finalize()
    return net


def load_city(path: str) -> RoadNetwork:
    """Load a city from a JSON file (see the module docstring for the schema)."""
    file_path = Path(path)
    data = json.loads(file_path.read_text(encoding="utf-8"))
    net = RoadNetwork(name=data.get("name", file_path.stem))

    for node in data["nodes"]:
        net.add_node(node["id"], node.get("name", node["id"]), float(node["x"]), float(node["y"]))

    for segment in data["segments"]:
        hourly = segment.get("hourly_pedestrian")
        if not hourly:
            hourly = [float(segment.get("pedestrian_activity", 0.3))] * 24
        net.add_segment(
            segment["u"],
            segment["v"],
            segment.get("road_type", "collector"),
            lighting=float(segment.get("lighting", 0.5)),
            hourly_pedestrian=[float(value) for value in hourly],
            shops_open_until=int(segment.get("shops_open_until", 21)),
            length_m=segment.get("length_m"),
        )

    for incident in data.get("incidents", []):
        day = incident["date"]
        if isinstance(day, str):
            day = date.fromisoformat(day)
        net.add_incident(
            float(incident["x"]),
            float(incident["y"]),
            day,
            int(incident.get("severity", 3)),
            incident.get("kind", "incident"),
            incident.get("id"),
        )

    net.finalize()
    return net
