import os
import math
import asyncio
import httpx
import logging

from fastapi import FastAPI, HTTPException
from dotenv import load_dotenv
from datetime import datetime
from timezonefinder import TimezoneFinder
import pytz
from cachetools import TTLCache

# Configure logging to help you debug in the terminal
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 1. Load secrets and initialize the server
load_dotenv()
besttime_api_key = os.getenv("BESTTIME_API_KEY")
# NOTE: google_api_key and directions_api_key are no longer needed for search
# but keep them if you still intend to use Google for the Map Polyline/Routing.

app = FastAPI(title="Lova Backend API (OSM Version)")

# --- INITIALIZE TOOLS ---
tf = TimezoneFinder()
vibe_cache = TTLCache(maxsize=2000, ttl=3600)

# --- IN-MEMORY STORAGE (Replacing Postgres for quick testing) ---
in_memory_reports = {}

# --- PSYCHOLOGICAL ENGINE ---
def calculate_lova_score(busyness: int) -> dict:
    lova_score = 100 - busyness
    if lova_score >= 80:
        return {"score": lova_score, "status": "🟢 Undisturbed", "message": "Optimal refuge. High privacy."}
    elif lova_score >= 60:
        return {"score": lova_score, "status": "🟡 Low Friction", "message": "Manageable environment."}
    elif lova_score >= 40:
        return {"score": lova_score, "status": "🟠 Moderate Drain", "message": "Active background noise."}
    elif lova_score >= 20:
        return {"score": lova_score, "status": "🔴 High Social Cost", "message": "Very lively environment."}
    else:
        return {"score": lova_score, "status": "🚫 Overwhelming", "message": "Currently peaking. Find another spot."}

# --- HELPER FUNCTIONS ---
def calculate_distance_miles(lat1, lon1, lat2, lon2):
    R = 3958.8 
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2)**2 + 
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

async def real_osm_search(client: httpx.AsyncClient, query: str, lat: float, lng: float):
    """
    Replaces Google Places with OpenStreetMap (Nominatim).
    No API Key required.
    """
    url = "https://nominatim.openstreetmap.org/search"
    params = {
        "q": query,
        "format": "json",
        "limit": 5,
        "lat": lat,
        "lon": lng,
        "addressdetails": 1,
        "extratags": 1
    }
    # Nominatim REQUIRES a User-Agent header or they will block the request.
    headers = {"User-Agent": "LovaApp/1.0 (contact: your-email@example.com)"}
    
    try:
        response = await client.get(url, params=params, headers=headers)
        response.raise_for_status()
        data = response.json()
        
        results = []
        for place in data:
            p_lat = float(place.get("lat", 0.0))
            p_lng = float(place.get("lon", 0.0))
            
            # Extract clean address parts for BestTime API compatibility
            addr = place.get("address", {})
            road = addr.get("road", "")
            house_number = addr.get("house_number", "")
            city = addr.get("city") or addr.get("town") or addr.get("suburb", "")
            
            # Format: "123 Main St, New York"
            clean_address = f"{house_number} {road}, {city}".strip(", ")
            if not clean_address:
                clean_address = place.get("display_name", "").split(",")[0]

            distance = calculate_distance_miles(lat, lng, p_lat, p_lng)
            results.append({
                "name": place.get("name") or place.get("display_name").split(',')[0], 
                "address": clean_address,
                "lat": p_lat,
                "lng": p_lng,
                "distance_miles": round(distance, 1)
            })
        return results
    except Exception as e:
        logger.error(f"OSM Search Error: {e}")
        return []

async def get_single_vibe_forecast(client: httpx.AsyncClient, venue_name: str, venue_address: str, venue_lat: float, venue_lng: float, local_day: int, local_hour: int):
    cache_key = f"{venue_address}_{local_day}_{local_hour}"
    
    if cache_key in vibe_cache:
        return calculate_lova_score(vibe_cache[cache_key])

    # 1. Historical Data (BestTime API)
    url = "https://besttime.app/api/v1/forecasts"
    params = {"api_key_private": besttime_api_key, "venue_name": venue_name, "venue_address": venue_address}
    busyness = 50 
    
    try:
        # BestTime can be slow, using a 5s timeout
        response = await client.post(url, params=params, timeout=5.0)
        if response.status_code == 200:
            raw_data = response.json()
            for day in raw_data.get("analysis", []):
                if day.get("day_info", {}).get("day_int") == local_day:
                    raw_hours = day.get("day_raw", [])
                    if len(raw_hours) > local_hour:
                        busyness = raw_hours[local_hour]
                    break
    except Exception as e:
        logger.warning(f"BestTime fetch failed for {venue_name}: {e}")

    # 2. Live Calibration
    adjustment = 0
    if venue_address in in_memory_reports:
        reports = in_memory_reports[venue_address]
        adjustment = sum(reports) / len(reports) 

    # 3. Final Calculation
    base_lova_score = 100 - busyness
    final_lova_score = max(0, min(100, base_lova_score + adjustment))
    final_busyness = 100 - final_lova_score
    
    vibe_cache[cache_key] = final_busyness
    return calculate_lova_score(final_busyness)


# --- THE MAIN ENDPOINTS ---

@app.get("/api/vibe-search")
async def search_smart_vibes(query: str, lat: float, lng: float):
    # Handle the "0.0, 0.0" coordinate case
    if lat == 0.0 and lng == 0.0:
        logger.warning("Coordinates are 0.0, 0.0. Ensure Emulator GPS is set.")

    tz_str = tf.timezone_at(lng=lng, lat=lat)
    local_tz = pytz.timezone(tz_str) if tz_str else pytz.UTC
    local_time = datetime.now(local_tz)
    local_day, local_hour = local_time.weekday(), local_time.hour

    async with httpx.AsyncClient(timeout=15.0) as client:
        # Use OpenStreetMap instead of Google
        nearby_venues = await real_osm_search(client, query, lat, lng)
        
        if not nearby_venues:
            raise HTTPException(status_code=404, detail="No venues found nearby. Try a different search term or check GPS.")

        tasks = [
            get_single_vibe_forecast(client, v["name"], v["address"], v["lat"], v["lng"], local_day, local_hour) 
            for v in nearby_venues
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
                "vibeStatus": vibe_data.get("status"),
                "advice": vibe_data.get("message")
            })

        nearest = min(analyzed_venues, key=lambda x: x["distanceValue"])
        quietest = max(analyzed_venues, key=lambda x: x["lovaScore"])
        
        return {"nearest": nearest, "quietest": quietest}

@app.post("/api/vouch")
async def submit_vouch(venue_address: str, impact: int):
    if venue_address not in in_memory_reports:
        in_memory_reports[venue_address] = []
    in_memory_reports[venue_address].append(impact)
    
    keys_to_remove = [k for k in vibe_cache.keys() if k.startswith(venue_address)]
    for k in keys_to_remove: 
        vibe_cache.pop(k, None)
        
    return {
        "status": "success", 
        "message": "Aura calibrated! The map will update shortly."
    }