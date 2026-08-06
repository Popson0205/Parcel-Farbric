# Parcel plotting pipeline — test service

Tests only the plotting pipeline: COGO/direct capture → coordinate transform →
polygon build → topology validation (self-intersection + overlap) → PostGIS
storage → GeoJSON out. Nothing else from GeoEstate/GeoCore — no management
dashboard, no auth, no org/project model. That's deliberately out of scope
here and lives elsewhere.

## Mapping a Nigerian survey plan into this pipeline

Modeled against two real Osun State cadastral plans (Modeseg Survey &
Properties). This is what's on a plan's title block / traverse table, and
where it lands in the pipeline:

| On the plan | Field in this service |
|---|---|
| Bearing, e.g. `310° 30'` | `Leg.bearing = {deg, min, sec}` (or `bearing_deg` if you already have decimal) |
| Distance (m) | `Leg.distance_m` |
| Beacon number, e.g. `SC/OS BB8215JP` | `Leg.to_beacon_id` / `start_beacon_id`, stored per-vertex in a `beacons` table, returned in each fabric feature's `properties.beacons` |
| `(Cal.)` bearing annotation | `Leg.calculated = true` — flags a computed/back-bearing check rather than a field-measured leg |
| Corner coordinates (`679829.843mE 887959.725mN`) | `start_x/start_y` (COGO) or `points` (direct), with `source_epsg` telling the service what grid they're on |
| `ORIGIN: UNIVERSAL ZONE 31` | Resolved via `source_epsg` — see **CRS note** below, this is *not* automatic |
| GNSS OBSERVATION block (reference station + tie bearing/distance) | `control_tie` — cross-checks your start point against an independently-computed one from the reference station, doesn't move anything |
| `PLAN NO: OS/2428/2024/031` | `plan_no`, auto-generated as `{PLAN_STATE_CODE}/{PLAN_JOB_NO}/{year}/{serial}` if you don't supply one |
| `AREA: 1118.152m²` | `meta.surveyed_area_sqm` — cross-checked against the pipeline's own geodesic computation, returned as `area_diff_pct` |
| Owner(s), village/road, LGA, State, surveyor, firm | `meta.owners/locality/lga/state/surveyor_name/firm_name` — stored, not validated |
| Existing road / wire fence annotations, key plan | Not modeled — these are context for a human reader, not parcel geometry |

**CRS note — read before transcribing a plan.** Nigerian plans print a zone
number but never a datum, and it's not safe to assume. Checked against both
sample plans here: their beacon coordinates only land in Osun State under
**WGS84 / UTM zone 31N (EPSG:32631)** — under the legacy **Minna / UTM zone
31N (EPSG:26391)** the same numbers land ~800km away, near the Cameroon
border. That's consistent with these plans being tied to the state's GNSS
CORS network (`OS-APPSN`), which broadcasts in WGS84. Older plans not tied
to a CORS network may still be genuinely on Minna. `GET /crs-options` lists
what the service supports; **confirm the datum with the surveyor per plan**
rather than assuming — a wrong guess moves the parcel by ~100-200m without
producing an error.

**What this doesn't attempt:** OCR/auto-extraction from the plan PDF itself.
Text pulled from these PDFs comes out in the drawing's spatial layout order,
not reading order, so leg/bearing/beacon labels arrive jumbled and can't be
safely auto-paired — transcription has to be done by a person reading the
actual drawing. The closure check and control-tie check exist specifically
to catch a bad transcription before it's saved.

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

## Driver note

This uses `asyncpg`, not `psycopg2`. `psycopg2-binary`'s wheel needs
`libpq.so.5` at runtime, and Railway's build/runtime image split can drop
that shared library, causing an `ImportError: libpq.so.5` crash after a
successful build. `asyncpg` implements the Postgres wire protocol itself, no
system library dependency, so this failure mode goes away entirely. Your
Supabase connection string works as-is — asyncpg understands `sslmode` in
the URL the same way libpq does.

## Testing with real plan data

**1. COGO from a DMS traverse, with beacon IDs and a control tie:**

```bash
curl -X POST https://<your-service>/plot/cogo \
  -H "Content-Type: application/json" \
  -d '{
    "start_x": 668572.647, "start_y": 866736.599, "source_epsg": 32631,
    "start_beacon_id": "SC/OS BC6317JP",
    "legs": [
      {"bearing": {"deg": 61, "min": 21}, "distance_m": 11.95, "to_beacon_id": "SC/OS BC6318JP"},
      {"bearing": {"deg": 139, "min": 32}, "distance_m": 28.3,  "to_beacon_id": "SC/OS BC6319JP"},
      {"bearing": {"deg": 227, "min": 23}, "distance_m": 31.39, "to_beacon_id": "SC/OS BC6320JP", "calculated": true},
      {"bearing": {"deg": 323, "min": 39}, "distance_m": 13.95, "to_beacon_id": "SC/OS BC6317JP"}
    ],
    "closure_tolerance_m": 0.5,
    "control_tie": {
      "station_id": "OS-APPSN 01S",
      "station_x": 668351.770, "station_y": 857149.713, "source_epsg": 32631,
      "bearing": {"deg": 181, "min": 26}, "distance_m": 9564.61
    }
  }'
```

A large `closure_error_m` or `control_tie.discrepancy_m` in the response
usually means a leg was mis-transcribed (order, bearing, or which value is
the distance) — check against the drawing before retrying, not the raw
extracted PDF text.

**2. Save with the plan's title-block metadata:**

```bash
curl -X POST https://<your-service>/parcels \
  -H "Content-Type: application/json" \
  -d '{
    "points": [[4.6317,8.0298],[4.6320,8.0298],[4.6320,8.0301],[4.6317,8.0301]],
    "beacon_ids": ["SC/OS BB8215JP","SC/OS BB8216JP","SC/OS BB8217JP","SC/OS BB8218JP"],
    "source_epsg": 4326,
    "meta": {
      "owners": ["Mr. Emmanuel Oyetunde Fasola", "Mrs. Kemi Oyetunde Fasola"],
      "locality": "Durodola Village, Odo-Afa Road, Owode-Ede",
      "lga": "Ede South", "state": "Osun",
      "surveyor_name": "SURV. A. O. Adeyemo",
      "firm_name": "Modeseg Survey & Properties Consult",
      "surveyed_area_sqm": 1118.152
    }
  }'
```

The response includes `area_diff_pct` — the gap between the plan's stated
area and the pipeline's own geodesic computation.

**3. Plan No. format:** set `PLAN_STATE_CODE` and `PLAN_JOB_NO` in Railway
Variables (e.g. `OS` and `2428`) to match your office's numbering; the
service then generates `OS/2428/2026/001`, `.../002`, ... per calendar year.
Or pass `meta.plan_no` explicitly to use the plan's actual number.

## What this deliberately leaves out

No auth, no organisations/projects, no survey-plan PDF/image attachment
linkage (you transcribe the traverse and title block; the file itself isn't
stored here), no OCR. This is scoped to prove the plotting mechanics — now
including the Nigerian-plan capture format — before it's wired into the
rest of GeoEstate. The management dashboard is a separate piece of work.
