#!/usr/bin/env python3
"""Run the transform against a local ADSB.lol fixture with no network calls."""

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("flight_transform", ROOT / "src" / "transform.py")
transform = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(transform)


def main():
    with (ROOT / "fixtures" / "adsblol.json").open() as handle:
        payload = json.load(handle)

    payload["now"] = int(datetime.now(timezone.utc).timestamp() * 1000)
    payload["trmnl"] = {
        "plugin_settings": {
            "custom_fields_values": {
                "lat_lon": "37.7749,-122.4194",
                "location_label": "Fixture roof",
                "radius_nm": "20",
                "map_up_bearing_deg": "0",
                "max_age_minutes": "5",
                "provider_order": "open_only",
                "fr24_api_token": "",
                "flightaware_api_key": "",
            }
        }
    }

    result = transform.run(payload)
    assert result["status"] == "ok", result
    assert result["provider_used"] == "adsblol", result
    assert result["aircraft"]["identifier"] == "UAL123", result
    assert result["aircraft"]["operator"] == "United Airlines", result
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
