import asyncio
import csv
import json
import logging
import os
from pathlib import Path

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("gtc")

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

BASE_DIR = Path(__file__).parent
PLACES_FILE = BASE_DIR / "places.csv"
# Cache geocoded coords locally; keep out of git via .gitignore
CACHE_FILE = (
    Path("/tmp/places_cache.json")
    if os.environ.get("VERCEL")
    else BASE_DIR / "places_cache.json"
)

_places_cache: list | None = None
_places_lock = asyncio.Lock()


# ── Geocoding ────────────────────────────────────────────────────────────────

def _load_geo_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_geo_cache(cache: dict) -> None:
    try:
        CACHE_FILE.write_text(json.dumps(cache, indent=2))
    except Exception:
        pass


async def _coords_from_gmaps(url: str, client: httpx.AsyncClient) -> tuple[float, float] | None:
    """Follow a Google Maps short URL and extract coordinates from the final URL."""
    import re
    try:
        r = await client.get(url, follow_redirects=True, timeout=15,
                             headers={"User-Agent": "Mozilla/5.0"})
        final_url = str(r.url)
        # Matches /@lat,lng or @lat,lng in the URL path
        m = re.search(r'@(-?\d+\.\d+),(-?\d+\.\d+)', final_url)
        if m:
            return float(m.group(1)), float(m.group(2))
        # Fallback: ?q=lat,lng or &q=lat,lng
        m = re.search(r'[?&]q=(-?\d+\.\d+),(-?\d+\.\d+)', final_url)
        if m:
            return float(m.group(1)), float(m.group(2))
    except Exception:
        pass
    return None


async def _geocode(address: str, client: httpx.AsyncClient) -> tuple[float, float] | None:
    try:
        r = await client.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": address, "format": "json", "limit": 1},
            headers={"User-Agent": "goingtocalifornia/1.0"},
            timeout=15,
        )
        data = r.json()
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception:
        pass
    return None


# ── Places loading ────────────────────────────────────────────────────────────

async def get_places() -> list:
    global _places_cache
    if _places_cache is not None:
        return _places_cache

    async with _places_lock:
        if _places_cache is not None:
            return _places_cache

        places_data = os.environ.get("PLACES_DATA")
        log.info("PLACES_FILE exists: %s", PLACES_FILE.exists())
        log.info("PLACES_DATA env var present: %s, length: %s",
                 bool(places_data), len(places_data) if places_data else 0)

        if not PLACES_FILE.exists() and not places_data:
            log.warning("No places source found — returning empty list")
            _places_cache = []
            return []

        geo_cache = _load_geo_cache()
        rows: list[dict] = []

        import io
        if PLACES_FILE.exists():
            raw = PLACES_FILE.read_text(encoding="utf-8")
            log.info("Reading from file, first 200 chars: %r", raw[:200])
            reader = csv.DictReader(io.StringIO(raw.strip()))
            for row in reader:
                rows.append({k.strip(): (v.strip() if v else '') for k, v in row.items() if k})
        else:
            log.info("Reading from PLACES_DATA env var, first 200 chars: %r", places_data[:200])
            # Env var is JSON to avoid CSV quoting issues with commas in values
            rows = json.loads(places_data)

        log.info("Parsed %d rows: %s", len(rows), [r.get("name") for r in rows])

        needs_save = False

        async with httpx.AsyncClient() as client:
            for row in rows:
                # Accept pre-supplied lat/lng in the CSV
                try:
                    if row.get("lat") and row.get("lng"):
                        row["lat"] = float(row["lat"])
                        row["lng"] = float(row["lng"])
                        continue
                except ValueError:
                    pass

                address = row.get("address", "")
                if not address:
                    log.warning("Row with no address: %s", row)
                    continue

                if address in geo_cache:
                    row["lat"] = geo_cache[address]["lat"]
                    row["lng"] = geo_cache[address]["lng"]
                    log.info("Cache hit: %s", row.get("name"))
                else:
                    # Nominatim rate-limit: 1 req/s
                    await asyncio.sleep(1.2)
                    coords = await _geocode(address, client)
                    if not coords and row.get("gmaps_url"):
                        log.info("Nominatim failed for %s, trying gmaps", row.get("name"))
                        coords = await _coords_from_gmaps(row["gmaps_url"], client)
                    if coords:
                        row["lat"], row["lng"] = coords
                        geo_cache[address] = {"lat": coords[0], "lng": coords[1]}
                        needs_save = True
                    else:
                        log.warning("Geocode failed for %s (%s)", row.get("name"), address)
                        row["geocode_failed"] = True

        if needs_save:
            _save_geo_cache(geo_cache)

        ok  = [r["name"] for r in rows if "lat" in r]
        bad = [r["name"] for r in rows if r.get("geocode_failed")]
        log.info("Geocoded OK: %s", ok)
        log.info("Geocode failed: %s", bad)

        _places_cache = rows
        return _places_cache


# ── Travel matrix (OSRM) ──────────────────────────────────────────────────────

async def get_travel_matrix(places: list) -> dict:
    n = len(places)
    null_row = [None] * n
    empty = {"durations": [null_row[:] for _ in range(n)],
             "distances": [null_row[:] for _ in range(n)]}

    # Only route between successfully geocoded places; track their original indices
    routable_idx = [i for i, p in enumerate(places) if not p.get("geocode_failed")]
    if len(routable_idx) < 2:
        return empty

    coords = ";".join(f"{places[i]['lng']},{places[i]['lat']}" for i in routable_idx)
    url = f"http://router.project-osrm.org/table/v1/driving/{coords}"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url, params={"annotations": "duration,distance"})
            data = r.json()
            if data.get("code") == "Ok":
                osrm_dur  = data.get("durations", [])
                osrm_dist = data.get("distances", [])
                # Expand back to full N×N matrix (null for failed places)
                full_dur  = [null_row[:] for _ in range(n)]
                full_dist = [null_row[:] for _ in range(n)]
                for ri, i in enumerate(routable_idx):
                    for rj, j in enumerate(routable_idx):
                        full_dur[i][j]  = osrm_dur[ri][rj]
                        full_dist[i][j] = osrm_dist[ri][rj]
                return {"durations": full_dur, "distances": full_dist}
    except Exception:
        pass

    return empty


# ── Route geometry (OSRM) ─────────────────────────────────────────────────────

async def get_route(origin: dict, dest: dict) -> dict | None:
    url = (f"http://router.project-osrm.org/route/v1/driving/"
           f"{origin['lng']},{origin['lat']};{dest['lng']},{dest['lat']}")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url, params={"overview": "full", "geometries": "geojson"})
            data = r.json()
            if data.get("code") == "Ok":
                route = data["routes"][0]
                return {
                    "duration": route["duration"],
                    "distance": route["distance"],
                    "geometry": route["geometry"],
                }
    except Exception:
        pass
    return None


# ── API routes ────────────────────────────────────────────────────────────────

app = FastAPI()


@app.get("/api/places")
async def api_places():
    return await get_places()


@app.get("/api/travel-matrix")
async def api_travel_matrix():
    places = await get_places()
    return await get_travel_matrix(places)


@app.get("/api/route")
async def api_route(from_idx: int, to_idx: int):
    places = await get_places()
    if from_idx >= len(places) or to_idx >= len(places):
        return {"error": "Invalid index"}
    result = await get_route(places[from_idx], places[to_idx])
    return result or {"error": "Route not found"}


# ── Frontend ──────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Going to California</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    *, *::before, *::after { margin: 0; padding: 0; box-sizing: border-box; }

    :root {
      --amber:       #f59e0b;
      --amber-light: #fbbf24;
      --indigo:      #6366f1;
      --green:       #10b981;
      --bg:          #07070f;
      --surface:     rgba(10, 10, 22, 0.97);
      --border:      rgba(255,255,255,0.06);
      --text-hi:     #f1f5f9;
      --text-mid:    #64748b;
      --text-lo:     #334155;
    }

    body {
      font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Segoe UI', system-ui, sans-serif;
      background: var(--bg);
      color: var(--text-hi);
      height: 100vh;
      overflow: hidden;
      display: flex;
    }

    /* ── Loading overlay ── */
    #loading {
      position: fixed; inset: 0;
      background: var(--bg);
      display: flex; flex-direction: column; align-items: center; justify-content: center;
      gap: 18px; z-index: 9999;
      transition: opacity 0.5s ease;
    }
    #loading.out { opacity: 0; pointer-events: none; }

    .spinner {
      width: 34px; height: 34px;
      border: 2px solid rgba(245,158,11,0.15);
      border-top-color: var(--amber);
      border-radius: 50%;
      animation: spin 0.75s linear infinite;
    }
    .loading-label { font-size: 12.5px; color: var(--text-mid); letter-spacing: 0.03em; }

    @keyframes spin { to { transform: rotate(360deg); } }

    /* ── Sidebar ── */
    #sidebar {
      width: 310px; min-width: 310px; height: 100vh;
      background: var(--surface);
      border-right: 1px solid var(--border);
      display: flex; flex-direction: column;
      position: relative; z-index: 500;
    }

    .sidebar-header {
      padding: 22px 18px 16px;
      border-bottom: 1px solid var(--border);
      background: linear-gradient(160deg, rgba(245,158,11,0.05) 0%, transparent 60%);
      flex-shrink: 0;
    }

    .eyebrow {
      font-size: 9.5px; font-weight: 700; letter-spacing: 0.14em;
      text-transform: uppercase; color: var(--amber); opacity: 0.75;
      margin-bottom: 6px;
    }

    .app-title {
      font-size: 18px; font-weight: 700; letter-spacing: -0.3px;
      color: var(--text-hi);
    }

    .app-hint {
      font-size: 11.5px; color: var(--text-mid); margin-top: 5px; line-height: 1.45;
    }

    /* ── Places list ── */
    #places-list {
      flex: 1; overflow-y: auto; padding: 10px;
      scrollbar-width: thin;
      scrollbar-color: rgba(255,255,255,0.07) transparent;
    }
    #places-list::-webkit-scrollbar { width: 3px; }
    #places-list::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.07); border-radius: 2px; }

    .place-card {
      padding: 12px 14px 11px;
      border-radius: 9px; margin-bottom: 6px;
      cursor: pointer;
      border: 1px solid var(--border);
      background: rgba(255,255,255,0.012);
      transition: background 0.15s, border-color 0.15s;
      position: relative; overflow: hidden;
    }
    .place-card:hover {
      background: rgba(255,255,255,0.038);
      border-color: rgba(245,158,11,0.22);
    }
    .place-card.active {
      background: rgba(245,158,11,0.07);
      border-color: rgba(245,158,11,0.32);
    }

    /* Left accent bar */
    .card-bar {
      position: absolute; left: 0; top: 0; bottom: 0;
      width: 2.5px; border-radius: 9px 0 0 9px;
      background: linear-gradient(180deg, var(--amber), var(--amber-light));
      opacity: 0; transition: opacity 0.15s;
    }
    .place-card.active .card-bar { opacity: 1; }

    .card-top {
      display: flex; align-items: flex-start;
      justify-content: space-between; gap: 8px;
      margin-bottom: 3px;
    }

    .place-name {
      font-size: 13px; font-weight: 600; color: var(--text-hi); line-height: 1.3;
    }

    .rank-badge {
      font-size: 9.5px; font-weight: 700; color: var(--text-lo);
      background: rgba(255,255,255,0.04);
      border: 1px solid var(--border);
      padding: 1px 6px; border-radius: 10px;
      white-space: nowrap; flex-shrink: 0; margin-top: 1px;
    }

    .place-addr {
      font-size: 10.5px; color: var(--text-lo);
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
      margin-bottom: 7px;
    }

    .place-travel { display: flex; align-items: center; gap: 6px; }

    .chip {
      display: inline-flex; align-items: center; gap: 4px;
      padding: 2px 8px; border-radius: 20px;
      font-size: 11px; font-weight: 600; letter-spacing: 0.01em;
    }
    .chip.drive {
      background: rgba(99,102,241,0.12);
      border: 1px solid rgba(99,102,241,0.22);
      color: #a5b4fc;
    }
    .chip.origin {
      background: rgba(245,158,11,0.11);
      border: 1px solid rgba(245,158,11,0.28);
      color: var(--amber);
    }
    .chip.na {
      background: rgba(255,255,255,0.04);
      border: 1px solid var(--border);
      color: var(--text-lo);
    }

    .place-card.failed {
      opacity: 0.55;
      cursor: default;
      border-style: dashed;
    }
    .place-card.failed:hover {
      background: rgba(255,255,255,0.012);
      border-color: rgba(239,68,68,0.25);
    }

    .warn-badge {
      font-size: 9.5px; font-weight: 700;
      color: #f87171;
      background: rgba(239,68,68,0.1);
      border: 1px solid rgba(239,68,68,0.25);
      padding: 1px 6px; border-radius: 10px;
      white-space: nowrap; flex-shrink: 0; margin-top: 1px;
    }

    .dist-label { font-size: 10.5px; color: var(--text-lo); }

    /* ── Status bar ── */
    #statusbar {
      padding: 10px 16px;
      border-top: 1px solid var(--border);
      flex-shrink: 0;
      display: flex; align-items: center; gap: 7px;
    }
    .status-dot {
      width: 5px; height: 5px; border-radius: 50%;
      background: #22c55e; flex-shrink: 0;
    }
    #status-text { font-size: 11px; color: var(--text-mid); }

    /* ── Map ── */
    #map { flex: 1; height: 100vh; }

    /* ── Leaflet overrides ── */
    .leaflet-tooltip {
      background: rgba(7,7,15,0.92) !important;
      border: 1px solid rgba(245,158,11,0.28) !important;
      color: var(--text-hi) !important;
      font-size: 11.5px !important;
      font-family: inherit !important;
      border-radius: 6px !important;
      padding: 4px 10px !important;
      box-shadow: 0 4px 24px rgba(0,0,0,0.5) !important;
      white-space: nowrap;
    }
    .leaflet-tooltip::before { display: none !important; }

    .leaflet-control-zoom {
      border: none !important;
      box-shadow: none !important;
      margin-bottom: 20px !important;
      margin-right: 12px !important;
    }
    .leaflet-control-zoom a {
      background: rgba(10,10,22,0.88) !important;
      border: 1px solid var(--border) !important;
      color: var(--text-mid) !important;
      transition: all 0.18s !important;
    }
    .leaflet-control-zoom a:hover {
      background: rgba(30,30,60,0.95) !important;
      color: var(--text-hi) !important;
      border-color: rgba(245,158,11,0.3) !important;
    }

    .leaflet-control-attribution {
      background: rgba(7,7,15,0.7) !important;
      color: var(--text-lo) !important;
      font-size: 10px !important;
    }
    .leaflet-control-attribution a { color: var(--text-lo) !important; }

    /* ── Custom marker ── */
    .mkr {
      display: flex; align-items: center; justify-content: center;
      position: relative;
    }
    .mkr-core {
      width: 11px; height: 11px; border-radius: 50%;
      background: var(--amber);
      border: 1.5px solid rgba(255,255,255,0.18);
      box-shadow: 0 0 8px rgba(245,158,11,0.45);
      position: relative; z-index: 2;
      transition: all 0.25s;
    }
    .mkr.sel .mkr-core {
      width: 15px; height: 15px;
      background: var(--amber-light);
      box-shadow: 0 0 0 3px rgba(245,158,11,0.18), 0 0 22px rgba(245,158,11,0.75);
      animation: core-pulse 2.4s ease-in-out infinite;
    }
    .mkr-ring {
      position: absolute; border-radius: 50%;
      border: 1.5px solid rgba(245,158,11,0.55);
      pointer-events: none; opacity: 0;
    }
    .mkr.sel .mkr-ring {
      animation: ring-out 2.4s ease-out infinite;
    }

    @keyframes core-pulse {
      0%,100% { box-shadow: 0 0 0 3px rgba(245,158,11,0.18), 0 0 22px rgba(245,158,11,0.65); }
      50%      { box-shadow: 0 0 0 6px rgba(245,158,11,0.08), 0 0 38px rgba(245,158,11,0.9); }
    }
    @keyframes ring-out {
      0%   { width: 15px; height: 15px; opacity: 0.65; }
      100% { width: 46px; height: 46px; opacity: 0; }
    }
    @keyframes march { to { stroke-dashoffset: -300; } }

    /* ── Destination marker (indigo) ── */
    .mkr.dest .mkr-core {
      background: #818cf8;
      box-shadow: 0 0 8px rgba(99,102,241,0.5);
    }
    .mkr.dest .mkr-ring {
      border-color: rgba(99,102,241,0.55);
      animation: ring-out 2.4s ease-out infinite;
    }
    .mkr.dest .mkr-core {
      width: 15px; height: 15px;
      animation: dest-pulse 2.4s ease-in-out infinite;
    }
    @keyframes dest-pulse {
      0%,100% { box-shadow: 0 0 0 3px rgba(99,102,241,0.18), 0 0 22px rgba(99,102,241,0.65); }
      50%      { box-shadow: 0 0 0 6px rgba(99,102,241,0.08), 0 0 38px rgba(99,102,241,0.9); }
    }

    /* ── Destination card accent ── */
    .place-card.dest-active {
      background: rgba(99,102,241,0.07);
      border-color: rgba(99,102,241,0.32);
    }
    .place-card.dest-active:hover {
      border-color: rgba(99,102,241,0.45);
    }
    .card-bar.dest-bar {
      background: linear-gradient(180deg, #6366f1, #818cf8);
    }

    .chip.dest-chip {
      background: rgba(99,102,241,0.12);
      border: 1px solid rgba(99,102,241,0.28);
      color: #a5b4fc;
    }

    /* ── Route card ── */
    .route-card {
      margin: 4px 0 10px;
      padding: 14px 14px 12px;
      border-radius: 10px;
      background: linear-gradient(135deg, rgba(245,158,11,0.07), rgba(99,102,241,0.07));
      border: 1px solid rgba(245,158,11,0.2);
    }
    .route-endpoints { display: flex; flex-direction: column; gap: 6px; margin-bottom: 10px; }
    .route-ep {
      display: flex; align-items: center; gap: 8px;
      font-size: 12.5px; font-weight: 600; color: var(--text-hi);
    }
    .route-dot {
      width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0;
    }
    .origin-dot { background: var(--amber); box-shadow: 0 0 6px rgba(245,158,11,0.6); }
    .dest-dot   { background: #818cf8;      box-shadow: 0 0 6px rgba(99,102,241,0.6); }

    .route-line-divider {
      width: 1px; height: 14px;
      background: linear-gradient(180deg, var(--amber), #818cf8);
      margin-left: 3.5px; opacity: 0.4;
    }
    .route-stats {
      display: flex; align-items: baseline; gap: 8px; margin-bottom: 10px;
    }
    .route-stat-main {
      font-size: 18px; font-weight: 700; color: var(--text-hi);
    }
    .route-stat-sub {
      font-size: 11.5px; color: var(--text-mid);
    }
    .clear-btn {
      background: rgba(255,255,255,0.05);
      border: 1px solid var(--border);
      color: var(--text-mid);
      font-size: 11px; font-family: inherit;
      padding: 4px 10px; border-radius: 6px;
      cursor: pointer; transition: all 0.15s;
    }
    .clear-btn:hover {
      background: rgba(255,255,255,0.1);
      color: var(--text-hi);
    }

    /* ── Empty state ── */
    .empty {
      display: flex; flex-direction: column; align-items: center; justify-content: center;
      height: 100%; padding: 32px 20px; text-align: center; gap: 10px;
    }
    .empty-icon  { font-size: 26px; opacity: 0.4; }
    .empty-title { font-size: 13.5px; color: var(--text-mid); font-weight: 500; }
    .empty-desc  { font-size: 11.5px; color: var(--text-lo); line-height: 1.55; }
    code {
      font-family: 'SF Mono','Cascadia Code','Roboto Mono',Consolas,monospace;
      font-size: 10.5px;
      background: rgba(255,255,255,0.05);
      padding: 2px 5px; border-radius: 4px;
      color: var(--amber);
    }
  </style>
</head>
<body>

<div id="loading">
  <div class="spinner"></div>
  <div class="loading-label">Loading locations…</div>
</div>

<div id="sidebar">
  <div class="sidebar-header">
    <div class="eyebrow">Travel Planner</div>
    <div class="app-title">Going to California</div>
    <div class="app-hint">Click any location to see driving times</div>
  </div>
  <div id="places-list"></div>
  <div id="statusbar">
    <div class="status-dot"></div>
    <span id="status-text">Initializing…</span>
  </div>
</div>

<div id="map"></div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
// ── Map init ────────────────────────────────────────────────────────────────
const map = L.map('map', {
  center: [36.7783, -119.4179],
  zoom: 6,
  zoomControl: false,
  attributionControl: true,
});

L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com">CARTO</a>',
  subdomains: 'abcd',
  maxZoom: 19,
}).addTo(map);

L.control.zoom({ position: 'bottomright' }).addTo(map);

// ── State ───────────────────────────────────────────────────────────────────
let places    = [];
let matrix    = { durations: [], distances: [] };
let markers   = [];
let lines     = [];
let routeLayer = null;
let originIdx  = null;
let destIdx    = null;

// ── Marker icons ─────────────────────────────────────────────────────────────
function mkIcon(role) {
  // role: 'origin' | 'dest' | 'normal'
  const isOrigin = role === 'origin';
  const isDest   = role === 'dest';
  const isActive = isOrigin || isDest;
  const sz = isActive ? 50 : 24;
  const cls = isOrigin ? 'mkr sel' : isDest ? 'mkr dest' : 'mkr';
  return L.divIcon({
    html: `<div class="${cls}" style="width:${sz}px;height:${sz}px">
             <div class="mkr-ring" style="width:15px;height:15px"></div>
             <div class="mkr-core"></div>
           </div>`,
    className: '',
    iconSize: [sz, sz],
    iconAnchor: [sz / 2, sz / 2],
  });
}

// ── Render markers ──────────────────────────────────────────────────────────
function renderMarkers() {
  markers.forEach(m => m && m.remove());
  markers = places.map((p, i) => {
    if (p.geocode_failed) return null;
    const role = i === originIdx ? 'origin' : i === destIdx ? 'dest' : 'normal';
    const m = L.marker([p.lat, p.lng], { icon: mkIcon(role) })
      .addTo(map)
      .bindTooltip(p.name, { direction: 'top', offset: [0, -10] });
    m.on('click', () => pick(i));
    return m;
  });
}

// ── Dashed overview lines (origin-only mode) ─────────────────────────────────
function drawOverviewLines() {
  lines.forEach(l => l.remove());
  lines = [];
  if (originIdx === null || !matrix.durations.length) return;

  const ori = places[originIdx];
  const row = matrix.durations[originIdx] || [];
  const validDurs = row.filter((d, i) => i !== originIdx && d != null);
  const maxDur = validDurs.length ? Math.max(...validDurs) : 1;

  places.forEach((dst, i) => {
    if (i === originIdx || dst.geocode_failed) return;
    const dur   = row[i];
    const ratio = (dur != null && maxDur) ? Math.min(dur / maxDur, 1) : 0.5;
    const color = ratio < 0.35 ? '#f59e0b' : ratio < 0.65 ? '#a78bfa' : '#6366f1';
    const opacity = 0.12 + (1 - ratio) * 0.48;

    const line = L.polyline(
      [[ori.lat, ori.lng], [dst.lat, dst.lng]],
      { color, weight: 1.5, opacity, dashArray: '5 10' }
    ).addTo(map);

    const el = line.getElement();
    if (el) el.style.animation = 'march 28s linear infinite';
    lines.push(line);
  });
}

// ── Draw actual route ────────────────────────────────────────────────────────
function clearRoute() {
  if (routeLayer) { routeLayer.remove(); routeLayer = null; }
}

async function drawRoute() {
  clearRoute();
  lines.forEach(l => l.remove());
  lines = [];
  if (originIdx === null || destIdx === null) return;

  setStatus('Fetching route…');
  const r = await fetch(`/api/route?from_idx=${originIdx}&to_idx=${destIdx}`);
  const data = await r.json();
  if (data.error || !data.geometry) { setStatus('No route found'); return; }

  // Glow layer (thick, dim) + route layer (thin, bright)
  const coords = data.geometry.coordinates.map(([lng, lat]) => [lat, lng]);
  const glow = L.polyline(coords, { color: '#f59e0b', weight: 10, opacity: 0.12 }).addTo(map);
  const route = L.polyline(coords, { color: '#fbbf24', weight: 3.5, opacity: 0.9 }).addTo(map);

  // Animate flow
  const el = route.getElement();
  if (el) {
    el.style.strokeDasharray = '12 8';
    el.style.animation = 'march 18s linear infinite';
  }

  // Wrap both in a layer group so we can remove together
  routeLayer = L.layerGroup([glow, route]).addTo(map);
  // layerGroup.addTo adds the group but layers are already added — remove the doubles
  glow.remove(); route.remove();
  glow.addTo(map); route.addTo(map);
  routeLayer = { remove: () => { glow.remove(); route.remove(); } };

  // Fit to route
  map.fitBounds(L.latLngBounds(coords), { padding: [60, 80], animate: true });

  const mins  = Math.round(data.duration / 60);
  const h = Math.floor(mins / 60), m = mins % 60;
  const time  = h > 0 ? (m > 0 ? `${h}h ${m}m` : `${h}h`) : `${m}m`;
  const km = (data.distance / 1000).toFixed(0);

  renderSidebar(data);
  setStatus(`${places[originIdx].name} → ${places[destIdx].name} · ${time} · ${km} km`);
}

// ── Render sidebar ──────────────────────────────────────────────────────────
function renderSidebar(routeData = null) {
  const list = document.getElementById('places-list');

  if (!places.length) {
    list.innerHTML = `<div class="empty">
      <div class="empty-icon">🗺️</div>
      <div class="empty-title">No locations found</div>
      <div class="empty-desc">Create <code>places.csv</code> with<br><code>name</code> and <code>address</code> columns</div>
    </div>`;
    return;
  }

  let routeCard = '';
  if (routeData && originIdx !== null && destIdx !== null) {
    const mins  = Math.round(routeData.duration / 60);
    const h = Math.floor(mins / 60), m = mins % 60;
    const time  = h > 0 ? (m > 0 ? `${h}h ${m}m` : `${h}h`) : `${m}m`;
    const km = (routeData.distance / 1000).toFixed(0);
    routeCard = `
      <div class="route-card">
        <div class="route-endpoints">
          <div class="route-ep origin-ep">
            <span class="route-dot origin-dot"></span>
            <span>${esc(places[originIdx].name)}</span>
          </div>
          <div class="route-line-divider"></div>
          <div class="route-ep dest-ep">
            <span class="route-dot dest-dot"></span>
            <span>${esc(places[destIdx].name)}</span>
          </div>
        </div>
        <div class="route-stats">
          <span class="route-stat-main">🚗 ${time}</span>
          <span class="route-stat-sub">${km} km by road</span>
        </div>
        <button class="clear-btn" onclick="reset()">✕ Clear route</button>
      </div>`;
  }

  const durRow = (originIdx !== null && matrix.durations.length)
    ? (matrix.durations[originIdx] || []) : [];

  const ordered = places
    .map((p, i) => ({ ...p, idx: i }))
    .sort((a, b) => {
      if (a.geocode_failed && !b.geocode_failed) return 1;
      if (!a.geocode_failed && b.geocode_failed) return -1;
      if (a.idx === originIdx) return -1;
      if (b.idx === originIdx) return 1;
      if (a.idx === destIdx) return -1;
      if (b.idx === destIdx) return 1;
      if (!durRow.length) return 0;
      return (durRow[a.idx] ?? Infinity) - (durRow[b.idx] ?? Infinity);
    });

  let rank = 1;
  const cards = ordered.map(p => {
    const i = p.idx;
    const isOrigin = i === originIdx;
    const isDest   = i === destIdx;

    if (p.geocode_failed) {
      return `<div class="place-card failed">
        <div class="card-top">
          <div class="place-name" style="color:var(--text-mid)">${esc(p.name)}</div>
          <span class="warn-badge">⚠ Not found</span>
        </div>
        <div class="place-addr">${esc(p.address)}</div>
        <div class="place-travel"><span style="font-size:11px;color:var(--text-lo)">Could not geocode address</span></div>
      </div>`;
    }

    const dur  = durRow[i];
    const dist = (matrix.distances[originIdx] || [])[i];

    let travelHtml;
    if (isOrigin) {
      travelHtml = `<div class="place-travel"><span class="chip origin">📍 Origin</span></div>`;
    } else if (isDest && routeData) {
      travelHtml = `<div class="place-travel"><span class="chip dest-chip">🏁 Destination</span></div>`;
    } else if (dur != null && originIdx !== null) {
      const mins = Math.round(dur / 60);
      const h = Math.floor(mins / 60), m = mins % 60;
      const t = h > 0 ? (m > 0 ? `${h}h ${m}m` : `${h}h`) : `${m}m`;
      const km = dist ? `${(dist / 1000).toFixed(0)} km` : '';
      travelHtml = `<div class="place-travel">
        <span class="chip drive">🚗 ${t}</span>
        ${km ? `<span class="dist-label">${km}</span>` : ''}
      </div>`;
    } else if (originIdx !== null && dur == null) {
      travelHtml = `<div class="place-travel"><span class="chip na">No route</span></div>`;
    } else {
      travelHtml = `<div class="place-travel"><span style="font-size:11px;color:var(--text-lo)">Select to view</span></div>`;
    }

    const rankHtml = (!isOrigin && !isDest && originIdx !== null && durRow.length && dur != null)
      ? `<span class="rank-badge">#${rank++}</span>` : '';

    const activeClass = isOrigin ? ' active' : isDest ? ' active dest-active' : '';
    return `<div class="place-card${activeClass}" onclick="pick(${i})">
      <div class="card-bar${isDest ? ' dest-bar' : ''}"></div>
      <div class="card-top">
        <div class="place-name">${esc(p.name)}</div>
        ${rankHtml}
      </div>
      <div class="place-addr">${esc(p.address)}</div>
      ${travelHtml}
    </div>`;
  }).join('');

  list.innerHTML = routeCard + cards;
}

// ── Pick logic ───────────────────────────────────────────────────────────────
function pick(idx) {
  if (places[idx]?.geocode_failed) return;

  if (destIdx !== null) {
    // Route active → any click resets
    reset();
  } else if (originIdx === null) {
    // Nothing selected → set origin
    originIdx = idx;
    renderMarkers();
    renderSidebar();
    drawOverviewLines();
    setStatus(`Driving times from ${places[idx].name} — click another to route`);
  } else if (idx === originIdx) {
    // Click origin again → full reset
    reset();
  } else {
    // Different place → set as destination, show route
    destIdx = idx;
    renderMarkers();
    drawRoute();
  }
}

function reset() {
  originIdx = null;
  destIdx   = null;
  clearRoute();
  lines.forEach(l => l.remove());
  lines = [];
  renderMarkers();
  renderSidebar();
  setStatus('Click a location to start');
}

// ── Utils ───────────────────────────────────────────────────────────────────
const esc = s => String(s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');

const setStatus = t => document.getElementById('status-text').textContent = t;

// ── Init ────────────────────────────────────────────────────────────────────
async function init() {
  setStatus('Loading locations…');
  try {
    const [pr, mr] = await Promise.all([
      fetch('/api/places'),
      fetch('/api/travel-matrix'),
    ]);
    if (!pr.ok || !mr.ok) throw new Error('API error');
    places = await pr.json();
    matrix = await mr.json();
  } catch (e) {
    setStatus('Failed to load — is the server running?');
    document.getElementById('loading').innerHTML =
      '<div style="color:#ef4444;font-size:13px">Failed to connect to server</div>';
    return;
  }

  const loading = document.getElementById('loading');
  loading.classList.add('out');
  setTimeout(() => loading.remove(), 550);

  if (!places.length) {
    renderSidebar();
    setStatus('No locations — add places.csv');
    return;
  }

  const routable = places.filter(p => !p.geocode_failed);
  const bounds = L.latLngBounds(routable.map(p => [p.lat, p.lng]));
  map.fitBounds(bounds, { padding: [60, 80] });

  renderMarkers();
  renderSidebar();
  setStatus('Click a location to start');
}

init();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def root():
    return HTML
