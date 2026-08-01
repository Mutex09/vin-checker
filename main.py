import asyncio
import re
import io
from typing import Dict, Any, List
from fastapi import FastAPI, HTTPException, Path
from fastapi.responses import StreamingResponse
import httpx
from bs4 import BeautifulSoup
from fpdf import FPDF

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
# PDF GENERATOR
# ==========================================

def generate_pdf_report(data: dict) -> io.BytesIO:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    
    # Заголовок
    pdf.cell(0, 10, f"VIN Vehicle History Report", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_font("Helvetica", "", 12)
    pdf.cell(0, 10, f"VIN Code: {data['vin']}", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(5)
    
    # Спецификация
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 8, "Vehicle Specifications", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)
    
    specs = data.get("vehicle_specs", {})
    pdf.cell(0, 6, f"Make: {specs.get('make', 'N/A')}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, f"Model: {specs.get('model', 'N/A')}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, f"Year: {specs.get('year', 'N/A')}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, f"Engine: {specs.get('engine', 'N/A')}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)
    
    # История пробегов
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 8, "Mileage Records", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)
    
    timeline = data.get("history", {}).get("mileage_timeline", [])
    if timeline:
        for record in timeline:
            pdf.cell(0, 6, f"- {record['source']}: {record['value']}", new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.cell(0, 6, "No mileage records found in open sources.", new_x="LMARGIN", new_y="NEXT")
        
    pdf.ln(10)
    pdf.set_font("Helvetica", "I", 9)
    pdf.cell(0, 5, "Generated automatically by Free VIN History Aggregator API", new_x="LMARGIN", new_y="NEXT", align="C")
    
    # Возвращаем файл в виде потока байт
    pdf_output = io.BytesIO()
    pdf_output.write(pdf.output())
    pdf_output.seek(0)
    return pdf_output

# ==========================================
# ENDPOINTS
# ==========================================

@app.get("/api/v1/vin/{vin}")
async def get_vin_history(
    vin: str = Path(..., min_length=17, max_length=17, description="17-значный VIN-код")
):
    vin = vin.upper()
    
    async with httpx.AsyncClient(follow_redirects=True) as client:
        specs, auctions, footprint = await asyncio.gather(
            decode_vin_basic(client, vin),
            check_auction_history(client, vin),
            search_public_footprint(client, vin)
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

@app.get("/api/v1/vin/{vin}/pdf")
async def get_vin_history_pdf(
    vin: str = Path(..., min_length=17, max_length=17, description="17-значный VIN-код")
):
    """Генерация и скачивание PDF-отчета"""
    data = await get_vin_history(vin)
    pdf_stream = generate_pdf_report(data)
    
    headers = {
        "Content-Disposition": f"attachment; filename=VIN_Report_{vin}.pdf"
    }
    return StreamingResponse(pdf_stream, media_type="application/pdf", headers=headers)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)