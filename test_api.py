import os
import math
import asyncio
import logging
from datetime import datetime
from typing import Any

import httpx
import pytz
from cachetools import TTLCache
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from timezonefinder import TimezoneFinder

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("lova-backend")

load_dotenv()

BESTTIME_API_KEY = os.getenv("BESTTIME_API_KEY", "").strip()
NOMINATIM_CONTACT_EMAIL = os.getenv("NOMINATIM_CONTACT_EMAIL", "contact@example.com").strip()
NOMINATIM_USER_AGENT = f"LovaBackend/1.1 (contact: {NOMINATIM_CONTACT_EMAIL})"

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_ROUTE_URL = "https://router.project-osrm.org/route/v1/driving"

tf = TimezoneFinder()
vibe_cache = TTLCache(maxsize=2000, ttl=3600)
in_memory_reports: dict[str, list[int]] = {}

app = FastAPI(title="Lova Backend API", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def calculate_lova_score(busyness: int) -> dict[str, Any]:
    lova_score = max(0, min(100, 100 - int(busyness)))

    if lova_score >= 80:
        return {"score": lova_score, "status": "Undisturbed", "message": "Optimal refuge. High privacy."}
    elif lova_score >= 60:
        return {"score": lova_score, "status": "Low Friction", "message": "Manageable environment."}
    elif lova_score >= 40:
        return {"score": lova_score, "status": "Moderate Drain", "message": "Active background noise."}
    elif lova_score >= 20:
        return {"score": lova_score, "status": "High Social Cost", "message": "Very lively environment."}
    else:
        return {"score": lova_score, "status": "Overwhelming", "message": "Currently peaking. Find another spot."}

def calculate_distance_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c

def build_fallback_venues(query: str, lat: float, lng: float) -> list[dict[str, Any]]:
    base_name = query.strip().title() if query.strip() else "Quiet Spot"
    return [
        {"name": f"{base_name} A", "address": "Nearby fallback result", "lat": lat + 0.0020, "lng": lng + 0.0015, "distance_miles": 0.2},
        {"name": f"{base_name} B", "address": "Nearby fallback result", "lat": lat - 0.0030, "lng": lng + 0.0020, "distance_miles": 0.3},
        {"name": f"{base_name} C", "address": "Nearby fallback result", "lat": lat + 0.0040, "lng": lng - 0.0025, "distance_miles": 0.4},
    ]

async def real_osm_search(client: httpx.AsyncClient, query: str, lat: float, lng: float) -> list[dict[str, Any]]:
    params = {
        "q": query,
        "format": "jsonv2",
        "limit": 10,
        "addressdetails": 1,
        "extratags": 1,
        "viewbox": f"{lng - 0.15},{lat + 0.15},{lng + 0.15},{lat - 0.15}",
        "bounded": 1,
        "email": NOMINATIM_CONTACT_EMAIL,
    }
    headers = {
        "User-Agent": NOMINATIM_USER_AGENT,
        "Accept": "application/json",
    }

    response = await client.get(NOMINATIM_URL, params=params, headers=headers, timeout=15.0)
    response.raise_for_status()
    data = response.json()

    results = []
    for place in data:
        p_lat = float(place.get("lat", 0.0))
        p_lng = float(place.get("lon", 0.0))

        addr = place.get("address", {})
        road = addr.get("road", "")
        house_number = addr.get("house_number", "")
        city = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("suburb", "")

        clean_address = f"{house_number} {road}, {city}".strip(", ").strip()
        if not clean_address:
            clean_address = place.get("display_name", "").split(",")[0].strip()

        name = place.get("name") or place.get("display_name", "").split(",")[0].strip() or "Unknown Place"
        distance = calculate_distance_miles(lat, lng, p_lat, p_lng)

        results.append({
            "name": name,
            "address": clean_address,
            "lat": p_lat,
            "lng": p_lng,
            "distance_miles": round(distance, 1),
        })

    return results

async def get_single_vibe_forecast(
    client: httpx.AsyncClient,
    venue_name: str,
    venue_address: str,
    venue_lat: float,
    venue_lng: float,
    local_day: int,
    local_hour: int,
) -> dict[str, Any]:
    cache_key = f"{venue_address}_{local_day}_{local_hour}"
    if cache_key in vibe_cache:
        return calculate_lova_score(vibe_cache[cache_key])

    busyness = 50

    if BESTTIME_API_KEY:
        try:
            response = await client.post(
                "https://besttime.app/api/v1/forecasts",
                params={
                    "api_key_private": BESTTIME_API_KEY,
                    "venue_name": venue_name,
                    "venue_address": venue_address,
                },
                timeout=5.0,
            )
            if response.status_code == 200:
                raw_data = response.json()
                for day in raw_data.get("analysis", []):
                    if day.get("day_info", {}).get("day_int") == local_day:
                        raw_hours = day.get("day_raw", [])
                        if len(raw_hours) > local_hour:
                            busyness = raw_hours[local_hour]
                        break
        except Exception as exc:
            logger.warning("BestTime fetch failed for %s: %s", venue_name, exc)

    adjustment = 0.0
    if venue_address in in_memory_reports:
        reports = in_memory_reports[venue_address]
        adjustment = sum(reports) / len(reports)

    base_lova_score = 100 - busyness
    final_lova_score = max(0, min(100, base_lova_score + adjustment))
    final_busyness = 100 - final_lova_score

    vibe_cache[cache_key] = final_busyness
    return calculate_lova_score(final_busyness)

async def get_route_polyline(
    client: httpx.AsyncClient,
    origin_lat: float,
    origin_lng: float,
    dest_lat: float,
    dest_lng: float,
) -> str:
    url = f"{OSRM_ROUTE_URL}/{origin_lng},{origin_lat};{dest_lng},{dest_lat}"
    params = {"overview": "full", "geometries": "polyline"}

    response = await client.get(url, params=params, timeout=15.0)
    response.raise_for_status()

    data = response.json()
    routes = data.get("routes", [])
    if not routes:
        raise HTTPException(status_code=404, detail="No route found.")

    polyline = routes[0].get("geometry")
    if not polyline:
        raise HTTPException(status_code=404, detail="Route geometry missing.")

    return polyline

@app.get("/")
async def root():
    return {"message": "Lova backend is running."}

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/api/vibe-search")
async def search_smart_vibes(
    query: str = Query(..., min_length=1),
    lat: float = Query(...),
    lng: float = Query(...),
):
    if abs(lat) > 90 or abs(lng) > 180:
        raise HTTPException(status_code=400, detail="Invalid coordinates.")

    tz_str = tf.timezone_at(lng=lng, lat=lat)
    local_tz = pytz.timezone(tz_str) if tz_str else pytz.UTC
    local_time = datetime.now(local_tz)
    local_day = local_time.weekday()
    local_hour = local_time.hour

    async with httpx.AsyncClient() as client:
        try:
            nearby_venues = await real_osm_search(client, query, lat, lng)
        except Exception as exc:
            logger.warning("OSM lookup failed, using fallback venues: %s", exc)
            nearby_venues = build_fallback_venues(query, lat, lng)

        if not nearby_venues:
            nearby_venues = build_fallback_venues(query, lat, lng)

        tasks = [
            get_single_vibe_forecast(
                client,
                venue["name"],
                venue["address"],
                venue["lat"],
                venue["lng"],
                local_day,
                local_hour,
            )
            for venue in nearby_venues
        ]

        vibe_results = await asyncio.gather(*tasks)

        analyzed_venues = []
        for venue, vibe_data in zip(nearby_venues, vibe_results):
            analyzed_venues.append({
                "venueName": venue["name"],
                "address": venue["address"],
                "lat": venue["lat"],
                "lng": venue["lng"],
                "distance": f'{venue["distance_miles"]} miles',
                "distanceValue": venue["distance_miles"],
                "lovaScore": vibe_data.get("score", 50),
                "vibeStatus": vibe_data.get("status", "Moderate Drain"),
                "advice": vibe_data.get("message", "No additional advice available."),
            })

        nearest = min(analyzed_venues, key=lambda x: x["distanceValue"])
        quietest = max(analyzed_venues, key=lambda x: x["lovaScore"])
        return {"nearest": nearest, "quietest": quietest}

@app.get("/api/get-route")
async def get_route(origin_lat: float, origin_lng: float, dest_lat: float, dest_lng: float):
    async with httpx.AsyncClient() as client:
        try:
            polyline = await get_route_polyline(client, origin_lat, origin_lng, dest_lat, dest_lng)
            return {"polyline": polyline}
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("Route fetch failed: %s", exc)
            raise HTTPException(status_code=502, detail="Route service unavailable.")

@app.post("/api/vouch")
async def submit_vouch(venue_address: str, impact: int = Query(..., ge=-20, le=20)):
    if venue_address not in in_memory_reports:
        in_memory_reports[venue_address] = []

    in_memory_reports[venue_address].append(impact)

    keys_to_remove = [key for key in vibe_cache.keys() if key.startswith(venue_address)]
    for key in keys_to_remove:
        vibe_cache.pop(key, None)

    return {"status": "success", "message": "Aura calibrated! The map will update shortly."}