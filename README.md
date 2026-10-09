# Safe Route Finder for Night Travel

**OFFGRID · Tech Ideate — Manipal University Jaipur / IEEE Student Branch · Smart Cities (Problem 1)**

> *"To design and develop a data-driven routing system that identifies, evaluates and
> compares routes on the basis of multiple public-safety parameters, such as street
> lighting, pedestrian activity, road type, and reported incidents, rather than distance
> or travel time alone."*

A short route through a dark, empty road is **not** the same as a slightly longer one
through a busy, well-lit street. This project is a working routing engine that makes that
trade-off explicit, measurable and comparable.

---

## 1. What it does

1. **Identifies** candidate routes: Yen's *K*-shortest-loopless-paths algorithm finds the
   *k* best distinct routes between two locations.
2. **Evaluates** every street segment on five public-safety parameters
   (see §3) at the traveller's departure hour (23:30 behaves very differently from 09:00).
3. **Compares** the routes in one table — distance, walking time, mean and *worst-segment*
   safety, unlit share, footfall and incident exposure — then recommends the safest route
   that stays within a time budget, and shows the sensitivity of that ranking to each
   parameter weight.

```
python -m safe_route --from "Sitapura" --to "Bari Chaupar" --time 23:30
```

## 2. Quick start

```bash
cd offgrid                     # this folder
python -m safe_route --list    # every known location in the demo city
python -m safe_route --from "Sitapura" --to "Bari Chaupar" --time 23:30 --k 4
python -m safe_route --from "Sitapura" --to "Bari Chaupar" --time 01:15 --k 3 --sensitivity
python -m safe_route --from "Sitapura" --to "Bari Chaupar" --profile lighting_led --risk 40 --k 5
python -m unittest discover -s tests -v
```

Requires **Python 3.8+**, standard library only. Maps need an optional extra
(`pip install folium` for `.html`, or `matplotlib` for `.png`).

### Try the web app (live OpenStreetMap data)

```bash
python -m safe_route.web --port 8001
```

Open `http://127.0.0.1:8001` to search places and compare routes. The web app uses Photon
for place search, FOSSGIS/OpenStreetMap for walking and car routes, Valhalla's
`motor_scooter` profile for powered two-wheelers, and Esri satellite imagery. It needs an
internet connection. Start navigation to share GPS with the browser; off-route detection
can request a new route from the current position.

Route cards identify the fastest available option and other provider alternatives. A
“safest” ranking is deliberately disabled: these route providers do not supply verified
street lighting, pedestrian counts or official geolocated incident history. The web app
shows only community-submitted reports within 50 m of the selected route; reports are
unverified and an empty list does not mean a road is incident-free. Reports are stored
locally in `~/.offgrid/incident_reports.sqlite3`. For India, the app shows emergency `112`
and women helpline `181`; other countries need a local verified helpline source. The
command-line app still uses the synthetic demo city unless you supply a dataset.

### Useful flags

| Flag | Meaning |
| --- | --- |
| `--from` / `--to` | origin / destination location name (fuzzy, see `--list`) |
| `--time HH:MM` | departure time; default *now*. Night hours change every score. |
| `--profile` | `balanced` (default), `equal`, `lighting_led`, `crowd_led`, `incident_led` |
| `--risk λ` | risk aversion: extra minutes per km of fully unsafe road (0 = plain fastest route) |
| `--k N` | how many distinct candidate routes to compare |
| `--max-extra` | extra minutes tolerated when recommending the safe route |
| `--sensitivity` | do the rankings survive different parameter weights? |
| `--data file.json` | use your own city dataset instead of the demo city (schema in §6) |
| `--map` / `--json` | save a map / a machine-readable report |

### Real output

```
$ python -m safe_route --from "Sitapura" --to "Bari Chaupar" --time 23:30 --k 4
==============================================================================
SAFE ROUTE FINDER - night-time route comparison (OFFGRID Problem 1)
==============================================================================
Route          : Sitapura  ->  Bari Chaupar
Departure      : 23:30
Network        : Demo city (Jaipur-inspired synthetic street grid)
                 (42 junctions, 74 segments, 26 incidents)
Weight profile : lighting 0.30, pedestrian 0.25, road_type 0.15, incidents 0.20, eyes_on_street 0.10
Risk aversion  : lambda = 25.0 min per km of fully unsafe road

4 candidate routes compared:

+---+---------+----------+----------+--------+-------+-------+-----+----------+---------+
| # | Route   | Distance |     Walk | Safety | Worst | Unlit | Lit | Footfall | RiskExp |
+---+---------+----------+----------+--------+-------+-------+-----+----------+---------+
| 1 | Fastest |  1.13 km | 16.4 min |    44% |   26% |   40% | 14% |     0.11 |    0.02 |
| 2 | Alt 1   |  1.31 km | 17.8 min |    52% |   26% |   22% | 38% |     0.14 |    0.02 |
| 3 | Safest  |  1.30 km | 17.7 min |    50% |   26% |   22% | 27% |     0.14 |    0.03 |
| 4 | Alt 2   |  1.30 km | 17.7 min |    50% |   24% |   21% | 39% |     0.14 |    0.04 |
+---+---------+----------+----------+--------+-------+-------+-----+----------+---------+

Safety = weighted blend of lighting / footfall / road type / incidents / eyes-on-street.
Worst  = weakest single segment on the route.
Unlit  = share of the route scoring below 40% safety;  Lit = share with lighting >= 60%.
RiskExp= km of route weighted by nearby incident risk.

Worst segments per route:
  1. [Fastest] Kishanpole - New Gate
  2. [Alt 1] Kishanpole - New Gate
  3. [Safest] Jawahar Circle - Pink City Square
  4. [Alt 2] Kishanpole - New Gate

Recommendation: Take 'Safest', only 1.3 min slower than the fastest candidate, worst-segment safety 26% vs 26%, unsafe-road exposure 0.03 vs 0.02 km*risk.
```

The **fastest** route is 400 m shorter precisely because it chains dark, empty lanes at
1 a.m. - 40% of it scores below the 40% safety threshold and only 14% is well lit, against
22%/27% for the safe route, which costs 1.3 extra minutes. That is exactly the trade-off
the problem statement asks us to expose. `--sensitivity` additionally shows the ranking
holds under every weight profile (equal / balanced / lighting-led / crowd-led /
incident-led all prefer the same route), which is what makes the recommendation
defensible rather than a matter of taste.

## 3. The safety model

Each segment gets a score in **[0, 1]** as a weighted blend of five measurable, transparent
parameters:

| Parameter | Signal | Time dependence |
| --- | --- | --- |
| `lighting` | streetlight level/reliability of the segment | static per segment |
| `pedestrian` | footfall on that street | 24-value hourly profile, interpolated at departure hour |
| `road_type` | prior per road class (alley 0.25, highway 0.35, arterial 0.72, collector 0.78, footpath 0.80) | static |
| `incidents` | `1 − risk` where risk = incidents within 75 m, severity-weighted, **recency-decayed** (half-life 120 d) | decays over time |
| `eyes_on_street` | natural surveillance: shops closing time, decaying 2 h after closing | hour of day |

```
safety(segment) = w_l·lighting + w_p·footfall + w_r·roadtype + w_i·(1−incident_risk)
                + w_e·eyes_on_street        (weights normalised, see PROFILES)
```

### Turning safety into a route cost

```
cost(segment)    = walk_minutes(segment) + λ · risk_exposure(segment)
risk_exposure(s) = length_km(s) · (1 − safety(s))
```

`λ` (risk aversion) is *extra minutes per km of completely unsafe road*:

* `λ = 0`  → the classic fastest/shortest route (the baseline the problem complains about),
* `λ = 25` → detours freely around dark stretches,
* `λ → ∞`  → the maximally safe route.

### Route-level metrics

Because "a route is only as safe as its worst stretch", routes are compared on:

* **mean safety** — length-weighted average,
* **worst-segment safety** — the weakest link,
* **unlit share** — % of the route below the 40 % safety threshold,
* **lit share** — % of the route with lighting ≥ 60 %,
* **footfall** — average pedestrian activity along the route,
* **risk exposure** — km of route weighted by incident risk,
* **extra minutes** — cost of safety in time.

`recommend()` then picks the route with the best worst-segment among those no more than
`--max-extra` minutes slower than the fastest candidate, and prints the trade-off in words.

## 4. Project layout

```
offgrid/
├── safe_route/
│   ├── __init__.py        # public API re-exports
│   ├── models.py          # Node / Segment / Incident / RoadNetwork + spatial index
│   ├── safety.py          # SafetyModel: the five parameters, scores, edge costs, PROFILES
│   ├── routing.py         # Dijkstra + Yen's K-shortest-loopless-paths
│   ├── analysis.py        # route metrics, labelling, recommendation, reports, sensitivity
│   ├── dataset.py         # deterministic demo city + JSON loader for real data
│   ├── visualize.py       # optional folium HTML / matplotlib PNG maps
│   ├── cli.py             # argparse CLI
│   ├── web.py             # live OpenStreetMap web app (Photon + FOSSGIS routing)
│   ├── static/index.html  # frontend for the web app
│   └── __main__.py        # python -m safe_route
├── offgrid.py             # flattened single-file build of the graph model
├── tests/test_problem1.py
├── requirements.txt
└── README.md
```

## 5. Demo city

`dataset.build_demo_city()` generates a deterministic (seeded) 7×6 street grid with Jaipur
localities: a lit, busy old-city core; darker outskirts; ~10 % missing links (irregular
streets); corner-cutting **alleys that are shorter but far less safe** (the trap a normal
navigator falls into); main roads with late-night eateries; and 26 incidents clustered in
dark corners with severities 1–5 and ages up to 240 days. Same seed → same city, every run.

## 6. Plugging in real data

The engine is data-driven: pass `--data my_city.json` (schema documented in
`safe_route/dataset.py`):

```json
{
  "name": "My city",
  "nodes": [{"id": "n1", "name": "Hawa Mahal", "x": 0, "y": 0}],
  "segments": [{"u": "n1", "v": "n2", "road_type": "collector",
                "lighting": 0.8, "shops_open_until": 22,
                "hourly_pedestrian": [24 floats 0..1]}],
  "incidents": [{"x": 40, "y": 20, "date": "2026-01-15", "severity": 4,
                 "kind": "poor lighting"}]
}
```

Natural real sources: OSM extracts (road class + streetlight tags + `opening_hours`),
municipal streetlight-layers (GIS), police/NCRB-style crime datasets (lat/lon, date,
severity), footfall from mobile-data aggregates. Coordinates are planar metres; use
`safe_route.visualize.to_latlon` to render on a real basemap.

## 7. Tests

```bash
python -m unittest discover -s tests -v
```

Covers: Dijkstra on a known graph + unreachable pairs; Yen's algorithm returns distinct,
cost-ordered loopless paths; dark alleys score far below lit streets at night; incident
recency/severity decay and buffer behaviour; eyes-on-street after closing time;
recommendation picks the safer route; demo city determinism and routability.

## 8. Known limitations & next steps

* **Static profile within a query** — costs are computed at the departure hour
  (standard practice); a walk-aware version can interpolate cost at arrival time of each
  segment.
* **Undirected graph** — fine for pedestrians; vehicles need per-direction costs.
* **Data is synthetic by default** — parameters are illustrative priors; calibrate
  weights against real incident/lighting data.
* **Public routing APIs carry no safety data** — the web app is honest about this
  (`safety_data_available: false`); scoring real cities needs a municipal data feed
  (§6).
* **No user feedback loop** — add "I felt unsafe here" reports to re-weight segments
  online (bandit/convex optimisation over `λ` and the profile).
* Extensions already scaffolded: `--profile` presets, `--sensitivity` analysis, JSON
  in/out, bike mode (`SafetyModel.walk_minutes(mode="bike")`).
