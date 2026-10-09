"""Shortest-path machinery: Dijkstra plus Yen's K-shortest-loopless-paths.

Both algorithms work on :class:`~safe_route.models.RoadNetwork` and take an arbitrary
``cost_fn(segment) -> float`` callable, which is how the safety-weighted edge cost from
:meth:`safe_route.safety.SafetyModel.cost_function` gets plugged in.
"""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass
from typing import Callable, FrozenSet, List, Optional

from .models import NodeID, Pair, RoadNetwork, Segment

CostFn = Callable[[Segment], float]


@dataclass
class PathResult:
    """A path with its total cost."""

    cost: float
    nodes: List[NodeID]


def _pair_cost(network: RoadNetwork, a: NodeID, b: NodeID, cost_fn: CostFn) -> float:
    segment = network.segment_between(a, b)
    return cost_fn(segment) if segment is not None else 0.0


def dijkstra(
    network: RoadNetwork,
    source: NodeID,
    target: NodeID,
    cost_fn: CostFn,
    banned: FrozenSet[Pair] = frozenset(),
) -> Optional[PathResult]:
    """Cheapest path from *source* to *target* with the given edge-cost callable.

    ``banned`` holds ordered endpoint pairs that may not be traversed (used by Yen's
    algorithm to force alternative routes).
    """
    if source not in network.nodes or target not in network.nodes:
        return None
    if source == target:
        return PathResult(0.0, [source])

    dist = {source: 0.0}
    previous: dict = {}
    settled = set()
    heap = [(0.0, source)]

    while heap:
        d, u = heapq.heappop(heap)
        if u in settled:
            continue
        settled.add(u)
        if u == target:
            path = [u]
            while path[-1] != source:
                path.append(previous[path[-1]])
            path.reverse()
            return PathResult(d, path)
        for segment in network.neighbors(u):
            v = segment.other(u)
            if v in settled or segment.key in banned:
                continue
            candidate = d + cost_fn(segment)
            if candidate < dist.get(v, float("inf")):
                dist[v] = candidate
                previous[v] = u
                heapq.heappush(heap, (candidate, v))
    return None


def yen_k_shortest(
    network: RoadNetwork,
    source: NodeID,
    target: NodeID,
    cost_fn: CostFn,
    k: int = 3,
) -> List[PathResult]:
    """The *k* cheapest loopless paths from *source* to *target*, cheapest first.

    Yen's algorithm: repeatedly take the previous best path, remove ("spur") one of its
    edges at a time, forbid revisiting the already-fixed root nodes, and re-run Dijkstra;
    the best completed candidate overall is promoted next.
    """
    if k <= 0:
        return []

    first = dijkstra(network, source, target, cost_fn)
    if first is None:
        return []

    a_list = [first]                 # accepted paths, cheapest first
    in_a = {tuple(first.nodes)}
    b_heap: list = []                # candidate pool: (cost, counter, nodes)
    in_b: set = set()
    counter = itertools.count()

    for _ in range(1, k):
        previous_path = a_list[-1].nodes
        for i in range(len(previous_path) - 1):
            spur_node = previous_path[i]
            root_path = previous_path[: i + 1]

            banned: set = set()
            # 1. forbid the edges that produced identical roots in accepted paths
            for result in a_list:
                nodes = result.nodes
                if len(nodes) > i and nodes[: i + 1] == root_path:
                    segment = network.segment_between(nodes[i], nodes[i + 1])
                    if segment is not None:
                        banned.add(segment.key)
            # 2. keep paths loopless: root nodes may not be re-entered
            root_nodes = set(root_path)
            for root_node in root_nodes:
                for segment in network.neighbors(root_node):
                    if segment.other(root_node) in root_nodes:
                        banned.add(segment.key)

            spur = dijkstra(network, spur_node, target, cost_fn, banned=frozenset(banned))
            if spur is None:
                continue

            total_nodes = root_path[:-1] + spur.nodes
            total_key = tuple(total_nodes)
            if total_key in in_a or total_key in in_b:
                continue

            root_cost = sum(
                _pair_cost(network, a, b, cost_fn) for a, b in zip(root_path[:-1], root_path[1:])
            )
            heapq.heappush(b_heap, (root_cost + spur.cost, next(counter), total_nodes))
            in_b.add(total_key)

        promoted = False
        while b_heap:
            cost, _, nodes = heapq.heappop(b_heap)
            if tuple(nodes) in in_a:
                continue
            a_list.append(PathResult(cost, nodes))
            in_a.add(tuple(nodes))
            promoted = True
            break
        if not promoted:
            break

    return a_list
