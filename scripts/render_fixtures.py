#!/usr/bin/env python3
"""Generate deterministic, credential-free transform outputs for view checks."""

import copy
import importlib.util
import json
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("flight_transform", ROOT / "src/transform.py")
transform = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(transform)
now = datetime(2026, 8, 25, 12, 2, tzinfo=timezone.utc)
transform._now_utc = lambda: now
payload = json.loads((ROOT / "fixtures/adsblol.json").read_text())
payload["trmnl"] = {"plugin_settings": {"custom_fields_values": {
    "lat_lon": "37.7749,-122.4194", "location_label": "Observation point",
    "radius_nm": "20", "max_age_minutes": "5", "provider_order": "open_only",
}}}
fixtures = {}
for scenario in (
    "aircraft", "partial_route", "rotated", "empty", "provider_error",
    "configuration_error",
):
    data = copy.deepcopy(payload)
    if scenario == "partial_route":
        data["ac"][0]["origin"] = "SFO"
    elif scenario == "rotated":
        data["trmnl"]["plugin_settings"]["custom_fields_values"]["map_up_bearing_deg"] = "90"
    elif scenario == "empty":
        data["ac"] = []
    elif scenario == "provider_error":
        del data["ac"]
    elif scenario == "configuration_error":
        data["trmnl"]["plugin_settings"]["custom_fields_values"]["lat_lon"] = "invalid"
    fixtures[scenario] = {**transform.run(data), "trmnl": {}}
output = ROOT / "_build/view-fixtures.json"
output.parent.mkdir(exist_ok=True)
output.write_text(json.dumps(fixtures, indent=2) + "\n")
print(f"Wrote {len(fixtures)} view fixtures to {output}")
