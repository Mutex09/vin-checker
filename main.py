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
    description="Профессиональный агрегатор публичных данных об авто (США, РФ, РБ)",
    version="1.2.0"
)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# ==========================================
# SCRAPERS & COLLECTORS
# ==========================================

async def decode_vin_basic(client: httpx.AsyncClient, vin: str) -> Dict[str, Any]:
    """Декодер глобальных характеристик (NHTSA)"""
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
                "manufacturer": res.get("Manufacturer") or "N/A"
            }
    except Exception:
        pass
    return {}

async def check_auction_history(client: httpx.AsyncClient, vin: str) -> List[Dict[str, Any]]:
    """Аукционы США/Европы (BidCars)"""
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
                
                records.append({
                    "source": "BidCars Archive",
                    "title": title.text.strip() if title else "N/A",
                    "odometer": odometer.text.strip() if odometer else "Unknown",
                    "damage": damage.text.strip() if damage else "Not specified"
                })
    except Exception:
        pass
    return records

async def search_ru_by_footprint(client: httpx.AsyncClient, vin: str) -> List[Dict[str, Any]]:
    """Специализированный OSINT-поиск по сайтам РФ и РБ (Avito, Auto.ru, av.by, Drom, Нотариат)"""
    query = f'"{vin}" (site:avito.ru OR site:auto.ru OR site:drom.ru OR site:av.by OR site:reestr-zalogov.ru)'
    url = f"https://html.duckduckgo.com/html/?q={query}"
    results = []
    
    try:
        response = await client.get(url, headers={"User-Agent": USER_AGENT}, timeout=7.0)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")
            snippets = soup.select(".result__body")
            for snippet in snippets[:5]:
                title_elem = snippet.select_one(".result__title")
                snippet_elem = snippet.select_one(".result__snippet")
                url_elem = snippet.select_one(".result__url")
                
                if snippet_elem:
                    text = snippet_elem.text
                    # Ищем пробег в км
                    mileage_match = re.search(r"(\d{1,3}[\s,.]?\d{3})\s*(км)", text, re.IGNORECASE)
                    # Ищем упоминания цены в рублях / $
                    price_match = re.search(r"(\d{1,3}[\s,.]?\d{3}[\s,.]?\d{3})\s*(руб|рублей|BYN|\$)", text, re.IGNORECASE)
                    
                    link_text = url_elem.text.strip() if url_elem else ""
                    
                    # Определение источника
                    region = "СНГ (Общее)"
                    if "av.by" in link_text:
                        region = "Беларусь (av.by)"
                    elif "avito.ru" in link_text:
                        region = "Россия (Авито)"
                    elif "auto.ru" in link_text:
                        region = "Россия (Auto.ru)"
                    elif "reestr-zalogov.ru" in link_text:
                        region = "РФ (Реестр Залогов)"
                        
                    results.append({
                        "source_region": region,
                        "title": title_elem.text.strip() if title_elem else "",
                        "snippet": text.strip(),
                        "url": link_text,
                        "extracted_mileage": mileage_match.group(0) if mileage_match else None,
                        "extracted_price": price_match.group(0) if price_match else None
                    })
    except Exception:
        pass
    return results

# ==========================================
# ADVANCED PDF GENERATOR (WITH RU/BY SECTIONS)
# ==========================================

class PDFReport(FPDF):
    def header(self):
        self.set_fill_color(30, 41, 59)
        self.rect(0, 0, 210, 25, 'F')
        
        self.set_font("Helvetica", "B", 15)
        self.set_text_color(255, 255, 255)
        self.cell(0, 5, "VEHICLE HISTORY REPORT (RU / BY / GLOBAL)", align="L", new_x="LMARGIN", new_y="NEXT")
        self.set_font("Helvetica", "", 9)
        self.set_text_color(203, 213, 225)
        self.cell(0, 8, "Aggregated Data from CIS Databases & Global Archives", align="L", new_x="LMARGIN", new_y="NEXT")
        self.ln(10)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(148, 163, 184)
        self.cell(0, 10, f"Page {self.page_no()} | Free VIN History Aggregator API", align="C")

def generate_pdf_report(data: dict) -> io.BytesIO:
    pdf = PDFReport()
    pdf.add_page()
    
    vin = data.get("vin", "UNKNOWN")
    specs = data.get("vehicle_specs", {})
    auctions = data.get("history", {}).get("auction_records", [])
    cis_data = data.get("history", {}).get("cis_records", [])
    
    # 1. VIN HEADER BLOCK
    pdf.set_fill_color(241, 245, 249)
    pdf.rect(10, 30, 190, 15, 'F')
    pdf.set_xy(15, 33)
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(15, 23, 42)
    pdf.cell(25, 8, "VIN CODE:")
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_text_color(37, 99, 235)
    pdf.cell(0, 8, vin)
    pdf.ln(15)
    
    # 2. SPECIFICATIONS
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(15, 23, 42)
    pdf.cell(0, 7, "1. Vehicle Specifications", new_x="LMARGIN", new_y="NEXT")
    pdf.set_draw_color(226, 232, 240)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(3)
    
    table_data = [
        [("Make", specs.get("make")), ("Model", specs.get("model"))],
        [("Year", specs.get("year")), ("Body Class", specs.get("body_class"))],
        [("Engine HP", specs.get("engine_hp")), ("Fuel Type", specs.get("fuel_type"))],
        [("Drive Type", specs.get("drive_type")), ("Country", specs.get("plant_country"))]
    ]
    
    pdf.set_font("Helvetica", "", 9)
    for row in table_data:
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_text_color(100, 116, 139)
        pdf.cell(30, 5, f"{row[0][0]}:", border=0)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(15, 23, 42)
        pdf.cell(65, 5, str(row[0][1]), border=0)
        
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_text_color(100, 116, 139)
        pdf.cell(30, 5, f"{row[1][0]}:", border=0)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(15, 23, 42)
        pdf.cell(65, 5, str(row[1][1]), border=0, new_x="LMARGIN", new_y="NEXT")
        
    pdf.ln(4)
    
    # 3. CIS HISTORY (РФ / РБ БЛОК)
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(15, 23, 42)
    pdf.cell(0, 7, "2. CIS Web History & Board Records (RU / BY)", new_x="LMARGIN", new_y="NEXT")
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(4)
    
    if cis_data:
        for idx, item in enumerate(cis_data[:4], 1):
            pdf.set_font("Helvetica", "B", 9)
            pdf.set_text_color(37, 99, 235)
            pdf.cell(0, 4, f"[{item.get('source_region')}] {item.get('title')[:75]}", new_x="LMARGIN", new_y="NEXT")
            
            pdf.set_font("Helvetica", "", 8)
            pdf.set_text_color(71, 85, 105)
            snippet = item.get('snippet', '').replace('\n', ' ')[:110]
            pdf.cell(0, 4, f"   Snippet: {snippet}...", new_x="LMARGIN", new_y="NEXT")
            
            # Если вытащили цену или пробег из объявлений РФ/РБ
            details = []
            if item.get('extracted_mileage'):
                details.append(f"Mileage: {item.get('extracted_mileage')}")
            if item.get('extracted_price'):
                details.append(f"Price: {item.get('extracted_price')}")
                
            if details:
                pdf.set_font("Helvetica", "B", 8)
                pdf.set_text_color(16, 185, 129) # Зеленый акцент
                pdf.cell(0, 4, f"   Found Details: {' | '.join(details)}", new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)
    else:
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_text_color(100, 116, 139)
        pdf.cell(0, 5, "No active archive records found in RU/BY classifieds (Avito, Auto.ru, av.by).", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(4)

    # 4. AUCTION & IMPORT HISTORY
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(15, 23, 42)
    pdf.cell(0, 7, "3. Import & Copart/IAAI Auction Records", new_x="LMARGIN", new_y="NEXT")
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(4)
    
    if auctions:
        pdf.set_fill_color(248, 250, 252)
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_text_color(71, 85, 105)
        pdf.cell(45, 6, "Source", border=1, fill=True)
        pdf.cell(75, 6, "Lot Title", border=1, fill=True)
        pdf.cell(35, 6, "Odometer", border=1, fill=True)
        pdf.cell(35, 6, "Damage", border=1, fill=True, new_x="LMARGIN", new_y="NEXT")
        
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(15, 23, 42)
        for item in auctions:
            pdf.cell(45, 5, str(item.get("source"))[:22], border=1)
            pdf.cell(75, 5, str(item.get("title"))[:40], border=1)
            pdf.cell(35, 5, str(item.get("odometer")), border=1)
            pdf.cell(35, 5, str(item.get("damage"))[:18], border=1, new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_text_color(100, 116, 139)
        pdf.cell(0, 5, "No US/EU copart auction records found for this VIN.", new_x="LMARGIN", new_y="NEXT")

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
        specs, auctions, cis_data = await asyncio.gather(
            decode_vin_basic(client, vin),
            check_auction_history(client, vin),
            search_ru_by_footprint(client, vin)
        )

    return {
        "status": "success",
        "vin": vin,
        "vehicle_specs": specs,
        "history": {
            "cis_records": cis_data,
            "auction_records": auctions
        },
        "meta": {
            "sources_checked": ["NHTSA", "BidCars Archives", "Avito Archive", "Auto.ru Archive", "av.by Belarus"],
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