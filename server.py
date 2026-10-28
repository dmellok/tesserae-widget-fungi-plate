"""fungi_plate: the fungi recorded around the panel this month, as a numbered
mycological plate.

Runs on the Tesserae side. The data is iNaturalist's research-grade fungus
observations with openly licensed photographs, within a circle round the
place (or one user's own records), grouped by species and ranked by how many
records each has. Each species carries its best photograph (the most faved
observation's), who took it and under which licence, and the day it was last
seen. The answer is kept six hours in the widget's data dir.

Pictures are the photographs, or AI paintings made from them through
``app.artwork`` (Tesserae 0.463 and later): one painting per species and
style, kept, budgeted and queued by the host. On an older Tesserae the
import fails and the plate shows photographs with a note saying why.

Returns a JSON-serialisable dict, or {"error": "friendly sentence"}; never
raises.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from typing import Any

try:
    from app import artwork  # Tesserae 0.463+: the pictures a widget makes and keeps
    from app import fal as _fal
except ImportError:  # an older Tesserae: photographs only, with a note
    artwork = None  # type: ignore[assignment]
    _fal = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

WIDGET = "fungi_plate"
VERSION = "0.1.0"
USER_AGENT = f"tesserae/{WIDGET}/{VERSION} (+https://github.com/dmellok/tesserae-widget-{WIDGET.replace('_', '-')})"

INAT = "https://api.inaturalist.org/v1"
LICENCES = "cc0,cc-by,cc-by-nc,cc-by-sa,cc-by-nc-sa,cc-by-nd,cc-by-nc-nd"
CACHE_S = 6 * 3600
HTTP_TIMEOUT_S = 25.0
IMG_MAX_BYTES = 8_000_000
SPECIES_PER_PAGE = 40
OBS_PER_PAGE = 200
COUNTS = (1, 2, 4, 6, 8, 9)
WINDOWS = ("month", "30", "90")
RADII = ("2", "5", "10", "15", "25", "50")
LEVELS = ("colour", "gray16", "gray4", "mono")
PAPERS = ("cream", "white", "aged")
LAYOUTS = ("plate", "boxes")

ERR_NO_PLACE = (
    "Set a place in this cell's settings, or in Settings, to look for fungi round it."
)
ERR_NO_USER = "Give an iNaturalist username, or switch the source back to the place."
ERR_UNREACHABLE = "Couldn't reach iNaturalist right now."
ERR_BUSY = "iNaturalist is busy (it answered {err}); back on the next refresh."
ERR_SHAPE = "iNaturalist's answer wasn't in the expected shape."
NEEDS_HOST = "AI paintings need Tesserae 0.463 or later; this server is older, so the photographs show."

MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
ROMAN = ("I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII")

# The styles a painting may be made in: (label, how). Every prompt ends with
# the same instructions for a plate: the whole fruiting body as the photograph
# shows it, a small cross-section beside it, white paper and nothing else.
STYLES: dict[str, tuple[str, str]] = {
    "engraving": (
        "Hand-coloured engraving",
        "a hand-coloured copperplate engraving from a nineteenth-century mycological plate, in the manner of Sowerby and Bulliard: fine engraved contour lines and hatching with soft hand-applied watercolour tints, true to the species' colours",
    ),
    "potter": (
        "Watercolour after Beatrix Potter",
        "a precise botanical watercolour in the manner of Beatrix Potter's fungus studies: delicate layered washes, fine detail in the cap texture, the gills and the stem, a gentle natural palette, true to the species' colours",
    ),
    "woodblock": (
        "Woodblock print",
        "a woodblock print: flat areas of colour inside a dark carved keyline, the grain of the block showing in the larger shapes, a restrained palette true to the species' colours",
    ),
    "ink": (
        "Ink line drawing",
        "a black ink line drawing: clean confident contour lines with hatching and stippling for the shading, no colour at all and no grey wash, as on a page of a mycological monograph",
    ),
    "gouache": (
        "Gouache field guide",
        "a gouache field guide illustration: opaque matte colour, crisp edges, every feature a mycologist would look for painted clearly, true to the species' colours",
    ),
}
DEFAULT_STYLE = "engraving"

PROMPT = (
    "Paint the fungus in this photograph, {name} ({sci}), as {how}. Show the whole "
    "fruiting body in the form the photograph shows (a cap and stem, a bracket, a cup, "
    "a coral, a puffball or a crust), standing on nothing, with a small cross-section "
    "of the same fungus beside it showing the flesh and the gills or pores. On plain "
    "pure white paper and nothing else: no text, no labels, no numbers, no ground, no "
    "leaf litter, no moss, no shadow, no border, no signature."
)

# The models a painting may be made with, all on fal.ai, all painting from a
# photograph so the species stays right. ``app.fal`` holds the table; these
# are the labels for a host without it.
MODEL_LABELS = {
    "nano_banana": "Nano Banana",
    "nano_banana_pro": "Nano Banana Pro",
    "flux_kontext": "FLUX.1 Kontext Pro",
    "gpt_image": "GPT Image 2",
    "seedream": "Seedream 4",
}
DEFAULT_MODEL = "nano_banana"


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")[:60] or "x"


def roman(month: int) -> str:
    """The plate number: the month of the year in roman numerals."""
    return ROMAN[max(1, min(12, int(month))) - 1]


def _pick(value: Any, allowed: tuple[str, ...], default: str) -> str:
    v = str(value if value is not None else "").strip()
    return v if v in allowed else default


def _int(value: Any, default: int) -> int:
    with contextlib.suppress(TypeError, ValueError):
        return int(float(value))
    return default


def http(url: str, accept: str = "application/json") -> tuple[bytes | None, str | None]:
    """(bytes, None) or (None, why): "unreachable", "too large" or "HTTP <code>"."""
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": accept}
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            data = resp.read(IMG_MAX_BYTES + 1)
            return (None, "too large") if len(data) > IMG_MAX_BYTES else (data, None)
    except urllib.error.HTTPError as err:
        return None, f"HTTP {err.code}"
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None, "unreachable"


def get_json(url: str) -> tuple[Any | None, str | None]:
    body, err = http(url)
    if err or body is None:
        return None, err
    try:
        return json.loads(body.decode("utf-8")), None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "not json"


def _read_json(path: Path, max_age_s: float) -> Any | None:
    try:
        if time.time() - path.stat().st_mtime > max_age_s:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data: Any) -> None:
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)


# ----------------------------------------------------------------------
# iNaturalist
# ----------------------------------------------------------------------


def query(options: dict[str, Any], today: date) -> tuple[dict[str, str], str | None]:
    """The parameters every iNaturalist call shares, or why there can be none."""
    q: dict[str, str] = {
        "iconic_taxa": "Fungi",
        "quality_grade": "research",
        "photos": "true",
        "photo_license": LICENCES,
        "locale": "en",
    }
    window = _pick(options.get("window"), WINDOWS, "month")
    if window == "month":
        q["month"] = str(today.month)
    else:
        q["d1"] = (today - timedelta(days=int(window))).isoformat()
    source = _pick(options.get("source"), ("place", "user"), "place")
    if source == "user":
        login = str(options.get("username") or "").strip().lstrip("@")
        if not login:
            return q, ERR_NO_USER
        q["user_login"] = login
        return q, None
    try:
        lat, lon = float(options.get("latitude")), float(options.get("longitude"))
    except (TypeError, ValueError):
        return q, ERR_NO_PLACE
    q["lat"], q["lng"] = f"{lat:.4f}", f"{lon:.4f}"
    q["radius"] = _pick(options.get("radius_km"), RADII, "15")
    return q, None


def _medium(url: str) -> str:
    return re.sub(r"/(square|small|thumb|large|original)\.", "/medium.", str(url))


def _licence(code: Any) -> str:
    c = str(code or "").strip().lower()
    if not c:
        return ""
    return "CC0" if c == "cc0" else c.upper().replace("-", " ", 1)


def _photo_of(obs: dict[str, Any]) -> dict[str, Any] | None:
    photos = obs.get("photos") or []
    p0 = photos[0] if photos and isinstance(photos[0], dict) else None
    if not p0 or not p0.get("url"):
        return None
    user = obs.get("user") or {}
    return {
        "url": _medium(p0["url"]),
        "by": str(
            user.get("name") or user.get("login") or p0.get("attribution_name") or ""
        ).strip()
        or "an iNaturalist user",
        "lic": _licence(p0.get("license_code")),
        "faves": int(obs.get("faves_count") or 0),
        "seen": str(obs.get("observed_on") or ""),
    }


def _taxon_key(taxon: dict[str, Any], ids: set[int]) -> int | None:
    """Which chosen species an observation's taxon belongs to: itself, or a
    subspecies' parent."""
    tid = taxon.get("id")
    if tid in ids:
        return int(tid)
    for a in taxon.get("ancestor_ids") or []:
        if a in ids:
            return int(a)
    return None


def shape(
    species: list[dict[str, Any]], obs: list[dict[str, Any]], count: int
) -> list[dict[str, Any]]:
    """Species rows from iNaturalist's species counts and observations: the
    top ``count`` by records, each with its best photograph (the most faved,
    then the newest) and the day it was last seen."""
    rows: list[dict[str, Any]] = []
    for s in species:
        t = s.get("taxon") or {}
        if not t.get("id") or not t.get("name"):
            continue
        if str(t.get("rank") or "species") not in (
            "species",
            "subspecies",
            "variety",
            "form",
        ):
            continue
        rows.append(
            {
                "taxon_id": int(t["id"]),
                "sci": str(t["name"]).strip(),
                "name": title_case(str(t.get("preferred_common_name") or "").strip()),
                "count": int(s.get("count") or 0),
                "photo": None,
                "last": "",
            }
        )
    rows.sort(key=lambda r: (-r["count"], r["sci"]))
    rows = rows[: max(1, count)]
    ids = {r["taxon_id"] for r in rows}
    by_taxon: dict[int, list[dict[str, Any]]] = {}
    for o in obs:
        k = _taxon_key(o.get("taxon") or {}, ids)
        if k is None:
            continue
        p = _photo_of(o)
        if p is not None:
            by_taxon.setdefault(k, []).append(p)
    for r in rows:
        got = by_taxon.get(r["taxon_id"]) or []
        if got:
            r["last"] = max(p["seen"] for p in got)
            best = max(got, key=lambda p: (p["faves"], p["seen"]))
            r["photo"] = {k: best[k] for k in ("url", "by", "lic")}
    return rows


def _fill_gap(r: dict[str, Any], q: dict[str, str]) -> None:
    oq = urllib.parse.urlencode(
        {
            **q,
            "taxon_id": str(r["taxon_id"]),
            "order_by": "votes",
            "order": "desc",
            "per_page": "1",
        }
    )
    got, err = get_json(f"{INAT}/observations?{oq}")
    results = got.get("results") if isinstance(got, dict) and not err else None
    if results and isinstance(results[0], dict):
        p = _photo_of(results[0])
        if p is not None:
            r["photo"] = {k: p[k] for k in ("url", "by", "lic")}
            r["last"] = r["last"] or p["seen"]


def _fill_gaps(rows: list[dict[str, Any]], q: dict[str, str]) -> None:
    """A species whose observations were not in the newest page gets one call
    of its own, for its most faved observation; the calls go side by side so
    a cold fetch stays inside the render budget."""
    gaps = [r for r in rows if not r.get("photo")]
    if not gaps:
        return
    with ThreadPoolExecutor(max_workers=min(3, len(gaps))) as pool:
        list(pool.map(lambda r: _fill_gap(r, q), gaps))


def load(q: dict[str, str], count: int) -> tuple[dict[str, Any] | None, str | None]:
    """The species for a query from iNaturalist: two calls, plus one per
    species the second missed."""
    sq = urllib.parse.urlencode(
        {**q, "hrank": "species", "per_page": str(SPECIES_PER_PAGE)}
    )
    species, err = get_json(f"{INAT}/observations/species_counts?{sq}")
    if err == "unreachable":
        return None, ERR_UNREACHABLE
    if err:
        return None, ERR_BUSY.format(err=err)
    if not isinstance(species, dict) or not isinstance(species.get("results"), list):
        return None, ERR_SHAPE
    rows = shape(species["results"], [], count)
    obs: list[dict[str, Any]] = []
    if rows:
        oq = urllib.parse.urlencode(
            {
                **q,
                "taxon_id": ",".join(str(r["taxon_id"]) for r in rows),
                "order_by": "observed_on",
                "order": "desc",
                "per_page": str(OBS_PER_PAGE),
            }
        )
        got, err = get_json(f"{INAT}/observations?{oq}")
        results = got.get("results") if isinstance(got, dict) and not err else None
        obs = results if isinstance(results, list) else []
    rows = shape(species["results"], obs, count)
    _fill_gaps(rows, q)
    return {
        "rows": rows,
        "species_total": int(species.get("total_results") or 0),
        "fetched_at": time.time(),
    }, None


# ----------------------------------------------------------------------
# Pictures: photographs converted for the panel, and paintings
# ----------------------------------------------------------------------


def _data_uri(raw: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def convert(raw: bytes, px: int, levels: str) -> bytes:
    """A photograph resized to ``px`` on its longest side and converted for a
    panel without colour, as app.artwork does for paintings."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return raw
    try:
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))
    except Exception:
        return b""
    if im is None:
        return b""
    im = im.convert("RGB")
    im.thumbnail((px, px), Image.Resampling.LANCZOS)
    if levels != "colour":
        grey = ImageOps.autocontrast(
            im.convert("L"), cutoff=0.5 if levels == "gray16" else 1.0
        )
        if levels == "mono":
            grey = grey.point([round(255 * (i / 255) ** 0.8) for i in range(256)])
        im = grey
    out = io.BytesIO()
    im.save(out, "JPEG", quality=82, optimize=True, progressive=True)
    return out.getvalue()


def photo_uri(url: str, data_dir: Path, px: int, levels: str) -> str:
    """The photograph as a data URI, fetched once and kept, converted per
    size and panel. Empty when it cannot be had; a miss is remembered a day."""
    folder = data_dir / "img"
    key = hashlib.sha1(url.encode()).hexdigest()[:20]
    levels = levels if levels in LEVELS else "colour"
    derived = folder / f"{key}_{px}_{levels}.jpg"
    with contextlib.suppress(OSError):
        if derived.exists():
            return _data_uri(derived.read_bytes())
    raw_path, miss = folder / f"{key}.bin", folder / f"{key}.miss"
    raw: bytes | None = None
    with contextlib.suppress(OSError):
        if raw_path.exists():
            raw = raw_path.read_bytes()
    if raw is None:
        with contextlib.suppress(OSError):
            if miss.exists() and time.time() - miss.stat().st_mtime < 86400:
                return ""
        raw, err = http(url, accept="image/*")
        folder.mkdir(parents=True, exist_ok=True)
        if err or not raw:
            with contextlib.suppress(OSError):
                miss.write_bytes(b"")
            return ""
        with contextlib.suppress(OSError):
            raw_path.write_bytes(raw)
    data = convert(raw, px, levels)
    if not data:
        return ""
    with contextlib.suppress(OSError):
        folder.mkdir(parents=True, exist_ok=True)
        derived.write_bytes(data)
    return _data_uri(data)


WHITE_FROM = 228  # a channel value this high is taken as the paper
TRIM_PAD = 0.03  # of the longer side, kept round the subject
# An ink drawing has no colour of its own, so any light, unsaturated wash that
# touches the edge of the sheet is a ground the model kept from the photo, not
# the drawing: it is taken as paper down to this grey.
INK_GROUND_FROM = 150
INK_GROUND_SAT = 40
LOOSE_STYLES = ("ink",)


def _ground_from_edges(im: Any, lum_from: int, sat_max: int) -> Any:
    """A mask (255 where paper) of the light, unsaturated pixels joined to
    the edge of the sheet, found by flooding in from a border."""
    from PIL import Image, ImageChops, ImageDraw

    r, g, b = im.split()
    lo = ImageChops.lighter(ImageChops.lighter(r, g), b)
    hi = ImageChops.darker(ImageChops.darker(r, g), b)
    sat = ImageChops.subtract(lo, hi)
    light = im.convert("L").point(lambda v: 255 if v >= lum_from else 0)
    grey = sat.point(lambda v: 255 if v <= sat_max else 0)
    cand = ImageChops.multiply(light, grey)
    framed = Image.new("L", (im.width + 2, im.height + 2), 255)
    framed.paste(cand, (1, 1))
    ImageDraw.floodfill(framed, (0, 0), 128, thresh=0)
    filled = framed.crop((1, 1, im.width + 1, im.height + 1))
    return filled.point(lambda v: 255 if v == 128 else 0)


def trim_painting(raw: bytes, loose: bool = False) -> bytes:
    """A painting cropped to its subject with its ground made pure white, so
    on the plate it multiplies into the paper without a visible rectangle and
    the fungus takes the room its slot allows. ``loose`` (ink drawings) also
    takes a light grey wash joined to the sheet's edge as paper. A PNG; empty
    when it cannot be done."""
    try:
        from PIL import Image, ImageChops, ImageOps
    except ImportError:
        return b""
    try:
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))
    except Exception:
        return b""
    if im is None:
        return b""
    im = im.convert("RGB")
    r, g, b = im.split()
    white = [255 if v >= WHITE_FROM else 0 for v in range(256)]
    paper = ImageChops.multiply(
        ImageChops.multiply(r.point(white), g.point(white)), b.point(white)
    )
    if loose:
        paper = ImageChops.lighter(
            paper, _ground_from_edges(im, INK_GROUND_FROM, INK_GROUND_SAT)
        )
    box = ImageChops.invert(paper).getbbox()
    if not box:
        return b""
    im.paste((255, 255, 255), mask=paper)
    pad = int(max(im.size) * TRIM_PAD)
    box = (
        max(0, box[0] - pad),
        max(0, box[1] - pad),
        min(im.width, box[2] + pad),
        min(im.height, box[3] + pad),
    )
    out = io.BytesIO()
    im.crop(box).save(out, "PNG", optimize=True)
    return out.getvalue()


def trimmed_variant(art: Any, key: str) -> str:
    """The name of the trimmed copy beside a kept painting, made once; empty
    when the painting is used as it came."""
    c = getattr(art, "c", None)
    if c is None:
        return ""
    folder = c.folder(art.data_dir)
    done, none = folder / f"{key}__trim.png", folder / f"{key}__trim.none"
    if done.exists():
        return "trim"
    if none.exists():
        return ""
    got = art.picture(key)
    style = str(got[1].get("style") or "") if got else ""
    data = trim_painting(got[0], loose=style in LOOSE_STYLES) if got else b""
    with contextlib.suppress(OSError):
        if data:
            done.write_bytes(data)
            return "trim"
        none.write_bytes(b"")
    return ""


class _NoArt:
    """What the collection opens to on a Tesserae without app.artwork: every
    request answers no_host, nothing is started, the note says why."""

    key = ""
    started = 0

    def __init__(self) -> None:
        self.states: dict[str, int] = {}

    def request(self, key: str, payload: dict[str, Any], *, title: str = "") -> Any:
        from types import SimpleNamespace

        self.states["no_host"] = self.states.get("no_host", 0) + 1
        return SimpleNamespace(
            key=key, state="no_host", ready=False, meta={}, credit="", why=NEEDS_HOST
        )

    def url(self, *a: Any, **k: Any) -> str:
        return ""

    def live(self) -> dict[str, int]:
        return {"live_refresh_s": 0, "live_pending": 0}

    def notes(self) -> list[str]:
        return [NEEDS_HOST] if self.states.get("no_host") else []

    def status(self) -> list[str]:
        return []

    def finish(self, *a: Any, **k: Any) -> None:
        return None


class _NoArtwork:
    """The collection on an older Tesserae: opens to _NoArt."""

    def open(
        self, ctx: dict[str, Any], settings: dict[str, Any] | None, *, subject: str = ""
    ) -> _NoArt:
        return _NoArt()


def model_of(model: Any) -> str:
    m = str(model or "")
    return m if m in MODEL_LABELS else DEFAULT_MODEL


def model_label(model: str) -> str:
    if _fal is not None:
        with contextlib.suppress(Exception):
            return str(_fal.label_of(model))
    return MODEL_LABELS.get(model, MODEL_LABELS[DEFAULT_MODEL])


def painting_key(sci: str, style: str, model: str) -> str:
    """``<slug(sci)>--<style>--p1``; a model other than the default carries
    its id, so each model keeps its own paintings."""
    key = f"{slug(sci)}--{style}--p1"
    return key if model == DEFAULT_MODEL else f"{key}--{model}"


def prompt_for(payload: dict[str, Any]) -> str:
    style = _pick(payload.get("style"), tuple(STYLES), DEFAULT_STYLE)
    sci = str(payload.get("sci") or "").strip()
    name = str(payload.get("common") or "").strip() or sci
    return PROMPT.format(name=name, sci=sci, how=STYLES[style][1])


def paint(job: Any) -> tuple[bytes, dict[str, Any]]:
    """One painting, on a worker thread: the species' photograph is the
    reference, the style's words the prompt."""
    p = dict(job.payload)
    photo = str(p.get("photo") or "")
    if not photo:
        raise artwork.ArtworkError(
            "no openly licensed photograph of the species to paint from"
        )
    model = model_of(p.get("model"))
    raw = job.fal.generate(model, prompt_for(p), refs=[photo], aspect="3:4", retries=1)
    lic = str(p.get("lic") or "")
    credit = f"AI painting ({model_label(model)} on fal.ai) from a photo by {p.get('by') or 'an iNaturalist user'}"
    credit += f" ({lic})" if lic else ""
    return raw, {
        "credit": credit,
        "style": _pick(p.get("style"), tuple(STYLES), DEFAULT_STYLE),
        "name": str(p.get("common") or ""),
        "sci": str(p.get("sci") or ""),
        "model": model,
    }


ART: Any = (
    artwork.Collection(
        WIDGET,
        "plates",
        make=paint,
        label="Fungi Plate",
        title="Painting fungi",
        facet="style",
        facet_label=lambda s: STYLES.get(s, (s, ""))[0],
    )
    if artwork is not None
    else _NoArtwork()
)


# ----------------------------------------------------------------------
# The fetch
# ----------------------------------------------------------------------


def _place_name(options: dict[str, Any], q: dict[str, str]) -> str:
    """The place as the title names it: the promoted label, else the
    location's own name, else the coordinates."""
    label = str(options.get("label") or "").strip()
    if label:
        return label
    loc = options.get("location")
    if isinstance(loc, dict):
        for k in ("label", "name"):
            if str(loc.get(k) or "").strip():
                return str(loc[k]).strip()
    elif isinstance(loc, str) and loc.strip():
        return loc.strip()
    lat = float(q["lat"])
    return f"{abs(lat):.2f}°{'S' if lat < 0 else 'N'}"


def title_case(name: str) -> str:
    """iNaturalist's common names arrive in whatever case the community gave
    them ("red-cage fungus", "Shaggy Parasol"); a plate wants them all alike.
    Each word, and each half of a hyphenated word, starts with a capital;
    a word that already has one inside it (McKinty's) is left alone."""

    def cap(word: str) -> str:
        if not word or any(c.isupper() for c in word[1:]):
            return word
        return word[0].upper() + word[1:]

    return " ".join(
        "-".join(cap(part) for part in w.split("-")) for w in str(name).split(" ")
    )


def _window_words(window: str, today: date) -> str:
    if window == "month":
        return MONTHS[today.month - 1]
    return f"last {window} days"


def _credit_line(rows: list[dict[str, Any]], pictures: str, model: str) -> str:
    """The foot's credit: who took each photograph, and what painted from it."""
    bits = []
    for r in rows:
        p = r.get("photo") or {}
        if not p.get("by"):
            continue
        lic = f" ({p['lic']})" if p.get("lic") else ""
        bits.append(f"{r['n']} {p['by']}{lic}")
    if not bits:
        return ""
    who = ", ".join(bits)
    if pictures == "gen" and any(
        r.get("pic", {}).get("kind") == "painting" for r in rows
    ):
        return f"AI paintings ({model_label(model)} on fal.ai) from photographs on iNaturalist by {who}"
    return f"Photographs on iNaturalist by {who}"


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    today = date.today()
    data_dir = Path(ctx.get("data_dir") or ".")
    q, why = query(options, today)
    if why:
        return {"error": why}

    count = _int(options.get("count"), 6)
    count = count if count in COUNTS else 6
    window = _pick(options.get("window"), WINDOWS, "month")
    layout = _pick(options.get("layout"), LAYOUTS, "plate")
    pictures = _pick(options.get("pictures"), ("photos", "gen"), "photos")
    style = _pick(options.get("style"), tuple(STYLES), DEFAULT_STYLE)
    model = model_of(options.get("model"))
    paper = _pick(options.get("paper"), PAPERS, "cream")
    panel = _pick(options.get("panel"), LEVELS, "colour")
    credits = options.get("credits", True) is not False
    source = "user" if "user_login" in q else "place"
    place = f"@{q['user_login']}" if source == "user" else _place_name(options, q)
    within = f"within {q['radius']} km" if source == "place" else "their own records"
    title = str(options.get("title") or "").strip()
    if not title:
        head = (
            "FUNGI OF THE MONTH"
            if window == "month"
            else f"FUNGI OF THE LAST {window} DAYS"
        )
        title = f"{head} · {place}"

    # --- the species, kept six hours ----------------------------------
    # The top nine are loaded whatever the cell shows, so a change of How many
    # comes from the cache, and a cold fetch costs the same for every cell.
    stamp = hashlib.sha1(json.dumps(q, sort_keys=True).encode()).hexdigest()[:16]
    cache = data_dir / f"inat_{stamp}.json"
    got = None if ctx.get("fresh") else _read_json(cache, CACHE_S)
    if got is None:
        got, err = load(q, max(COUNTS))
        if err:
            stale = _read_json(cache, 30 * 86400)
            if stale is None:
                return {"error": err}
            got = stale
        else:
            _write_json(cache, got)
    rows = [dict(r) for r in got.get("rows") or []][:count]

    out: dict[str, Any] = {
        "title": title,
        "place": place,
        "within": within,
        "plate": f"Plate {roman(today.month)}",
        "roman": roman(today.month),
        "month": MONTHS[today.month - 1],
        "year": today.year,
        "window": window,
        "window_words": _window_words(window, today),
        "layout": layout,
        "paper": paper,
        "panel": panel,
        "pictures": pictures,
        "credits": credits,
        "species_total": int(got.get("species_total") or 0),
        "species": [],
        "notices": [],
        "status": [],
        "credit_line": "",
        "live_refresh_s": 0,
        "live_pending": 0,
    }
    if not rows:
        when = (
            "this " + MONTHS[today.month - 1]
            if window == "month"
            else f"in the last {window} days"
        )
        where = f"{within} of {place}" if source == "place" else f"by {place}"
        out["empty"] = (
            f"No research-grade fungi with openly licensed photos {where} {when}."
        )
        return out

    # --- pictures -----------------------------------------------------
    px = 640 if count <= 2 else 480
    urls = [r["photo"]["url"] for r in rows if r.get("photo", {}).get("url")]
    photo_src: dict[str, str] = {}
    if urls:
        with ThreadPoolExecutor(max_workers=min(9, len(urls))) as pool:
            photo_src = dict(
                zip(urls, pool.map(lambda u: photo_uri(u, data_dir, px, panel), urls))
            )
    art: Any = None
    if pictures == "gen":
        subject = slug(f"{place} {window} {source} {count}")
        art = ART.open(ctx, settings, subject=subject)
    for i, r in enumerate(rows, 1):
        r["n"] = i
        r["name"] = r.get("name") or ""
        photo = r.get("photo") or {}
        r["pic"] = {"src": "", "kind": "none"}
        if art is not None and photo.get("url"):
            key = painting_key(r["sci"], style, model)
            payload = {
                "style": style,
                "photo": photo["url"],
                "common": r["name"],
                "sci": r["sci"],
                "model": model,
                "by": photo.get("by", ""),
                "lic": photo.get("lic", ""),
            }
            g = art.request(key, payload, title=r["name"] or r["sci"])
            if g.ready:
                src = art.url(
                    key, px=px, levels=panel, variant=trimmed_variant(art, key)
                )
                if src:
                    r["pic"] = {"src": src, "kind": "painting", "credit": g.credit}
        if r["pic"]["kind"] == "none" and photo.get("url"):
            src = photo_src.get(photo["url"], "")
            if src:
                r["pic"] = {"src": src, "kind": "photo"}
    if art is not None:
        art.finish(ctx, facet=style)
        out["notices"] = list(art.notes())
        out["status"] = list(art.status())
        out.update(art.live())
        logger.info(
            "fungi_plate: %s render, %d species, painted %d, started %d, held %d, capped %d, no key %d, failed %d",
            "preview" if ctx.get("preview") else "panel",
            len(rows),
            art.states.get("ready", 0),
            art.started,
            art.states.get("held", 0),
            art.states.get("capped", 0),
            art.states.get("no_key", 0),
            art.states.get("failed", 0),
        )
    out["species"] = [
        {
            "n": r["n"],
            "name": r["name"],
            "sci": r["sci"],
            "count": r["count"],
            "last": r.get("last") or "",
            "pic": r["pic"],
        }
        for r in rows
    ]
    out["credit_line"] = _credit_line(rows, pictures, model) if credits else ""
    return out
