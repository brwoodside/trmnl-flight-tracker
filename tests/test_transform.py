import copy
import importlib.util
import json
import unittest
from datetime import datetime, timedelta, timezone
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
        "lat_lon": "37.7749,-122.4194",
        "location_label": "Test roof",
        "radius_nm": "20",
        "map_up_bearing_deg": "0",
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

    def test_scope_range_uses_smallest_enclosing_step(self):
        cases = {
            0: 1,
            1: 1,
            1.001: 2,
            2: 2,
            2.001: 5,
            10.001: 20,
            20.001: 50,
            100.001: 250,
            250: 250,
        }
        for distance, expected in cases.items():
            with self.subTest(distance=distance):
                self.assertEqual(transform.select_scope_range(distance), expected)

    def test_cardinal_scope_coordinates_are_north_up(self):
        cases = {
            0: (50, 10),
            90: (90, 50),
            180: (50, 90),
            270: (10, 50),
        }
        for bearing, expected in cases.items():
            with self.subTest(bearing=bearing):
                self.assertEqual(
                    transform.scope_coordinates(10, bearing, 10, 0), expected
                )

    def test_ninety_degree_map_up_rotates_position_and_true_north(self):
        self.assertEqual(transform.scope_coordinates(10, 90, 10, 90), (50, 10))
        scope = transform._scope_data(
            distance_nm=10,
            bearing_deg=90,
            direction_deg=180,
            map_up_bearing_deg=90,
            fallback_range_nm=20,
        )
        self.assertEqual((scope["aircraft_x"], scope["aircraft_y"]), (50, 10))
        self.assertEqual(scope["aircraft_rotation_deg"], 90)
        self.assertEqual((scope["north_x"], scope["north_y"]), (10, 50))


class NormalizationTests(unittest.TestCase):
    def test_adsblol_resolves_airlines_without_owner_metadata(self):
        cases = {
            "UAL1083 ": "United Airlines",
            " asa924 ": "Alaska Airlines",
            "SKW410Z": "SkyWest Airlines",
            "FDX123": "FedEx Express",
        }
        for callsign, expected in cases.items():
            with self.subTest(callsign=callsign):
                payload = fixture("adsblol.json")
                self.assertNotIn("ownOp", payload["ac"][0])
                payload["ac"][0]["flight"] = callsign
                self.assertEqual(transform.normalize_adsblol(payload)[0]["operator"], expected)

    def test_adsblol_preserves_provider_name_over_callsign(self):
        payload = fixture("adsblol.json")
        payload["ac"][0]["ownOp"] = "  Custom Aircraft Operator  "
        self.assertEqual(
            transform.normalize_adsblol(payload)[0]["operator"], "Custom Aircraft Operator"
        )

    def test_blank_owner_metadata_still_uses_callsign(self):
        payload = fixture("adsblol.json")
        payload["ac"][0]["ownOp"] = "  "
        self.assertEqual(transform.normalize_adsblol(payload)[0]["operator"], "United Airlines")

    def test_unrecognized_or_nonflight_callsigns_remain_unavailable(self):
        for callsign in (None, "", "  ", "N123UA", "C-FABC", "ZZZ123", "UA123", "UAL",
                         "UALTEST", "UAL123456", "UAL123!", "UAL 123", "UAL１２３"):
            with self.subTest(callsign=callsign):
                payload = fixture("adsblol.json")
                payload["ac"][0]["flight"] = callsign
                self.assertIsNone(transform.normalize_adsblol(payload)[0]["operator"])

    def test_callsign_equal_to_registration_is_not_an_airline(self):
        payload = fixture("adsblol.json")
        payload["ac"][0].update(flight="ual123 ", r="UAL123")
        self.assertIsNone(transform.normalize_adsblol(payload)[0]["operator"])

    def test_fr24_expands_operator_code(self):
        payload = fixture("fr24.json")
        payload["data"][0]["operating_as"] = " sas "
        self.assertEqual(transform.normalize_fr24(payload)[0]["operator"], "Scandinavian Airlines")

    def test_fr24_operating_airline_wins_over_callsign_and_livery(self):
        payload = fixture("fr24.json")
        payload["data"][0].update(operating_as="SKW", painted_as="UAL", callsign="UAL123")
        self.assertEqual(transform.normalize_fr24(payload)[0]["operator"], "SkyWest Airlines")

    def test_fr24_callsign_wins_over_livery_when_operator_missing(self):
        payload = fixture("fr24.json")
        payload["data"][0].update(operating_as=" ", painted_as="UAL", callsign="SKW410Z")
        self.assertEqual(transform.normalize_fr24(payload)[0]["operator"], "SkyWest Airlines")

    def test_fr24_livery_is_last_resort(self):
        payload = fixture("fr24.json")
        payload["data"][0].update(operating_as=None, callsign=None)
        self.assertEqual(transform.normalize_fr24(payload)[0]["operator"], "Scandinavian Airlines")

    def test_fr24_preserves_unknown_explicit_operator(self):
        for operator in ("ZZZ", "Custom Airline"):
            with self.subTest(operator=operator):
                payload = fixture("fr24.json")
                payload["data"][0]["operating_as"] = operator
                self.assertEqual(transform.normalize_fr24(payload)[0]["operator"], operator)

    def test_flightaware_resolves_callsign_without_operator(self):
        self.assertEqual(
            transform.normalize_flightaware(fixture("flightaware.json"))[0]["operator"],
            "Alaska Airlines",
        )

    def test_flightaware_prefers_explicit_operator_over_callsign(self):
        for fields in ({"operator_icao": "SKW", "operator": "OO"},
                       {"operator": "SKW"},
                       {"operator_icao": " ", "operator": "SKW"}):
            with self.subTest(fields=fields):
                payload = fixture("flightaware.json")
                payload["flights"][0].update(fields)
                self.assertEqual(transform.normalize_flightaware(payload)[0]["operator"], "SkyWest Airlines")

    def test_flightaware_preserves_supplied_name(self):
        payload = fixture("flightaware.json")
        payload["flights"][0]["operator"] = "Custom Airline"
        self.assertEqual(transform.normalize_flightaware(payload)[0]["operator"], "Custom Airline")

    def test_adsblol_ignores_ground_aircraft(self):
        normalized = transform.normalize_adsblol(fixture("adsblol.json"))
        self.assertEqual(len(normalized), 2)
        self.assertNotIn("GROUND1", {item["identifier"] for item in normalized})

    def test_local_aircraft_name_fills_optional_provider_description(self):
        self.assertEqual(transform._aircraft_name("PA18"), "Piper PA-18 Super Cub")
        self.assertEqual(
            transform._aircraft_name("PA18", "Custom provider description"),
            "Custom provider description",
        )

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

    def test_provider_motion_fields_are_ground_tracks(self):
        self.assertEqual(transform.normalize_fr24(fixture("fr24.json"))[0]["track_deg"], 219)
        self.assertEqual(
            transform.normalize_flightaware(fixture("flightaware.json"))[0]["track_deg"],
            5,
        )
        adsb = transform.normalize_adsblol(fixture("adsblol.json"))[0]
        self.assertEqual(adsb["track_deg"], 132)
        self.assertIsNone(adsb["true_heading_deg"])

    def test_adsblol_preserves_true_heading_separately(self):
        payload = fixture("adsblol.json")
        payload["ac"][0]["true_heading"] = 126
        normalized = transform.normalize_adsblol(payload)[0]
        self.assertEqual(normalized["true_heading_deg"], 126)
        self.assertEqual(normalized["track_deg"], 132)


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
        self.assertEqual(result["aircraft"]["operator"], "United Airlines")
        self.assertEqual(result["aircraft"]["aircraft_name"], "BOEING 737-800")
        self.assertEqual(result["aircraft"]["aircraft_label"], "BOEING 737-800 · N123UA")
        self.assertEqual(result["aircraft"]["route"], "Route unavailable")
        self.assertLess(result["aircraft"]["distance_nm"], 1)
        self.assertEqual(result["scope"]["range_nm"], 1)
        self.assertEqual(result["aircraft"]["direction_label"], "Ground track")
        self.assertEqual(result["aircraft"]["heading_deg"], 132)

    def test_true_heading_takes_precedence_over_ground_track(self):
        payload = fixture("adsblol.json")
        payload["ac"][0]["true_heading"] = 87
        result = transform.run(plugin_input(payload, provider_order="open_only"))
        self.assertEqual(result["aircraft"]["true_heading_deg"], 87)
        self.assertEqual(result["aircraft"]["track_deg"], 132)
        self.assertEqual(result["aircraft"]["direction_deg"], 87)
        self.assertEqual(result["aircraft"]["direction_label"], "True heading")
        self.assertEqual(result["aircraft"]["heading_deg"], 87)

    def test_custom_map_up_rotates_aircraft_glyph(self):
        result = transform.run(
            plugin_input(
                fixture("adsblol.json"),
                provider_order="open_only",
                map_up_bearing_deg="90",
            )
        )
        self.assertEqual(result["scope"]["map_up_bearing_deg"], 90)
        self.assertEqual(result["scope"]["map_up_compass"], "E")
        self.assertEqual(result["scope"]["aircraft_rotation_deg"], 42)

    def test_missing_orientation_uses_dot_fallback(self):
        payload = fixture("adsblol.json")
        for aircraft in payload["ac"]:
            aircraft.pop("track", None)
            aircraft.pop("true_heading", None)
        result = transform.run(plugin_input(payload, provider_order="open_only"))
        self.assertIsNone(result["aircraft"]["direction_deg"])
        self.assertEqual(result["aircraft"]["direction_label"], "Direction unavailable")
        self.assertIsNone(result["aircraft"]["heading_deg"])
        self.assertIsNone(result["scope"]["aircraft_rotation_deg"])

    def test_fr24_is_preferred_when_token_is_present(self):
        with mock.patch.object(transform, "_fetch_json", return_value=fixture("fr24.json")) as fetch:
            result = transform.run(
                plugin_input(fixture("adsblol.json"), fr24_api_token="fr24-secret")
            )
        self.assertEqual(result["provider_used"], "flightradar24")
        self.assertEqual(result["aircraft"]["route"], "SFO → SEA")
        request_headers = fetch.call_args.args[1]
        self.assertEqual(request_headers["Authorization"], "Bearer fr24-secret")
        self.assertEqual(result["aircraft"]["operator"], "Scandinavian Airlines")
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
        self.assertEqual(result["aircraft"]["operator"], "Alaska Airlines")
        self.assertEqual(result["aircraft"]["identifier"], "AS331")
        self.assertEqual(result["aircraft"]["aircraft_name"], "Boeing 737 MAX 9")
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
        result = transform.run(plugin_input({"ac": []}, lat_lon="north,-122.4194"))
        self.assertEqual(result["status"], "configuration_error")
        self.assertIn("latitude", result["message"])

    def test_map_up_bearing_defaults_to_north(self):
        data = plugin_input(fixture("adsblol.json"), provider_order="open_only")
        del data["trmnl"]["plugin_settings"]["custom_fields_values"]["map_up_bearing_deg"]
        result = transform.run(data)
        self.assertEqual(result["scope"]["map_up_bearing_deg"], 0)
        self.assertEqual(result["scope"]["map_up_compass"], "N")

    def test_valid_map_up_bearing_is_accepted(self):
        result = transform.run(
            plugin_input(
                fixture("adsblol.json"),
                provider_order="open_only",
                map_up_bearing_deg="359",
            )
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["scope"]["map_up_bearing_deg"], 359)

    def test_invalid_map_up_bearing_returns_configuration_error(self):
        for bearing in ("-1", "360", "north"):
            with self.subTest(bearing=bearing):
                result = transform.run(
                    plugin_input(fixture("adsblol.json"), map_up_bearing_deg=bearing)
                )
                self.assertEqual(result["status"], "configuration_error")
                self.assertIn("map_up_bearing_deg", result["message"])

    def test_local_trmnlp_env_placeholders_are_resolved(self):
        data = plugin_input(fixture("adsblol.json"))
        fields = data["trmnl"]["plugin_settings"]["custom_fields_values"]
        fields["lat_lon"] = (
            "{{ env.TRACKER_LATITUDE | default: 37.7749 }},"
            "{{ env.TRACKER_LONGITUDE | default: -122.4194 }}"
        )
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


class RouteRetentionTests(unittest.TestCase):
    def setUp(self):
        self.clock = mock.patch.object(transform, "_now_utc", return_value=FIXED_NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.fr24 = fixture("fr24.json")
        self.fr24["data"][0].update(callsign="UAL123", flight="UA123", reg="N123UA")
        self.fa = fixture("flightaware.json")
        self.fa["flights"][0].update(ident="UAL123", ident_icao="UAL123",
                                     ident_iata="UA123", registration="N123UA")

    def first_result(self, provider="flightradar24", order="auto"):
        with mock.patch.object(transform, "_fetch_json", return_value=(
            self.fr24 if provider == "flightradar24" else self.fa
        )):
            return transform.run(plugin_input(
                fixture("adsblol.json"), provider_order=order,
                fr24_api_token="token" if provider == "flightradar24" else "",
                flightaware_api_key="key" if provider == "flightaware" else "",
            ))

    def fallback(self, state, payload=None, **fields):
        data = plugin_input(fixture("adsblol.json") if payload is None else payload,
                            **fields)
        data["trmnl"]["state"] = state
        with mock.patch.object(transform, "_fetch_json",
                               side_effect=transform.ProviderError("HTTP 429")):
            return transform.run(data)

    def test_fr24_route_survives_repeated_fallbacks_with_live_adsb_position(self):
        first = self.first_result()
        state = first["trmnl_state"]
        for _ in range(3):
            result = self.fallback(state, fr24_api_token="token")
            aircraft = result["aircraft"]
            self.assertEqual(result["provider_used"], "adsblol")
            self.assertEqual(aircraft["route"], "SFO → SEA")
            self.assertEqual(aircraft["route_source"], "flightradar24")
            self.assertTrue(aircraft["route_retained"])
            self.assertEqual(aircraft["latitude"], fixture("adsblol.json")["ac"][0]["lat"])
            self.assertEqual(aircraft["altitude_ft"], fixture("adsblol.json")["ac"][0]["alt_baro"])
            self.assertEqual(result["trmnl_state"], first["trmnl_state"])
            state = result["trmnl_state"]
        self.assertLess(len(json.dumps(state).encode()), 8192)
        self.assertNotIn("latitude", state["last_route"])

    def test_flightaware_first_route_survives_adsb_fallback(self):
        first = self.first_result("flightaware", "flightaware_first")
        result = self.fallback(first["trmnl_state"], provider_order="flightaware_first")
        self.assertEqual(result["aircraft"]["route"], "SJC → PDX")
        self.assertEqual(result["aircraft"]["route_source"], "flightaware")

    def test_flightaware_can_inherit_fr24_route_without_mixing_endpoints(self):
        first = self.first_result()
        self.fa["flights"][0].update(origin={"code_iata": "SFO"}, destination=None)
        data = plugin_input(flightaware_api_key="key")
        data["trmnl"]["state"] = first["trmnl_state"]
        with mock.patch.object(transform, "_fetch_json", return_value=self.fa):
            result = transform.run(data)
        self.assertEqual(result["provider_used"], "flightaware")
        self.assertEqual(result["aircraft"]["route"], "SFO → SEA")

    def test_live_route_wins_and_replaces_saved_route(self):
        first = self.first_result()
        data = plugin_input(flightaware_api_key="key")
        data["trmnl"]["state"] = first["trmnl_state"]
        with mock.patch.object(transform, "_fetch_json", return_value=self.fa):
            result = transform.run(data)
        self.assertEqual(result["aircraft"]["route"], "SJC → PDX")
        self.assertFalse(result["aircraft"]["route_retained"])
        self.assertEqual(result["trmnl_state"]["last_route"]["source"], "flightaware")

    def test_conflicting_partial_route_invalidates_saved_route(self):
        first = self.first_result()
        self.fa["flights"][0]["destination"] = None
        data = plugin_input(flightaware_api_key="key")
        data["trmnl"]["state"] = first["trmnl_state"]
        with mock.patch.object(transform, "_fetch_json", return_value=self.fa):
            result = transform.run(data)
        self.assertFalse(result["aircraft"]["route_available"])
        self.assertEqual(result["trmnl_state"], {})

    def test_different_flight_or_conflicting_tail_does_not_inherit_route(self):
        state = self.first_result()["trmnl_state"]
        for changes in ({"flight": "UAL456"}, {"r": "N456UA"},
                        {"flight": "N123UA"}, {"flight": ""}):
            with self.subTest(changes=changes):
                payload = fixture("adsblol.json")
                payload["ac"][0].update(changes)
                result = self.fallback(state, payload)
                self.assertFalse(result["aircraft"]["route_available"])

    def test_conflicting_tail_invalidates_route_for_later_refreshes(self):
        state = self.first_result()["trmnl_state"]
        payload = fixture("adsblol.json")
        payload["ac"][0]["r"] = "N456UA"
        result = self.fallback(state, payload)
        self.assertEqual(result["trmnl_state"], {})
        payload["ac"][0].pop("r")
        self.assertFalse(self.fallback(result["trmnl_state"], payload)["aircraft"]["route_available"])

    def test_flightaware_first_route_can_fill_fr24_route(self):
        state = self.first_result("flightaware", "flightaware_first")["trmnl_state"]
        self.fr24["data"][0].update(orig_iata=None, orig_icao=None, dest_iata=None, dest_icao=None)
        data = plugin_input(fr24_api_token="token", provider_order="flightaware_first")
        data["trmnl"]["state"] = state
        with mock.patch.object(transform, "_fetch_json", return_value=self.fr24):
            result = transform.run(data)
        self.assertEqual(result["provider_used"], "flightradar24")
        self.assertEqual(result["aircraft"]["route"], "SJC → PDX")
        self.assertEqual(result["aircraft"]["route_source"], "flightaware")

    def test_callsign_case_whitespace_and_registration_hyphens_are_normalized(self):
        state = self.first_result()["trmnl_state"]
        payload = fixture("adsblol.json")
        payload["ac"][0].update(flight=" ual123 ", r="N-123UA")
        self.assertTrue(self.fallback(state, payload)["aircraft"]["route_retained"])

    def test_missing_tail_allows_callsign_match(self):
        state = self.first_result()["trmnl_state"]
        payload = fixture("adsblol.json")
        payload["ac"][0].pop("r")
        self.assertTrue(self.fallback(state, payload)["aircraft"]["route_retained"])

    def test_tail_number_callsigns_are_not_saved(self):
        self.fr24["data"][0].update(callsign="N123UA")
        self.assertEqual(self.first_result()["trmnl_state"], {})

    def test_expired_and_future_routes_are_discarded(self):
        for offset in (-transform.ROUTE_MAX_AGE_SECONDS, 1):
            with self.subTest(offset=offset):
                state = self.first_result()["trmnl_state"]
                state["last_route"]["observed_at"] = (FIXED_NOW + timedelta(seconds=offset)).isoformat()
                result = self.fallback(state)
                self.assertFalse(result["aircraft"]["route_available"])
                self.assertEqual(result["trmnl_state"], {})

    def test_empty_error_and_configuration_error_refreshes_preserve_route(self):
        state = self.first_result()["trmnl_state"]
        for payload, fields, status in (({"ac": []}, {}, "no_aircraft"),
                                        ({}, {}, "provider_error"),
                                        ({}, {"lat_lon": "invalid"}, "configuration_error")):
            with self.subTest(status=status):
                result = self.fallback(state, payload, **fields)
                self.assertEqual(result["status"], status)
                self.assertEqual(result["trmnl_state"], state)
                self.assertTrue(self.fallback(result["trmnl_state"])["aircraft"]["route_available"])

    def test_malformed_saved_state_is_ignored(self):
        record = self.first_result()["trmnl_state"]["last_route"]
        bad_states = [None, [], "bad", {}, {"last_route": []}]
        for key in record:
            bad_states.append({"last_route": {**record, key: {"unexpected": "object"}}})
        bad_states.append({"last_route": {**record, "origin": "X" * 9000}})
        for state in bad_states:
            with self.subTest(state=str(state)[:100]):
                result = self.fallback(state)
                self.assertFalse(result["aircraft"]["route_available"])

    def test_lower_priority_route_does_not_fill_higher_priority_provider(self):
        state = self.first_result("flightaware")["trmnl_state"]
        self.fr24["data"][0].update(orig_iata=None, orig_icao=None, dest_iata=None, dest_icao=None)
        data = plugin_input(fr24_api_token="token")
        data["trmnl"]["state"] = state
        with mock.patch.object(transform, "_fetch_json", return_value=self.fr24):
            result = transform.run(data)
        self.assertFalse(result["aircraft"]["route_available"])


class LocationTests(unittest.TestCase):
    def test_location_picker_coordinates_take_precedence_over_legacy_fields(self):
        config = transform._extract_config(plugin_input(
            lat_lon=" 47.6062, -122.3321 ", latitude="37.7749", longitude="-122.4194"
        ))
        self.assertEqual((config["latitude"], config["longitude"]), (47.6062, -122.3321))

    def test_location_picker_accepts_zero_and_boundary_coordinates(self):
        for latitude, longitude in ((0, 0), (-90, -180), (90, 180)):
            with self.subTest(latitude=latitude, longitude=longitude):
                config = transform._extract_config(plugin_input(lat_lon=f"{latitude},{longitude}"))
                self.assertEqual((config["latitude"], config["longitude"]), (latitude, longitude))

    def test_invalid_location_picker_values_do_not_fall_back_to_legacy_location(self):
        for value in (None, "", " ", "37.7", ",", "37.7,", ",-122.4", "1,2,3",
                      "city,address", "91,0", "0,-181", "nan,0", "0,inf", [37.7, -122.4]):
            with self.subTest(value=value):
                result = transform.run(plugin_input(
                    {"ac": []}, lat_lon=value, latitude="37.7", longitude="-122.4"
                ))
                self.assertEqual(result["status"], "configuration_error")

    def test_legacy_coordinates_remain_supported(self):
        data = plugin_input(latitude="47.6062", longitude="-122.3321")
        del data["trmnl"]["plugin_settings"]["custom_fields_values"]["lat_lon"]
        config = transform._extract_config(data)
        self.assertEqual((config["latitude"], config["longitude"]), (47.6062, -122.3321))

    def test_local_picker_template_uses_environment_and_sample_defaults(self):
        data = plugin_input(lat_lon=(
            "{{ env.TRACKER_LATITUDE | default: 37.7749 }},"
            "{{ env.TRACKER_LONGITUDE | default: -122.4194 }}"
        ))
        for env, expected in (({}, (37.7749, -122.4194)),
                              ({"TRACKER_LATITUDE": "0", "TRACKER_LONGITUDE": "0"}, (0, 0)),
                              ({"TRACKER_LATITUDE": "47.6062", "TRACKER_LONGITUDE": "-122.3321"},
                               (47.6062, -122.3321))):
            with self.subTest(env=env), mock.patch.dict(transform.os.environ, env, clear=True):
                config = transform._extract_config(data)
                self.assertEqual((config["latitude"], config["longitude"]), expected)


if __name__ == "__main__":
    unittest.main()
