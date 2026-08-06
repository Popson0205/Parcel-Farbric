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


class CogoRequest(BaseModel):
    start_easting: float = Field(..., description="Easting (m) on the local grid, not longitude")
    start_northing: float = Field(..., description="Northing (m) on the local grid, not latitude")
    source_epsg: int = Field(..., description="EPSG code of the local grid, e.g. 26332 (Nigeria Mid Belt/Minna) or a UTM zone")
    legs: List[Leg]
    closure_tolerance_m: float = 0.5


class DirectRequest(BaseModel):
    points: List[List[float]] = Field(..., description="[[lon, lat], ...] boundary points in order")
    source_epsg: Optional[int] = Field(None, description="EPSG code of input coords, if not already WGS84 (4326)")


class PlotResult(BaseModel):
    valid: bool
    reason: Optional[str] = None
    closure_error_m: Optional[float] = None
    area_sqm: Optional[float] = None
    geojson: Optional[dict] = None
    overlaps_existing: Optional[bool] = None


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


async def run_pipeline(points, closure_error_m: Optional[float] = None) -> PlotResult:
    """Shared steps: build polygon -> validate -> check overlap -> report (no save)."""
    polygon = build_polygon(points)

    if not polygon.is_valid:
        return PlotResult(valid=False, reason=explain_validity(polygon), closure_error_m=closure_error_m)

    overlaps = await check_overlap(polygon)
    area = geodesic_area(polygon)

    return PlotResult(
        valid=not overlaps,
        reason="Overlaps an existing parcel in the fabric" if overlaps else None,
        closure_error_m=closure_error_m,
        area_sqm=round(area, 2),
        geojson=mapping(polygon),
        overlaps_existing=overlaps,
    )


# ---------- Endpoints ----------

@app.post("/plot/cogo", response_model=PlotResult)
async def plot_cogo(req: CogoRequest):
    """Test COGO traverse -> polygon, without saving. Checks closure on the local grid first."""
    raw_points = cogo_to_points(req.start_easting, req.start_northing, req.legs)

    closure_error = math.hypot(
        raw_points[-1][0] - raw_points[0][0], raw_points[-1][1] - raw_points[0][1]
    )

    if closure_error > req.closure_tolerance_m:
        return PlotResult(
            valid=False,
            reason=f"Traverse does not close: {closure_error:.2f}m error (tolerance {req.closure_tolerance_m}m)",
            closure_error_m=closure_error,
        )

    wgs84_points = transform_points(raw_points, req.source_epsg)
    return await run_pipeline(wgs84_points, closure_error_m=closure_error)


@app.post("/plot/direct", response_model=PlotResult)
async def plot_direct(req: DirectRequest):
    """Test direct GPS/import points -> polygon, without saving."""
    points = [(p[0], p[1]) for p in req.points]
    points = transform_points(points, req.source_epsg)
    return await run_pipeline(points)


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
<title>Parcel plotting — test UI</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>
  body { margin: 0; font-family: system-ui, sans-serif; display: flex; height: 100vh; }
  #panel { width: 400px; padding: 16px; overflow-y: auto; box-sizing: border-box; border-right: 1px solid #ddd; }
  #map { flex: 1; }
  h2 { font-size: 16px; margin: 20px 0 8px; }
  h2:first-child { margin-top: 0; }
  label { display: block; font-size: 12px; color: #555; margin-top: 8px; }
  input, textarea, select { width: 100%; box-sizing: border-box; padding: 6px; font-family: monospace; font-size: 12px; margin-top: 2px; }
  textarea { height: 70px; }
  button { margin-top: 10px; margin-right: 6px; padding: 8px 12px; cursor: pointer; }
  #result { white-space: pre-wrap; background: #f5f5f5; padding: 8px; font-size: 11px; margin-top: 10px; max-height: 220px; overflow-y: auto; border-radius: 4px; }
  .mode-toggle { display: flex; gap: 8px; margin-top: 8px; }
  .mode-toggle button { flex: 1; background: #eee; border: 1px solid #ccc; }
  .mode-toggle button.active { background: #2563eb; color: white; border-color: #2563eb; }
  #directFields, #cogoFields { display: none; }
  .status-ok { color: #16a34a; }
  .status-bad { color: #dc2626; }
</style>
</head>
<body>

<div id="panel">
  <h2>1. Build a boundary</h2>
  <div class="mode-toggle">
    <button id="btnModeDirect" class="active" onclick="setMode('direct')">Direct points</button>
    <button id="btnModeCogo" onclick="setMode('cogo')">COGO traverse</button>
  </div>

  <div id="directFields" style="display:block">
    <label>Points — [[lon,lat], ...]</label>
    <textarea id="directPoints">[[7.4900,9.0500],[7.4910,9.0500],[7.4910,9.0510],[7.4900,9.0510]]</textarea>
    <label>Source EPSG (blank = already WGS84)</label>
    <input id="sourceEpsg" placeholder="e.g. 26332">
  </div>

  <div id="cogoFields">
    <label>Start Easting, Northing (m — local grid, not lon/lat)</label>
    <input id="cogoStart" value="350000, 1000000">
    <label>Grid EPSG code (e.g. 26332 = Nigeria Mid Belt/Minna)</label>
    <input id="cogoEpsg" value="26332">
    <label>Legs — one per line: bearing_deg, distance_m</label>
    <textarea id="cogoLegs">90, 50
180, 50
270, 50
0, 50</textarea>
    <label>Closure tolerance (m)</label>
    <input id="closureTol" value="0.5">
  </div>

  <button onclick="testPlot()">Test (validate only)</button>
  <button id="saveBtn" onclick="savePlot()" disabled>Save to fabric</button>

  <div id="result">Run a test to see the pipeline output here.</div>

  <h2>2. Fabric</h2>
  <button onclick="loadFabric()">Refresh map</button>
  <button onclick="resetFabric()">Reset fabric (delete all)</button>
</div>

<div id="map"></div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
let mode = 'direct';
let lastGeojson = null;

const map = L.map('map').setView([9.05, 7.49], 15);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '&copy; OpenStreetMap contributors'
}).addTo(map);

let fabricLayer = L.geoJSON(null, {
  style: { color: '#2563eb', weight: 2, fillOpacity: 0.15 },
  onEachFeature: (f, layer) => layer.bindPopup(`PIN: ${f.properties.pin}<br>Area: ${f.properties.area_sqm} m²`)
}).addTo(map);

let previewLayer = L.geoJSON(null, { style: { color: '#dc2626', weight: 2, dashArray: '4' } }).addTo(map);

function setMode(m) {
  mode = m;
  document.getElementById('directFields').style.display = m === 'direct' ? 'block' : 'none';
  document.getElementById('cogoFields').style.display = m === 'cogo' ? 'block' : 'none';
  document.getElementById('btnModeDirect').classList.toggle('active', m === 'direct');
  document.getElementById('btnModeCogo').classList.toggle('active', m === 'cogo');
}

function showResult(obj, ok) {
  const el = document.getElementById('result');
  el.textContent = JSON.stringify(obj, null, 2);
  el.className = ok ? 'status-ok' : 'status-bad';
}

async function testPlot() {
  document.getElementById('saveBtn').disabled = true;
  lastGeojson = null;
  previewLayer.clearLayers();

  let url, body;
  if (mode === 'direct') {
    let points;
    try { points = JSON.parse(document.getElementById('directPoints').value); }
    catch (e) { showResult({ error: 'Points must be valid JSON: ' + e.message }, false); return; }
    const epsg = document.getElementById('sourceEpsg').value.trim();
    body = { points, source_epsg: epsg ? parseInt(epsg) : null };
    url = '/plot/direct';
  } else {
    const [easting, northing] = document.getElementById('cogoStart').value.split(',').map(s => parseFloat(s.trim()));
    const epsg = parseInt(document.getElementById('cogoEpsg').value.trim());
    const legs = document.getElementById('cogoLegs').value.trim().split('\\n').filter(Boolean).map(line => {
      const [b, d] = line.split(',').map(s => parseFloat(s.trim()));
      return { bearing_deg: b, distance_m: d };
    });
    body = {
      start_easting: easting, start_northing: northing, source_epsg: epsg, legs,
      closure_tolerance_m: parseFloat(document.getElementById('closureTol').value) || 0.5
    };
    url = '/plot/cogo';
  }

  const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const data = await res.json();
  showResult(data, data.valid === true);

  if (data.geojson) {
    lastGeojson = data.geojson;
    previewLayer.addData(data.geojson);
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
    body: JSON.stringify({ points, source_epsg: null })
  });
  const data = await res.json();
  if (res.ok) {
    showResult(data, true);
    document.getElementById('saveBtn').disabled = true;
    previewLayer.clearLayers();
    loadFabric();
  } else {
    showResult(data, false);
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
  showResult({ status: 'fabric cleared' }, true);
}

loadFabric();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def test_ui():
    return TEST_UI
