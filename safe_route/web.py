"""Web app backed by live OpenStreetMap geocoding and pedestrian routes."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Union
from urllib.parse import urlparse
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode
from urllib.request import Request, urlopen


USER_AGENT = "OFFGRID-SafeRoute/1.0 (local prototype)"
STATIC_DIR = Path(__file__).with_name("static")
REPORT_DB = Path.home() / ".offgrid" / "incident_reports.sqlite3"
REPORT_CATEGORIES = {"poor_lighting", "harassment", "theft", "assault", "road_hazard", "other"}


def _fetch_json(url: str, service_name: str) -> Union[Dict[str, Any], List[Any]]:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise ValueError(f"{service_name} returned HTTP {error.code}. Please try again shortly.") from error
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        raise ValueError(f"Could not reach {service_name}. Check your internet connection and try again.") from error


@lru_cache(maxsize=256)
def _geocode(place: str) -> Dict[str, Any]:
    query = urlencode({"q": place, "limit": 1})
    result = _fetch_json(f"https://photon.komoot.io/api/?{query}", "OpenStreetMap place search")
    features = result.get("features", []) if isinstance(result, dict) else []
    if not features:
        raise ValueError(f"We could not find '{place}'. Try adding the city or country.")

    feature = features[0]
    properties = feature.get("properties", {})
    coordinates = feature.get("geometry", {}).get("coordinates", [])
    if len(coordinates) < 2:
        raise ValueError(f"We could not locate '{place}'. Try a more specific address.")
    name_parts = [
        properties.get("name"),
        properties.get("city") or properties.get("district"),
        properties.get("state"),
    ]
    label_parts = list(dict.fromkeys(part for part in name_parts if part))
    return {
        "name": properties.get("name") or place,
        "label": ", ".join(label_parts) or place,
        "city": properties.get("city") or properties.get("district") or properties.get("state") or "",
        "country": properties.get("country") or "",
        "country_code": (properties.get("countrycode") or "").upper(),
        "longitude": float(coordinates[0]),
        "latitude": float(coordinates[1]),
    }


def _decode_polyline6(encoded: str) -> List[List[float]]:
    coordinates = []
    index = latitude = longitude = 0
    while index < len(encoded):
        values = []
        for _ in range(2):
            result = shift = 0
            while True:
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            values.append(~(result >> 1) if result & 1 else result >> 1)
        latitude += values[0]
        longitude += values[1]
        coordinates.append([longitude / 1_000_000, latitude / 1_000_000])
    return coordinates


def _instructions(legs: list) -> List[Dict[str, Any]]:
    instructions = []
    for leg in legs:
        for step in leg.get("steps", []):
            maneuver = step.get("maneuver", {})
            instruction = step.get("instruction")
            if not instruction:
                modifier = maneuver.get("modifier", "continue")
                road_name = step.get("name")
                instruction = f"Continue {modifier}"
                if road_name:
                    instruction += f" onto {road_name}"
            if instruction:
                instructions.append({
                    "text": instruction,
                    "distance_m": step.get("distance", 0),
                    "duration_seconds": step.get("duration", 0),
                    "coordinates": step.get("geometry", {}).get("coordinates", []),
                })
    return instructions


def _fetch_motor_scooter_route(coordinates: str) -> List[Dict[str, Any]]:
    body = json.dumps({
        "locations": [
            {"lat": float(pair.split(",")[1]), "lon": float(pair.split(",")[0])}
            for pair in coordinates.split(";")
        ],
        "costing": "motor_scooter",
        "units": "kilometers",
        "directions_options": {"units": "kilometers", "shape_format": "geojson"},
    }).encode("utf-8")
    request = Request(
        "https://valhalla1.openstreetmap.de/route",
        data=body,
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=25) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
        raise ValueError("Could not reach the motorcycle route service. Try again shortly.") from error
    trip_payload = payload.get("trip", {})
    if not isinstance(trip_payload, dict) or trip_payload.get("status") != 0:
        raise ValueError(
            trip_payload.get("status_message") or payload.get("error") or "No motorcycle route was found."
        )

    trips = [trip_payload]
    trips.extend(alternative.get("trip", alternative) for alternative in payload.get("alternates", []))
    routes = []
    for trip in trips:
        summary = trip.get("summary", {})
        legs = trip.get("legs", [])
        if not legs:
            continue
        coordinates_geo = _decode_polyline6(legs[0].get("shape", ""))
        if len(coordinates_geo) < 2:
            continue
        maneuvers = legs[0].get("maneuvers", [])
        routes.append({
            "coordinates": coordinates_geo,
            "distance_km": summary.get("length", 0),
            "duration_seconds": summary.get("time", 0),
            "instructions": [
                {"text": item.get("instruction", "Continue"),
                 "distance_m": item.get("length", 0) * 1000,
                 "duration_seconds": item.get("time", 0),
                 "start_index": item.get("begin_shape_index", 0),
                 "coordinates": coordinates_geo[
                     item.get("begin_shape_index", 0):item.get("end_shape_index", 0) + 1
                 ]}
                for item in maneuvers if item.get("instruction")
            ],
        })
    return routes


def _route_payload(
    origin_text: str,
    destination_text: str,
    mode: str = "walk",
    current_origin: Union[Dict[str, float], None] = None,
) -> Dict[str, Any]:
    profiles = {"walk": "routed-foot", "car": "routed-car"}
    if mode not in ("walk", "car", "motorcycle"):
        raise ValueError("Choose walking, four-wheel driving, or motorcycle travel.")
    if not origin_text.strip() or not destination_text.strip():
        raise ValueError("Enter both a starting point and a destination.")

    if current_origin is not None:
        latitude = float(current_origin["latitude"])
        longitude = float(current_origin["longitude"])
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise ValueError("Current location coordinates are not valid.")
        origin = {
            "name": "Current location", "label": "Current location", "city": "",
            "country": "", "country_code": "", "latitude": latitude,
            "longitude": longitude,
        }
    else:
        origin = _geocode(origin_text.strip())
    destination = _geocode(destination_text.strip())
    if (origin["longitude"], origin["latitude"]) == (destination["longitude"], destination["latitude"]):
        raise ValueError("Choose two different places for your trip.")

    coordinates = (
        f"{origin['longitude']},{origin['latitude']};"
        f"{destination['longitude']},{destination['latitude']}"
    )
    if mode == "motorcycle":
        routes = _fetch_motor_scooter_route(coordinates)
        source = "OpenStreetMap via Valhalla motor_scooter routing"
    else:
        options = urlencode({
            "alternatives": "3", "overview": "full", "geometries": "geojson", "steps": "true",
        })
        profile = profiles[mode]
        url = f"https://routing.openstreetmap.de/{profile}/route/v1/driving/{coordinates}?{options}"
        response = _fetch_json(url, "OpenStreetMap route service")
        if not isinstance(response, dict) or response.get("code") != "Ok":
            raise ValueError("No route was found for that travel mode and these places.")
        provider_routes = response.get("routes", [])
        provider_routes.sort(key=lambda route: route.get("duration", float("inf")))
        routes = [
            {
                "coordinates": route.get("geometry", {}).get("coordinates", []),
                "distance_km": route.get("distance", 0) / 1000.0,
                "duration_seconds": route.get("duration", 0),
                "instructions": _instructions(route.get("legs", [])),
            }
            for route in provider_routes
        ]
        source = f"OpenStreetMap via FOSSGIS {mode} routing"
    routes = [route for route in routes if len(route["coordinates"]) >= 2]
    routes.sort(key=lambda route: route["duration_seconds"])
    for index, route in enumerate(routes):
        route["label"] = "Fastest route" if index == 0 else f"Alternative {index}"
        route["fastest"] = index == 0
    if not routes:
        raise ValueError("The routing service returned no usable route geometry.")

    return {
        "origin": origin,
        "destination": destination,
        "routes": routes,
        "mode": mode,
        "source": source,
        "safety_data_available": False,
        "safest_route_available": False,
    }


def _report_connection() -> sqlite3.Connection:
    REPORT_DB.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(REPORT_DB, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """CREATE TABLE IF NOT EXISTS incident_reports (
            id INTEGER PRIMARY KEY,
            city TEXT NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            category TEXT NOT NULL,
            note TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    return connection


def _submit_report(payload: Dict[str, Any]) -> Dict[str, Any]:
    city = str(payload.get("city", "")).strip()[:100]
    category = str(payload.get("category", ""))
    note = str(payload.get("note", "")).strip()[:240]
    if not city:
        raise ValueError("The nearest city is required for this report.")
    if category not in REPORT_CATEGORIES:
        raise ValueError("Choose a report category from the list.")
    try:
        latitude = float(payload["latitude"])
        longitude = float(payload["longitude"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("A map location is required to submit a report.") from error
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError("The report location is not valid.")

    from datetime import datetime, timezone

    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    connection = _report_connection()
    try:
        cursor = connection.execute(
            "INSERT INTO incident_reports (city, latitude, longitude, category, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (city, latitude, longitude, category, note, created_at),
        )
        connection.commit()
        report_id = cursor.lastrowid
    finally:
        connection.close()
    return {"id": report_id, "created_at": created_at, "verified": False}


def _point_to_segment_m(point: List[float], start: List[float], end: List[float]) -> float:
    reference_latitude = math.radians((point[1] + start[1] + end[1]) / 3.0)
    meters_per_lon = 111_320.0 * max(math.cos(reference_latitude), 0.01)
    point_x = (point[0] - start[0]) * meters_per_lon
    point_y = (point[1] - start[1]) * 111_320.0
    end_x = (end[0] - start[0]) * meters_per_lon
    end_y = (end[1] - start[1]) * 111_320.0
    length_squared = end_x * end_x + end_y * end_y
    if length_squared == 0:
        return math.hypot(point_x, point_y)
    projection = max(0.0, min(1.0, (point_x * end_x + point_y * end_y) / length_squared))
    return math.hypot(point_x - projection * end_x, point_y - projection * end_y)


def _reports_near_route(city: str, coordinates: Any, radius_m: float = 50.0) -> List[Dict[str, Any]]:
    city = city.strip()[:100]
    if not city:
        raise ValueError("The route city is required to check nearby reports.")
    if not isinstance(coordinates, list) or not 2 <= len(coordinates) <= 5000:
        raise ValueError("Route geometry is not valid.")

    points: List[List[float]] = []
    for coordinate in coordinates:
        if not isinstance(coordinate, (list, tuple)) or len(coordinate) < 2:
            raise ValueError("Route geometry is not valid.")
        longitude, latitude = float(coordinate[0]), float(coordinate[1])
        if not math.isfinite(longitude) or not math.isfinite(latitude):
            raise ValueError("Route geometry is not valid.")
        if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
            raise ValueError("Route geometry is not valid.")
        points.append([longitude, latitude])

    min_latitude = min(point[1] for point in points)
    max_latitude = max(point[1] for point in points)
    reference_latitude = math.radians(sum(point[1] for point in points) / len(points))
    latitude_padding = radius_m / 111_320.0
    longitude_padding = radius_m / (111_320.0 * max(math.cos(reference_latitude), 0.01))
    min_longitude = min(point[0] for point in points) - longitude_padding
    max_longitude = max(point[0] for point in points) + longitude_padding

    connection = _report_connection()
    try:
        rows = connection.execute(
            "SELECT id, latitude, longitude, category, note, created_at "
            "FROM incident_reports WHERE lower(city) = lower(?) "
            "AND latitude BETWEEN ? AND ? AND longitude BETWEEN ? AND ? "
            "ORDER BY created_at DESC LIMIT 250",
            (city, min_latitude - latitude_padding, max_latitude + latitude_padding,
             min_longitude, max_longitude),
        ).fetchall()
    finally:
        connection.close()

    matches = []
    for row in rows:
        report_point = [row["longitude"], row["latitude"]]
        distance = min(
            _point_to_segment_m(report_point, start, end)
            for start, end in zip(points, points[1:])
        )
        if distance <= radius_m:
            matches.append({
                **dict(row),
                "distance_to_route_m": round(distance),
                "source": "community report",
                "verified": False,
            })
    return matches


def create_handler():
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
            self._send(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/api/routes":
                    params = parse_qs(parsed.query)
                    origin = params.get("from", [""])[0]
                    destination = params.get("to", [""])[0]
                    mode = params.get("mode", ["walk"])[0]
                    current_origin = None
                    if "from_lat" in params or "from_lon" in params:
                        try:
                            current_origin = {
                                "latitude": float(params["from_lat"][0]),
                                "longitude": float(params["from_lon"][0]),
                            }
                        except (KeyError, ValueError) as error:
                            raise ValueError("A valid current location is required to reroute.") from error
                    self._send_json(200, _route_payload(origin, destination, mode, current_origin))
                    return
                if parsed.path not in ("/", "/index.html"):
                    self._send_json(404, {"error": "Page not found."})
                    return
                self._send(200, (STATIC_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            except ValueError as error:
                self._send_json(400, {"error": str(error)})
            except Exception as error:
                self.log_error("Request failed: %s", error)
                self._send_json(500, {"error": "Something went wrong while finding this route."})

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path not in ("/api/reports", "/api/route-incidents"):
                self._send_json(404, {"error": "Page not found."})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                max_length = 1_000_000 if parsed.path == "/api/route-incidents" else 4096
                if length <= 0 or length > max_length:
                    raise ValueError("The request is empty or too large.")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("The request format is not valid.")
                if parsed.path == "/api/route-incidents":
                    reports = _reports_near_route(payload.get("city", ""), payload.get("coordinates"))
                    self._send_json(200, {
                        "reports": reports,
                        "radius_m": 50,
                        "verified": False,
                    })
                    return
                report = _submit_report(payload)
                self._send_json(201, {"report": report})
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
                self._send_json(400, {"error": str(error) or "The report format is not valid."})
            except sqlite3.Error as error:
                self.log_error("Report storage failed: %s", error)
                self._send_json(500, {"error": "Could not save the report on this device."})

        def log_message(self, format: str, *args) -> None:
            request_line = args[0] if args and isinstance(args[0], str) else ""
            parts = request_line.split()
            if len(parts) >= 2 and parts[0] in {"GET", "POST"}:
                print(f"{self.address_string()} - {parts[0]} {urlparse(parts[1]).path}")
            else:
                print(f"{self.address_string()} - request")

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the live-data Safe Route Finder web app.")
    parser.add_argument("--host", default="127.0.0.1", help="interface to bind (default: localhost)")
    parser.add_argument("--port", type=int, default=8000, help="port to use (default: 8000)")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), create_handler())
    print(f"Safe Route Finder is ready at http://{args.host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down Safe Route Finder.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()