"""Unit tests for fungi_plate. Offline and deterministic: every outbound
call is a fake ``urllib.request.urlopen``, and fal.ai is faked at the
host client's HTTP layer, so the painting path runs end to end without a
key or a network."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
# The widget paints through Tesserae's ``app.artwork`` (0.463+): the server's
# repo must be importable. A checkout beside this one, or TESSERAE_REPO.
_TESSERAE = Path(os.environ.get("TESSERAE_REPO") or ROOT.parent / "tesserae")
if str(_TESSERAE) not in sys.path:
    sys.path.insert(0, str(_TESSERAE))
from app import fal as fal_mod  # noqa: E402


def _load():
    spec = importlib.util.spec_from_file_location(
        "fungi_plate_server", ROOT / "server.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


server = _load()

MELBOURNE = {"latitude": -37.81, "longitude": 144.96, "label": "Melbourne"}


def _jpeg(w: int = 300, h: int = 400) -> bytes:
    im = Image.new("RGB", (w, h), (243, 234, 214))
    ImageDraw.Draw(im).ellipse(
        (w * 0.3, h * 0.25, w * 0.7, h * 0.7), fill=(200, 120, 40)
    )
    buf = io.BytesIO()
    im.save(buf, "JPEG")
    return buf.getvalue()


def _taxon(tid: int, sci: str, common: str, rank: str = "species", ancestors=()):
    return {
        "id": tid,
        "name": sci,
        "preferred_common_name": common,
        "rank": rank,
        "ancestor_ids": [47170, *ancestors],
    }


SPECIES = [
    {"count": 12, "taxon": _taxon(1, "Omphalotus nidiformis", "ghost fungus")},
    {"count": 45, "taxon": _taxon(2, "Cantharellus cibarius", "Chanterelle")},
    {"count": 3, "taxon": _taxon(3, "Coprinus comatus", "shaggy ink cap")},
    {"count": 7, "taxon": _taxon(4, "Laccaria amethystina", "")},
    {"count": 99, "taxon": _taxon(5, "Amanita", "amanitas", rank="genus")},
]


def _obs(
    tid: int, seen: str, faves: int, who: str, lic: str = "cc-by-nc", pid: int = 1
):
    return {
        "observed_on": seen,
        "faves_count": faves,
        "user": {"login": who.lower().replace(" ", "_"), "name": who},
        "taxon": {"id": tid, "ancestor_ids": [47170]},
        "photos": [
            {
                "url": f"https://inat.example/photos/{pid}/square.jpg",
                "license_code": lic,
            }
        ],
    }


OBS = [
    _obs(2, "2025-10-20", 0, "Jane Birder", pid=20),
    _obs(2, "2024-10-05", 4, "Eric Cameron", lic="cc-by", pid=21),
    _obs(1, "2025-10-01", 1, "Ben", lic="cc0", pid=10),
    {
        **_obs(1, "2025-10-12", 1, "Zoe", pid=11),
        "taxon": {"id": 101, "ancestor_ids": [47170, 1]},
    },
    _obs(3, "2023-10-30", 0, "Sam", pid=30),
]
GAP = [_obs(4, "2025-10-09", 2, "Kim", lic="cc-by-sa", pid=40)]


class _Resp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, n: int = -1) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def offline():
    """iNaturalist and its photo CDN answer from here; the calls are kept."""
    calls: list[str] = []

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        url = req.full_url
        calls.append(url)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        if "/observations/species_counts?" in url:
            return _Resp(json.dumps({"total_results": 58, "results": SPECIES}).encode())
        if "/observations?" in url:
            if q.get("per_page") == ["1"]:
                return _Resp(json.dumps({"total_results": 1, "results": GAP}).encode())
            return _Resp(json.dumps({"total_results": 5, "results": OBS}).encode())
        if "inat.example/photos/" in url:
            return _Resp(_jpeg())
        raise urllib.error.URLError("no such host")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        yield calls


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.delenv("FAL_KEY", raising=False)
    monkeypatch.delenv("FAL_API_KEY", raising=False)


def _ctx(tmp_path: Path, **extra):
    return {
        "data_dir": str(tmp_path),
        "cell_w": 1200,
        "cell_h": 800,
        "preview": False,
        **extra,
    }


# ----- pure shaping ---------------------------------------------------


def test_roman_plate_numbers():
    assert server.roman(1) == "I"
    assert server.roman(4) == "IV"
    assert server.roman(10) == "X"
    assert server.roman(12) == "XII"


def test_common_names_are_set_in_title_case():
    assert server.title_case("red-cage fungus") == "Red-Cage Fungus"
    assert server.title_case("Shaggy Parasol") == "Shaggy Parasol"
    assert server.title_case("McKinty's bonnet") == "McKinty's Bonnet"
    assert server.title_case("") == ""


def test_shape_ranks_by_records_and_picks_the_best_photo():
    rows = server.shape(SPECIES, OBS, 6)
    # the genus-level row is left out; the rest by records, most first
    assert [r["sci"] for r in rows] == [
        "Cantharellus cibarius",
        "Omphalotus nidiformis",
        "Laccaria amethystina",
        "Coprinus comatus",
    ]
    chant = rows[0]
    assert chant["name"] == "Chanterelle" and chant["count"] == 45
    # the most faved observation's photo, at medium size, with who and the licence
    assert chant["photo"] == {
        "url": "https://inat.example/photos/21/medium.jpg",
        "by": "Eric Cameron",
        "lic": "CC BY",
    }
    # but the day last seen is the newest observation's
    assert chant["last"] == "2025-10-20"
    # a subspecies observation counts for its species through ancestor_ids
    ghost = rows[1]
    assert ghost["last"] == "2025-10-12" and ghost["photo"]["by"] == "Zoe", (
        "a tie on faves goes to the newer"
    )
    # no observation in the page: no photo yet, and no common name stays empty
    assert rows[2]["photo"] is None and rows[2]["name"] == ""


def test_shape_takes_only_count():
    assert len(server.shape(SPECIES, OBS, 2)) == 2
    assert len(server.shape(SPECIES, OBS, 1)) == 1


def test_query_windows_and_sources():
    from datetime import date

    today = date(2026, 10, 28)
    q, err = server.query({**MELBOURNE, "window": "month", "radius_km": "15"}, today)
    assert err is None and q["month"] == "10" and "d1" not in q
    assert q["lat"] == "-37.8100" and q["radius"] == "15"
    assert q["iconic_taxa"] == "Fungi" and q["quality_grade"] == "research"
    assert q["photo_license"].startswith("cc0,cc-by")
    q, _ = server.query({**MELBOURNE, "window": "30"}, today)
    assert q["d1"] == "2026-09-28" and "month" not in q
    q, _ = server.query({**MELBOURNE, "window": "90"}, today)
    assert q["d1"] == "2026-07-30"
    q, err = server.query({"source": "user", "username": "@kayden"}, today)
    assert err is None and q["user_login"] == "kayden" and "lat" not in q
    assert server.query({"source": "user"}, today)[1] == server.ERR_NO_USER
    assert server.query({}, today)[1] == server.ERR_NO_PLACE


def test_painting_keys_and_prompt():
    assert server.painting_key("Omphalotus nidiformis", "engraving", "nano_banana") == (
        "omphalotus_nidiformis--engraving--p1"
    )
    assert server.painting_key("Omphalotus nidiformis", "ink", "gpt_image").endswith(
        "--p1--gpt_image"
    )
    p = server.prompt_for(
        {"style": "potter", "common": "Ghost Fungus", "sci": "Omphalotus nidiformis"}
    )
    assert "Ghost Fungus (Omphalotus nidiformis)" in p
    assert "Beatrix Potter" in p
    for phrase in (
        "whole fruiting body",
        "cross-section",
        "standing on nothing",
        "plain pure white paper",
        "no text",
        "no ground",
        "no leaf litter",
        "no shadow",
    ):
        assert phrase in p, phrase
    assert chr(0x2014) not in p


def _smudged(ink: bool) -> bytes:
    """A white sheet with a dark line in the middle and a soft grey wash in
    the bottom left corner, joined to the edge, as a kept ground looks."""
    im = Image.new("RGB", (300, 400), (255, 255, 255))
    d = ImageDraw.Draw(im)
    for i in range(0, 120, 2):
        v = 160 + i // 2
        d.rectangle((0, 280 + i, 120 - i, 400), fill=(v, v, v))
    d.rectangle((140, 150, 200, 250), fill=(20, 20, 20) if ink else (200, 40, 30))
    d.rectangle(
        (150, 160, 190, 240), fill=(200, 200, 200)
    )  # a light tone enclosed by the line
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def test_trim_whitens_the_ground_and_crops_to_the_subject():
    out = Image.open(io.BytesIO(server.trim_painting(_smudged(False))))
    assert out.width < 300 and out.height < 400
    # a strict trim keeps the grey wash (it may be a pale colour) and so the crop reaches it
    assert out.getpixel((2, out.height - 2)) != (255, 255, 255)
    out = Image.open(io.BytesIO(server.trim_painting(_smudged(True), loose=True)))
    # the loose trim takes the wash as paper: the crop holds the line and its tone only
    assert out.width < 100 and out.height < 140
    assert out.getpixel((0, out.height - 1)) == (255, 255, 255)
    assert (20, 20, 20) in [
        out.getpixel((x, out.height // 2)) for x in range(out.width)
    ]
    assert (200, 200, 200) in [
        out.getpixel((x, out.height // 2 - 10)) for x in range(out.width)
    ]


# ----- fetch ----------------------------------------------------------


def test_fetch_without_network_returns_an_error_not_a_raise(tmp_path, no_key):
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
        out = server.fetch({**MELBOURNE}, {}, ctx=_ctx(tmp_path))
    assert out == {"error": server.ERR_UNREACHABLE}


def test_fetch_without_a_place_asks_for_one(tmp_path, no_key):
    out = server.fetch({}, {}, ctx=_ctx(tmp_path))
    assert "place" in out["error"].lower()


def test_fetch_builds_the_plate(tmp_path, offline, no_key):
    out = server.fetch({**MELBOURNE, "count": "6"}, {}, ctx=_ctx(tmp_path))
    assert "error" not in out
    assert out["title"] == "FUNGI OF THE MONTH · Melbourne"
    assert out["plate"].startswith("Plate ") and out["roman"] == server.roman(
        time.localtime().tm_mon
    )
    assert out["within"] == "within 15 km" and out["species_total"] == 58
    names = [(s["n"], s["name"] or s["sci"]) for s in out["species"]]
    assert names == [
        (1, "Chanterelle"),
        (2, "Ghost Fungus"),
        (3, "Laccaria amethystina"),
        (4, "Shaggy Ink Cap"),
    ]
    # every species has its photo as a data URI, converted on the server
    assert all(
        s["pic"]["kind"] == "photo" and s["pic"]["src"].startswith("data:image/jpeg")
        for s in out["species"]
    )
    # the gap was filled with one call of its own
    assert any("per_page=1" in c and "taxon_id=4" in c for c in offline)
    assert out["species"][2]["count"] == 7
    assert (
        "Photographs on iNaturalist by 1 Eric Cameron (CC BY), 2 Zoe (CC BY-NC)"
        in out["credit_line"]
    )
    assert "3 Kim (CC BY-SA)" in out["credit_line"]
    assert out["live_refresh_s"] == 0 and out["live_pending"] == 0


def test_fetch_is_cached_six_hours_and_honours_fresh(tmp_path, offline, no_key):
    server.fetch({**MELBOURNE}, {}, ctx=_ctx(tmp_path))
    n = len(offline)
    server.fetch({**MELBOURNE}, {}, ctx=_ctx(tmp_path))
    assert len(offline) == n, "the second fetch came from the cache"
    server.fetch({**MELBOURNE}, {}, ctx=_ctx(tmp_path, fresh=True))
    assert len(offline) > n


def test_a_stale_cache_covers_an_outage(tmp_path, offline, no_key):
    server.fetch({**MELBOURNE}, {}, ctx=_ctx(tmp_path))
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
        out = server.fetch({**MELBOURNE}, {}, ctx=_ctx(tmp_path, fresh=True))
    assert "error" not in out and len(out["species"]) == 4


def test_empty_plate_says_so(tmp_path, no_key):
    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        return _Resp(json.dumps({"total_results": 0, "results": []}).encode())

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        out = server.fetch({**MELBOURNE, "radius_km": "2"}, {}, ctx=_ctx(tmp_path))
    assert out["species"] == []
    assert out["empty"].startswith(
        "No research-grade fungi with openly licensed photos within 2 km of Melbourne"
    )


def test_panel_conversion_makes_grey_pictures(tmp_path, offline, no_key):
    out = server.fetch({**MELBOURNE, "panel": "mono"}, {}, ctx=_ctx(tmp_path))
    import base64

    raw = base64.b64decode(out["species"][0]["pic"]["src"].split(",", 1)[1])
    assert Image.open(io.BytesIO(raw)).mode == "L"
    assert out["panel"] == "mono"


def test_credits_can_be_turned_off(tmp_path, offline, no_key):
    out = server.fetch({**MELBOURNE, "credits": False}, {}, ctx=_ctx(tmp_path))
    assert out["credit_line"] == ""


# ----- paintings ------------------------------------------------------


def test_paintings_need_a_key_and_the_photos_show_meanwhile(tmp_path, offline, no_key):
    out = server.fetch({**MELBOURNE, "pictures": "gen"}, {}, ctx=_ctx(tmp_path))
    assert all(s["pic"]["kind"] == "photo" for s in out["species"])
    assert any("fal.ai key" in n for n in out["notices"])


def _settle(timeout: float = 15.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        pr = server.ART.progress(Path("."), {})
        if not pr["running"] and not pr["queued"]:
            return
        time.sleep(0.02)
    raise AssertionError("paintings did not settle")


@pytest.fixture
def fake_fal():
    """fal.ai answers every painting with a picture; the prompts are kept."""
    prompts: list[dict] = []

    def fake_post(url, body, key, timeout):  # noqa: ARG001
        assert key == "k"
        prompts.append({**body, "_url": url})
        return {"images": [{"url": "https://fal.example/out.jpg"}]}

    def fake_download(url, timeout=60.0):  # noqa: ARG001
        return _jpeg(300, 400)

    with (
        patch.object(fal_mod, "_http_json", side_effect=fake_post),
        patch.object(fal_mod, "_download", side_effect=fake_download),
    ):
        yield prompts
    server.ART.stop_all(Path("."))


def test_paintings_are_made_from_each_photo_and_credited(tmp_path, offline, fake_fal):
    options = {**MELBOURNE, "pictures": "gen", "style": "ink", "count": "4"}
    settings = {"fal_api_key": "k", "gen_daily_usd": 2}
    out = server.fetch(options, settings, ctx=_ctx(tmp_path))
    assert out["live_refresh_s"] > 0 and out["live_pending"] == 4
    assert all(s["pic"]["kind"] == "photo" for s in out["species"]), (
        "photos until the paintings land"
    )
    _settle()
    assert len(fake_fal) == 4
    sent = {p["image_urls"][0] for p in fake_fal}
    assert "https://inat.example/photos/21/medium.jpg" in sent
    assert all(
        "cross-section" in p["prompt"] and "black ink line drawing" in p["prompt"]
        for p in fake_fal
    )
    assert all(p["aspect_ratio"] == "3:4" for p in fake_fal)
    out = server.fetch(options, settings, ctx=_ctx(tmp_path))
    assert out["live_refresh_s"] == 0
    assert all(s["pic"]["kind"] == "painting" for s in out["species"])
    assert out["species"][0]["pic"]["credit"] == (
        "AI painting (Nano Banana on fal.ai) from a photo by Eric Cameron (CC BY)"
    )
    assert out["credit_line"].startswith(
        "AI paintings (Nano Banana on fal.ai) from photographs on iNaturalist by"
    )
    # painted once: a third fetch asks fal.ai for nothing more
    server.fetch(options, settings, ctx=_ctx(tmp_path))
    assert len(fake_fal) == 4
    # a mono panel gets the kept painting converted, not painted again
    out = server.fetch({**options, "panel": "mono"}, settings, ctx=_ctx(tmp_path))
    assert len(fake_fal) == 4 and out["species"][0]["pic"]["kind"] == "painting"
