"""Command-line interface for the Safe Route Finder.

Examples::

    python -m safe_route --list
    python -m safe_route --from "Hawa Mahal" --to "Jaipur Junction" --time 23:30
    python -m safe_route --from "Bari Chaupar" --to "Sitapura" --profile lighting_led --risk 40 --k 5
    python -m safe_route --from X --to Y --map route.html --json report.json --sensitivity
    python -m safe_route --data my_city.json --from A --to B
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from typing import List, Optional

from . import dataset
from .analysis import (
    annotate,
    assess_route,
    recommend,
    render_report,
    render_sensitivity,
    sensitivity,
    write_json_report,
)
from .routing import yen_k_shortest
from .safety import PROFILES, SafetyModel
from .visualize import render_map


def parse_hour(text: Optional[str]) -> float:
    """Parse ``HH:MM`` (or ``HH``) into a fractional hour of day; None means "now"."""
    if not text:
        now = datetime.now()
        return now.hour + now.minute / 60.0
    parts = text.strip().split(":")
    if len(parts) == 1:
        return float(parts[0]) % 24.0
    return (int(parts[0]) + int(parts[1]) / 60.0) % 24.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="safe-route",
        description="OFFGRID Problem 1 - Safe Route Finder for Night Travel",
    )
    parser.add_argument("--from", dest="origin", help="origin location name (see --list)")
    parser.add_argument("--to", dest="destination", help="destination location name")
    parser.add_argument("--time", dest="departure", default=None,
                        help="departure time HH:MM (default: now)")
    parser.add_argument("--profile", default="balanced", choices=sorted(PROFILES),
                        help="safety weight profile (default: balanced)")
    parser.add_argument("--risk", type=float, default=25.0,
                        help="risk aversion lambda: extra minutes per km of fully unsafe road")
    parser.add_argument("--k", type=int, default=3,
                        help="number of candidate routes to compare (default: 3)")
    parser.add_argument("--max-extra", type=float, default=10.0,
                        help="extra walking minutes tolerated when recommending the safe route")
    parser.add_argument("--list", action="store_true", help="list known locations and exit")
    parser.add_argument("--data", default=None,
                        help="path to a custom city JSON dataset instead of the demo city")
    parser.add_argument("--seed", type=int, default=dataset.SEED, help="seed of the demo dataset")
    parser.add_argument("--map", dest="map_path", default=None,
                        help="write a route map to this file (.html or .png)")
    parser.add_argument("--json", dest="json_path", default=None,
                        help="write a machine-readable report to this file")
    parser.add_argument("--sensitivity", action="store_true",
                        help="also show how the ranking changes under other weight profiles")
    parser.add_argument("--quiet", action="store_true",
                        help="print only the recommendation")
    return parser


def resolve_location(network, text: str):
    """Turn a user-supplied location name into a node (with helpful errors)."""
    node = network.find(text)
    if node is not None:
        return node
    suggestions = network.suggestions(text)
    hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else \
        " Use --list to see all locations."
    raise SystemExit(f"Unknown location {text!r}.{hint}")


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    network = dataset.load_city(args.data) if args.data else dataset.build_demo_city(seed=args.seed)

    model = SafetyModel(weights=PROFILES[args.profile], risk_aversion=args.risk)
    model.bind(network)

    if args.list:
        for node in sorted(network.nodes.values(), key=lambda n: n.name):
            print(f"  {node.name}")
        return 0

    if not args.origin or not args.destination:
        print("error: --from and --to are required (or use --list)", file=sys.stderr)
        return 2

    origin = resolve_location(network, args.origin)
    destination = resolve_location(network, args.destination)
    hour = parse_hour(args.departure)

    cost_fn = model.cost_function(network, hour)
    results = yen_k_shortest(network, origin.id, destination.id, cost_fn, k=max(1, args.k))
    if not results:
        print(f"No route found between {origin.name} and {destination.name}.")
        return 1

    assessments = [assess_route(network, model, result, hour) for result in results]
    annotate(assessments)
    recommended = recommend(assessments, max_extra_minutes=args.max_extra)

    if args.quiet:
        print(
            f"Recommended route: {recommended.label} "
            f"({recommended.distance_m / 1000:.2f} km, {recommended.travel_min:.1f} min, "
            f"safety {100 * recommended.mean_safety:.0f}%)"
        )
    else:
        print(render_report(network, model, assessments, recommended, hour,
                            origin.name, destination.name))
        if args.sensitivity:
            labels = [a.label for a in assessments]
            rows = sensitivity(network, results, hour, model, labels)
            print()
            print(render_sensitivity(rows, labels))

    if args.json_path:
        write_json_report(
            args.json_path, network, model, assessments, recommended, hour,
            extra={"origin": origin.name, "destination": destination.name},
        )
        print(f"\nJSON report written to {args.json_path}")

    if args.map_path:
        try:
            writer = render_map(network, [(a.label, a.nodes) for a in assessments], args.map_path)
        except ImportError:
            print("Map not written: install folium (pip install folium) or matplotlib.",
                  file=sys.stderr)
        else:
            print(f"Map written to {args.map_path} (via {writer})")

    return 0
