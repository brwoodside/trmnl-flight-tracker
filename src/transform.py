"""Normalize aircraft providers and select the closest airborne aircraft.

TRMNL polls ADSB.lol first, so an openly licensed fallback response is already
available to this transform. The transform optionally queries Flightradar24 and
FlightAware with credentials stored in the plugin's custom fields. It returns a
small, provider-independent object for the Liquid templates.

Only Python's standard library is used because TRMNL's hosted serverless Python
runtime does not install this project's development dependencies.
"""

import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Iterable


FR24_URL = "https://fr24api.flightradar24.com/api/live/flight-positions/full"
FLIGHTAWARE_URL = "https://aeroapi.flightaware.com/aeroapi/flights/search/advanced"
HTTP_TIMEOUT_SECONDS = 8
USER_AGENT = "TRMNL-Overhead-Flight-Tracker/1.0"
EARTH_RADIUS_NM = 3440.065


class ConfigurationError(ValueError):
    """Raised for invalid user configuration."""


class ProviderError(RuntimeError):
    """Raised when a provider cannot return a usable response."""


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _fetch_json(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    request_headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    request_headers.update(headers or {})
    request = urllib.request.Request(url, headers=request_headers)

    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise ProviderError(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProviderError("request unavailable") from exc

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProviderError("invalid JSON response") from exc

    if not isinstance(payload, dict):
        raise ProviderError("unexpected response shape")
    return payload


def _coalesce(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _required_float(value: Any, name: str, minimum: float, maximum: float) -> float:
    number = _float(value)
    if number is None or number < minimum or number > maximum:
        raise ConfigurationError(f"{name} must be between {minimum:g} and {maximum:g}")
    return number


def _parse_time(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None

    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso_time(value: Any) -> str | None:
    parsed = _parse_time(value)
    if parsed is None:
        return None
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return great-circle distance in nautical miles."""

    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return EARTH_RADIUS_NM * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def initial_bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return initial great-circle bearing from point one to point two."""

    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_lambda = math.radians(lon2 - lon1)
    y = math.sin(delta_lambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(delta_lambda)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def compass_point(degrees: float | None) -> str:
    if degrees is None:
        return "—"
    points = (
        "N",
        "NNE",
        "NE",
        "ENE",
        "E",
        "ESE",
        "SE",
        "SSE",
        "S",
        "SSW",
        "SW",
        "WSW",
        "W",
        "WNW",
        "NW",
        "NNW",
    )
    return points[int((degrees % 360) / 22.5 + 0.5) % 16]


def _normalize_longitude(value: float) -> float:
    return ((value + 180) % 360) - 180


def bounding_boxes(lat: float, lon: float, radius_nm: float) -> list[tuple[float, float, float, float]]:
    """Return one or two (north, south, west, east) provider bounding boxes."""

    lat_delta = radius_nm / 60.0
    north = min(90.0, lat + lat_delta)
    south = max(-90.0, lat - lat_delta)
    cosine = max(abs(math.cos(math.radians(lat))), 0.01)
    lon_delta = radius_nm / (60.0 * cosine)

    if lon_delta >= 180:
        return [(north, south, -180.0, 180.0)]

    west = _normalize_longitude(lon - lon_delta)
    east = _normalize_longitude(lon + lon_delta)
    if west <= east:
        return [(north, south, west, east)]
    return [(north, south, west, 180.0), (north, south, -180.0, east)]


def _airport_code(value: Any) -> str | None:
    if not isinstance(value, dict):
        return _clean_text(value)
    for key in ("code_iata", "code_icao", "code_lid", "code"):
        code = _clean_text(value.get(key))
        if code and code.replace("-", "").isalnum() and 2 <= len(code) <= 5:
            return code.upper()
    return None


def _candidate(
    *,
    source: str,
    identifier: Any,
    latitude: Any,
    longitude: Any,
    altitude_ft: Any = None,
    groundspeed_kts: Any = None,
    heading_deg: Any = None,
    vertical_rate_fpm: Any = None,
    vertical_trend: Any = None,
    observed_at: Any = None,
    callsign: Any = None,
    flight: Any = None,
    registration: Any = None,
    aircraft_type: Any = None,
    operator: Any = None,
    origin: Any = None,
    destination: Any = None,
) -> dict[str, Any] | None:
    lat = _float(latitude)
    lon = _float(longitude)
    if lat is None or lon is None or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return None

    altitude = _float(altitude_ft)
    if altitude is not None and altitude <= 0:
        return None

    callsign_text = _clean_text(callsign)
    flight_text = _clean_text(flight)
    registration_text = _clean_text(registration)
    identifier_text = _clean_text(identifier) or flight_text or callsign_text or registration_text or "Unknown"
    rate = _float(vertical_rate_fpm)
    trend = _clean_text(vertical_trend)
    if not trend and rate is not None:
        if rate > 200:
            trend = "Climbing"
        elif rate < -200:
            trend = "Descending"
        else:
            trend = "Level"

    return {
        "source": source,
        "identifier": identifier_text,
        "callsign": callsign_text,
        "flight": flight_text,
        "registration": registration_text,
        "aircraft_type": _clean_text(aircraft_type),
        "operator": _clean_text(operator),
        "origin": _clean_text(origin),
        "destination": _clean_text(destination),
        "latitude": lat,
        "longitude": lon,
        "altitude_ft": altitude,
        "groundspeed_kts": _float(groundspeed_kts),
        "heading_deg": _float(heading_deg),
        "vertical_rate_fpm": rate,
        "vertical_trend": trend or "Unknown",
        "observed_at": _iso_time(observed_at),
    }


def normalize_fr24(payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for row in payload.get("data") or []:
        if not isinstance(row, dict):
            continue
        item = _candidate(
            source="flightradar24",
            identifier=_coalesce(row.get("flight"), row.get("callsign"), row.get("reg"), row.get("hex")),
            callsign=row.get("callsign"),
            flight=row.get("flight"),
            registration=row.get("reg"),
            aircraft_type=row.get("type"),
            operator=_coalesce(row.get("operating_as"), row.get("painted_as")),
            origin=_coalesce(row.get("orig_iata"), row.get("orig_icao")),
            destination=_coalesce(row.get("dest_iata"), row.get("dest_icao")),
            latitude=row.get("lat"),
            longitude=row.get("lon"),
            altitude_ft=row.get("alt"),
            groundspeed_kts=row.get("gspeed"),
            heading_deg=row.get("track"),
            vertical_rate_fpm=row.get("vspeed"),
            observed_at=row.get("timestamp"),
        )
        if item:
            candidates.append(item)
    return candidates


def normalize_flightaware(payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for row in payload.get("flights") or []:
        if not isinstance(row, dict):
            continue
        position = row.get("last_position") or {}
        if not isinstance(position, dict):
            continue
        altitude_hundreds = _float(position.get("altitude"))
        change = _clean_text(position.get("altitude_change"))
        trend = {"C": "Climbing", "D": "Descending", "-": "Level"}.get(change or "")
        item = _candidate(
            source="flightaware",
            identifier=_coalesce(row.get("ident_iata"), row.get("ident_icao"), row.get("ident"), row.get("registration")),
            callsign=_coalesce(row.get("ident_icao"), row.get("ident")),
            flight=_coalesce(row.get("ident_iata"), row.get("ident")),
            registration=row.get("registration"),
            aircraft_type=row.get("aircraft_type"),
            origin=_airport_code(row.get("origin")),
            destination=_airport_code(row.get("destination")),
            latitude=position.get("latitude"),
            longitude=position.get("longitude"),
            altitude_ft=altitude_hundreds * 100 if altitude_hundreds is not None else None,
            groundspeed_kts=position.get("groundspeed"),
            heading_deg=position.get("heading"),
            vertical_trend=trend,
            observed_at=position.get("timestamp"),
        )
        if item:
            candidates.append(item)
    return candidates


def normalize_adsblol(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if "ac" not in payload:
        raise ProviderError("fallback response unavailable")
    now_ms = _float(payload.get("now"))
    candidates: list[dict[str, Any]] = []
    for row in payload.get("ac") or []:
        if not isinstance(row, dict):
            continue
        altitude = row.get("alt_baro")
        if isinstance(altitude, str) and altitude.lower() == "ground":
            continue
        observed_at: float | None = None
        seen_seconds = _float(_coalesce(row.get("seen_pos"), row.get("seen")))
        if now_ms is not None:
            observed_at = now_ms / 1000.0 - (seen_seconds or 0)
        item = _candidate(
            source="adsblol",
            identifier=_coalesce(row.get("flight"), row.get("r"), row.get("hex")),
            callsign=row.get("flight"),
            registration=row.get("r"),
            aircraft_type=row.get("t"),
            operator=row.get("ownOp"),
            latitude=row.get("lat"),
            longitude=row.get("lon"),
            altitude_ft=altitude,
            groundspeed_kts=row.get("gs"),
            heading_deg=_coalesce(row.get("track"), row.get("true_heading"), row.get("mag_heading")),
            vertical_rate_fpm=_coalesce(row.get("baro_rate"), row.get("geom_rate")),
            observed_at=observed_at,
        )
        if item:
            candidates.append(item)
    return candidates


def _fetch_fr24(config: dict[str, Any]) -> list[dict[str, Any]]:
    token = config["fr24_api_token"]
    all_candidates: list[dict[str, Any]] = []
    for north, south, west, east in bounding_boxes(config["latitude"], config["longitude"], config["radius_nm"]):
        params = urllib.parse.urlencode(
            {
                "bounds": f"{north:.6f},{south:.6f},{west:.6f},{east:.6f}",
                "limit": 100,
            }
        )
        payload = _fetch_json(
            f"{FR24_URL}?{params}",
            {
                "Authorization": f"Bearer {token}",
                "Accept-Version": "v1",
            },
        )
        all_candidates.extend(normalize_fr24(payload))
    return all_candidates


def _fetch_flightaware(config: dict[str, Any]) -> list[dict[str, Any]]:
    api_key = config["flightaware_api_key"]
    all_candidates: list[dict[str, Any]] = []
    for north, south, west, east in bounding_boxes(config["latitude"], config["longitude"], config["radius_nm"]):
        query = f"{{range lat {south:.6f} {north:.6f}}} {{range lon {west:.6f} {east:.6f}}} {{true inAir}}"
        params = urllib.parse.urlencode({"query": query, "max_pages": 1})
        payload = _fetch_json(f"{FLIGHTAWARE_URL}?{params}", {"x-apikey": api_key})
        all_candidates.extend(normalize_flightaware(payload))
    return all_candidates


def _age_minutes(observed_at: str | None, now: datetime) -> float | None:
    parsed = _parse_time(observed_at)
    if parsed is None:
        return None
    return max(0.0, (now - parsed).total_seconds() / 60.0)


def _select_nearest(
    candidates: Iterable[dict[str, Any]], config: dict[str, Any], now: datetime
) -> tuple[dict[str, Any] | None, int]:
    usable: list[dict[str, Any]] = []
    for candidate in candidates:
        distance = haversine_nm(
            config["latitude"],
            config["longitude"],
            candidate["latitude"],
            candidate["longitude"],
        )
        if distance > config["radius_nm"]:
            continue
        age = _age_minutes(candidate.get("observed_at"), now)
        if age is not None and age > config["max_age_minutes"]:
            continue
        enriched = dict(candidate)
        enriched["distance_nm"] = distance
        enriched["bearing_deg"] = initial_bearing_deg(
            config["latitude"],
            config["longitude"],
            candidate["latitude"],
            candidate["longitude"],
        )
        enriched["age_minutes"] = age
        usable.append(enriched)

    usable.sort(key=lambda item: (item["distance_nm"], -(item.get("altitude_ft") or 0)))
    return (usable[0] if usable else None, len(usable))


def _provider_sequence(config: dict[str, Any]) -> list[str]:
    if config["provider_order"] == "open_only":
        return ["adsblol"]
    if config["provider_order"] == "flightaware_first":
        return ["flightaware", "flightradar24", "adsblol"]
    return ["flightradar24", "flightaware", "adsblol"]


def _local_env_field(
    fields: dict[str, Any], key: str, env_name: str, default: Any = None
) -> Any:
    """Resolve `.trmnlp.yml` env placeholders passed raw to local transforms.

    Hosted TRMNL forms provide final values. Current trmnlp releases render env
    placeholders in polling settings but retain the placeholder text inside the
    transform's custom_fields_values namespace. Only treat a value as local
    indirection when it still visibly contains an env Liquid expression.
    """

    value = fields.get(key)
    if isinstance(value, str) and "{{" in value and f"env.{env_name}" in value:
        return os.environ.get(env_name) or default
    return _coalesce(value, default)


def _extract_config(input_data: dict[str, Any]) -> dict[str, Any]:
    fields = (
        input_data.get("trmnl", {})
        .get("plugin_settings", {})
        .get("custom_fields_values", {})
    )
    if not isinstance(fields, dict):
        fields = {}

    latitude = _required_float(
        _local_env_field(fields, "latitude", "TRACKER_LATITUDE", 37.7749),
        "latitude",
        -90,
        90,
    )
    longitude = _required_float(
        _local_env_field(fields, "longitude", "TRACKER_LONGITUDE", -122.4194),
        "longitude",
        -180,
        180,
    )
    radius = _required_float(
        _local_env_field(fields, "radius_nm", "TRACKER_RADIUS_NM", 20),
        "radius_nm",
        1,
        250,
    )
    max_age = _required_float(
        _local_env_field(fields, "max_age_minutes", "TRACKER_MAX_AGE_MINUTES", 5),
        "max_age_minutes",
        1,
        60,
    )
    provider_order = _clean_text(
        _local_env_field(fields, "provider_order", "TRACKER_PROVIDER_ORDER", "auto")
    ) or "auto"
    if provider_order not in {"auto", "flightaware_first", "open_only"}:
        provider_order = "auto"

    return {
        "latitude": latitude,
        "longitude": longitude,
        "location_label": _clean_text(
            _local_env_field(
                fields, "location_label", "TRACKER_LOCATION_LABEL", "Observation point"
            )
        )
        or "Observation point",
        "radius_nm": radius,
        "max_age_minutes": max_age,
        "provider_order": provider_order,
        "fr24_api_token": _clean_text(
            _local_env_field(fields, "fr24_api_token", "FR24_API_TOKEN")
        ),
        "flightaware_api_key": _clean_text(
            _local_env_field(fields, "flightaware_api_key", "FLIGHTAWARE_AEROAPI_KEY")
        ),
    }


def _format_aircraft(candidate: dict[str, Any]) -> dict[str, Any]:
    altitude = candidate.get("altitude_ft")
    speed = candidate.get("groundspeed_kts")
    heading = candidate.get("heading_deg")
    vertical_rate = candidate.get("vertical_rate_fpm")
    origin = candidate.get("origin")
    destination = candidate.get("destination")
    age = candidate.get("age_minutes")
    source_labels = {
        "flightradar24": "Flightradar24",
        "flightaware": "FlightAware",
        "adsblol": "ADSB.lol",
    }
    if altitude is None:
        altitude_compact = "—"
    elif altitude >= 10000:
        altitude_compact = f"{altitude / 1000:.1f}k ft".replace(".0k", "k")
    else:
        altitude_compact = f"{altitude:,.0f} ft"

    return {
        **candidate,
        "source_label": source_labels.get(candidate["source"], candidate["source"]),
        "distance_nm": round(candidate["distance_nm"], 1),
        "distance_display": f"{candidate['distance_nm']:.1f} nm",
        "bearing_deg": round(candidate["bearing_deg"]),
        "bearing_compass": compass_point(candidate["bearing_deg"]),
        "altitude_ft": round(altitude) if altitude is not None else None,
        "altitude_display": f"{altitude:,.0f} ft" if altitude is not None else "—",
        "altitude_compact": altitude_compact,
        "groundspeed_kts": round(speed) if speed is not None else None,
        "speed_display": f"{speed:,.0f} kt" if speed is not None else "—",
        "heading_deg": round(heading) if heading is not None else None,
        "heading_compass": compass_point(heading),
        "vertical_rate_fpm": round(vertical_rate) if vertical_rate is not None else None,
        "vertical_rate_display": f"{vertical_rate:+,.0f} ft/min" if vertical_rate is not None else "—",
        "vertical_trend_short": {
            "Climbing": "Up",
            "Descending": "Down",
            "Level": "Level",
        }.get(candidate.get("vertical_trend"), "—"),
        "route": f"{origin} → {destination}" if origin and destination else "Route unavailable",
        "route_available": bool(origin and destination),
        "aircraft_label": " · ".join(
            value for value in (candidate.get("aircraft_type"), candidate.get("registration")) if value
        )
        or "Aircraft details unavailable",
        "age_minutes": round(age, 1) if age is not None else None,
        "age_display": f"{age:.0f} min ago" if age is not None and age >= 1 else "just now",
    }


def _base_result(config: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    return {
        "has_aircraft": False,
        "status": "provider_error",
        "message": "Aircraft data is temporarily unavailable.",
        "provider_used": None,
        "provider_attempts": [],
        "candidate_count": 0,
        "location_label": config["location_label"] if config else "Flight tracker",
        "search_radius_nm": config["radius_nm"] if config else None,
        "updated_at": now.isoformat(timespec="minutes").replace("+00:00", "Z"),
    }


def run(input: dict[str, Any]) -> dict[str, Any]:
    """TRMNL serverless entrypoint."""

    now = _now_utc()
    try:
        config = _extract_config(input)
    except ConfigurationError as exc:
        result = _base_result(None, now)
        result.update(status="configuration_error", message=f"Configuration error: {exc}")
        return result

    result = _base_result(config, now)
    providers = _provider_sequence(config)
    any_successful_response = False

    for provider in providers:
        if provider == "flightradar24" and not config["fr24_api_token"]:
            result["provider_attempts"].append(
                {"provider": provider, "status": "skipped", "detail": "no API token"}
            )
            continue
        if provider == "flightaware" and not config["flightaware_api_key"]:
            result["provider_attempts"].append(
                {"provider": provider, "status": "skipped", "detail": "no API key"}
            )
            continue

        try:
            if provider == "flightradar24":
                candidates = _fetch_fr24(config)
            elif provider == "flightaware":
                candidates = _fetch_flightaware(config)
            else:
                candidates = normalize_adsblol(input)
            any_successful_response = True
            nearest, candidate_count = _select_nearest(candidates, config, now)
        except ProviderError as exc:
            result["provider_attempts"].append(
                {"provider": provider, "status": "error", "detail": str(exc)}
            )
            continue

        if nearest is None:
            result["provider_attempts"].append(
                {"provider": provider, "status": "empty", "detail": "no fresh airborne positions"}
            )
            continue

        result.update(
            has_aircraft=True,
            status="ok",
            message="",
            provider_used=provider,
            candidate_count=candidate_count,
            aircraft=_format_aircraft(nearest),
        )
        result["provider_attempts"].append(
            {"provider": provider, "status": "selected", "detail": f"{candidate_count} candidates"}
        )
        return result

    if any_successful_response:
        result.update(
            status="no_aircraft",
            message=f"No fresh airborne aircraft within {config['radius_nm']:g} nm.",
        )
    return result
