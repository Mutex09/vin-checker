import asyncio
import re
import io
from typing import Dict, Any, List
from fastapi import FastAPI, Path
from fastapi.responses import StreamingResponse
import httpx
from bs4 import BeautifulSoup
from fpdf import FPDF

app = FastAPI(
    title="Free VIN History Aggregator API",
    description="Продвинутый агрегатор автоистории с расшифровкой терминов и глубоким СНГ-поиском",
    version="2.1.0"
)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# ==========================================
# GLOASSARY / SLANG DICTIONARY
# ==========================================

GLOSSARY = {
    "Salvage Title": "Автомобиль признан тотальным (ремонт превышал стоимость авто). Был списан страховой.",
    "Clean Title": "Юридически чистый документ. Машина не списывалась страховой компанией в тоталь.",
    "Rebuilt / Prior Salvage": "Автомобиль был в тотальном ДТП, но затем официально восстановлен и пройден техосмотр.",
    "Primary Damage": "Основной вид повреждения (например, Front End - фронтальный удар, Rollover - переворот).",
    "Run & Drive": "Двигатель заводится, и машина способна самостоятельно передвигаться (хотя бы на пару метров).",
    "Odometer Rollback": "Обнаружено скручивание пробега (несоответствие текущих показаний с предыдущими).",
    "Pledge / Encumbrance": "Наличие авто в реестре залогов (машина может быть в кредите у банка)."
}

# ==========================================
# ADVANCED SCRAPERS
# ==========================================

async def decode_vin_basic(client: httpx.AsyncClient, vin: str) -> Dict[str, Any]:
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
                "displacement_l": res.get("DisplacementL") or "N/A",
                "fuel_type": res.get("FuelTypePrimary") or "N/A",
                "plant_country": res.get("PlantCountry") or "N/A"
            }
    except Exception:
        pass
    return {}

async def check_deep_auction_data(client: httpx.AsyncClient, vin: str) -> Dict[str, Any]:
    url = f"https://bid.cars/en/search/vin/{vin}"
    auction_info = {
        "found": False,
        "lot_number": "N/A",
        "primary_damage": "No severe damage reported",
        "doc_type": "Clean / Standard",
        "odometer_at_sale": "N/A",
        "photo_url": None,
        "photo_bytes": None
    }
    try:
        response = await client.get(url, headers={"User-Agent": USER_AGENT}, timeout=8.0)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")
            card = soup.select_one(".auction-item")
            if card:
                auction_info["found"] = True
                title_elem = card.select_one(".title")
                odo_elem = card.select_one(".odometer")
                dmg_elem = card.select_one(".damage")
                img_elem = card.select_one("img")
                
                if title_elem: auction_info["lot_number"] = title_elem.text.strip()
                if odo_elem: auction_info["odometer_at_sale"] = odo_elem.text.strip()
                if dmg_elem: auction_info["primary_damage"] = dmg_elem.text.strip()
                if img_elem and img_elem.get("src"):
                    auction_info["photo_url"] = img_elem.get("src")
                    try:
                        img_res = await client.get(auction_info["photo_url"], timeout=5.0)
                        if img_res.status_code == 200:
                            auction_info["photo_bytes"] = img_res.content
                    except Exception:
                        pass
    except Exception:
        pass
    return auction_info

async def search_cis_deep_footprint(client: httpx.AsyncClient, vin: str) -> Dict[str, Any]:
    """Глубокий поиск по расширенным реестрам СНГ (Авито, av.by, Залоги, Такси, ОСАГО, Дром)"""
    query = f'"{vin}" (site:avito.ru OR site:auto.ru OR site:drom.ru OR site:av.by OR site:reestr-zalogov.ru OR site:mos.ru OR site:rsa.su)'
    url = f"https://html.duckduckgo.com/html/?q={query}"
    
    cis_summary = {
        "records": [],
        "is_pledged": False,
        "is_taxi": False,
        "found_listings_count": 0
    }
    
    try:
        response = await client.get(url, headers={"User-Agent": USER_AGENT}, timeout=8.0)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")
            snippets = soup.select(".result__body")
            
            for snippet in snippets[:5]:
                title_elem = snippet.select_one(".result__title")
                snippet_elem = snippet.select_one(".result__snippet")
                url_elem = snippet.select_one(".result__url")
                
                if snippet_elem:
                    text = snippet_elem.text
                    link_text = url_elem.text.strip() if url_elem else ""
                    
                    # Детекторы спец-статусов
                    if "reestr-zalogov" in link_text or "залог" in text.lower():
                        cis_summary["is_pledged"] = True
                    if "такси" in text.lower() or "taxi" in text.lower() or "лицензия" in text.lower():
                        cis_summary["is_taxi"] = True
                        
                    mileage_match = re.search(r"(\d{1,3}[\s,.]?\d{3})\s*(км)", text, re.IGNORECASE)
                    price_match = re.search(r"(\d{1,3}[\s,.]?\d{3}[\s,.]?\d{3})\s*(руб|рублей|BYN|\$)", text, re.IGNORECASE)
                    
                    region = "СНГ (Общее)"
                    if "av.by" in link_text: region = "Беларусь (av.by)"
                    elif "avito.ru" in link_text: region = "Россия (Авито)"
                    elif "auto.ru" in link_text: region = "Россия (Auto.ru)"
                    elif "reestr-zalogov.ru" in link_text: region = "РФ (Нотариат / Залог)"
                    
                    cis_summary["records"].append({
                        "region": region,
                        "title": title_elem.text.strip() if title_elem else "",
                        "snippet": text.strip()[:120],
                        "mileage": mileage_match.group(0) if mileage_match else None,
                        "price": price_match.group(0) if price_match else None
                    })
            
            cis_summary["found_listings_count"] = len(cis_summary["records"])
    except Exception:
        pass
        
    return cis_summary

# ==========================================
# ADVANCED PDF GENERATOR
# ==========================================

class PDFReport(FPDF):
    def header(self):
        self.set_fill_color(15, 23, 42)
        self.rect(0, 0, 210, 25, 'F')
        self.set_font("Helvetica", "B", 15)
        self.set_text_color(255, 255, 255)
        self.cell(0, 5, "EXPERT VEHICLE & HISTORY REPORT", align="L", new_x="LMARGIN", new_y="NEXT")
        self.set_font("Helvetica", "", 9)
        self.set_text_color(203, 213, 225)
        self.cell(0, 8, "Global Auctions | CIS Registers | Terms Decoded", align="L", new_x="LMARGIN", new_y="NEXT")
        self.ln(10)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(148, 163, 184)
        self.cell(0, 10, f"Page {self.page_no()} | VIN Aggregator API v2.1", align="C")

def generate_pdf_report(data: dict) -> io.BytesIO:
    pdf = PDFReport()
    pdf.add_page()
    
    vin = data.get("vin", "UNKNOWN")
    specs = data.get("vehicle_specs", {})
    auction = data.get("auction_details", {})
    cis = data.get("cis_details", {})
    
    # 1. VIN HEADER
    pdf.set_fill_color(241, 245, 249)
    pdf.rect(10, 28, 190, 12, 'F')
    pdf.set_xy(13, 30)
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(15, 23, 42)
    pdf.cell(25, 7, "VIN CODE:")
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_text_color(37, 99, 235)
    pdf.cell(0, 7, vin)
    pdf.ln(12)
    
    # 2. AUCTION & DAMAGE INSPECTION
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(15, 23, 42)
    pdf.cell(0, 7, "1. Auction History & Accident Inspection", new_x="LMARGIN", new_y="NEXT")
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(4)
    
    start_y = pdf.get_y()
    
    if auction.get("photo_bytes"):
        try:
            img_io = io.BytesIO(auction["photo_bytes"])
            pdf.image(img_io, x=10, y=start_y, w=65)
        except Exception:
            pdf.rect(10, start_y, 65, 45)
    else:
        pdf.set_fill_color(248, 250, 252)
        pdf.rect(10, start_y, 65, 45, 'F')
        pdf.set_xy(15, start_y + 20)
        pdf.set_font("Helvetica", "I", 8)
        pdf.set_text_color(100, 116, 139)
        pdf.cell(50, 5, "No Auction Photos")

    pdf.set_xy(80, start_y)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(100, 116, 139)
    pdf.cell(35, 5, "Primary Damage:", border=0)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(220, 38, 38)
    pdf.cell(0, 5, str(auction.get("primary_damage")), new_x="LMARGIN", new_y="NEXT")
    
    pdf.set_x(80)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(100, 116, 139)
    pdf.cell(35, 5, "Odometer at Sale:", border=0)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(15, 23, 42)
    pdf.cell(0, 5, str(auction.get("odometer_at_sale")), new_x="LMARGIN", new_y="NEXT")

    pdf.set_x(80)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(100, 116, 139)
    pdf.cell(35, 5, "Pledge / Bank Risk:", border=0)
    pdf.set_font("Helvetica", "B", 9)
    if cis.get("is_pledged"):
        pdf.set_text_color(220, 38, 38)
        pdf.cell(0, 5, "ATTENTION: Found in Pledge Register!", new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.set_text_color(16, 185, 129)
        pdf.cell(0, 5, "No active pledge records found", new_x="LMARGIN", new_y="NEXT")

    pdf.set_x(80)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(100, 116, 139)
    pdf.cell(35, 5, "Taxi Usage Check:", border=0)
    pdf.set_font("Helvetica", "B", 9)
    if cis.get("is_taxi"):
        pdf.set_text_color(220, 38, 38)
        pdf.cell(0, 5, "WARNING: Commercial / Taxi footprint", new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.set_text_color(16, 185, 129)
        pdf.cell(0, 5, "No Taxi License records", new_x="LMARGIN", new_y="NEXT")

    pdf.set_y(start_y + 50)

    # 3. SPECIFICATIONS
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(15, 23, 42)
    pdf.cell(0, 7, "2. Technical Specifications", new_x="LMARGIN", new_y="NEXT")
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(3)
    
    table_data = [
        [("Make", specs.get("make")), ("Model", specs.get("model"))],
        [("Year", specs.get("year")), ("Body Class", specs.get("body_class"))],
        [("Engine HP", specs.get("engine_hp")), ("Fuel Type", specs.get("fuel_type"))]
    ]
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

    # 4. GLOSSARY / RENDER ABBREVIATIONS
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(15, 23, 42)
    pdf.cell(0, 7, "3. Terms & Glossary (Расшифровка аббревиатур)", new_x="LMARGIN", new_y="NEXT")
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(3)

    pdf.set_fill_color(248, 250, 252)
    for term, definition in GLOSSARY.items():
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_text_color(37, 99, 235)
        pdf.cell(45, 4, f"{term}:", border=0)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(51, 65, 85)
        pdf.cell(0, 4, definition, border=0, new_x="LMARGIN", new_y="NEXT")

    pdf_output = io.BytesIO()
    pdf_output.write(pdf.output())
    pdf_output.seek(0)
    return pdf_output

# ==========================================
# ENDPOINTS
# ==========================================

@app.get("/api/v1/vin/{vin}")
async def get_vin_history(vin: str = Path(..., min_length=17, max_length=17)):
    vin = vin.upper()
    async with httpx.AsyncClient(follow_redirects=True) as client:
        specs, auction, cis = await asyncio.gather(
            decode_vin_basic(client, vin),
            check_deep_auction_data(client, vin),
            search_cis_deep_footprint(client, vin)
        )

    return {
        "status": "success",
        "vin": vin,
        "vehicle_specs": specs,
        "auction_details": {
            "found": auction["found"],
            "lot_number": auction["lot_number"],
            "primary_damage": auction["primary_damage"],
            "odometer_at_sale": auction["odometer_at_sale"],
            "photo_url": auction["photo_url"]
        },
        "cis_details": cis,
        "glossary": GLOSSARY
    }

@app.get("/api/v1/vin/{vin}/pdf")
async def get_vin_history_pdf(vin: str = Path(..., min_length=17, max_length=17)):
    vin = vin.upper()
    async with httpx.AsyncClient(follow_redirects=True) as client:
        specs, auction, cis = await asyncio.gather(
            decode_vin_basic(client, vin),
            check_deep_auction_data(client, vin),
            search_cis_deep_footprint(client, vin)
        )
        
    data = {
        "vin": vin,
        "vehicle_specs": specs,
        "auction_details": auction,
        "cis_details": cis
    }
    
    pdf_stream = generate_pdf_report(data)
    headers = {"Content-Disposition": f"attachment; filename=VIN_Report_{vin}.pdf"}
    return StreamingResponse(pdf_stream, media_type="application/pdf", headers=headers)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)