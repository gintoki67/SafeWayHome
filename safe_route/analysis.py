"""Route comparison, metrics, recommendations and report rendering.

This module answers the "identifies, evaluates and *compares*" part of the problem
statement: every candidate route is scored on the safety parameters, the routes are
labelled (Fastest / Safest / Alternative) and a recommendation is produced with the
explicit trade-offs (extra minutes vs. worst-segment safety, unlit share, ...).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Sequence

from .models import RoadNetwork, interpolate_hourly
from .routing import PathResult
from .safety import LIT_THRESHOLD, PROFILES, UNSAFE_THRESHOLD, SafetyModel, SafetyWeights


@dataclass
class RouteAssessment:
    """Everything we know about one candidate route."""

    label: str
    nodes: List[str]
    distance_m: float
    travel_min: float
    mean_safety: float      # length-weighted average safety score
    min_safety: float       # weakest segment on the route (a route is only as safe...)
    unsafe_share: float     # fraction of the route's length below the unsafe threshold
    lit_share: float        # fraction of the route's length that is well lit
    mean_pedestrian: float  # average footfall along the route
    incident_exposure: float  # km of road weighted by incident risk
    worst_segment: str = ""
    extra_min: float = 0.0  # extra walking minutes vs the fastest route


def assess_route(
    network: RoadNetwork,
    model: SafetyModel,
    result: PathResult,
    hour: float,
    label: str = "route",
) -> RouteAssessment:
    """Score a single path with the safety *model* at the given *hour*."""
    if len(result.nodes) < 2:
        raise ValueError("a route needs at least two nodes")

    total_m = 0.0
    safety_sum = 0.0
    min_safety = 1.0
    unsafe_m = 0.0
    lit_m = 0.0
    footfall_sum = 0.0
    incident_exposure = 0.0
    minutes = 0.0
    worst = ""

    for a, b in zip(result.nodes, result.nodes[1:]):
        segment = network.segment_between(a, b)
        if segment is None:
            continue
        safety = model.segment_safety(network, segment, hour)
        length_m = segment.length_m
        total_m += length_m
        safety_sum += safety * length_m
        minutes += model.walk_minutes(segment)
        if safety < min_safety:
            min_safety = safety
            worst = network.label(segment)
        if safety < UNSAFE_THRESHOLD:
            unsafe_m += length_m
        if segment.lighting >= LIT_THRESHOLD:
            lit_m += length_m
        footfall_sum += interpolate_hourly(segment.hourly_pedestrian, hour) * length_m
        incident_exposure += (length_m / 1000.0) * segment.incident_risk

    return RouteAssessment(
        label=label,
        nodes=list(result.nodes),
        distance_m=total_m,
        travel_min=minutes,
        mean_safety=safety_sum / total_m if total_m else 0.0,
        min_safety=min_safety,
        unsafe_share=unsafe_m / total_m if total_m else 0.0,
        lit_share=lit_m / total_m if total_m else 0.0,
        mean_pedestrian=footfall_sum / total_m if total_m else 0.0,
        incident_exposure=incident_exposure,
        worst_segment=worst,
    )


def annotate(assessments: List[RouteAssessment]) -> None:
    """Label routes as Fastest / Safest / Alt n and fill in ``extra_min`` (in place)."""
    if not assessments:
        return
    fastest_idx = min(range(len(assessments)), key=lambda i: assessments[i].travel_min)
    safest_idx = max(
        range(len(assessments)),
        key=lambda i: (round(assessments[i].min_safety, 6), round(assessments[i].mean_safety, 6)),
    )
    base_minutes = assessments[fastest_idx].travel_min
    alt_count = 0
    for i, assessment in enumerate(assessments):
        tags = []
        if i == fastest_idx:
            tags.append("Fastest")
        if i == safest_idx and i != fastest_idx:
            tags.append("Safest")
        if not tags:
            alt_count += 1
            tags.append(f"Alt {alt_count}")
        assessment.label = " & ".join(tags)
        assessment.extra_min = assessment.travel_min - base_minutes


def recommend(
    assessments: List[RouteAssessment], max_extra_minutes: float = 10.0
) -> RouteAssessment:
    """Pick the safest route that is no more than *max_extra_minutes* slower than the fastest.

    If even the safest route exceeds the time budget, the safest of all candidates is
    returned anyway - the trade-off then belongs to the traveller.
    """
    if not assessments:
        raise ValueError("no routes to recommend")
    base = min(a.travel_min for a in assessments)
    pool = [a for a in assessments if a.travel_min - base <= max_extra_minutes]
    if not pool:
        pool = list(assessments)
    return max(
        pool,
        key=lambda a: (round(a.min_safety, 6), round(a.mean_safety, 6), -a.extra_min),
    )


def recommendation_sentence(
    recommended: RouteAssessment, assessments: List[RouteAssessment]
) -> str:
    """Human-readable justification for the recommendation."""
    fastest = min(a.travel_min for a in assessments)
    extra = recommended.travel_min - fastest
    alternatives = [a for a in assessments if a is not recommended]
    # the most dangerous alternative: lowest worst-segment safety
    riskiest = min(
        alternatives, key=lambda a: (a.min_safety, a.mean_safety), default=None
    )
    parts = [f"Take '{recommended.label}'"]
    if extra <= 0.01:
        parts.append("it is also the fastest of the candidates")
    else:
        parts.append(f"only {extra:.1f} min slower than the fastest candidate")
    if riskiest is not None:
        parts.append(
            f"worst-segment safety {100 * recommended.min_safety:.0f}% vs "
            f"{100 * riskiest.min_safety:.0f}%"
        )
        parts.append(
            f"unsafe-road exposure {recommended.incident_exposure:.2f} vs "
            f"{riskiest.incident_exposure:.2f} km*risk"
        )
    return ": " + ", ".join(parts) + "."


# --------------------------------------------------------------------- reports
def format_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[object]],
    aligns: Optional[Sequence[str]] = None,
) -> str:
    """Small ASCII table formatter (kept dependency-free)."""
    cells = [["" if value is None else str(value) for value in row] for row in rows]
    aligns = list(aligns or ["left"] * len(headers))
    widths = []
    for i in range(len(headers)):
        width = len(headers[i])
        for row in cells:
            width = max(width, len(row[i]))
        widths.append(width)

    def render(row: Sequence[str]) -> str:
        padded = []
        for i, value in enumerate(row):
            padded.append(value.rjust(widths[i]) if aligns[i] == "right" else value.ljust(widths[i]))
        return "| " + " | ".join(padded) + " |"

    separator = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    lines = [separator, render(list(headers)), separator]
    lines.extend(render(row) for row in cells)
    lines.append(separator)
    return "\n".join(lines)


def format_hour(hour: float) -> str:
    """Render a fractional hour as ``HH:MM``."""
    total = int(round(hour * 60)) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def render_report(
    network: RoadNetwork,
    model: SafetyModel,
    assessments: List[RouteAssessment],
    recommended: RouteAssessment,
    hour: float,
    origin_name: str,
    destination_name: str,
) -> str:
    """The full CLI comparison report as a string."""
    weights = ", ".join(
        f"{name} {getattr(model.weights, name):.2f}" for name in SafetyWeights.FIELDS
    )
    lines = [
        "=" * 78,
        "SAFE ROUTE FINDER - night-time route comparison (OFFGRID Problem 1)",
        "=" * 78,
        f"Route          : {origin_name}  ->  {destination_name}",
        f"Departure      : {format_hour(hour)}",
        f"Network        : {network.name} ({network.stats()})",
        f"Weight profile : {weights}",
        f"Risk aversion  : lambda = {model.risk_aversion:.1f} min per km of fully unsafe road",
        "",
        f"{len(assessments)} candidate routes compared:",
        "",
        format_table(
            headers=["#", "Route", "Distance", "Walk", "Safety", "Worst", "Unlit", "Lit", "Footfall", "RiskExp"],
            aligns=["right", "left", "right", "right", "right", "right", "right", "right", "right", "right"],
            rows=[
                [
                    i,
                    a.label,
                    f"{a.distance_m / 1000:.2f} km",
                    f"{a.travel_min:.1f} min",
                    f"{100 * a.mean_safety:.0f}%",
                    f"{100 * a.min_safety:.0f}%",
                    f"{100 * a.unsafe_share:.0f}%",
                    f"{100 * a.lit_share:.0f}%",
                    f"{a.mean_pedestrian:.2f}",
                    f"{a.incident_exposure:.2f}",
                ]
                for i, a in enumerate(assessments, start=1)
            ],
        ),
        "",
        "Safety = weighted blend of lighting / footfall / road type / incidents / eyes-on-street.",
        "Worst  = weakest single segment on the route.",
        "Unlit  = share of the route scoring below 40% safety;  Lit = share with lighting >= 60%.",
        "RiskExp= km of route weighted by nearby incident risk.",
        "",
        "Worst segments per route:",
    ]
    for i, assessment in enumerate(assessments, start=1):
        lines.append(f"  {i}. [{assessment.label}] {assessment.worst_segment}")
    lines += ["", "Recommendation" + recommendation_sentence(recommended, assessments)]
    return "\n".join(lines)


def sensitivity(
    network: RoadNetwork,
    results: Sequence[PathResult],
    hour: float,
    base_model: SafetyModel,
    labels: Sequence[str],
    profiles: Optional[Dict[str, SafetyWeights]] = None,
) -> List[Dict[str, object]]:
    """How does the ranking change if the parameter weights change?

    Each candidate route is re-scored under every preset weight profile.  The per-segment
    incident risk is *not* recomputed: incidents are data, only their weight changes.
    """
    profiles = profiles or PROFILES
    rows: List[Dict[str, object]] = []
    for name, weights in profiles.items():
        model = SafetyModel(weights=weights, risk_aversion=base_model.risk_aversion)
        means, mins = [], []
        for result in results:
            assessment = assess_route(network, model, result, hour)
            means.append(assessment.mean_safety)
            mins.append(assessment.min_safety)
        best = max(range(len(results)), key=lambda i: (round(mins[i], 6), round(means[i], 6)))
        rows.append(
            {
                "profile": name,
                "winner": labels[best] if best < len(labels) else f"route {best + 1}",
                "mean_safety": means,
                "min_safety": mins,
            }
        )
    return rows


def render_sensitivity(rows: Sequence[Dict[str, object]], labels: Sequence[str]) -> str:
    """Table of the sensitivity analysis (mean/min safety per route, per profile)."""
    table_rows = []
    for row in rows:
        means = row["mean_safety"]
        mins = row["min_safety"]
        table_rows.append(
            [row["profile"], row["winner"]]
            + [f"{100 * m:.0f}%/{100 * s:.0f}%" for m, s in zip(means, mins)]
        )
    lines = [
        "Sensitivity of the ranking to the parameter weights (mean/min safety per route):",
        "",
        format_table(["Weight profile", "Preferred"] + list(labels), table_rows),
        "",
        "Read 84%/61% as mean safety 84%, weakest segment 61%.",
    ]
    return "\n".join(lines)


def write_json_report(
    path: str,
    network: RoadNetwork,
    model: SafetyModel,
    assessments: Sequence[RouteAssessment],
    recommended: RouteAssessment,
    hour: float,
    extra: Optional[Dict[str, object]] = None,
) -> None:
    """Persist a machine-readable report of the comparison."""
    payload: Dict[str, object] = {
        "problem": "OFFGRID Problem 1 - Safe Route Finder for Night Travel",
        "departure": format_hour(hour),
        "network": {
            "name": network.name,
            "junctions": len(network.nodes),
            "segments": len(network.segments),
            "incidents": len(network.incidents),
        },
        "weights": model.weights.as_dict(),
        "risk_aversion_min_per_unsafe_km": model.risk_aversion,
        "routes": [asdict(a) for a in assessments],
        "recommended": recommended.label,
    }
    if extra:
        payload.update(extra)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, default=str)
