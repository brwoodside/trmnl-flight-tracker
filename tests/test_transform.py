import copy
import importlib.util
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("flight_transform", ROOT / "src" / "transform.py")
transform = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(transform)

FIXED_NOW = datetime(2026, 8, 25, 12, 2, tzinfo=timezone.utc)


def fixture(name):
    with (ROOT / "fixtures" / name).open() as handle:
        return json.load(handle)


def plugin_input(payload=None, **overrides):
    fields = {
        "latitude": "37.7749",
        "longitude": "-122.4194",
        "location_label": "Test roof",
        "radius_nm": "20",
        "max_age_minutes": "5",
        "provider_order": "auto",
        "fr24_api_token": "",
        "flightaware_api_key": "",
    }
    fields.update(overrides)
    data = copy.deepcopy(payload or {})
    data["trmnl"] = {"plugin_settings": {"custom_fields_values": fields}}
    return data


class GeometryTests(unittest.TestCase):
    def test_one_degree_latitude_is_about_sixty_nautical_miles(self):
        self.assertAlmostEqual(transform.haversine_nm(0, 0, 1, 0), 60.04, places=1)

    def test_cardinal_bearing(self):
        self.assertAlmostEqual(transform.initial_bearing_deg(0, 0, 1, 0), 0.0, places=2)
        self.assertEqual(transform.compass_point(270), "W")

    def test_antimeridian_search_splits_into_two_boxes(self):
        boxes = transform.bounding_boxes(10, 179.9, 100)
        self.assertEqual(len(boxes), 2)
        self.assertEqual(boxes[0][3], 180.0)
        self.assertEqual(boxes[1][2], -180.0)


class NormalizationTests(unittest.TestCase):
    def test_adsblol_ignores_ground_aircraft(self):
        normalized = transform.normalize_adsblol(fixture("adsblol.json"))
        self.assertEqual(len(normalized), 2)
        self.assertNotIn("GROUND1", {item["identifier"] for item in normalized})

    def test_flightaware_altitude_is_hundreds_of_feet(self):
        normalized = transform.normalize_flightaware(fixture("flightaware.json"))
        self.assertEqual(normalized[0]["altitude_ft"], 14500)
        self.assertEqual(normalized[0]["origin"], "SJC")
        self.assertEqual(normalized[0]["vertical_trend"], "Climbing")

    def test_fr24_full_fields_are_preserved(self):
        normalized = transform.normalize_fr24(fixture("fr24.json"))
        self.assertEqual(normalized[0]["identifier"], "SK7679")
        self.assertEqual(normalized[0]["destination"], "SEA")
        self.assertEqual(normalized[0]["registration"], "EI-SIN")


class RunTests(unittest.TestCase):
    def setUp(self):
        self.clock = mock.patch.object(transform, "_now_utc", return_value=FIXED_NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def test_open_provider_works_without_credentials(self):
        result = transform.run(plugin_input(fixture("adsblol.json")))
        self.assertTrue(result["has_aircraft"])
        self.assertEqual(result["provider_used"], "adsblol")
        self.assertEqual(result["aircraft"]["identifier"], "UAL123")
        self.assertEqual(result["aircraft"]["route"], "Route unavailable")
        self.assertLess(result["aircraft"]["distance_nm"], 1)

    def test_fr24_is_preferred_when_token_is_present(self):
        with mock.patch.object(transform, "_fetch_json", return_value=fixture("fr24.json")) as fetch:
            result = transform.run(
                plugin_input(fixture("adsblol.json"), fr24_api_token="fr24-secret")
            )
        self.assertEqual(result["provider_used"], "flightradar24")
        self.assertEqual(result["aircraft"]["route"], "SFO → SEA")
        request_headers = fetch.call_args.args[1]
        self.assertEqual(request_headers["Authorization"], "Bearer fr24-secret")
        self.assertEqual(request_headers["Accept-Version"], "v1")

    def test_flightaware_is_used_after_fr24_error(self):
        def fake_fetch(url, headers=None):
            if url.startswith(transform.FR24_URL):
                raise transform.ProviderError("HTTP 429")
            return fixture("flightaware.json")

        with mock.patch.object(transform, "_fetch_json", side_effect=fake_fetch):
            result = transform.run(
                plugin_input(
                    fixture("adsblol.json"),
                    fr24_api_token="fr24-secret",
                    flightaware_api_key="fa-secret",
                )
            )
        self.assertEqual(result["provider_used"], "flightaware")
        self.assertEqual(result["aircraft"]["identifier"], "AS331")
        self.assertEqual(result["aircraft"]["route"], "SJC → PDX")
        self.assertEqual(result["provider_attempts"][0]["status"], "error")

    def test_flightaware_can_be_first(self):
        with mock.patch.object(transform, "_fetch_json", return_value=fixture("flightaware.json")) as fetch:
            result = transform.run(
                plugin_input(
                    fixture("adsblol.json"),
                    provider_order="flightaware_first",
                    fr24_api_token="fr24-secret",
                    flightaware_api_key="fa-secret",
                )
            )
        self.assertEqual(result["provider_used"], "flightaware")
        self.assertTrue(fetch.call_args.args[0].startswith(transform.FLIGHTAWARE_URL))
        self.assertEqual(fetch.call_args.args[1]["x-apikey"], "fa-secret")

    def test_public_fallback_survives_both_commercial_errors(self):
        with mock.patch.object(
            transform, "_fetch_json", side_effect=transform.ProviderError("request unavailable")
        ):
            result = transform.run(
                plugin_input(
                    fixture("adsblol.json"),
                    fr24_api_token="fr24-secret",
                    flightaware_api_key="fa-secret",
                )
            )
        self.assertEqual(result["provider_used"], "adsblol")
        self.assertEqual([a["status"] for a in result["provider_attempts"]], ["error", "error", "selected"])

    def test_empty_sky_is_not_reported_as_provider_failure(self):
        payload = {"ac": [], "now": int(FIXED_NOW.timestamp() * 1000)}
        result = transform.run(plugin_input(payload, provider_order="open_only"))
        self.assertFalse(result["has_aircraft"])
        self.assertEqual(result["status"], "no_aircraft")

    def test_stale_position_is_rejected(self):
        payload = fixture("adsblol.json")
        payload["now"] = int(FIXED_NOW.timestamp() * 1000)
        for aircraft in payload["ac"]:
            aircraft["seen_pos"] = 601
        result = transform.run(plugin_input(payload, provider_order="open_only"))
        self.assertEqual(result["status"], "no_aircraft")

    def test_invalid_location_returns_displayable_error(self):
        result = transform.run(plugin_input({"ac": []}, latitude="north"))
        self.assertEqual(result["status"], "configuration_error")
        self.assertIn("latitude", result["message"])

    def test_local_trmnlp_env_placeholders_are_resolved(self):
        data = plugin_input(fixture("adsblol.json"))
        fields = data["trmnl"]["plugin_settings"]["custom_fields_values"]
        fields["latitude"] = "{{ env.TRACKER_LATITUDE | default: 37.7749 }}"
        fields["longitude"] = "{{ env.TRACKER_LONGITUDE | default: -122.4194 }}"
        fields["fr24_api_token"] = "{{ env.FR24_API_TOKEN }}"
        with mock.patch.dict(
            transform.os.environ,
            {
                "TRACKER_LATITUDE": "37.7749",
                "TRACKER_LONGITUDE": "-122.4194",
                "FR24_API_TOKEN": "",
            },
            clear=False,
        ):
            result = transform.run(data)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["provider_used"], "adsblol")


if __name__ == "__main__":
    unittest.main()
