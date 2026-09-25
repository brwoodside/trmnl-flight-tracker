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
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Iterable


FR24_URL = "https://fr24api.flightradar24.com/api/live/flight-positions/full"
FR24_SUMMARY_URL = "https://fr24api.flightradar24.com/api/flight-summary/light"
FLIGHTAWARE_URL = "https://aeroapi.flightaware.com/aeroapi/flights/search/advanced"
# Leave time inside TRMNL's five-second runtime for fallback and formatting.
PAID_PROVIDER_BUDGET_SECONDS = 3.0
PROVIDER_TIMEOUT_SECONDS = 1.4
ROUTE_ENRICHMENT_TIMEOUT_SECONDS = 0.8
HTTP_TIMEOUT_SECONDS = 1.0
ROUTE_MAX_AGE_SECONDS = 2 * 60 * 60
USER_AGENT = "TRMNL-Overhead-Flight-Tracker/1.0"
EARTH_RADIUS_NM = 3440.065
SCOPE_RANGES_NM = (1, 2, 5, 10, 20, 50, 100, 250)
SCOPE_CENTER = 50.0
SCOPE_RADIUS = 40.0

# Common ICAO operator designators, checked against the FAA's company decode:
# https://www.faa.gov/air_traffic/publications/atpubs/cnt_html/chap3_section_3.html
# Use familiar display names; regional operators stay distinct from marketing
# carriers. Keep this in the transform because TRMNL uploads it as one file.
AIRLINE_NAMES = {
    "ABX": "ABX Air",
    "AAL": "American Airlines",
    "AAR": "Asiana Airlines",
    "AAY": "Allegiant Air",
    "ACA": "Air Canada",
    "AEA": "Air Europa",
    "AFR": "Air France",
    "AIC": "Air India",
    "AJT": "Amerijet International",
    "AMF": "Ameriflight",
    "AMX": "Aeromexico",
    "ANA": "All Nippon Airways",
    "ANZ": "Air New Zealand",
    "ASA": "Alaska Airlines",
    "ASH": "Mesa Airlines",
    "ATN": "Air Transport International",
    "AVA": "Avianca",
    "AZU": "Azul Brazilian Airlines",
    "BAW": "British Airways",
    "BTQ": "Boutique Air",
    "CCA": "Air China",
    "CES": "China Eastern Airlines",
    "CFG": "Condor",
    "CKS": "Kalitta Air",
    "CLX": "Cargolux",
    "CMP": "Copa Airlines",
    "CNS": "PlaneSense",
    "CPA": "Cathay Pacific",
    "CSN": "China Southern Airlines",
    "DAL": "Delta Air Lines",
    "DLH": "Lufthansa",
    "EDV": "Endeavor Air",
    "EJA": "NetJets",
    "EIN": "Aer Lingus",
    "ENY": "Envoy Air",
    "ETD": "Etihad Airways",
    "ETH": "Ethiopian Airlines",
    "EVA": "EVA Air",
    "EZY": "easyJet",
    "FDX": "FedEx Express",
    "FFT": "Frontier Airlines",
    "FDY": "Southern Airways Express",
    "FIN": "Finnair",
    "GJS": "GoJet Airlines",
    "GLO": "GOL Linhas Aereas",
    "GTI": "Atlas Air",
    "HAL": "Hawaiian Airlines",
    "IBE": "Iberia",
    "ICE": "Icelandair",
    "ITY": "ITA Airways",
    "JAL": "Japan Airlines",
    "JIA": "PSA Airlines",
    "JBU": "JetBlue Airways",
    "JSX": "JSX",
    "JZA": "Jazz Aviation",
    "KAI": "KaiserAir",
    "KAL": "Korean Air",
    "KAP": "Cape Air",
    "KII": "Kalitta Charters",
    "KLM": "KLM Royal Dutch Airlines",
    "KQA": "Kenya Airways",
    "LAN": "LATAM Airlines",
    "LOT": "LOT Polish Airlines",
    "LXJ": "Flexjet",
    "MXY": "Breeze Airways",
    "NCR": "National Airlines",
    "NKS": "Spirit Airlines",
    "NOZ": "Norwegian Air Shuttle",
    "PAC": "Polar Air Cargo",
    "PDT": "Piedmont Airlines",
    "POE": "Porter Airlines",
    "QFA": "Qantas",
    "QTR": "Qatar Airways",
    "QXE": "Horizon Air",
    "ROU": "Air Canada Rouge",
    "RPA": "Republic Airways",
    "RYR": "Ryanair",
    "SAS": "Scandinavian Airlines",
    "SCX": "Sun Country Airlines",
    "SIA": "Singapore Airlines",
    "SKW": "SkyWest Airlines",
    "SVA": "Saudia",
    "SWA": "Southwest Airlines",
    "SWG": "Sunwing Airlines",
    "SWR": "Swiss International Air Lines",
    "TAP": "TAP Air Portugal",
    "THY": "Turkish Airlines",
    "TRA": "Transavia",
    "TSC": "Air Transat",
    "UAE": "Emirates",
    "UAL": "United Airlines",
    "UCA": "CommuteAir",
    "UPS": "UPS Airlines",
    "VJA": "VistaJet",
    "VIR": "Virgin Atlantic",
    "VLG": "Vueling",
    "VOI": "Volaris",
    "VTE": "Contour Airlines",
    "VXP": "Avelo Airlines",
    "WGN": "Western Global Airlines",
    "WJA": "WestJet",
    "WSN": "Advanced Air",
    "XOJ": "XOJET Aviation",
    "XSR": "Airshare",
}

# IATA flight numbers are common in provider marketing-flight fields and are
# occasionally entered directly as the ADS-B callsign. Map them to the same
# canonical ICAO identity used by route retention. Codes may contain a digit.
IATA_TO_ICAO = {
    "4B": "BTQ",
    "5X": "UPS",
    "5Y": "GTI",
    "8C": "ATN",
    "9E": "EDV",
    "9K": "KAP",
    "9X": "FDY",
    "AA": "AAL",
    "AC": "ACA",
    "AD": "AZU",
    "AF": "AFR",
    "AI": "AIC",
    "AM": "AMX",
    "AN": "WSN",
    "AS": "ASA",
    "AV": "AVA",
    "AY": "FIN",
    "AZ": "ITY",
    "B6": "JBU",
    "BA": "BAW",
    "BR": "EVA",
    "C5": "UCA",
    "CA": "CCA",
    "CM": "CMP",
    "CV": "CLX",
    "CX": "CPA",
    "CZ": "CSN",
    "DE": "CFG",
    "DL": "DAL",
    "DY": "NOZ",
    "EI": "EIN",
    "EK": "UAE",
    "ET": "ETH",
    "EY": "ETD",
    "F9": "FFT",
    "FI": "ICE",
    "FR": "RYR",
    "FX": "FDX",
    "G3": "GLO",
    "G4": "AAY",
    "G7": "GJS",
    "GB": "ABX",
    "HA": "HAL",
    "HV": "TRA",
    "IB": "IBE",
    "JL": "JAL",
    "K4": "CKS",
    "KE": "KAL",
    "KL": "KLM",
    "KQ": "KQA",
    "LA": "LAN",
    "LF": "VTE",
    "LH": "DLH",
    "LO": "LOT",
    "LX": "SWR",
    "M6": "AJT",
    "MQ": "ENY",
    "MU": "CES",
    "MX": "MXY",
    "N8": "NCR",
    "NH": "ANA",
    "NK": "NKS",
    "NZ": "ANZ",
    "OH": "JIA",
    "OO": "SKW",
    "OZ": "AAR",
    "PD": "POE",
    "PO": "PAC",
    "PT": "PDT",
    "QF": "QFA",
    "QR": "QTR",
    "QX": "QXE",
    "RV": "ROU",
    "SK": "SAS",
    "SQ": "SIA",
    "SV": "SVA",
    "SY": "SCX",
    "TK": "THY",
    "TP": "TAP",
    "TS": "TSC",
    "U2": "EZY",
    "UA": "UAL",
    "UX": "AEA",
    "VS": "VIR",
    "VY": "VLG",
    "WG": "SWG",
    "WN": "SWA",
    "WS": "WJA",
    "XE": "JSX",
    "XP": "VXP",
    "Y4": "VOI",
    "YV": "ASH",
    "YX": "RPA",
}

# Provider descriptions are more specific and always win. This deliberately
# compact fallback covers common, unambiguous ICAO designators without adding a
# paid lookup request or shipping the full, frequently revised Doc 8643 data.
AIRCRAFT_TYPE_NAMES = {
    "A318": "Airbus A318",
    "A319": "Airbus A319",
    "A320": "Airbus A320",
    "A321": "Airbus A321",
    "A20N": "Airbus A320neo",
    "A21N": "Airbus A321neo",
    "A332": "Airbus A330-200",
    "A333": "Airbus A330-300",
    "A338": "Airbus A330-800neo",
    "A339": "Airbus A330-900neo",
    "A343": "Airbus A340-300",
    "A346": "Airbus A340-600",
    "A359": "Airbus A350-900",
    "A35K": "Airbus A350-1000",
    "A388": "Airbus A380-800",
    "AT43": "ATR 42-300",
    "AT45": "ATR 42-500",
    "AT46": "ATR 42-600",
    "AT72": "ATR 72-200",
    "AT75": "ATR 72-500",
    "AT76": "ATR 72-600",
    "B712": "Boeing 717-200",
    "B733": "Boeing 737-300",
    "B734": "Boeing 737-400",
    "B735": "Boeing 737-500",
    "B736": "Boeing 737-600",
    "B737": "Boeing 737-700",
    "B738": "Boeing 737-800",
    "B739": "Boeing 737-900",
    "B37M": "Boeing 737 MAX 7",
    "B38M": "Boeing 737 MAX 8",
    "B39M": "Boeing 737 MAX 9",
    "B3XM": "Boeing 737 MAX 10",
    "B744": "Boeing 747-400",
    "B748": "Boeing 747-8",
    "B752": "Boeing 757-200",
    "B753": "Boeing 757-300",
    "B762": "Boeing 767-200",
    "B763": "Boeing 767-300",
    "B764": "Boeing 767-400",
    "B772": "Boeing 777-200",
    "B773": "Boeing 777-300",
    "B77L": "Boeing 777-200LR",
    "B77W": "Boeing 777-300ER",
    "B788": "Boeing 787-8",
    "B789": "Boeing 787-9",
    "B78X": "Boeing 787-10",
    "BCS1": "Airbus A220-100",
    "BCS3": "Airbus A220-300",
    "C152": "Cessna 152",
    "C172": "Cessna 172 Skyhawk",
    "C182": "Cessna 182 Skylane",
    "C206": "Cessna 206 Stationair",
    "C208": "Cessna 208 Caravan",
    "C25A": "Cessna Citation CJ2",
    "C25B": "Cessna Citation CJ3",
    "C25C": "Cessna Citation CJ4",
    "CRJ2": "Bombardier CRJ200",
    "CRJ7": "Bombardier CRJ700",
    "CRJ9": "Bombardier CRJ900",
    "CRJX": "Bombardier CRJ1000",
    "DA40": "Diamond DA40",
    "DA42": "Diamond DA42 Twin Star",
    "DA62": "Diamond DA62",
    "DH8A": "De Havilland Dash 8-100",
    "DH8B": "De Havilland Dash 8-200",
    "DH8C": "De Havilland Dash 8-300",
    "DH8D": "De Havilland Dash 8-400",
    "E170": "Embraer E170",
    "E190": "Embraer E190",
    "E195": "Embraer E195",
    "E75L": "Embraer E175",
    "E75S": "Embraer E175",
    "E290": "Embraer E190-E2",
    "E295": "Embraer E195-E2",
    "PA18": "Piper PA-18 Super Cub",
    "P28A": "Piper PA-28 Cherokee",
    "P28R": "Piper PA-28R Arrow",
    "PA34": "Piper PA-34 Seneca",
    "PA44": "Piper PA-44 Seminole",
    "PC12": "Pilatus PC-12",
    "PC24": "Pilatus PC-24",
    "SR20": "Cirrus SR20",
    "SR22": "Cirrus SR22",
}


class ConfigurationError(ValueError):
    """Raised for invalid user configuration."""


class ProviderError(RuntimeError):
    """Raised when a provider cannot return a usable response."""


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _remaining_request_time(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProviderError("request time budget exhausted")
    return remaining


def _fetch_json(
    url: str, headers: dict[str, str] | None = None, *, deadline: float,
) -> dict[str, Any]:
    request_headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    request_headers.update(headers or {})
    request = urllib.request.Request(url, headers=request_headers)

    try:
        timeout = min(HTTP_TIMEOUT_SECONDS, _remaining_request_time(deadline))
        with urllib.request.urlopen(request, timeout=timeout) as response:
            _remaining_request_time(deadline)
            body = response.read()
        _remaining_request_time(deadline)
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


def _fetch_provider(
    provider: str, config: dict[str, Any], deadline: float,
) -> list[dict[str, Any]]:
    """Bound caller wait, including DNS and body reads not bounded by socket timeout."""
    _remaining_request_time(deadline)
    attempt_deadline = min(deadline, time.monotonic() + PROVIDER_TIMEOUT_SECONDS)
    responses: queue.Queue = queue.Queue(maxsize=1)
    request_config = {**config, "request_deadline": attempt_deadline}

    def fetch() -> None:
        try:
            function = _fetch_fr24 if provider == "flightradar24" else _fetch_flightaware
            responses.put((function(request_config), None))
        except Exception as exc:
            responses.put((None, exc))

    # An executor context would join a stalled request on exit. A daemon worker
    # cannot hold up the result or process shutdown. Late results are discarded;
    # the worker never mutates the screen result or starts requests past deadline.
    threading.Thread(target=fetch, daemon=True).start()
    try:
        candidates, error = responses.get(timeout=_remaining_request_time(attempt_deadline))
    except queue.Empty as exc:
        raise ProviderError("request time budget exhausted") from exc
    _remaining_request_time(attempt_deadline)
    if error is not None:
        raise error
    return candidates


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


def _aircraft_name(aircraft_type: Any, provider_name: Any = None) -> str | None:
    name = _clean_text(provider_name)
    if name:
        return name
    designator = _clean_text(aircraft_type)
    return AIRCRAFT_TYPE_NAMES.get(designator.upper()) if designator else None


def _operator_name(value: Any) -> str | None:
    """Expand a known ICAO or IATA operator code, preserving provider names."""
    name = _clean_text(value)
    if not name:
        return None
    code = name.upper()
    icao = IATA_TO_ICAO.get(code, code)
    return AIRLINE_NAMES.get(icao, name)


def _flight_identity(value: Any) -> tuple[str, str | None] | None:
    """Return a canonical ICAO-style flight identity and known airline name."""
    identifier = (_clean_text(value) or "").upper()
    icao_match = re.fullmatch(r"([A-Z]{3})([0-9][A-Z0-9]{0,4})", identifier)
    if icao_match:
        icao, number = icao_match.groups()
        return (f"{icao}{number}", AIRLINE_NAMES.get(icao))

    iata_match = re.fullmatch(r"([A-Z0-9]{2})([0-9][A-Z0-9]{0,5})", identifier)
    if iata_match:
        iata, number = iata_match.groups()
        icao = IATA_TO_ICAO.get(iata)
        if icao:
            return (f"{icao}{number}", AIRLINE_NAMES.get(icao))
    return None


def _airline_name(
    operator: Any,
    callsign: Any,
    registration: Any,
    fallback_operator: Any = None,
    flight: Any = None,
) -> str | None:
    """Prefer provider identity, then callsign/flight inference, then livery."""
    name = _operator_name(operator)
    if name:
        return name

    tail = (_clean_text(registration) or "").upper()
    normalized_tail = tail.replace("-", "")
    for identifier in (callsign, flight):
        identity = _flight_identity(identifier)
        if identity and identity[0] != normalized_tail and identity[1]:
            return identity[1]

    return _operator_name(fallback_operator)


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


def _direction_deg(value: Any) -> float | None:
    """Return a provider direction normalized to true degrees."""

    number = _float(value)
    return number % 360 if number is not None else None


def select_scope_range(distance_nm: float) -> int:
    """Return the smallest supported radar range enclosing a distance."""

    distance = max(0.0, distance_nm)
    for range_nm in SCOPE_RANGES_NM:
        if distance <= range_nm:
            return range_nm
    return SCOPE_RANGES_NM[-1]


def scope_coordinates(
    distance_nm: float, bearing_deg: float, range_nm: float, map_up_bearing_deg: float
) -> tuple[float, float]:
    """Plot range and true bearing in the scope's normalized 0-100 view box."""

    radius = min(max(distance_nm / range_nm, 0.0), 1.0) * SCOPE_RADIUS
    angle = math.radians(bearing_deg - map_up_bearing_deg)
    x = SCOPE_CENTER + radius * math.sin(angle)
    y = SCOPE_CENTER - radius * math.cos(angle)
    return (round(x, 3), round(y, 3))


def _selected_direction(candidate: dict[str, Any]) -> tuple[float | None, str]:
    true_heading = candidate.get("true_heading_deg")
    if true_heading is not None:
        return (true_heading, "True heading")
    track = candidate.get("track_deg")
    if track is not None:
        return (track, "Ground track")
    return (None, "Direction unavailable")


def _scope_data(
    *,
    distance_nm: float | None,
    bearing_deg: float | None,
    direction_deg: float | None,
    map_up_bearing_deg: float,
    fallback_range_nm: float,
) -> dict[str, Any]:
    range_nm = select_scope_range(
        distance_nm if distance_nm is not None else fallback_range_nm
    )
    if distance_nm is not None and bearing_deg is not None:
        aircraft_x, aircraft_y = scope_coordinates(
            distance_nm, bearing_deg, range_nm, map_up_bearing_deg
        )
    else:
        aircraft_x, aircraft_y = (None, None)
    north_x, north_y = scope_coordinates(
        range_nm, 0, range_nm, map_up_bearing_deg
    )
    return {
        "range_nm": range_nm,
        "aircraft_x": aircraft_x,
        "aircraft_y": aircraft_y,
        "aircraft_rotation_deg": (
            round((direction_deg - map_up_bearing_deg) % 360, 1)
            if direction_deg is not None
            else None
        ),
        "north_x": north_x,
        "north_y": north_y,
        "map_up_bearing_deg": round(map_up_bearing_deg, 1),
        "map_up_compass": compass_point(map_up_bearing_deg),
    }


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
    provider_flight_id: Any = None,
    latitude: Any,
    longitude: Any,
    altitude_ft: Any = None,
    groundspeed_kts: Any = None,
    true_heading_deg: Any = None,
    track_deg: Any = None,
    vertical_rate_fpm: Any = None,
    vertical_trend: Any = None,
    observed_at: Any = None,
    callsign: Any = None,
    flight: Any = None,
    registration: Any = None,
    aircraft_type: Any = None,
    aircraft_name: Any = None,
    operator: Any = None,
    fallback_operator: Any = None,
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
        "provider_flight_id": _clean_text(provider_flight_id),
        "identifier": identifier_text,
        "callsign": callsign_text,
        "flight": flight_text,
        "registration": registration_text,
        "aircraft_type": _clean_text(aircraft_type),
        "aircraft_name": _clean_text(aircraft_name),
        "operator": _airline_name(
            operator, callsign_text, registration_text, fallback_operator, flight_text
        ),
        "origin": _clean_text(origin),
        "destination": _clean_text(destination),
        "latitude": lat,
        "longitude": lon,
        "altitude_ft": altitude,
        "groundspeed_kts": _float(groundspeed_kts),
        "true_heading_deg": _direction_deg(true_heading_deg),
        "track_deg": _direction_deg(track_deg),
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
            provider_flight_id=row.get("fr24_id"),
            callsign=row.get("callsign"),
            flight=row.get("flight"),
            registration=row.get("reg"),
            aircraft_type=row.get("type"),
            aircraft_name=_coalesce(row.get("aircraft_name"), row.get("description"), row.get("desc")),
            operator=row.get("operating_as"),
            fallback_operator=row.get("painted_as"),
            origin=_coalesce(row.get("orig_iata"), row.get("orig_icao")),
            destination=_coalesce(row.get("dest_iata"), row.get("dest_icao")),
            latitude=row.get("lat"),
            longitude=row.get("lon"),
            altitude_ft=row.get("alt"),
            groundspeed_kts=row.get("gspeed"),
            track_deg=row.get("track"),
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
            operator=_coalesce(_clean_text(row.get("operator_icao")), row.get("operator")),
            aircraft_name=_coalesce(row.get("aircraft_name"), row.get("description"), row.get("desc")),
            origin=_airport_code(row.get("origin")),
            destination=_airport_code(row.get("destination")),
            latitude=position.get("latitude"),
            longitude=position.get("longitude"),
            altitude_ft=altitude_hundreds * 100 if altitude_hundreds is not None else None,
            groundspeed_kts=position.get("groundspeed"),
            track_deg=position.get("heading"),
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
            aircraft_name=row.get("desc"),
            operator=row.get("ownOp"),
            latitude=row.get("lat"),
            longitude=row.get("lon"),
            altitude_ft=altitude,
            groundspeed_kts=row.get("gs"),
            true_heading_deg=row.get("true_heading"),
            track_deg=row.get("track"),
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
            deadline=config["request_deadline"],
        )
        all_candidates.extend(normalize_fr24(payload))
    return all_candidates


def _fetch_fr24_summary_route(
    candidate: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any]:
    """Fill a selected FR24 record from its same-provider light summary."""
    flight_id = _clean_text(candidate.get("provider_flight_id"))
    if not flight_id:
        return candidate
    params = urllib.parse.urlencode({"flight_ids": flight_id, "limit": 1})
    payload = _fetch_json(
        f"{FR24_SUMMARY_URL}?{params}",
        {
            "Authorization": f"Bearer {config['fr24_api_token']}",
            "Accept-Version": "v1",
        },
        deadline=config["request_deadline"],
    )
    summary = next(
        (
            row
            for row in payload.get("data") or []
            if isinstance(row, dict) and _clean_text(row.get("fr24_id")) == flight_id
        ),
        None,
    )
    if summary is None:
        return candidate

    summary_origin = _coalesce(summary.get("orig_iata"), summary.get("orig_icao"))
    summary_destination = _coalesce(
        summary.get("dest_iata_actual"),
        summary.get("dest_iata"),
        summary.get("dest_icao_actual"),
        summary.get("dest_icao"),
    )
    for current, replacement in (
        (candidate.get("origin"), summary_origin),
        (candidate.get("destination"), summary_destination),
    ):
        current_code = _route_token(current)
        replacement_code = _route_token(replacement)
        # Light summaries use ICAO airport codes while live records prefer IATA.
        # Only reject a same-code-system conflict; the fr24_id already identifies
        # the exact flight across the two same-provider responses.
        if (
            current_code
            and replacement_code
            and len(current_code) == len(replacement_code)
            and current_code != replacement_code
        ):
            return candidate

    enriched = dict(candidate)
    enriched["origin"] = _clean_text(_coalesce(candidate.get("origin"), summary_origin))
    enriched["destination"] = _clean_text(
        _coalesce(candidate.get("destination"), summary_destination)
    )
    for key, summary_key in (
        ("callsign", "callsign"),
        ("flight", "flight"),
        ("registration", "reg"),
        ("aircraft_type", "type"),
    ):
        enriched[key] = _clean_text(_coalesce(candidate.get(key), summary.get(summary_key)))
    if not enriched.get("operator"):
        enriched["operator"] = _airline_name(
            summary.get("operating_as"),
            enriched.get("callsign"),
            enriched.get("registration"),
            summary.get("painted_as"),
            enriched.get("flight"),
        )
    enriched["route_enriched"] = bool(
        enriched.get("origin") and enriched.get("destination")
    )
    return enriched


def _try_fr24_summary_route(
    candidate: dict[str, Any], config: dict[str, Any], deadline: float
) -> dict[str, Any]:
    """Bound optional route enrichment so it can never delay ADSB fallback."""
    if (
        candidate.get("source") != "flightradar24"
        or not candidate.get("provider_flight_id")
        or time.monotonic() >= deadline
    ):
        return candidate

    attempt_deadline = min(
        deadline, time.monotonic() + ROUTE_ENRICHMENT_TIMEOUT_SECONDS
    )
    responses: queue.Queue = queue.Queue(maxsize=1)
    request_candidate = dict(candidate)
    request_config = {**config, "request_deadline": attempt_deadline}

    def fetch() -> None:
        try:
            responses.put((_fetch_fr24_summary_route(request_candidate, request_config), None))
        except Exception as exc:
            responses.put((None, exc))

    threading.Thread(target=fetch, daemon=True).start()
    try:
        enriched, error = responses.get(timeout=_remaining_request_time(attempt_deadline))
    except (queue.Empty, ProviderError):
        return candidate
    if error is not None or not isinstance(enriched, dict):
        return candidate
    return enriched


def _fetch_flightaware(config: dict[str, Any]) -> list[dict[str, Any]]:
    api_key = config["flightaware_api_key"]
    all_candidates: list[dict[str, Any]] = []
    for north, south, west, east in bounding_boxes(config["latitude"], config["longitude"], config["radius_nm"]):
        query = f"{{range lat {south:.6f} {north:.6f}}} {{range lon {west:.6f} {east:.6f}}} {{true inAir}}"
        params = urllib.parse.urlencode({"query": query, "max_pages": 1})
        payload = _fetch_json(
            f"{FLIGHTAWARE_URL}?{params}", {"x-apikey": api_key},
            deadline=config["request_deadline"],
        )
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

    # The location picker stores a comma-separated pair. Legacy installations
    # can still supply separate fields until their location is saved again.
    coordinates = fields
    if "lat_lon" in fields:
        location = fields["lat_lon"]
        if not isinstance(location, str) or len(location.split(",")) != 2:
            raise ConfigurationError("lat_lon must contain latitude,longitude")
        lat, lon = location.split(",")
        if not lat.strip() or not lon.strip():
            raise ConfigurationError("lat_lon must contain latitude,longitude")
        coordinates = {"latitude": lat.strip(), "longitude": lon.strip()}

    latitude = _required_float(
        _local_env_field(coordinates, "latitude", "TRACKER_LATITUDE", 37.7749),
        "latitude",
        -90,
        90,
    )
    longitude = _required_float(
        _local_env_field(coordinates, "longitude", "TRACKER_LONGITUDE", -122.4194),
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
    map_up_bearing = _required_float(
        _local_env_field(
            fields, "map_up_bearing_deg", "TRACKER_MAP_UP_BEARING_DEG", 0
        ),
        "map_up_bearing_deg",
        0,
        359,
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
        "map_up_bearing_deg": map_up_bearing,
        "provider_order": provider_order,
        "fr24_api_token": _clean_text(
            _local_env_field(fields, "fr24_api_token", "FR24_API_TOKEN")
        ),
        "flightaware_api_key": _clean_text(
            _local_env_field(fields, "flightaware_api_key", "FLIGHTAWARE_AEROAPI_KEY")
        ),
    }


def _route_token(value: Any) -> str:
    """Normalize identity/airport codes and bound the saved-state payload."""
    if not isinstance(value, str):
        return ""
    token = value.strip().upper().replace("-", "")
    return token if re.fullmatch(r"[A-Z0-9]{1,16}", token) else ""


def _route_record(candidate: Any, now: datetime) -> dict[str, str] | None:
    if not isinstance(candidate, dict):
        return None
    identity = _flight_identity(candidate.get("callsign")) or _flight_identity(
        candidate.get("flight")
    )
    fields = {
        "callsign": identity[0] if identity else "",
        "registration": _route_token(candidate.get("registration")),
        "origin": _route_token(candidate.get("origin")),
        "destination": _route_token(candidate.get("destination")),
    }
    if candidate.get("registration") and not fields["registration"]:
        return None
    # A tail number identifies an aircraft, not a flight. Never retain its route.
    if (not fields["callsign"] or fields["callsign"] == fields["registration"]
            or not fields["origin"] or not fields["destination"]):
        return None
    source = candidate.get("source")
    observed_at = _parse_time(candidate.get("observed_at"))
    if source not in ("flightradar24", "flightaware") or observed_at is None:
        return None
    if not 0 <= (now - observed_at).total_seconds() < ROUTE_MAX_AGE_SECONDS:
        return None
    return {**fields, "source": source, "observed_at": observed_at.isoformat()}


def _saved_route(input: dict[str, Any], now: datetime) -> dict[str, str] | None:
    trmnl = input.get("trmnl")
    state = trmnl.get("state") if isinstance(trmnl, dict) else None
    return _route_record(state.get("last_route"), now) if isinstance(state, dict) else None


def _retain_route(
    candidate: dict[str, Any], saved: dict[str, str] | None,
    providers: list[str], now: datetime,
) -> dict[str, str] | None:
    """Fill missing route metadata without replacing current position or live route."""
    candidate["route_retained"] = False
    complete_route = bool(candidate.get("origin") and candidate.get("destination"))
    candidate["route_source"] = candidate["source"] if complete_route else None
    if complete_route:
        return _route_record(candidate, now)
    if not saved or saved["source"] not in providers:
        return saved
    identity = _flight_identity(candidate.get("callsign")) or _flight_identity(
        candidate.get("flight")
    )
    callsign = identity[0] if identity else ""
    registration = _route_token(candidate.get("registration"))
    if callsign != saved["callsign"] or callsign == registration:
        return saved
    if registration and saved["registration"] and registration != saved["registration"]:
        return None
    # A changed endpoint can indicate a new leg or a diversion. Do not splice routes.
    if any(candidate.get(key) and _route_token(candidate[key]) != saved[key]
           for key in ("origin", "destination")):
        return None
    if providers.index(saved["source"]) > providers.index(candidate["source"]):
        return saved
    candidate.update(origin=saved["origin"], destination=saved["destination"],
                     route_source=saved["source"], route_retained=True)
    # Reusing the route must not renew its expiry on every fallback refresh.
    return saved


def _format_aircraft(candidate: dict[str, Any]) -> dict[str, Any]:
    altitude = candidate.get("altitude_ft")
    speed = candidate.get("groundspeed_kts")
    direction, direction_label = _selected_direction(candidate)
    vertical_rate = candidate.get("vertical_rate_fpm")
    origin = candidate.get("origin")
    destination = candidate.get("destination")
    age = candidate.get("age_minutes")
    aircraft_type = candidate.get("aircraft_type")
    aircraft_name = _aircraft_name(aircraft_type, candidate.get("aircraft_name"))
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
        "aircraft_name": aircraft_name,
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
        "true_heading_deg": (
            round(candidate["true_heading_deg"])
            if candidate.get("true_heading_deg") is not None
            else None
        ),
        "track_deg": (
            round(candidate["track_deg"])
            if candidate.get("track_deg") is not None
            else None
        ),
        "direction_deg": round(direction) if direction is not None else None,
        "direction_compass": compass_point(direction),
        "direction_label": direction_label,
        # Compatibility aliases used by the compact layouts.
        "heading_deg": round(direction) if direction is not None else None,
        "heading_compass": compass_point(direction),
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
            value for value in (aircraft_name or aircraft_type, candidate.get("registration")) if value
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
        "scope": (
            _scope_data(
                distance_nm=None,
                bearing_deg=None,
                direction_deg=None,
                map_up_bearing_deg=config["map_up_bearing_deg"],
                fallback_range_nm=config["radius_nm"],
            )
            if config
            else None
        ),
        "updated_at": now.isoformat(timespec="minutes").replace("+00:00", "Z"),
    }


def run(input: dict[str, Any]) -> dict[str, Any]:
    """TRMNL serverless entrypoint."""

    request_deadline = time.monotonic() + PAID_PROVIDER_BUDGET_SECONDS
    now = _now_utc()
    saved_route = _saved_route(input, now)
    route_state = {"last_route": saved_route} if saved_route else {}
    try:
        config = _extract_config(input)
    except ConfigurationError as exc:
        result = _base_result(None, now)
        result["trmnl_state"] = route_state
        result.update(status="configuration_error", message=f"Configuration error: {exc}")
        return result

    result = _base_result(config, now)
    result["trmnl_state"] = route_state
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
            if provider in ("flightradar24", "flightaware"):
                candidates = _fetch_provider(provider, config, request_deadline)
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

        saved_route = _retain_route(nearest, saved_route, providers, now)
        if not (nearest.get("origin") and nearest.get("destination")):
            nearest = _try_fr24_summary_route(nearest, config, request_deadline)
            saved_route = _retain_route(nearest, saved_route, providers, now)
        result["trmnl_state"] = {"last_route": saved_route} if saved_route else {}
        aircraft = _format_aircraft(nearest)
        result.update(
            has_aircraft=True,
            status="ok",
            message="",
            provider_used=provider,
            candidate_count=candidate_count,
            aircraft=aircraft,
            scope=_scope_data(
                distance_nm=nearest["distance_nm"],
                bearing_deg=nearest["bearing_deg"],
                direction_deg=_selected_direction(nearest)[0],
                map_up_bearing_deg=config["map_up_bearing_deg"],
                fallback_range_nm=config["radius_nm"],
            ),
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
