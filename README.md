# Parcel plotting pipeline — test service

Tests only the plotting pipeline: COGO/direct capture → coordinate transform →
polygon build → topology validation (self-intersection + overlap) → PostGIS
storage → GeoJSON out. Nothing else from GeoEstate/GeoCore.

## Testing UI

Open `https://<your-service>.up.railway.app/` directly — it's a self-contained
test page (Leaflet map + form), no separate frontend to deploy:

- Switch between **Direct points** and **COGO traverse** input.
- **Test (validate only)** hits `/plot/direct` or `/plot/cogo` and draws the
  candidate polygon in red without saving — use this to check closure,
  self-intersection, and overlap before committing anything.
- **Save to fabric** only enables once a test comes back valid, then posts to
  `/parcels` and redraws the fabric in blue with its PIN and area in a popup.
- **Reset fabric** wipes the table so you can re-run scenarios cleanly.

`/docs` (Swagger UI) is still there if you want to hit the raw endpoints
directly instead.

## Deploy on Railway, using Supabase as the database

1. **Enable PostGIS on Supabase first**: Dashboard → Database → Extensions →
   search "postgis" → enable.
2. **Get the connection string — use the Session Pooler, not Direct
   connection**: Dashboard → Project Settings → Database → Connection
   string → select the **"Session pooler"** tab (not "Direct connection").
   Supabase's direct connection host (`db.<project-ref>.supabase.co`)
   resolves to an IPv6-only address, and Railway has no outbound IPv6
   routing — connecting to it always fails with `OSError: Network is
   unreachable`, no matter what the app code does. The Session Pooler host
   (`aws-0-<region>.pooler.supabase.com:5432`) resolves over IPv4 and works
   from Railway. Don't use the **Transaction Pooler** (port 6543) either —
   it disables server-side prepared statements, which asyncpg relies on by
   default.
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

**2. COGO traverse** (bearing/distance legs from a start beacon, on the
local grid — Easting/Northing in metres, not lon/lat):

```bash
curl -X POST https://<your-service>/plot/cogo \
  -H "Content-Type: application/json" \
  -d '{
    "start_easting": 350000, "start_northing": 1000000,
    "source_epsg": 26332,
    "legs": [
      {"bearing_deg": 90,  "distance_m": 50},
      {"bearing_deg": 180, "distance_m": 50},
      {"bearing_deg": 270, "distance_m": 50},
      {"bearing_deg": 0,   "distance_m": 50}
    ]
  }'
```

`source_epsg` is the surveyor's local grid — e.g. `26332` for Nigeria Mid
Belt/Minna, or the relevant UTM zone. The traverse itself is computed as
plane trigonometry on that grid (correct surveying practice), then the
closed polygon is reprojected to WGS84 for storage.

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

## Driver note

This uses `asyncpg`, not `psycopg2`. `psycopg2-binary`'s wheel needs
`libpq.so.5` at runtime, and Railway's build/runtime image split can drop
that shared library, causing an `ImportError: libpq.so.5` crash after a
successful build. `asyncpg` implements the Postgres wire protocol itself, no
system library dependency, so this failure mode goes away entirely. Your
Supabase connection string works as-is — asyncpg understands `sslmode` in
the URL the same way libpq does.

## What this deliberately leaves out

No auth, no organisations/projects, no PIN format beyond a counter, no
survey-plan attachment linkage. This is scoped to prove the plotting
mechanics alone before it's wired into the rest of GeoEstate.
