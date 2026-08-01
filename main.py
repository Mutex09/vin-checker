import asyncio
import re
from typing import Dict, Any, List
from fastapi import FastAPI, HTTPException, Query, Path
import httpx
from bs4 import BeautifulSoup

app = FastAPI(
    title="Free VIN History Aggregator API",
    description="Освобожденный от платных подписок агрегатор публичных данных об авто",
    version="1.0.0"
)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# ==========================================
# SCRAPERS & COLLECTORS
# ==========================================

async def decode_vin_basic(client: httpx.AsyncClient, vin: str) -> Dict[str, Any]:
    """Декодирование базовых характеристик через публичный API NHTSA"""
    url = f"https://vpic.nhtsa.dot.gov/api/vehicles/decodevinvalues/{vin}?format=json"
    try:
        response = await client.get(url, timeout=5.0)
        if response.status_code == 200:
            results = response.json().get("Results", [{}])[0]
            return {
                "make": results.get("Make"),
                "model": results.get("Model"),
                "year": results.get("ModelYear"),
                "engine": f"{results.get('DisplacementL', '')}L {results.get('EngineConfiguration', '')}",
                "country": results.get("PlantCountry")
            }
    except Exception:
        pass
    return {}

async def check_auction_history(client: httpx.AsyncClient, vin: str) -> List[Dict[str, Any]]:
    """Поиск истории в открытых архивах США/Европы (BidCars)"""
    url = f"https://bid.cars/en/search/vin/{vin}"
    records = []
    try:
        response = await client.get(url, headers={"User-Agent": USER_AGENT}, timeout=7.0)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")
            cards = soup.select(".auction-item") or []
            for card in cards[:3]:
                title = card.select_one(".title")
                odometer = card.select_one(".odometer")
                damage = card.select_one(".damage")
                img = card.select_one("img")
                
                records.append({
                    "source": "BidCars Archive",
                    "title": title.text.strip() if title else "N/A",
                    "odometer": odometer.text.strip() if odometer else "Unknown",
                    "damage": damage.text.strip() if damage else "Not specified",
                    "photo": img.get("src") if img else None
                })
    except Exception:
        pass
    return records

async def search_public_footprint(client: httpx.AsyncClient, vin: str) -> List[Dict[str, str]]:
    """Поиск упоминаний VIN в открытом индексе DuckDuckGo"""
    url = f"https://html.duckduckgo.com/html/?q=\"{vin}\""
    results = []
    try:
        response = await client.get(url, headers={"User-Agent": USER_AGENT}, timeout=6.0)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")
            snippets = soup.select(".result__body")
            for snippet in snippets[:5]:
                title_elem = snippet.select_one(".result__title")
                snippet_elem = snippet.select_one(".result__snippet")
                url_elem = snippet.select_one(".result__url")
                
                if snippet_elem:
                    text = snippet_elem.text
                    mileage_match = re.search(r"(\d{1,3}[\s,.]?\d{3})\s*(км|km)", text, re.IGNORECASE)
                    
                    results.append({
                        "title": title_elem.text.strip() if title_elem else "",
                        "snippet": text.strip(),
                        "url": url_elem.text.strip() if url_elem else "",
                        "extracted_mileage": mileage_match.group(0) if mileage_match else None
                    })
    except Exception:
        pass
    return results

# ==========================================
# AGGREGATION CORE
# ==========================================

@app.get("/api/v1/vin/{vin}")
async def get_vin_history(
    vin: str = Path(..., min_length=17, max_length=17, description="17-значный VIN-код")
):
    vin = vin.upper()
    
    async with httpx.AsyncClient(follow_redirects=True) as client:
        specs_task = decode_vin_basic(client, vin)
        auction_task = check_auction_history(client, vin)
        footprint_task = search_public_footprint(client, vin)
        
        specs, auctions, footprint = await asyncio.gather(
            specs_task, auction_task, footprint_task
        )
        
    mileage_timeline = []
    
    for item in auctions:
        if item.get("odometer") and item["odometer"] != "Unknown":
            mileage_timeline.append({"source": item["source"], "value": item["odometer"]})
            
    for item in footprint:
        if item.get("extracted_mileage"):
            mileage_timeline.append({"source": item["url"], "value": item["extracted_mileage"]})

    return {
        "status": "success",
        "vin": vin,
        "vehicle_specs": specs,
        "history": {
            "auction_records": auctions,
            "web_footprint": footprint,
            "mileage_timeline": mileage_timeline
        },
        "meta": {
            "sources_checked": ["NHTSA Public API", "BidCars Archives", "Search Engine Footprint"],
            "is_free_report": True
        }
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
