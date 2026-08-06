"""
Parcel Plotting Pipeline — test service (v2: Nigerian cadastral plan mapping)

Same pipeline as before:

  COGO traverse / direct GPS points
      -> coordinate transform (to WGS84)
      -> build closed polygon
      -> topology validation (self-intersection + overlap with fabric)
      -> store in PostGIS with a PIN
      -> serve back as GeoJSON

...now extended to capture what a real Nigerian survey plan (see
Modeseg Survey & Properties / Osun State examples) actually carries, so a
plan can be transcribed into the fabric without losing information:

  - Bearings as DMS (deg/min/sec), not just decimal degrees
  - Beacon numbers (e.g. "SC/OS BB8215JP") attached to each corner
  - Minna-datum / UTM-zone input coordinates, not just WGS84
  - "(Cal.)" bearings — computed/back-bearing checks vs field-measured legs
  - A GNSS control tie (bearing/distance from a known reference station,
    e.g. an OS-APPSN pillar, to the first beacon) for provenance
  - Plan No. in the STATE/JOB/YEAR/SERIAL format offices actually use
  - Title-block metadata: owner(s), locality, LGA, state, surveyor, firm
  - Cross-check of the plan's stated area against the computed area

Deploy on Railway. Uses asyncpg (no libpq system dependency) against
Supabase or any Postgres with PostGIS.
"""

import json
import os
from datetime import date
from typing import List, Optional

import asyncpg
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, model_validator
from shapely.geometry import Point, Polygon, mapping
from shapely.validation import explain_validity
from pyproj import Transformer, Geod

app = FastAPI(title="GeoEstate Parcel Plotting — Test Service")

DATABASE_URL = os.environ.get("DATABASE_URL")  # set manually in Railway Variables for Supabase
geod = Geod(ellps="WGS84")

# Nigerian cadastral plans label the grid as just "ZONE 31" / "ZONE 32" /
# "ZONE 33" (or a belt name) — never an EPSG code, and never say which datum.
# Historically that meant the Minna datum, but plans tied to a state GNSS
# CORS network (e.g. this office's "OS-APPSN" reference stations) come out
# in WGS84 instead, because that's what the CORS network broadcasts in.
# Checked against both sample plans here: their beacon coordinates only land
# in Osun State under WGS84/UTM zone 31N (EPSG:32631) — under Minna/UTM 31N
# (EPSG:26391) they land ~800km away near the Cameroon border. So WGS84 UTM
# is listed first as the more likely default for GNSS-observed plans, but
# this MUST be confirmed per plan/surveyor — a wrong guess silently
# mis-plots the parcel by ~100-200m, not an error you'd notice on the map.
NIGERIA_CRS_CHOICES = {
    32631: "WGS84 / UTM zone 31N",
    32632: "WGS84 / UTM zone 32N",
    32633: "WGS84 / UTM zone 33N",
    26331: "Minna / Nigeria West Belt",
    26332: "Minna / Nigeria Mid Belt",
    26333: "Minna / Nigeria East Belt",
    26391: "Minna / UTM zone 31N",
    26392: "Minna / UTM zone 32N",
    26393: "Minna / UTM zone 33N",
    4326: "WGS84 lon/lat (already unprojected)",
}

# Plan No. format: "{STATE_CODE}/{JOB_NO}/{YEAR}/{SERIAL}", e.g. OS/2428/2024/031
STATE_CODE = os.environ.get("PLAN_STATE_CODE", "OS")
JOB_NO = os.environ.get("PLAN_JOB_NO", "0000")

# Surveyed area on the plan vs. our computed geodesic area — flag if they
# disagree by more than this fraction (surveyors round to the plan's scale,
# so some drift is normal; big drift usually means a transcription error).
AREA_CHECK_TOLERANCE_PCT = 1.0


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
                plan_no TEXT,
                owners TEXT[],
                locality TEXT,
                lga TEXT,
                state TEXT,
                surveyor_name TEXT,
                firm_name TEXT,
                area_sqm DOUBLE PRECISION,
                surveyed_area_sqm DOUBLE PRECISION,
                area_diff_pct DOUBLE PRECISION,
                control_station_id TEXT,
                control_tie_discrepancy_m DOUBLE PRECISION,
                geom GEOMETRY(Polygon, 4326) NOT NULL,
                created_at TIMESTAMP DEFAULT now()
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS beacons (
                id SERIAL PRIMARY KEY,
                parcel_id INTEGER REFERENCES parcels(id) ON DELETE CASCADE,
                beacon_id TEXT,
                seq INTEGER NOT NULL,
                geom GEOMETRY(Point, 4326) NOT NULL
            );
            """
        )

        # Additive migration for services deployed before this version.
        for stmt in (
            "ALTER TABLE parcels ADD COLUMN IF NOT EXISTS plan_no TEXT",
            "ALTER TABLE parcels ADD COLUMN IF NOT EXISTS owners TEXT[]",
            "ALTER TABLE parcels ADD COLUMN IF NOT EXISTS locality TEXT",
            "ALTER TABLE parcels ADD COLUMN IF NOT EXISTS lga TEXT",
            "ALTER TABLE parcels ADD COLUMN IF NOT EXISTS state TEXT",
            "ALTER TABLE parcels ADD COLUMN IF NOT EXISTS surveyor_name TEXT",
            "ALTER TABLE parcels ADD COLUMN IF NOT EXISTS firm_name TEXT",
            "ALTER TABLE parcels ADD COLUMN IF NOT EXISTS surveyed_area_sqm DOUBLE PRECISION",
            "ALTER TABLE parcels ADD COLUMN IF NOT EXISTS area_diff_pct DOUBLE PRECISION",
            "ALTER TABLE parcels ADD COLUMN IF NOT EXISTS control_station_id TEXT",
            "ALTER TABLE parcels ADD COLUMN IF NOT EXISTS control_tie_discrepancy_m DOUBLE PRECISION",
        ):
            await conn.execute(stmt)


@app.on_event("shutdown")
async def shutdown_db():
    await app.state.pool.close()


# ---------- Request / response models ----------

class Bearing(BaseModel):
    """DMS bearing as printed on a Nigerian plan, e.g. 310° 30' -> deg=310, min=30."""
    deg: float = Field(..., ge=0, lt=360)
    min: float = Field(0, ge=0, lt=60)
    sec: float = Field(0, ge=0, lt=60)

    def decimal(self) -> float:
        return self.deg + self.min / 60 + self.sec / 3600


class Leg(BaseModel):
    bearing: Optional[Bearing] = Field(None, description="DMS bearing, as printed on the plan")
    bearing_deg: Optional[float] = Field(None, description="Decimal-degree bearing, if you don't have DMS")
    distance_m: float = Field(..., gt=0)
    to_beacon_id: Optional[str] = Field(None, description="Beacon number at the end of this leg, e.g. 'SC/OS BB8216JP'")
    calculated: bool = Field(False, description="True if this bearing is a computed/back-bearing check ('(Cal.)' on the plan) rather than field-measured")

    @model_validator(mode="after")
    def _one_bearing_form(self):
        if self.bearing is None and self.bearing_deg is None:
            raise ValueError("leg needs either 'bearing' (deg/min/sec) or 'bearing_deg'")
        return self

    def bearing_decimal(self) -> float:
        return self.bearing.decimal() if self.bearing is not None else self.bearing_deg


class ControlTie(BaseModel):
    """
    Ties the traverse to a known reference station (e.g. an OS-APPSN GNSS
    pillar), the way the plan's "GNSS OBSERVATION" block does: bearing +
    distance from the station to the first beacon. Used to cross-check the
    supplied start point, not to move it.
    """
    station_id: str
    station_x: float = Field(..., description="Station easting, or lon if source_epsg is 4326/omitted")
    station_y: float = Field(..., description="Station northing, or lat if source_epsg is 4326/omitted")
    source_epsg: Optional[int] = Field(None, description="EPSG of station_x/station_y; omit or 4326 for lon/lat")
    bearing: Bearing
    distance_m: float = Field(..., gt=0)


class CogoRequest(BaseModel):
    start_x: float = Field(..., description="Start easting, or lon if source_epsg is 4326/omitted")
    start_y: float = Field(..., description="Start northing, or lat if source_epsg is 4326/omitted")
    source_epsg: Optional[int] = Field(None, description="EPSG of start_x/y and any control tie station. See /crs-options.")
    start_beacon_id: Optional[str] = None
    legs: List[Leg]
    closure_tolerance_m: float = 0.5
    control_tie: Optional[ControlTie] = None


class DirectRequest(BaseModel):
    points: List[List[float]] = Field(..., description="[[x,y], ...] boundary points in order")
    beacon_ids: Optional[List[Optional[str]]] = Field(None, description="Beacon number per point, same order/length as points")
    source_epsg: Optional[int] = Field(None, description="EPSG code of input coords, if not already WGS84 (4326). See /crs-options.")


class ParcelMeta(BaseModel):
    """Title-block fields from the plan. All optional — fill in what you have."""
    plan_no: Optional[str] = Field(None, description="If omitted, one is generated as STATE_CODE/JOB_NO/YEAR/SERIAL")
    owners: Optional[List[str]] = None
    locality: Optional[str] = None
    lga: Optional[str] = None
    state: Optional[str] = None
    surveyor_name: Optional[str] = None
    firm_name: Optional[str] = None
    surveyed_area_sqm: Optional[float] = Field(None, description="Area as stated/computed on the plan itself, for cross-check")


class SaveParcelRequest(DirectRequest):
    meta: Optional[ParcelMeta] = None
    control_tie: Optional[ControlTie] = None


class ControlTieResult(BaseModel):
    discrepancy_m: float
    computed_start: dict  # GeoJSON Point


class PlotResult(BaseModel):
    valid: bool
    reason: Optional[str] = None
    closure_error_m: Optional[float] = None
    area_sqm: Optional[float] = None
    geojson: Optional[dict] = None
    overlaps_existing: Optional[bool] = None
    beacon_ids: Optional[List[Optional[str]]] = None
    area_diff_pct: Optional[float] = None
    control_tie: Optional[ControlTieResult] = None


# ---------- Pipeline steps ----------

def cogo_to_points(start_lon, start_lat, legs: List[Leg]):
    """
    Walk bearing/distance legs geodesically from a start point.
    Returns (ring_points, ring_beacon_ids) where the ring is closed by
    beacon 0 again (i.e. the final leg's endpoint, which should be ~= the
    start within closure_tolerance_m, is dropped — closure is checked
    separately, and re-using it here would leave a near-duplicate vertex).
    """
    points = [(start_lon, start_lat)]
    lon, lat = start_lon, start_lat
    for leg in legs:
        lon, lat, _ = geod.fwd(lon, lat, leg.bearing_decimal(), leg.distance_m)
        points.append((lon, lat))
    return points  # includes the redundant closing point; caller trims it


def transform_points(points, source_epsg):
    """Reproject to WGS84 if the source survey used a local/state grid."""
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


async def next_plan_no() -> str:
    """STATE_CODE/JOB_NO/YEAR/SERIAL, e.g. OS/2428/2024/031 — serial resets per year."""
    year = date.today().year
    prefix = f"{STATE_CODE}/{JOB_NO}/{year}/"
    async with app.state.pool.acquire() as conn:
        n = await conn.fetchval("SELECT COUNT(*) FROM parcels WHERE plan_no LIKE $1;", prefix + "%")
    return f"{prefix}{n + 1:03d}"


def check_control_tie(control_tie: ControlTie, start_lon: float, start_lat: float) -> ControlTieResult:
    """
    Independently compute the start point from the control station's
    bearing/distance tie, and compare it against the start point actually
    supplied. This is the same cross-check the plan's own GNSS observation
    block exists to support — it doesn't move anything, it just tells you
    whether the traverse you captured actually ties to the reference network.
    """
    station_pt = transform_points([(control_tie.station_x, control_tie.station_y)], control_tie.source_epsg)[0]
    computed_lon, computed_lat, _ = geod.fwd(
        station_pt[0], station_pt[1], control_tie.bearing.decimal(), control_tie.distance_m
    )
    _, _, discrepancy_m = geod.inv(computed_lon, computed_lat, start_lon, start_lat)
    return ControlTieResult(
        discrepancy_m=round(discrepancy_m, 2),
        computed_start=mapping(Point(computed_lon, computed_lat)),
    )


async def run_pipeline(
    ring_points,
    beacon_ids: Optional[List[Optional[str]]] = None,
    closure_error_m: Optional[float] = None,
    surveyed_area_sqm: Optional[float] = None,
    control_tie_result: Optional[ControlTieResult] = None,
) -> PlotResult:
    """Shared steps: build polygon -> validate -> check overlap -> report (no save)."""
    polygon = build_polygon(ring_points)

    if not polygon.is_valid:
        return PlotResult(valid=False, reason=explain_validity(polygon), closure_error_m=closure_error_m, beacon_ids=beacon_ids)

    overlaps = await check_overlap(polygon)
    area = geodesic_area(polygon)

    area_diff_pct = None
    if surveyed_area_sqm:
        area_diff_pct = round(abs(area - surveyed_area_sqm) / surveyed_area_sqm * 100, 2)

    return PlotResult(
        valid=not overlaps,
        reason="Overlaps an existing parcel in the fabric" if overlaps else None,
        closure_error_m=closure_error_m,
        area_sqm=round(area, 2),
        geojson=mapping(polygon),
        overlaps_existing=overlaps,
        beacon_ids=beacon_ids,
        area_diff_pct=area_diff_pct,
        control_tie=control_tie_result,
    )


# ---------- Endpoints ----------

@app.get("/crs-options")
async def crs_options():
    """Coordinate systems the pipeline knows about, keyed by what the plan actually prints."""
    return NIGERIA_CRS_CHOICES


@app.post("/plot/cogo", response_model=PlotResult)
async def plot_cogo(req: CogoRequest):
    """Test COGO traverse -> polygon, without saving. Checks closure first, control tie if given."""
    start_lon, start_lat = transform_points([(req.start_x, req.start_y)], req.source_epsg)[0]

    raw_points = cogo_to_points(start_lon, start_lat, req.legs)
    _, _, closure_error = geod.inv(
        raw_points[-1][0], raw_points[-1][1], raw_points[0][0], raw_points[0][1]
    )

    beacon_ids = [req.start_beacon_id] + [leg.to_beacon_id for leg in req.legs[:-1]]

    control_tie_result = None
    if req.control_tie is not None:
        control_tie_result = check_control_tie(req.control_tie, start_lon, start_lat)

    if closure_error > req.closure_tolerance_m:
        return PlotResult(
            valid=False,
            reason=f"Traverse does not close: {closure_error:.2f}m error (tolerance {req.closure_tolerance_m}m)",
            closure_error_m=closure_error,
            beacon_ids=beacon_ids,
            control_tie=control_tie_result,
        )

    ring_points = raw_points[:-1]  # drop the redundant near-start closing point
    return await run_pipeline(
        ring_points, beacon_ids=beacon_ids, closure_error_m=closure_error, control_tie_result=control_tie_result
    )


@app.post("/plot/direct", response_model=PlotResult)
async def plot_direct(req: DirectRequest):
    """Test direct GPS/import points -> polygon, without saving."""
    points = [(p[0], p[1]) for p in req.points]
    points = transform_points(points, req.source_epsg)
    return await run_pipeline(points, beacon_ids=req.beacon_ids)


@app.post("/parcels")
async def save_parcel(req: SaveParcelRequest):
    """Validate then persist to the fabric with a PIN, plan No., beacons, and title-block metadata."""
    points = [(p[0], p[1]) for p in req.points]
    points = transform_points(points, req.source_epsg)
    polygon = build_polygon(points)

    if not polygon.is_valid:
        raise HTTPException(400, explain_validity(polygon))
    if await check_overlap(polygon):
        raise HTTPException(409, "Overlaps an existing parcel — rejected")

    area = geodesic_area(polygon)
    meta = req.meta or ParcelMeta()

    area_diff_pct = None
    if meta.surveyed_area_sqm:
        area_diff_pct = round(abs(area - meta.surveyed_area_sqm) / meta.surveyed_area_sqm * 100, 2)
        if area_diff_pct > AREA_CHECK_TOLERANCE_PCT:
            # Don't block the save — plans get transcribed with typos and we don't want to lose
            # the parcel over it — but surface it loudly so it gets checked.
            pass

    control_tie_discrepancy = None
    if req.control_tie is not None:
        tie_result = check_control_tie(req.control_tie, points[0][0], points[0][1])
        control_tie_discrepancy = tie_result.discrepancy_m

    pin = await next_pin()
    plan_no = meta.plan_no or await next_plan_no()

    async with app.state.pool.acquire() as conn:
        async with conn.transaction():
            parcel_id = await conn.fetchval(
                """
                INSERT INTO parcels (
                    pin, plan_no, owners, locality, lga, state, surveyor_name, firm_name,
                    area_sqm, surveyed_area_sqm, area_diff_pct, control_station_id,
                    control_tie_discrepancy_m, geom
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13, ST_GeomFromText($14, 4326))
                RETURNING id;
                """,
                pin, plan_no, meta.owners, meta.locality, meta.lga, meta.state,
                meta.surveyor_name, meta.firm_name, area, meta.surveyed_area_sqm,
                area_diff_pct, req.control_tie.station_id if req.control_tie else None,
                control_tie_discrepancy, polygon.wkt,
            )

            beacon_ids = req.beacon_ids or [None] * len(points)
            for seq, (pt, bid) in enumerate(zip(points, beacon_ids)):
                await conn.execute(
                    "INSERT INTO beacons (parcel_id, beacon_id, seq, geom) VALUES ($1,$2,$3, ST_GeomFromText($4, 4326));",
                    parcel_id, bid, seq, f"POINT({pt[0]} {pt[1]})",
                )

    return {
        "id": parcel_id,
        "pin": pin,
        "plan_no": plan_no,
        "area_sqm": round(area, 2),
        "surveyed_area_sqm": meta.surveyed_area_sqm,
        "area_diff_pct": area_diff_pct,
        "control_tie_discrepancy_m": control_tie_discrepancy,
        "geojson": mapping(polygon),
    }


@app.get("/parcels")
async def list_parcels():
    """Serve the whole fabric as GeoJSON — paste into geojson.io to see it plotted."""
    async with app.state.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, pin, plan_no, owners, locality, lga, state, surveyor_name, firm_name,
                   area_sqm, surveyed_area_sqm, area_diff_pct, control_station_id,
                   control_tie_discrepancy_m, ST_AsGeoJSON(geom) as geometry
            FROM parcels ORDER BY id;
            """
        )
        beacon_rows = await conn.fetch(
            "SELECT parcel_id, beacon_id, seq, ST_AsGeoJSON(geom) as geometry FROM beacons ORDER BY parcel_id, seq;"
        )

    beacons_by_parcel = {}
    for b in beacon_rows:
        beacons_by_parcel.setdefault(b["parcel_id"], []).append(
            {"beacon_id": b["beacon_id"], "seq": b["seq"], "geometry": json.loads(b["geometry"])}
        )

    features = [
        {
            "type": "Feature",
            "properties": {
                "id": r["id"],
                "pin": r["pin"],
                "plan_no": r["plan_no"],
                "owners": r["owners"],
                "locality": r["locality"],
                "lga": r["lga"],
                "state": r["state"],
                "surveyor_name": r["surveyor_name"],
                "firm_name": r["firm_name"],
                "area_sqm": r["area_sqm"],
                "surveyed_area_sqm": r["surveyed_area_sqm"],
                "area_diff_pct": r["area_diff_pct"],
                "control_station_id": r["control_station_id"],
                "control_tie_discrepancy_m": r["control_tie_discrepancy_m"],
                "beacons": beacons_by_parcel.get(r["id"], []),
            },
            "geometry": json.loads(r["geometry"]),
        }
        for r in rows
    ]
    return {"type": "FeatureCollection", "features": features}


@app.delete("/parcels")
async def reset_parcels():
    """Wipe the test fabric so you can re-run scenarios cleanly."""
    async with app.state.pool.acquire() as conn:
        await conn.execute("TRUNCATE parcels RESTART IDENTITY CASCADE;")
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
  #panel { width: 440px; padding: 16px; overflow-y: auto; box-sizing: border-box; border-right: 1px solid #ddd; }
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
  .hint { font-size: 11px; color: #888; margin-top: 2px; }
  fieldset { border: 1px solid #ddd; border-radius: 4px; margin-top: 10px; padding: 8px; }
  legend { font-size: 12px; color: #555; padding: 0 4px; }
</style>
</head>
<body>

<div id="panel">
  <h2>1. Build a boundary</h2>
  <div class="mode-toggle">
    <button id="btnModeDirect" class="active" onclick="setMode('direct')">Direct points</button>
    <button id="btnModeCogo" onclick="setMode('cogo')">COGO traverse (DMS)</button>
  </div>

  <div id="directFields" style="display:block">
    <label>Points — [[x,y], ...]</label>
    <textarea id="directPoints">[[7.4900,9.0500],[7.4910,9.0500],[7.4910,9.0510],[7.4900,9.0510]]</textarea>
    <label>Beacon IDs (one per point, blank line if none) — optional</label>
    <textarea id="directBeacons"></textarea>
    <label>Source CRS</label>
    <select id="sourceEpsgDirect"></select>
  </div>

  <div id="cogoFields">
    <label>Start beacon ID (optional)</label>
    <input id="startBeaconId" placeholder="e.g. SC/OS BB8215JP">
    <label>Start x, y (easting,northing or lon,lat)</label>
    <input id="cogoStart" value="7.4900, 9.0500">
    <label>Source CRS</label>
    <select id="sourceEpsgCogo"></select>
    <label>Legs — one per line: deg,min,sec,distance_m,to_beacon_id,calculated(0/1)</label>
    <textarea id="cogoLegs" style="height:100px">90,0,0,50,SC/OS BB0002,0
180,0,0,50,SC/OS BB0003,0
270,0,0,50,SC/OS BB0004,0
0,0,0,50,SC/OS BB0001,0</textarea>
    <div class="hint">"calculated" = 1 marks a bearing computed/back-checked rather than field-measured (the plan's "(Cal.)" note).</div>
    <label>Closure tolerance (m)</label>
    <input id="closureTol" value="0.5">

    <fieldset>
      <legend>GNSS control tie (optional)</legend>
      <label>Station ID</label>
      <input id="tieStationId" placeholder="e.g. OS-APPSN 01S">
      <label>Station x, y</label>
      <input id="tieStationXY" placeholder="e.g. 668351.770, 857149.713">
      <label>Tie bearing (deg,min,sec) and distance (m)</label>
      <input id="tieBearingDist" placeholder="e.g. 200,24,0,3284.18">
    </fieldset>
  </div>

  <button onclick="testPlot()">Test (validate only)</button>
  <button id="saveBtn" onclick="showSaveMeta()" disabled>Save to fabric</button>

  <div id="metaFields" style="display:none">
    <fieldset>
      <legend>Plan details (optional, matches the plan's title block)</legend>
      <label>Plan No. (blank = auto-generate)</label>
      <input id="metaPlanNo" placeholder="e.g. OS/2428/2024/031">
      <label>Owner(s), comma-separated</label>
      <input id="metaOwners">
      <label>Locality / Road</label>
      <input id="metaLocality">
      <label>LGA</label>
      <input id="metaLga">
      <label>State</label>
      <input id="metaState">
      <label>Surveyor name</label>
      <input id="metaSurveyor">
      <label>Firm name</label>
      <input id="metaFirm">
      <label>Surveyed area on plan (m²) — for cross-check</label>
      <input id="metaSurveyedArea">
    </fieldset>
    <button onclick="savePlot()">Confirm save</button>
  </div>

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
let lastBeaconIds = null;

const map = L.map('map').setView([9.05, 7.49], 15);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '&copy; OpenStreetMap contributors'
}).addTo(map);

let fabricLayer = L.geoJSON(null, {
  style: { color: '#2563eb', weight: 2, fillOpacity: 0.15 },
  onEachFeature: (f, layer) => {
    const p = f.properties;
    let beaconList = (p.beacons || []).map(b => b.beacon_id || '(unlabelled)').join(', ');
    layer.bindPopup(
      `<b>${p.pin}</b> ${p.plan_no ? '(' + p.plan_no + ')' : ''}<br>` +
      `Area: ${p.area_sqm} m²` + (p.area_diff_pct != null ? ` (Δ vs plan: ${p.area_diff_pct}%)` : '') + `<br>` +
      (p.owners ? `Owner(s): ${p.owners.join(', ')}<br>` : '') +
      (beaconList ? `Beacons: ${beaconList}` : '')
    );
  }
}).addTo(map);

let previewLayer = L.geoJSON(null, { style: { color: '#dc2626', weight: 2, dashArray: '4' } }).addTo(map);

async function loadCrsOptions() {
  const res = await fetch('/crs-options');
  const options = await res.json();
  const html = Object.entries(options).map(([epsg, label]) =>
    `<option value="${epsg}">${label} (EPSG:${epsg})</option>`
  ).join('');
  document.getElementById('sourceEpsgDirect').innerHTML = html;
  document.getElementById('sourceEpsgCogo').innerHTML = html;
}

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
  document.getElementById('metaFields').style.display = 'none';
  lastGeojson = null;
  lastBeaconIds = null;
  previewLayer.clearLayers();

  let url, body;
  if (mode === 'direct') {
    let points;
    try { points = JSON.parse(document.getElementById('directPoints').value); }
    catch (e) { showResult({ error: 'Points must be valid JSON: ' + e.message }, false); return; }
    const beaconIds = document.getElementById('directBeacons').value.split('\\n').map(s => s.trim() || null);
    const epsg = parseInt(document.getElementById('sourceEpsgDirect').value);
    body = { points, beacon_ids: beaconIds.length === points.length ? beaconIds : null, source_epsg: epsg };
    url = '/plot/direct';
  } else {
    const [x, y] = document.getElementById('cogoStart').value.split(',').map(s => parseFloat(s.trim()));
    const legs = document.getElementById('cogoLegs').value.trim().split('\\n').filter(Boolean).map(line => {
      const [deg, min, sec, dist, beaconId, calc] = line.split(',').map(s => s.trim());
      return {
        bearing: { deg: parseFloat(deg), min: parseFloat(min || 0), sec: parseFloat(sec || 0) },
        distance_m: parseFloat(dist),
        to_beacon_id: beaconId || null,
        calculated: calc === '1'
      };
    });
    body = {
      start_x: x, start_y: y,
      source_epsg: parseInt(document.getElementById('sourceEpsgCogo').value),
      start_beacon_id: document.getElementById('startBeaconId').value.trim() || null,
      legs,
      closure_tolerance_m: parseFloat(document.getElementById('closureTol').value) || 0.5
    };
    const stationId = document.getElementById('tieStationId').value.trim();
    if (stationId) {
      const [sx, sy] = document.getElementById('tieStationXY').value.split(',').map(s => parseFloat(s.trim()));
      const [tdeg, tmin, tsec, tdist] = document.getElementById('tieBearingDist').value.split(',').map(s => parseFloat(s.trim()));
      body.control_tie = {
        station_id: stationId, station_x: sx, station_y: sy,
        source_epsg: parseInt(document.getElementById('sourceEpsgCogo').value),
        bearing: { deg: tdeg, min: tmin || 0, sec: tsec || 0 }, distance_m: tdist
      };
    }
    url = '/plot/cogo';
  }

  const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const data = await res.json();
  showResult(data, data.valid === true);

  if (data.geojson) {
    lastGeojson = data.geojson;
    lastBeaconIds = data.beacon_ids;
    previewLayer.addData(data.geojson);
    map.fitBounds(previewLayer.getBounds(), { maxZoom: 18 });
    document.getElementById('saveBtn').disabled = !data.valid;
  }
}

function showSaveMeta() {
  document.getElementById('metaFields').style.display = 'block';
}

async function savePlot() {
  if (!lastGeojson) return;
  // geojson.coordinates[0] is the exterior ring, already closed — drop the repeated last point.
  const ring = lastGeojson.coordinates[0];
  const points = ring.slice(0, -1);

  const owners = document.getElementById('metaOwners').value.split(',').map(s => s.trim()).filter(Boolean);
  const meta = {
    plan_no: document.getElementById('metaPlanNo').value.trim() || null,
    owners: owners.length ? owners : null,
    locality: document.getElementById('metaLocality').value.trim() || null,
    lga: document.getElementById('metaLga').value.trim() || null,
    state: document.getElementById('metaState').value.trim() || null,
    surveyor_name: document.getElementById('metaSurveyor').value.trim() || null,
    firm_name: document.getElementById('metaFirm').value.trim() || null,
    surveyed_area_sqm: parseFloat(document.getElementById('metaSurveyedArea').value) || null,
  };

  const res = await fetch('/parcels', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ points, beacon_ids: lastBeaconIds, source_epsg: null, meta })
  });
  const data = await res.json();
  if (res.ok) {
    showResult(data, true);
    document.getElementById('saveBtn').disabled = true;
    document.getElementById('metaFields').style.display = 'none';
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

loadCrsOptions();
loadFabric();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def test_ui():
    return TEST_UI
