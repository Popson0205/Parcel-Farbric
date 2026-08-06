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
import os
from typing import List, Optional

import asyncpg
from fastapi import FastAPI, HTTPException, Response
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
    start_lon: float
    start_lat: float
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

def cogo_to_points(start_lon, start_lat, legs):
    """Walk bearing/distance legs geodesically from a start point."""
    points = [(start_lon, start_lat)]
    lon, lat = start_lon, start_lat
    for leg in legs:
        lon, lat, _ = geod.fwd(lon, lat, leg.bearing_deg, leg.distance_m)
        points.append((lon, lat))
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
    """Test COGO traverse -> polygon, without saving. Checks closure first."""
    raw_points = cogo_to_points(req.start_lon, req.start_lat, req.legs)

    _, _, closure_error = geod.inv(
        raw_points[-1][0], raw_points[-1][1], raw_points[0][0], raw_points[0][1]
    )

    if closure_error > req.closure_tolerance_m:
        return PlotResult(
            valid=False,
            reason=f"Traverse does not close: {closure_error:.2f}m error (tolerance {req.closure_tolerance_m}m)",
            closure_error_m=closure_error,
        )

    return await run_pipeline(raw_points, closure_error_m=closure_error)


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
