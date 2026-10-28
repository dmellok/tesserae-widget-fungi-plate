"""The options panel stays short: a handful of options up front, the rest
nested under the choice they depend on or under More options."""

from __future__ import annotations

import json
from pathlib import Path

MANIFEST = json.loads(
    (Path(__file__).resolve().parent.parent / "plugin.json").read_text(encoding="utf-8")
)
OPTIONS = MANIFEST["cell_options"]

# Saved cells key on these names; the order is how the panel reads.
ORDER = [
    "location",
    "radius_km",
    "window",
    "count",
    "layout",
    "pictures",
    "style",
    "paper",
    "source",
    "username",
    "model",
    "panel",
    "title",
    "credits",
]

UP_FRONT = ["window", "count", "layout", "pictures", "paper"]


def _up_front(o: dict) -> bool:
    return not o.get("show_if") and not o.get("advanced")


def test_manifest_basics() -> None:
    assert MANIFEST["name"] == "Fungi Plate"
    assert MANIFEST["kind"] == "widget"
    assert MANIFEST["tesserae_compat"] == "1.x"
    assert MANIFEST["tesserae_min"] == "0.463.0"
    assert MANIFEST["requires"] == ["network:*"]
    assert MANIFEST["render"]["needs_network"] is True
    assert MANIFEST["render"]["warm_ahead_s"] == 150
    assert MANIFEST["category"] == "art"


def test_option_names_and_order() -> None:
    assert [o["name"] for o in OPTIONS] == ORDER


def test_few_options_up_front() -> None:
    assert [o["name"] for o in OPTIONS if _up_front(o)] == UP_FRONT
    assert len(UP_FRONT) <= 5


def test_show_if_refers_to_real_options() -> None:
    names = {o["name"] for o in OPTIONS}
    for o in OPTIONS:
        for key in o.get("show_if") or {}:
            assert key in names, (o["name"], key)


def test_advanced_options_come_last() -> None:
    seen = False
    for o in OPTIONS:
        if o.get("advanced"):
            seen = True
        else:
            assert not seen, f"{o['name']} follows an advanced option"


def test_no_option_is_called_label() -> None:
    assert "label" not in {o["name"] for o in OPTIONS}


def test_settings_carry_the_key_and_the_budget() -> None:
    by_name = {s["name"]: s for s in MANIFEST["settings"]}
    assert by_name["fal_api_key"]["secret"] is True
    assert by_name["gen_daily_usd"]["default"] == 2
    assert by_name["gen_per_refresh"]["default"] == 3


def test_no_em_dashes_in_the_panel() -> None:
    assert chr(0x2014) not in json.dumps(MANIFEST, ensure_ascii=False)
