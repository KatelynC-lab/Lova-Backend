import os
import math
import asyncio
import httpx

from fastapi import FastAPI, HTTPException
from dotenv import load_dotenv
from datetime import datetime
from timezonefinder import TimezoneFinder
import pytz
from cachetools import TTLCache

# 1. Load secrets and initialize the server
load_dotenv()
besttime_api_key = os.getenv("BESTTIME_API_KEY")


app = FastAPI(title="Lova Backend API")

# --- INITIALIZE TOOLS ---
tf = TimezoneFinder()
vibe_cache = TTLCache(maxsize=2000, ttl=3600)

# --- POSTGRESQL DATA MOAT SETUP ---
def init_db():
    """Initializes your permanent PostgreSQL database on Render."""
    if not database_url:
        print("Warning: DATABASE_URL not found. Skipping DB init.")
        return
        
    try:
        conn = psycopg2.connect(database_url)
        cursor = conn.cursor()
        
        # Table 1: Historical Data
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS venue_history (
                id SERIAL PRIMARY KEY,
                venue_name VARCHAR(255),
                address TEXT,
                lat DOUBLE PRECISION,
                lng DOUBLE PRECISION,
                day_of_week INTEGER,
                hour_of_day INTEGER,
                busyness_score INTEGER,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Table 2: User Crowdsourced Reports (Fixed column name to impact_value)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_reports (
                id SERIAL PRIMARY KEY,
                venue_address TEXT,
                impact_value INTEGER, 
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        conn.commit()
        cursor.close()
        conn.close()
        print("✅ PostgreSQL Data Moat Initialized!")
    except Exception as e:
        print(f"❌ Database initialization failed: {e}")

init_db()

def save_to_data_moat(name: str, address: str, lat: float, lng: float, day: int, hour: int, busyness: int):
    if not database_url:
        return
    try:
        conn = psycopg2.connect(database_url)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO venue_history (venue_name, address, lat, lng, day_of_week, hour_of_day, busyness_score)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        ''', (name, address, lat, lng, day, hour, busyness))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Failed to save data moat: {e}")

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
    response = await client.post(url, headers=headers, json=payload)
    results = []
    if response.status_code == 200:
        data = response.json()
        for place in data.get("places", [])[:2]: 
            place_lat = place["location"]["latitude"]
            place_lng = place["location"]["longitude"]
            distance = calculate_distance_miles(lat, lng, place_lat, place_lng)
            results.append({
                "name": place["displayName"]["text"], 
                "address": place["formattedAddress"],
                "lat": place_lat,
                "lng": place_lng,
                "distance_miles": round(distance, 1)
            })
    return results

async def get_single_vibe_forecast(client: httpx.AsyncClient, venue_name: str, venue_address: str, venue_lat: float, venue_lng: float, local_day: int, local_hour: int):
    cache_key = f"{venue_address}_{local_day}_{local_hour}"
    
    if cache_key in vibe_cache:
        return calculate_lova_score(vibe_cache[cache_key])

    # 1. Historical Data
    url = "https://besttime.app/api/v1/forecasts"
    params = {"api_key_private": besttime_api_key, "venue_name": venue_name, "venue_address": venue_address}
    busyness = 50 
    try:
        response = await client.post(url, params=params)
        if response.status_code == 200:
            raw_data = response.json()
            for day in raw_data.get("analysis", []):
                if day.get("day_info", {}).get("day_int") == local_day:
                    busyness = day.get("day_raw", [])[local_hour] 
                    break
    except: pass

    # 2. Live Calibration from Render Postgres
    adjustment = 0
    try:
        conn = psycopg2.connect(database_url)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT AVG(impact_value) FROM user_reports 
            WHERE venue_address = %s 
            AND timestamp > NOW() - INTERVAL '1 hour'
        ''', (venue_address,))
        result = cursor.fetchone()[0]
        adjustment = int(result) if result is not None else 0
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Postgres Read Error: {e}")

    # 3. Final Calc
    base_lova_score = 100 - busyness
    final_lova_score = max(0, min(100, base_lova_score + adjustment))
    final_busyness = 100 - final_lova_score
    
    vibe_cache[cache_key] = final_busyness
    save_to_data_moat(venue_name, venue_address, venue_lat, venue_lng, local_day, local_hour, final_busyness)
    return calculate_lova_score(final_busyness)


# --- THE MAIN ENDPOINTS ---

@app.get("/api/vibe-search")
async def search_smart_vibes(query: str, lat: float, lng: float):
    tz_str = tf.timezone_at(lng=lng, lat=lat)
    local_tz = pytz.timezone(tz_str) if tz_str else pytz.UTC
    local_time = datetime.now(local_tz)
    local_day, local_hour = local_time.weekday(), local_time.hour

    async with httpx.AsyncClient(timeout=7.0) as client:
        nearby_venues = await real_google_places_search(client, query, lat, lng)
        tasks = [get_single_vibe_forecast(client, v["name"], v["address"], v["lat"], v["lng"], local_day, local_hour) for v in nearby_venues]
        vibe_results = await asyncio.gather(*tasks)

        analyzed_venues = []
        for venue, vibe_data in zip(nearby_venues, vibe_results):
            analyzed_venues.append({
                "venueName": venue["name"], "address": venue["address"],
                "lat": venue["lat"], "lng": venue["lng"],
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
    params = {"origin": f"{origin_lat},{origin_lng}", "destination": f"{dest_lat},{dest_lng}", "mode": "walking", "key": directions_api_key}
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(url, params=params)
        data = response.json()
        if data.get("status") == "OK":
            return {"status": "success", "polyline": data["routes"][0]["overview_polyline"]["points"]}
        raise HTTPException(status_code=400, detail="Route failed")

# --- VOUCH ENDPOINT (Correctly placed outside) ---
@app.post("/api/vouch")
async def submit_vouch(venue_address: str, impact: int):
    try:
        conn = psycopg2.connect(database_url)
        cursor = conn.cursor()
        cursor.execute('INSERT INTO user_reports (venue_address, impact_value) VALUES (%s, %s)', (venue_address, impact))
        conn.commit()
        cursor.close()
        conn.close()
        
        # Clear cache so map updates instantly
        keys_to_remove = [k for k in vibe_cache.keys() if k.startswith(venue_address)]
        for k in keys_to_remove: vibe_cache.pop(k, None)
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
