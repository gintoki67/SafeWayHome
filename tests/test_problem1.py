"""Tests for the Safe Route Finder (OFFGRID Problem 1). Run with::

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest
from datetime import date

from safe_route import dataset
from safe_route.analysis import annotate, assess_route, recommend
from safe_route.models import RoadNetwork
from safe_route.routing import dijkstra, yen_k_shortest
from safe_route.safety import PROFILES, SafetyModel


def line_network() -> RoadNetwork:
    net = RoadNetwork("test-line")
    net.add_node("a", "A", 0, 0)
    net.add_node("b", "B", 100, 0)
    net.add_node("c", "C", 200, 0)
    net.add_segment("a", "b", "collector", lighting=0.9)
    net.add_segment("b", "c", "collector", lighting=0.9)
    net.finalize()
    return net


def diamond_network() -> RoadNetwork:
    """Two s->t routes: a short dark alley and a longer lit, busy street."""
    net = RoadNetwork("test-diamond")
    net.add_node("s", "S", 0, 0)
    net.add_node("m1", "M1", 60, -10)   # alley corner (short but dark)
    net.add_node("m2", "M2", 50, -50)  # street corner (longer but safe)
    net.add_node("t", "T", 100, 0)
    net.add_segment("s", "m1", "alley", lighting=0.10)
    net.add_segment("m1", "t", "alley", lighting=0.10)
    net.add_segment("s", "m2", "collector", lighting=0.95)
    net.add_segment("m2", "t", "collector", lighting=0.95)
    net.finalize()
    return net


class DijkstraTests(unittest.TestCase):
    def test_shortest_path_on_a_line(self):
        net = line_network()
        result = dijkstra(net, "a", "c", lambda seg: seg.length_m)
        self.assertEqual(result.nodes, ["a", "b", "c"])
        self.assertAlmostEqual(result.cost, 200.0)

    def test_no_route_between_isolated_nodes(self):
        net = RoadNetwork("test-isolated")
        net.add_node("x", "X", 0, 0)
        net.add_node("y", "Y", 1, 1)
        net.finalize()
        self.assertIsNone(dijkstra(net, "x", "y", lambda seg: 1.0))

    def test_unknown_node_returns_none(self):
        net = line_network()
        self.assertIsNone(dijkstra(net, "a", "zzz", lambda seg: seg.length_m))


class YenTests(unittest.TestCase):
    def test_k_shortest_paths_are_distinct_and_ordered(self):
        net = diamond_network()
        model = SafetyModel(weights=PROFILES["balanced"])
        model.bind(net)
        cost_fn = model.cost_function(net, 23.0)
        results = yen_k_shortest(net, "s", "t", cost_fn, k=5)
        # a diamond graph only has two loopless s->t routes
        self.assertEqual(len(results), 2)
        self.assertLessEqual(results[0].cost, results[1].cost)
        self.assertNotEqual(results[0].nodes, results[1].nodes)

    def test_zero_k_returns_nothing(self):
        net = line_network()
        self.assertEqual(yen_k_shortest(net, "a", "c", lambda s: s.length_m, k=0), [])


class SafetyModelTests(unittest.TestCase):
    def test_dark_alley_scores_far_below_lit_street_at_night(self):
        net = diamond_network()
        model = SafetyModel()
        model.bind(net)
        alley = net.segment_between("s", "m1")
        street = net.segment_between("s", "m2")
        self.assertLess(
            model.segment_safety(net, alley, 23.5),
            model.segment_safety(net, street, 23.5),
        )

    def test_recent_incident_matters_more_than_old_one(self):
        def risk_on_date(incident_date: date) -> float:
            net = line_network()
            model = SafetyModel(reference_date=date(2026, 2, 1))
            net.add_incident(50, 0, incident_date, 5, "harassment")
            net.finalize()
            model.bind(net)
            return net.segment_between("a", "b").incident_risk

        recent = risk_on_date(date(2026, 1, 25))   # 7 days old
        old = risk_on_date(date(2025, 2, 10))      # ~1 year old
        self.assertGreater(recent, 0.3)             # fresh, severe, right on the street
        self.assertGreater(old, 0.0)               # still remembered...
        self.assertGreater(recent, old)            # ...but counts far less

    def test_incident_outside_buffer_is_ignored(self):
        net = line_network()
        model = SafetyModel(reference_date=date(2026, 2, 1))
        net.add_incident(500, 0, date(2026, 1, 25), 5, "harassment")  # 300 m away
        net.finalize()
        model.bind(net)
        for segment in net.segments:
            self.assertEqual(segment.incident_risk, 0.0)

    def test_eyes_on_street_drop_after_shops_close(self):
        net = diamond_network()
        model = SafetyModel()
        model.bind(net)
        street = net.segment_between("s", "m2")
        open_score = model.segment_safety(net, street, 19.0)
        closed_score = model.segment_safety(net, street, 23.0)
        self.assertGreater(open_score, closed_score)


class AnalysisTests(unittest.TestCase):
    def test_recommendation_prefers_the_safer_route(self):
        net = diamond_network()
        model = SafetyModel()
        model.bind(net)
        cost_fn = model.cost_function(net, 23.5)
        results = yen_k_shortest(net, "s", "t", cost_fn, k=5)
        assessments = [assess_route(net, model, r, 23.5) for r in results]
        annotate(assessments)
        recommended = recommend(assessments, max_extra_minutes=30.0)
        best = max(assessments, key=lambda a: a.mean_safety)
        self.assertEqual(recommended.label, best.label)
        self.assertGreater(best.mean_safety, 0.6)

    def test_fastest_label_follows_the_shortest_walk(self):
        net = diamond_network()
        model = SafetyModel(risk_aversion=0.0)  # lambda 0 -> pure walking time
        model.bind(net)
        cost_fn = model.cost_function(net, 23.5)
        results = yen_k_shortest(net, "s", "t", cost_fn, k=5)
        assessments = [assess_route(net, model, r, 23.5) for r in results]
        annotate(assessments)
        fastest = min(assessments, key=lambda a: a.travel_min)
        self.assertIn("Fastest", fastest.label)
        self.assertEqual(fastest.extra_min, 0.0)


class DemoCityTests(unittest.TestCase):
    def test_demo_city_is_routable_and_comparable(self):
        net = dataset.build_demo_city(seed=3)
        self.assertEqual(len(net.nodes), dataset.GRID_COLS * dataset.GRID_ROWS)
        model = SafetyModel()
        model.bind(net)
        cost_fn = model.cost_function(net, 22.5)
        origin = net.find("Hawa Mahal")
        destination = net.find("Jaipur Junction")
        self.assertIsNotNone(origin)
        self.assertIsNotNone(destination)
        results = yen_k_shortest(net, origin.id, destination.id, cost_fn, k=4)
        self.assertGreaterEqual(len(results), 2)
        assessments = [assess_route(net, model, r, 22.5) for r in results]
        annotate(assessments)
        recommended = recommend(assessments)
        self.assertGreater(len(recommended.nodes), 2)
        self.assertTrue(any(0.0 <= a.mean_safety <= 1.0 for a in assessments))

    def test_demo_city_is_deterministic(self):
        first = dataset.build_demo_city(seed=11)
        second = dataset.build_demo_city(seed=11)
        self.assertEqual(
            [(s.u, s.v, round(s.lighting, 6)) for s in first.segments],
            [(s.u, s.v, round(s.lighting, 6)) for s in second.segments],
        )


if __name__ == "__main__":
    unittest.main()
