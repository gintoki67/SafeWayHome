"""Optional map rendering for the candidate routes.

``render_map`` writes an interactive HTML map with `folium` if it is installed,
otherwise a static PNG with `matplotlib`; both are optional dependencies and are
imported lazily so the core engine stays stdlib-only.
"""

from __future__ import annotations

import math
from typing import List, Sequence, Tuple

from .models import RoadNetwork

#: Jaipur city centre - demo network coordinates are expressed relative to this point.
ORIGIN_LAT = 26.9124
ORIGIN_LON = 75.7873

#: One colour per candidate route.
PALETTE = ["#d62728", "#2ca02c", "#1f77b4", "#9467bd", "#ff7f0e", "#17becf"]


def to_latlon(x: float, y: float) -> Tuple[float, float]:
    """Convert local planar metres to (lat, lon) near Jaipur city centre."""
    lat = ORIGIN_LAT + y / 111_320.0
    lon = ORIGIN_LON + x / (111_320.0 * math.cos(math.radians(ORIGIN_LAT)))
    return lat, lon


def _route_points(network: RoadNetwork, nodes: Sequence[str]) -> List[Tuple[float, float]]:
    return [to_latlon(network.nodes[n].x, network.nodes[n].y) for n in nodes]


def render_map(
    network: RoadNetwork,
    routes: Sequence[Tuple[str, List[str]]],
    out_path: str,
    incidents: bool = True,
) -> str:
    """Render *routes* (label, node list) to *out_path*; returns the renderer used.

    Raises ``ImportError`` if neither folium nor matplotlib is available.
    """
    try:
        return write_folium_map(network, routes, out_path, incidents)
    except ImportError:
        return write_matplotlib_map(network, routes, out_path, incidents)


def write_folium_map(
    network: RoadNetwork,
    routes: Sequence[Tuple[str, List[str]]],
    out_path: str,
    incidents: bool = True,
) -> str:
    """Interactive HTML map (needs ``folium``)."""
    import folium  # optional dependency, imported lazily

    coords = [
        to_latlon(node.x, node.y) for node in network.nodes.values()
    ]
    center = [sum(c[0] for c in coords) / len(coords), sum(c[1] for c in coords) / len(coords)]
    fmap = folium.Map(location=center, zoom_start=15, tiles="OpenStreetMap")

    for i, (label, nodes) in enumerate(routes):
        color = PALETTE[i % len(PALETTE)]
        points = _route_points(network, nodes)
        folium.PolyLine(points, color=color, weight=6, opacity=0.85, tooltip=label).add_to(fmap)
        folium.CircleMarker(
            points[0], radius=8, color="black", fill=True, fill_color=color,
            tooltip=f"start: {label}",
        ).add_to(fmap)
        folium.CircleMarker(
            points[-1], radius=10, color="black", fill=True, fill_color=color,
            fill_opacity=1.0, tooltip=f"end: {label}",
        ).add_to(fmap)

    if incidents:
        for inc in network.incidents:
            lat, lon = to_latlon(inc.x, inc.y)
            folium.CircleMarker(
                [lat, lon], radius=3, color="#8b0000", fill=True, fill_opacity=0.7,
                tooltip=f"{inc.kind} (severity {inc.severity})",
            ).add_to(fmap)

    fmap.fit_bounds(fmap.get_bounds())
    fmap.save(out_path)
    return "folium"


def write_matplotlib_map(
    network: RoadNetwork,
    routes: Sequence[Tuple[str, List[str]]],
    out_path: str,
    incidents: bool = True,
) -> str:
    """Static PNG map (needs ``matplotlib``)."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 8))
    for segment in network.segments:
        a = network.nodes[segment.u]
        b = network.nodes[segment.v]
        ax.plot([a.x, b.x], [a.y, b.y], color="#c7c7c7", lw=1.2, zorder=1)

    if incidents:
        ax.scatter(
            [i.x for i in network.incidents],
            [i.y for i in network.incidents],
            marker="x", color="#8b0000", s=28, zorder=2, label="incidents",
        )

    for i, (label, nodes) in enumerate(routes):
        xs = [network.nodes[n].x for n in nodes]
        ys = [network.nodes[n].y for n in nodes]
        ax.plot(xs, ys, color=PALETTE[i % len(PALETTE)], lw=2.6, zorder=3, label=label)

    ax.set_title("Safe Route Finder - candidate night routes")
    ax.set_xlabel("x (m)")
    ax.set_ylabel("y (m)")
    ax.set_aspect("equal", adjustable="box")
    ax.legend(loc="best", fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return "matplotlib"
