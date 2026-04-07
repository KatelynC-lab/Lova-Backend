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
google_api_key = os.getenv("GOOGLE_API_KEY")
directions_api_key = os.getenv("DIRECTIONS_API_KEY")

app = FastAPI(title="Lova Backend API")

# --- INITIALIZE TOOLS ---
tf = TimezoneFinder()
vibe_cache = TTLCache(maxsize=2000, ttl=3600)

# --- IN-MEMORY STORAGE ---
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

async def real_google_places_search(client: httpx.AsyncClient, query: str, lat: float, lng: float):
    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": google_api_key,
        "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.location"
    }
    payload = {
        "textQuery": query,
        "locationBias": {"circle": {"center": {"latitude": lat, "longitude": lng}, "radius": 10000.0}}
    }
    
    try:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
        
        results = []
        # Defensive parsing: use .get() to prevent KeyErrors if Google returns partial data
        for place in data.get("places", [])[:5]: 
            name = place.get("displayName", {}).get("text", "Unknown Venue")
            address = place.get("formattedAddress", "No address provided")
            location = place.get("location", {})
            p_lat = location.get("latitude", 0.0)
            p_lng = location.get("longitude", 0.0)
            
            distance = calculate_distance_miles(lat, lng, p_lat, p_lng)
            results.append({
                "name": name, 
                "address": address,
                "lat": p_lat,
                "lng": p_lng,
                "distance_miles": round(distance, 1)
            })
        return results
    except Exception as e:
        logger.error(f"Google Places Error: {e}")
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
        response = await client.post(url, params=params, timeout=3.0)
        if response.status_code == 200:
            raw_data = response.json()
            for day in raw_data.get("analysis", []):
                if day.get("day_info", {}).get("day_int") == local_day:
                    # Access safety: ensure hour index exists
                    raw_hours = day.get("day_raw", [])
                    if len(raw_hours) > local_hour:
                        busyness = raw_hours[local_hour]
                    break
    except Exception as e:
        logger.warning(f"BestTime fetch failed for {venue_name}: {e}")

    # 2. Live Calibration from Crowd-sourced reports
    adjustment = 0
    if venue_address in in_memory_reports and in_memory_reports[venue_address]:
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
    # Handle the "0.0, 0.0" coordinate case gracefully
    if lat == 0.0 and lng == 0.0:
        logger.warning("Search triggered with 0.0, 0.0 coordinates.")

    tz_str = tf.timezone_at(lng=lng, lat=lat)
    local_tz = pytz.timezone(tz_str) if tz_str else pytz.UTC
    local_time = datetime.now(local_tz)
    local_day, local_hour = local_time.weekday(), local_time.hour

    async with httpx.AsyncClient(timeout=10.0) as client:
        nearby_venues = await real_google_places_search(client, query, lat, lng)
        
        if not nearby_venues:
            raise HTTPException(status_code=404, detail="No venues found nearby. Try enabling GPS.")

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

@app.get("/api/get-route")
async def get_quiet_route(origin_lat: float, origin_lng: float, dest_lat: float, dest_lng: float):
    url = "https://maps.googleapis.com/maps/api/directions/json"
    params = {
        "origin": f"{origin_lat},{origin_lng}", 
        "destination": f"{dest_lat},{dest_lng}", 
        "mode": "walking", 
        "key": directions_api_key
    }
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(url, params=params)
        data = response.json()
        if data.get("status") == "OK":
            return {"status": "success", "polyline": data["routes"][0]["overview_polyline"]["points"]}
        
        raise HTTPException(status_code=400, detail=f"Route failed: {data.get('status')}")

@app.post("/api/vouch")
async def submit_vouch(venue_address: str, impact: int):
    # Save report
    if venue_address not in in_memory_reports:
        in_memory_reports[venue_address] = []
    in_memory_reports[venue_address].append(impact)
    
    # Invalidate cache for this specific venue
    keys_to_remove = [k for k in vibe_cache.keys() if k.startswith(venue_address)]
    for k in keys_to_remove: 
        vibe_cache.pop(k, None)
        
    # Return both status AND message to satisfy the Kotlin data model
    return {
        "status": "success", 
        "message": "Aura calibrated! The map will update shortly."
    }