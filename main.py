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
    description="Профессиональный агрегатор публичных данных об авто",
    version="1.1.0"
)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# ==========================================
# SCRAPERS & COLLECTORS (EXPANDED)
# ==========================================

async def decode_vin_basic(client: httpx.AsyncClient, vin: str) -> Dict[str, Any]:
    """Глубокая расшифровка VIN через API NHTSA (собираем максимальное число полей)"""
    url = f"https://vpic.nhtsa.dot.gov/api/vehicles/decodevinvalues/{vin}?format=json"
    try:
        response = await client.get(url, timeout=5.0)
        if response.status_code == 200:
            res = response.json().get("Results", [{}])[0]
            return {
                "make": res.get("Make") or "N/A",
                "model": res.get("Model") or "N/A",
                "year": res.get("ModelYear") or "N/A",
                "body_class": res.get("BodyClass") or "N/A",
                "drive_type": res.get("DriveType") or "N/A",
                "engine_hp": res.get("EngineHP") or "N/A",
                "engine_cylinders": res.get("EngineCylinders") or "N/A",
                "displacement_l": res.get("DisplacementL") or "N/A",
                "fuel_type": res.get("FuelTypePrimary") or "N/A",
                "transmission": res.get("TransmissionStyle") or "N/A",
                "plant_country": res.get("PlantCountry") or "N/A",
                "plant_city": res.get("PlantCity") or "N/A",
                "vehicle_type": res.get("VehicleType") or "N/A",
                "manufacturer": res.get("Manufacturer") or "N/A"
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
            for card in cards[:5]:
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
# PROFESSIONAL PDF GENERATOR
# ==========================================

class PDFReport(FPDF):
    def header(self):
        # Верхняя шапка документа
        self.set_fill_color(30, 41, 59) # Темно-синий/серый цвет (Slate 800)
        self.rect(0, 0, 210, 25, 'F')
        
        self.set_font("Helvetica", "B", 16)
        self.set_text_color(255, 255, 255)
        self.cell(0, 5, "VEHICLE HISTORY REPORT", align="L", new_x="LMARGIN", new_y="NEXT")
        self.set_font("Helvetica", "", 10)
        self.set_text_color(203, 213, 225)
        self.cell(0, 8, "OSINT Aggregated Data Sheet", align="L", new_x="LMARGIN", new_y="NEXT")
        self.ln(10)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(148, 163, 184)
        self.cell(0, 10, f"Page {self.page_no()} | Generated automatically by Free VIN History Aggregator API", align="C")

def generate_pdf_report(data: dict) -> io.BytesIO:
    pdf = PDFReport()
    pdf.add_page()
    
    vin = data.get("vin", "UNKNOWN")
    specs = data.get("vehicle_specs", {})
    auctions = data.get("history", {}).get("auction_records", [])
    footprint = data.get("history", {}).get("web_footprint", [])
    timeline = data.get("history", {}).get("mileage_timeline", [])
    
    # 1. VIN BLOCK
    pdf.set_fill_color(241, 245, 249) # Светло-серый фон
    pdf.rect(10, 30, 190, 15, 'F')
    pdf.set_xy(15, 33)
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(15, 23, 42)
    pdf.cell(30, 8, "VIN CODE:")
    pdf.set_font("Helvetica", "B", 14)
    pdf.set_text_color(37, 99, 235) # Синий акцент
    pdf.cell(0, 8, vin)
    pdf.ln(15)
    
    # 2. VEHICLE SPECIFICATIONS (ТАБЛИЦА СЕТКОЙ)
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(15, 23, 42)
    pdf.cell(0, 8, "Vehicle Specifications", new_x="LMARGIN", new_y="NEXT")
    pdf.set_draw_color(226, 232, 240)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(3)
    
    table_data = [
        [("Make", specs.get("make")), ("Model", specs.get("model"))],
        [("Year", specs.get("year")), ("Body Class", specs.get("body_class"))],
        [("Engine Cylinders", specs.get("engine_cylinders")), ("Displacement (L)", specs.get("displacement_l"))],
        [("Horsepower (HP)", specs.get("engine_hp")), ("Fuel Type", specs.get("fuel_type"))],
        [("Drive Type", specs.get("drive_type")), ("Transmission", specs.get("transmission"))],
        [("Plant Country", specs.get("plant_country")), ("Manufacturer", specs.get("manufacturer"))]
    ]
    
    pdf.set_font("Helvetica", "", 10)
    for row in table_data:
        # Левая колонка
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(100, 116, 139)
        pdf.cell(35, 6, f"{row[0][0]}:", border=0)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(15, 23, 42)
        pdf.cell(60, 6, str(row[0][1]), border=0)
        
        # Правая колонка
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(100, 116, 139)
        pdf.cell(35, 6, f"{row[1][0]}:", border=0)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(15, 23, 42)
        pdf.cell(60, 6, str(row[1][1]), border=0, new_x="LMARGIN", new_y="NEXT")
        
    pdf.ln(6)
    
    # 3. MILEAGE & AUCTION RECORDS
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(15, 23, 42)
    pdf.cell(0, 8, "Mileage & Auction History", new_x="LMARGIN", new_y="NEXT")
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(4)
    
    if auctions:
        # Шапка таблицы
        pdf.set_fill_color(248, 250, 252)
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(71, 85, 105)
        pdf.cell(45, 7, "Source", border=1, fill=True)
        pdf.cell(75, 7, "Title / Lot", border=1, fill=True)
        pdf.cell(35, 7, "Odometer", border=1, fill=True)
        pdf.cell(35, 7, "Damage", border=1, fill=True, new_x="LMARGIN", new_y="NEXT")
        
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(15, 23, 42)
        for item in auctions:
            pdf.cell(45, 6, str(item.get("source"))[:22], border=1)
            pdf.cell(75, 6, str(item.get("title"))[:40], border=1)
            pdf.cell(35, 6, str(item.get("odometer")), border=1)
            pdf.cell(35, 6, str(item.get("damage"))[:18], border=1, new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.set_font("Helvetica", "I", 10)
        pdf.set_text_color(100, 116, 139)
        pdf.cell(0, 6, "No direct auction records found in primary archives.", new_x="LMARGIN", new_y="NEXT")
        
    pdf.ln(6)
    
    # 4. WEB FOOTPRINT (Опечатки / Поисковые упоминания)
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(15, 23, 42)
    pdf.cell(0, 8, "Web Footprint & Mentions", new_x="LMARGIN", new_y="NEXT")
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(4)
    
    if footprint:
        pdf.set_font("Helvetica", "", 9)
        for idx, item in enumerate(footprint[:3], 1):
            pdf.set_font("Helvetica", "B", 9)
            pdf.set_text_color(37, 99, 235)
            pdf.cell(0, 5, f"{idx}. {item.get('title')[:80]}", new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Helvetica", "", 8)
            pdf.set_text_color(71, 85, 105)
            snippet = item.get('snippet', '').replace('\n', ' ')[:120]
            pdf.cell(0, 4, f"   Snippet: {snippet}...", new_x="LMARGIN", new_y="NEXT")
            if item.get('extracted_mileage'):
                pdf.set_font("Helvetica", "B", 8)
                pdf.set_text_color(16, 185, 129) # Зеленый цвет для найденного пробега
                pdf.cell(0, 4, f"   Extracted Mileage: {item.get('extracted_mileage')}", new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)
    else:
        pdf.set_font("Helvetica", "I", 10)
        pdf.set_text_color(100, 116, 139)
        pdf.cell(0, 6, "No public web mentions detected.", new_x="LMARGIN", new_y="NEXT")

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
    data = await get_vin_history(vin)
    pdf_stream = generate_pdf_report(data)
    
    headers = {
        "Content-Disposition": f"attachment; filename=VIN_Report_{vin}.pdf"
    }
    return StreamingResponse(pdf_stream, media_type="application/pdf", headers=headers)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)