# Going to California

An interactive travel-planning map. Add locations to a CSV, see driving times between all of them, and draw the actual route between any two.

## Features

- Dark full-screen map (CartoDB Dark Matter + Leaflet)
- Loads places from `places.csv` (gitignored) or a `PLACES_DATA` environment variable
- Geocodes addresses via Nominatim, with Google Maps URL as fallback
- Fetches a full driving-time matrix from OSRM on startup — no API keys required
- Click a location → see driving times to all others, sorted and ranked
- Click a second location → draws the actual road route with duration and distance
- Failed geocodes shown with a warning, excluded from routing
- Geocoded coordinates cached locally to avoid repeat lookups

## Running Locally

```bash
uv sync
uv run fastapi dev main.py
```

Open http://localhost:8000.

### Places file

Create `places.csv` in the project root (it's gitignored):

```csv
name,address,gmaps_url
Marina Del Rey,4148 Via Marina Marina Del Rey CA 90292,https://maps.app.goo.gl/...
Yosemite,7229 Yosemite Park Way Yosemite West CA 95389,
```

- `gmaps_url` is optional but used as a geocoding fallback if Nominatim can't resolve the address
- You can also add `lat` and `lng` columns to skip geocoding entirely

## Deploying to Vercel

Set the `PLACES_DATA` environment variable in Vercel (Settings → Environment Variables) to the full contents of your `places.csv`. The app will read from it when the file isn't present on the server.

```
uv run vercel --prod
```
