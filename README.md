# Parcel plotting pipeline — test service

Tests only the plotting pipeline: COGO/direct capture → coordinate transform →
polygon build → topology validation (self-intersection + overlap) → PostGIS
storage → GeoJSON out. Nothing else from GeoEstate/GeoCore.

## Deploy on Railway, using Supabase as the database

1. **Enable PostGIS on Supabase first**: Dashboard → Database → Extensions →
   search "postgis" → enable. Do this before first deploy — the connection
   role Supabase gives you for the app may not have `CREATE EXTENSION`
   rights, so the app's own attempt to create it (on startup) is a no-op
   fallback, not the primary path.
2. **Get the connection string**: Dashboard → Project Settings → Database →
   Connection string → URI. Use the **Session pooler** or **direct
   connection** string (port 5432), not the transaction pooler (port 6543) —
   this app opens/closes a fresh connection per request and doesn't handle
   pooled prepared-statement restrictions. Append `?sslmode=require` if it's
   not already in the string.
3. Create a Railway project, add this folder as a service (push to a GitHub
   repo, or `railway up` from the CLI in this folder — no Railway Postgres
   plugin needed since Supabase is the database).
4. In the service's **Variables** tab, add `DATABASE_URL` manually, pasted
   from step 2.
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
