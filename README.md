# Parcel plotting pipeline — test service

Tests only the plotting pipeline: COGO/direct capture → coordinate transform →
polygon build → topology validation (self-intersection + overlap) → PostGIS
storage → GeoJSON out. Nothing else from GeoEstate/GeoCore.

## Deploy on Railway

1. Create a new Railway project.
2. **Add a database**: "+ New" → "Database" → "PostgreSQL". Railway's Postgres
   supports `CREATE EXTENSION postgis` out of the box — the app creates the
   extension and table automatically on startup.
3. **Add this service**: "+ New" → "GitHub Repo" (push this folder to a repo
   first) or "Empty Service" + `railway up` from the CLI in this folder.
4. In the service's **Variables** tab, click "Add Reference" and link the
   Postgres plugin's `DATABASE_URL` — Railway wires this in automatically if
   both are in the same project.
5. Deploy. Railway detects `requirements.txt` + `Procfile` and builds it as a
   Python service.
6. Once live, open `https://<your-service>.up.railway.app/docs` — FastAPI's
   Swagger UI, so you can test every endpoint from the browser without curl.

## Testing the pipeline

**1. Direct capture** (e.g. GPS corners, already lon/lat) — test without saving:

```bash
curl -X POST https://<your-service>/plot/direct \
  -H "Content-Type: application/json" \
  -d '{
    "points": [[7.4900,9.0500],[7.4910,9.0500],[7.4910,9.0510],[7.4900,9.0510]]
  }'
```

**2. COGO traverse** (bearing/distance legs from a start beacon):

```bash
curl -X POST https://<your-service>/plot/cogo \
  -H "Content-Type: application/json" \
  -d '{
    "start_lon": 7.4900, "start_lat": 9.0500,
    "legs": [
      {"bearing_deg": 90,  "distance_m": 50},
      {"bearing_deg": 180, "distance_m": 50},
      {"bearing_deg": 270, "distance_m": 50},
      {"bearing_deg": 0,   "distance_m": 50}
    ]
  }'
```

If the legs don't return you to (roughly) the start point, you'll get a
`"valid": false` with the closure error in metres — that's the traverse
closure check working as intended.

**3. Save a validated parcel to the fabric:**

```bash
curl -X POST https://<your-service>/parcels \
  -H "Content-Type: application/json" \
  -d '{"points": [[7.4900,9.0500],[7.4910,9.0500],[7.4910,9.0510],[7.4900,9.0510]]}'
```

**4. Try to save an overlapping parcel** (shift it slightly so it intersects
the one above) — expect a `409 Overlaps an existing parcel — rejected`.

**5. View the whole fabric:**

```bash
curl https://<your-service>/parcels
```

Copy that GeoJSON output and paste it into [geojson.io](https://geojson.io) —
fastest way to *see* the plotted parcels on a map without building any
frontend.

**6. Reset between test runs:**

```bash
curl -X DELETE https://<your-service>/parcels
```

## What this deliberately leaves out

No auth, no organisations/projects, no PIN format beyond a counter, no
survey-plan attachment linkage. This is scoped to prove the plotting
mechanics alone before it's wired into the rest of GeoEstate.
