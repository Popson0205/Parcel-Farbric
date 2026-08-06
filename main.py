"""
Parcel Plotting Pipeline — minimal test service.

Tests ONLY the plotting pipeline from the GeoEstate blueprint:

  COGO traverse / direct GPS points
      -> coordinate transform (to WGS84)
      -> build closed polygon
      -> topology validation (self-intersection + overlap with fabric)
      -> store in PostGIS with a PIN
      -> serve back as GeoJSON

Deploy on Railway. Uses asyncpg (no libpq system dependency) against
Supabase or any Postgres with PostGIS.
"""

import json
import math
import os
from typing import List, Optional

import asyncpg
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from shapely.geometry import Polygon, mapping
from shapely.validation import explain_validity
from pyproj import Transformer, Geod

app = FastAPI(title="GeoEstate Parcel Plotting — Test Service")

DATABASE_URL = os.environ.get("DATABASE_URL")  # set manually in Railway Variables for Supabase
geod = Geod(ellps="WGS84")


@app.on_event("startup")
async def setup_db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL not set — add your Supabase connection string in Railway Variables")

    app.state.pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

    async with app.state.pool.acquire() as conn:
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS postgis;")
        except Exception as e:
            # On Supabase, enable PostGIS via Database > Extensions in the dashboard
            # if this role lacks CREATE EXTENSION rights — safe to ignore once enabled there.
            print(f"Skipping CREATE EXTENSION (likely already enabled via dashboard): {e}")

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS parcels (
                id SERIAL PRIMARY KEY,
                pin TEXT UNIQUE NOT NULL,
                area_sqm DOUBLE PRECISION,
                geom GEOMETRY(Polygon, 4326) NOT NULL,
                created_at TIMESTAMP DEFAULT now()
            );
            """
        )


@app.on_event("shutdown")
async def shutdown_db():
    await app.state.pool.close()


# ---------- Request / response models ----------

class Leg(BaseModel):
    bearing_deg: float = Field(..., description="0 = North, clockwise")
    distance_m: float = Field(..., gt=0)
    beacon: Optional[str] = Field(None, description="Beacon/pillar number marking the point this leg walks TO")


class CogoRequest(BaseModel):
    start_easting: float = Field(..., description="Easting (m) on the local grid, not longitude")
    start_northing: float = Field(..., description="Northing (m) on the local grid, not latitude")
    source_epsg: int = Field(..., description="EPSG code of the local grid, e.g. 26392 (Nigeria Mid Belt/Minna) or a UTM zone")
    start_beacon: Optional[str] = Field(None, description="Beacon/pillar number of the start point")
    legs: List[Leg]
    closure_tolerance_m: float = 0.5


class DirectRequest(BaseModel):
    points: List[List[float]] = Field(..., description="[[lon, lat], ...] boundary points in order")
    source_epsg: Optional[int] = Field(None, description="EPSG code of input coords, if not already WGS84 (4326)")
    beacons: Optional[List[str]] = Field(None, description="Beacon/pillar numbers, one per point, in order")


class PlotResult(BaseModel):
    valid: bool
    reason: Optional[str] = None
    closure_error_m: Optional[float] = None
    area_sqm: Optional[float] = None
    geojson: Optional[dict] = None
    overlaps_existing: Optional[bool] = None
    beacons: Optional[List[str]] = None


# ---------- Pipeline steps ----------

def cogo_to_points(start_easting, start_northing, legs):
    """Walk bearing/distance legs on the local plane grid (surveying convention:
    bearing measured clockwise from Grid North). This is plane trigonometry,
    not geodesic math — traverses are computed in the surveyor's local grid,
    not on the sphere."""
    points = [(start_easting, start_northing)]
    easting, northing = start_easting, start_northing
    for leg in legs:
        rad = math.radians(leg.bearing_deg)
        easting += leg.distance_m * math.sin(rad)
        northing += leg.distance_m * math.cos(rad)
        points.append((easting, northing))
    return points


def transform_points(points, source_epsg):
    """Reproject to WGS84 if the source survey used a local grid."""
    if not source_epsg or source_epsg == 4326:
        return points
    transformer = Transformer.from_crs(f"EPSG:{source_epsg}", "EPSG:4326", always_xy=True)
    return [transformer.transform(x, y) for x, y in points]


def build_polygon(points):
    """Close the ring if needed, build the shapely polygon."""
    if points[0] != points[-1]:
        points = points + [points[0]]
    return Polygon(points)


def geodesic_area(polygon: Polygon):
    lons, lats = polygon.exterior.coords.xy
    area, _ = geod.polygon_area_perimeter(lons, lats)
    return abs(area)


async def check_overlap(polygon: Polygon) -> bool:
    """Check the candidate polygon against every parcel already in the fabric."""
    async with app.state.pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM parcels WHERE ST_Intersects(geom, ST_GeomFromText($1, 4326)));",
            polygon.wkt,
        )


async def next_pin() -> str:
    async with app.state.pool.acquire() as conn:
        n = await conn.fetchval("SELECT COUNT(*) FROM parcels;")
    return f"PIN-{n + 1:06d}"


async def run_pipeline(points, closure_error_m: Optional[float] = None, beacons: Optional[List[str]] = None) -> PlotResult:
    """Shared steps: build polygon -> validate -> check overlap -> report (no save)."""
    polygon = build_polygon(points)

    if not polygon.is_valid:
        return PlotResult(valid=False, reason=explain_validity(polygon), closure_error_m=closure_error_m, beacons=beacons)

    overlaps = await check_overlap(polygon)
    area = geodesic_area(polygon)

    return PlotResult(
        valid=not overlaps,
        reason="Overlaps an existing parcel in the fabric" if overlaps else None,
        closure_error_m=closure_error_m,
        area_sqm=round(area, 2),
        geojson=mapping(polygon),
        overlaps_existing=overlaps,
        beacons=beacons,
    )


# ---------- Endpoints ----------

@app.post("/plot/cogo", response_model=PlotResult)
async def plot_cogo(req: CogoRequest):
    """Test COGO traverse -> polygon, without saving. Checks closure on the local grid first."""
    raw_points = cogo_to_points(req.start_easting, req.start_northing, req.legs)

    closure_error = math.hypot(
        raw_points[-1][0] - raw_points[0][0], raw_points[-1][1] - raw_points[0][1]
    )

    beacons = [req.start_beacon] + [leg.beacon for leg in req.legs]

    if closure_error > req.closure_tolerance_m:
        return PlotResult(
            valid=False,
            reason=f"Traverse does not close: {closure_error:.2f}m error (tolerance {req.closure_tolerance_m}m)",
            closure_error_m=closure_error,
            beacons=beacons,
        )

    wgs84_points = transform_points(raw_points, req.source_epsg)
    return await run_pipeline(wgs84_points, closure_error_m=closure_error, beacons=beacons)


@app.post("/plot/direct", response_model=PlotResult)
async def plot_direct(req: DirectRequest):
    """Test direct GPS/import points -> polygon, without saving."""
    points = [(p[0], p[1]) for p in req.points]
    points = transform_points(points, req.source_epsg)
    return await run_pipeline(points, beacons=req.beacons)


@app.post("/parcels")
async def save_parcel(req: DirectRequest):
    """Validate then persist to the fabric with a PIN."""
    points = [(p[0], p[1]) for p in req.points]
    points = transform_points(points, req.source_epsg)
    polygon = build_polygon(points)

    if not polygon.is_valid:
        raise HTTPException(400, explain_validity(polygon))
    if await check_overlap(polygon):
        raise HTTPException(409, "Overlaps an existing parcel — rejected")

    pin = await next_pin()
    area = geodesic_area(polygon)

    async with app.state.pool.acquire() as conn:
        parcel_id = await conn.fetchval(
            "INSERT INTO parcels (pin, area_sqm, geom) VALUES ($1, $2, ST_GeomFromText($3, 4326)) RETURNING id;",
            pin, area, polygon.wkt,
        )

    return {"id": parcel_id, "pin": pin, "area_sqm": round(area, 2), "geojson": mapping(polygon)}


@app.get("/parcels")
async def list_parcels():
    """Serve the whole fabric as GeoJSON — paste into geojson.io to see it plotted."""
    async with app.state.pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, pin, area_sqm, ST_AsGeoJSON(geom) as geometry FROM parcels ORDER BY id;")

    features = [
        {
            "type": "Feature",
            "properties": {"id": r["id"], "pin": r["pin"], "area_sqm": r["area_sqm"]},
            "geometry": json.loads(r["geometry"]),
        }
        for r in rows
    ]
    return {"type": "FeatureCollection", "features": features}


@app.delete("/parcels")
async def reset_parcels():
    """Wipe the test fabric so you can re-run scenarios cleanly."""
    async with app.state.pool.acquire() as conn:
        await conn.execute("TRUNCATE parcels RESTART IDENTITY;")
    return {"status": "cleared"}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


TEST_UI = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>GeoEstate — Parcel Plotting</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {
    --navy: #0f1b3d;
    --navy-light: #16234f;
    --accent: #2563eb;
    --accent-dark: #1d4ed8;
    --accent-soft: #eef2ff;
    --green: #16a34a;
    --red: #dc2626;
    --amber: #d97706;
    --border: #e2e5ec;
    --text: #1e2433;
    --text-dim: #6b7280;
    --bg: #f6f7fb;
  }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: 'Inter', system-ui, sans-serif; display: flex; height: 100vh; color: var(--text); background: var(--bg); }
  #panel { width: 440px; min-width: 440px; overflow-y: auto; box-sizing: border-box; background: #fff; border-right: 1px solid var(--border); display: flex; flex-direction: column; }
  #map { flex: 1; }

  #brand { background: linear-gradient(135deg, var(--navy) 0%, var(--navy-light) 100%); color: #fff; padding: 18px 20px; }
  #brand .title { font-size: 17px; font-weight: 700; letter-spacing: 0.2px; }
  #brand .subtitle { font-size: 12px; color: #b8c0dd; margin-top: 2px; }

  .section { padding: 18px 20px; border-bottom: 1px solid var(--border); }
  .section-title { font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; color: var(--accent-dark); margin: 0 0 12px; display: flex; align-items: center; gap: 6px; }
  .section-title .num { display: inline-flex; align-items: center; justify-content: center; width: 18px; height: 18px; border-radius: 50%; background: var(--accent); color: #fff; font-size: 11px; }

  label { display: block; font-size: 12px; font-weight: 500; color: var(--text-dim); margin-top: 10px; margin-bottom: 4px; }
  label:first-child { margin-top: 0; }
  input, select, textarea {
    width: 100%; padding: 8px 10px; font-family: 'Inter', sans-serif; font-size: 13px;
    border: 1px solid var(--border); border-radius: 7px; background: #fff; color: var(--text);
    transition: border-color .15s, box-shadow .15s;
  }
  input:focus, select:focus, textarea:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
  textarea { font-family: 'JetBrains Mono', monospace; height: 64px; resize: vertical; }
  .row { display: flex; gap: 8px; }
  .row > div { flex: 1; }

  button { font-family: inherit; cursor: pointer; border: none; border-radius: 7px; font-weight: 600; font-size: 13px; padding: 9px 14px; transition: background .15s, transform .05s; }
  button:active { transform: translateY(1px); }
  .btn-primary { background: var(--accent); color: #fff; }
  .btn-primary:hover { background: var(--accent-dark); }
  .btn-secondary { background: var(--accent-soft); color: var(--accent-dark); }
  .btn-secondary:hover { background: #dfe7fd; }
  .btn-ghost { background: #fff; color: var(--text-dim); border: 1px solid var(--border); }
  .btn-ghost:hover { background: #f3f4f6; }
  .btn-danger { background: #fef2f2; color: var(--red); border: 1px solid #fecaca; }
  .btn-danger:hover { background: #fee2e2; }
  .btn-block { width: 100%; }
  .btn-row { display: flex; gap: 8px; margin-top: 12px; }
  .btn-row button { flex: 1; }
  button:disabled { opacity: .45; cursor: not-allowed; }

  .mode-toggle { display: flex; gap: 6px; background: #f1f2f6; padding: 4px; border-radius: 9px; }
  .mode-toggle button { flex: 1; background: transparent; color: var(--text-dim); padding: 7px 10px; border-radius: 6px; }
  .mode-toggle button.active { background: #fff; color: var(--accent-dark); box-shadow: 0 1px 3px rgba(0,0,0,.12); }
  #directFields, #cogoFields { display: none; margin-top: 14px; }

  #legsList { margin-top: 12px; display: flex; flex-direction: column; gap: 8px; }
  .leg-card { border: 1px solid var(--border); border-radius: 9px; padding: 10px; background: #fafbfe; position: relative; }
  .leg-card.closing { background: #f0fdf4; border-color: #bbf7d0; }
  .leg-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
  .leg-head .leg-label { font-size: 11px; font-weight: 700; color: var(--text-dim); text-transform: uppercase; letter-spacing: .04em; }
  .leg-head .leg-badge { font-size: 10px; font-weight: 700; background: var(--green); color: #fff; padding: 2px 6px; border-radius: 10px; }
  .leg-remove { background: none; border: none; color: var(--red); font-size: 15px; padding: 0 4px; cursor: pointer; line-height: 1; }
  .dms-row { display: flex; gap: 6px; align-items: flex-end; }
  .dms-box { flex: 1; }
  .dms-box label { margin: 0 0 3px; font-size: 10px; }
  .dms-box input { padding: 6px 7px; font-family: 'JetBrains Mono', monospace; font-size: 12px; text-align: center; }
  .dms-sep { padding-bottom: 8px; color: var(--text-dim); font-weight: 700; font-size: 12px; }
  .beacon-input { margin-top: 6px; }
  .beacon-input input { font-family: 'JetBrains Mono', monospace; }

  #result { white-space: pre-wrap; background: #0f1b3d; color: #d7e0ff; padding: 10px; font-family: 'JetBrains Mono', monospace; font-size: 11px; margin-top: 10px; max-height: 220px; overflow-y: auto; border-radius: 8px; line-height: 1.5; }
  #resultSummary { margin-top: 10px; padding: 10px 12px; border-radius: 8px; font-size: 12.5px; font-weight: 600; display: none; }
  #resultSummary.ok { display: block; background: #f0fdf4; color: var(--green); border: 1px solid #bbf7d0; }
  #resultSummary.bad { display: block; background: #fef2f2; color: var(--red); border: 1px solid #fecaca; }
  .hint { font-size: 11px; color: var(--text-dim); margin-top: 6px; line-height: 1.4; }
</style>
</head>
<body>

<div id="panel">
  <div id="brand">
    <div class="title">GeoEstate · Parcel Plotting</div>
    <div class="subtitle">COGO traverse &amp; direct-entry boundary testing</div>
  </div>

  <div class="section">
    <div class="section-title"><span class="num">1</span>Build a boundary</div>
    <div class="mode-toggle">
      <button id="btnModeDirect" class="active" onclick="setMode('direct')">Direct points</button>
      <button id="btnModeCogo" onclick="setMode('cogo')">COGO traverse</button>
    </div>

    <div id="directFields">
      <label>Points — [[lon,lat], ...]</label>
      <textarea id="directPoints">[[7.4900,9.0500],[7.4910,9.0500],[7.4910,9.0510],[7.4900,9.0510]]</textarea>
      <label>Grid / coordinate system</label>
      <select id="sourceEpsg"></select>
    </div>

    <div id="cogoFields">
      <div class="row">
        <div>
          <label>Start Easting (m)</label>
          <input id="startEasting" value="350000">
        </div>
        <div>
          <label>Start Northing (m)</label>
          <input id="startNorthing" value="1000000">
        </div>
      </div>
      <label>Start beacon number</label>
      <input id="startBeacon" placeholder="e.g. BN01" value="BN01">
      <label>Grid / coordinate system</label>
      <select id="cogoEpsg"></select>
      <label>Closure tolerance (m)</label>
      <input id="closureTol" value="0.5">

      <label style="margin-top:14px;">Traverse legs</label>
      <div class="hint">Enter each leg as bearing (D° M′ S″ clockwise from North) and distance, with the beacon number at the far end of the leg. Once the last real leg is in, press <b>Close traverse</b> — since the first bearing/distance should close the polygon, it computes the final closing leg for you.</div>
      <div id="legsList"></div>

      <div class="btn-row">
        <button class="btn-secondary" onclick="addLeg()">+ Add leg</button>
        <button class="btn-secondary" onclick="closeTraverse()">⤾ Close traverse</button>
      </div>
    </div>

    <div class="btn-row">
      <button class="btn-primary btn-block" onclick="testPlot()">Test (validate only)</button>
    </div>
    <button id="saveBtn" class="btn-ghost btn-block" style="margin-top:8px;" onclick="savePlot()" disabled>Save to fabric</button>

    <div id="resultSummary"></div>
    <div id="result">Run a test to see the pipeline output here.</div>
  </div>

  <div class="section" style="border-bottom:none;">
    <div class="section-title"><span class="num">2</span>Parcel fabric</div>
    <div class="btn-row" style="margin-top:0;">
      <button class="btn-secondary" onclick="loadFabric()">Refresh map</button>
      <button class="btn-danger" onclick="resetFabric()">Reset fabric</button>
    </div>
  </div>
</div>

<div id="map"></div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
// ---------- Nigeria grid systems ----------
const NIGERIA_GRIDS = [
  { group: "Minna Datum — Belts (cadastral)", items: [
    { epsg: 26391, label: "Nigeria West Belt (Minna)" },
    { epsg: 26392, label: "Nigeria Mid Belt (Minna)" },
    { epsg: 26393, label: "Nigeria East Belt (Minna)" },
  ]},
  { group: "Minna Datum — UTM", items: [
    { epsg: 26331, label: "Minna / UTM Zone 31N" },
    { epsg: 26332, label: "Minna / UTM Zone 32N" },
  ]},
  { group: "WGS 84 — UTM", items: [
    { epsg: 32631, label: "WGS 84 / UTM Zone 31N" },
    { epsg: 32632, label: "WGS 84 / UTM Zone 32N" },
    { epsg: 32633, label: "WGS 84 / UTM Zone 33N" },
  ]},
  { group: "Geographic", items: [
    { epsg: 4326, label: "WGS 84 (lon/lat — no conversion)" },
  ]},
];

function populateGridSelect(select, defaultEpsg) {
  NIGERIA_GRIDS.forEach(g => {
    const og = document.createElement('optgroup');
    og.label = g.group;
    g.items.forEach(it => {
      const opt = document.createElement('option');
      opt.value = it.epsg;
      opt.textContent = `${it.label} — EPSG:${it.epsg}`;
      if (it.epsg === defaultEpsg) opt.selected = true;
      og.appendChild(opt);
    });
    select.appendChild(og);
  });
}
populateGridSelect(document.getElementById('sourceEpsg'), 4326);
populateGridSelect(document.getElementById('cogoEpsg'), 26392);

// ---------- Leg builder ----------
let legIdCounter = 0;
let closingLegId = null;

function addLeg(prefill) {
  const id = ++legIdCounter;
  const wrap = document.createElement('div');
  wrap.className = 'leg-card';
  wrap.id = `leg-${id}`;
  wrap.dataset.legId = id;
  wrap.innerHTML = `
    <div class="leg-head">
      <span class="leg-label">Leg</span>
      <button class="leg-remove" onclick="removeLeg(${id})" title="Remove leg">✕</button>
    </div>
    <div class="dms-row">
      <div class="dms-box"><label>Deg</label><input type="number" class="leg-d" min="0" max="360" value="${prefill ? prefill.d : ''}"></div>
      <div class="dms-sep">°</div>
      <div class="dms-box"><label>Min</label><input type="number" class="leg-m" min="0" max="59" value="${prefill ? prefill.m : ''}"></div>
      <div class="dms-sep">′</div>
      <div class="dms-box"><label>Sec</label><input type="number" class="leg-s" min="0" max="59.99" step="0.01" value="${prefill ? prefill.s : ''}"></div>
      <div class="dms-sep">″</div>
      <div class="dms-box" style="flex:1.3;"><label>Distance (m)</label><input type="number" class="leg-dist" min="0" step="0.01" value="${prefill ? prefill.dist : ''}"></div>
    </div>
    <div class="beacon-input">
      <label>Beacon number (end of this leg)</label>
      <input type="text" class="leg-beacon" placeholder="e.g. BN02" value="${prefill ? prefill.beacon || '' : ''}">
    </div>
  `;
  document.getElementById('legsList').appendChild(wrap);
  renumberLegs();
  return id;
}

function removeLeg(id) {
  const el = document.getElementById(`leg-${id}`);
  if (el) el.remove();
  if (id === closingLegId) closingLegId = null;
  renumberLegs();
}

function renumberLegs() {
  const cards = document.querySelectorAll('.leg-card');
  cards.forEach((c, i) => {
    c.querySelector('.leg-label').textContent = `Leg ${i + 1}`;
  });
}

function readLegs() {
  const cards = document.querySelectorAll('.leg-card');
  return Array.from(cards).map(c => {
    const d = parseFloat(c.querySelector('.leg-d').value) || 0;
    const m = parseFloat(c.querySelector('.leg-m').value) || 0;
    const s = parseFloat(c.querySelector('.leg-s').value) || 0;
    const dist = parseFloat(c.querySelector('.leg-dist').value) || 0;
    const beacon = c.querySelector('.leg-beacon').value.trim() || null;
    return { bearing_deg: d + m / 60 + s / 3600, distance_m: dist, beacon };
  });
}

function decToDMS(dec) {
  dec = ((dec % 360) + 360) % 360;
  const d = Math.floor(dec);
  const remMin = (dec - d) * 60;
  const m = Math.floor(remMin);
  const s = Math.round((remMin - m) * 60 * 100) / 100;
  return { d, m, s };
}

function closeTraverse() {
  // Remove any previously computed closing leg so we recompute cleanly
  if (closingLegId) { removeLeg(closingLegId); }

  const startE = parseFloat(document.getElementById('startEasting').value);
  const startN = parseFloat(document.getElementById('startNorthing').value);
  if (isNaN(startE) || isNaN(startN)) { alert('Enter a valid start easting/northing first.'); return; }

  const legs = readLegs();
  if (!legs.length) { alert('Add at least one leg before closing the traverse.'); return; }

  let e = startE, n = startN;
  for (const leg of legs) {
    const rad = leg.bearing_deg * Math.PI / 180;
    e += leg.distance_m * Math.sin(rad);
    n += leg.distance_m * Math.cos(rad);
  }

  const dE = startE - e, dN = startN - n;
  const distance = Math.hypot(dE, dN);
  let bearing = Math.atan2(dE, dN) * 180 / Math.PI;
  if (bearing < 0) bearing += 360;
  const dms = decToDMS(bearing);

  const startBeacon = document.getElementById('startBeacon').value.trim() || 'BN01';
  const id = addLeg({ d: dms.d, m: dms.m, s: dms.s, dist: Math.round(distance * 100) / 100, beacon: startBeacon });
  closingLegId = id;

  const card = document.getElementById(`leg-${id}`);
  card.classList.add('closing');
  card.querySelector('.leg-head').insertAdjacentHTML('beforeend', '<span class="leg-badge">Auto-closes</span>');
}

// Start with two sample legs so the form isn't empty
addLeg({ d: 90, m: 0, s: 0, dist: 50, beacon: 'BN02' });
addLeg({ d: 180, m: 0, s: 0, dist: 50, beacon: 'BN03' });
addLeg({ d: 270, m: 0, s: 0, dist: 50, beacon: 'BN04' });

// ---------- Mode toggle ----------
let mode = 'direct';
let lastGeojson = null;
let lastBeacons = null;

const map = L.map('map').setView([9.05, 7.49], 15);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '&copy; OpenStreetMap contributors'
}).addTo(map);

let fabricLayer = L.geoJSON(null, {
  style: { color: '#2563eb', weight: 2, fillOpacity: 0.12 },
  onEachFeature: (f, layer) => layer.bindPopup(`<b>PIN:</b> ${f.properties.pin}<br><b>Area:</b> ${f.properties.area_sqm} m²`)
}).addTo(map);

let previewLayer = L.geoJSON(null, { style: { color: '#dc2626', weight: 2, dashArray: '5,4' } }).addTo(map);
let beaconMarkers = L.layerGroup().addTo(map);

function setMode(m) {
  mode = m;
  document.getElementById('directFields').style.display = m === 'direct' ? 'block' : 'none';
  document.getElementById('cogoFields').style.display = m === 'cogo' ? 'block' : 'none';
  document.getElementById('btnModeDirect').classList.toggle('active', m === 'direct');
  document.getElementById('btnModeCogo').classList.toggle('active', m === 'cogo');
}
setMode('direct');

function showResult(obj, ok, summary) {
  const el = document.getElementById('result');
  el.textContent = JSON.stringify(obj, null, 2);
  const sum = document.getElementById('resultSummary');
  if (summary) {
    sum.textContent = summary;
    sum.className = ok ? 'ok' : 'bad';
  } else {
    sum.className = '';
  }
}

function plotBeacons(beacons, ring) {
  beaconMarkers.clearLayers();
  if (!beacons || !ring) return;
  ring.slice(0, -1).forEach((pt, i) => {
    const label = beacons[i] || `P${i + 1}`;
    L.circleMarker([pt[1], pt[0]], { radius: 5, color: '#0f1b3d', weight: 2, fillColor: '#fff', fillOpacity: 1 })
      .bindTooltip(label, { permanent: true, direction: 'top', className: 'beacon-tooltip', offset: [0, -6] })
      .addTo(beaconMarkers);
  });
}

async function testPlot() {
  document.getElementById('saveBtn').disabled = true;
  lastGeojson = null;
  lastBeacons = null;
  previewLayer.clearLayers();
  beaconMarkers.clearLayers();

  let url, body;
  if (mode === 'direct') {
    let points;
    try { points = JSON.parse(document.getElementById('directPoints').value); }
    catch (e) { showResult({ error: 'Points must be valid JSON: ' + e.message }, false, '✗ Invalid points JSON'); return; }
    const epsg = parseInt(document.getElementById('sourceEpsg').value);
    body = { points, source_epsg: epsg === 4326 ? null : epsg };
    url = '/plot/direct';
  } else {
    const startEasting = parseFloat(document.getElementById('startEasting').value);
    const startNorthing = parseFloat(document.getElementById('startNorthing').value);
    const startBeacon = document.getElementById('startBeacon').value.trim() || null;
    const epsg = parseInt(document.getElementById('cogoEpsg').value);
    const legs = readLegs();
    if (!legs.length) { showResult({ error: 'Add at least one leg.' }, false, '✗ No legs entered'); return; }
    body = {
      start_easting: startEasting, start_northing: startNorthing, source_epsg: epsg,
      start_beacon: startBeacon, legs,
      closure_tolerance_m: parseFloat(document.getElementById('closureTol').value) || 0.5
    };
    url = '/plot/cogo';
  }

  const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const data = await res.json();

  let summary;
  if (data.valid) {
    summary = `✓ Valid — area ${data.area_sqm ? data.area_sqm.toLocaleString() : '?'} m²` +
      (data.closure_error_m !== null && data.closure_error_m !== undefined ? ` — closure error ${data.closure_error_m.toFixed(3)} m` : '');
  } else {
    summary = `✗ ${data.reason || 'Invalid boundary'}`;
  }
  showResult(data, data.valid === true, summary);

  if (data.geojson) {
    lastGeojson = data.geojson;
    lastBeacons = data.beacons || null;
    previewLayer.addData(data.geojson);
    plotBeacons(lastBeacons, data.geojson.coordinates[0]);
    map.fitBounds(previewLayer.getBounds(), { maxZoom: 18 });
    document.getElementById('saveBtn').disabled = !data.valid;
  }
}

async function savePlot() {
  if (!lastGeojson) return;
  // geojson.coordinates[0] is the exterior ring, already closed — drop the repeated last point.
  const ring = lastGeojson.coordinates[0];
  const points = ring.slice(0, -1);

  const res = await fetch('/parcels', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ points, source_epsg: null, beacons: lastBeacons })
  });
  const data = await res.json();
  if (res.ok) {
    showResult(data, true, `✓ Saved as ${data.pin}`);
    document.getElementById('saveBtn').disabled = true;
    previewLayer.clearLayers();
    beaconMarkers.clearLayers();
    loadFabric();
  } else {
    showResult(data, false, `✗ ${data.detail || 'Save failed'}`);
  }
}

async function loadFabric() {
  const res = await fetch('/parcels');
  const data = await res.json();
  fabricLayer.clearLayers();
  fabricLayer.addData(data);
  if (data.features.length) map.fitBounds(fabricLayer.getBounds(), { maxZoom: 18 });
}

async function resetFabric() {
  if (!confirm('Delete all saved parcels?')) return;
  await fetch('/parcels', { method: 'DELETE' });
  fabricLayer.clearLayers();
  showResult({ status: 'fabric cleared' }, true, '✓ Fabric cleared');
}

loadFabric();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def test_ui():
    return TEST_UI
