import asyncio
import re
import io
from typing import Dict, Any, List
from fastapi import FastAPI, Path
from fastapi.responses import StreamingResponse
import httpx
from bs4 import BeautifulSoup
from fpdf import FPDF
from PIL import Image

app = FastAPI(title="VIN Checker API Pro", version="2.3.0")

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"

GLOSSARY_EN = {
    "Primary Damage": "Main crash area (Front End, Rear End, Rollover, etc.).",
    "Salvage Title": "Total loss status assigned by insurance company.",
    "Rebuilt Title": "Vehicle was salvaged, rebuilt and passed technical inspection.",
    "Odometer Rollback": "Inconsistency detected in mileage records.",
    "Clean Title": "No insurance total loss history recorded."
}

DAMAGE_TRANSLATIONS = {
    "FRONT END": "Удар спереди (Front End)",
    "REAR END": "Удар сзади (Rear End)",
    "SIDE": "Боковой удар (Side)",
    "ALL OVER": "Повреждения по кругу (All Over)",
    "ROLLOVER": "Переворот (Rollover)",
    "UNDERCARRIAGE": "Повреждение днища/подвески",
    "BURN": "Пожар / Горелый",
    "WATER/FLOOD": "Утопленник (Затопление)",
    "NO SEVERE DAMAGE REPORTED": "Серьёзных повреждений на аукционе не зафиксировано",
    "N/A": "Данные отсутствуют"
}

def translate_damage(text: str) -> str:
    if not text:
        return "Не указано"
    upper_text = text.upper().strip()
    return DAMAGE_TRANSLATIONS.get(upper_text, text)

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
        "primary_damage": "NO SEVERE DAMAGE REPORTED",
        "odometer_at_sale": "N/A",
        "photo_url": None,
        "photo_bytes": None
    }
    try:
        response = await client.get(url, headers={"User-Agent": USER_AGENT}, timeout=7.0)
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
                        img_res = await client.get(auction_info["photo_url"], timeout=4.0)
                        if img_res.status_code == 200:
                            img_io = io.BytesIO(img_res.content)
                            with Image.open(img_io) as img:
                                img.verify()
                            auction_info["photo_bytes"] = img_res.content
                    except Exception:
                        auction_info["photo_bytes"] = None
    except Exception:
        pass
    return auction_info

async def search_cis_deep_footprint(client: httpx.AsyncClient, vin: str) -> Dict[str, Any]:
    query = f'"{vin}" (site:avito.ru OR site:auto.ru OR site:drom.ru OR site:av.by OR site:reestr-zalogov.ru)'
    url = f"https://html.duckduckgo.com/html/?q={query}"
    cis_summary = {"records": [], "is_pledged": False, "is_taxi": False}
    try:
        response = await client.get(url, headers={"User-Agent": USER_AGENT}, timeout=7.0)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")
            snippets = soup.select(".result__body")
            for snippet in snippets[:4]:
                title_elem = snippet.select_one(".result__title")
                snippet_elem = snippet.select_one(".result__snippet")
                url_elem = snippet.select_one(".result__url")
                if snippet_elem:
                    text = snippet_elem.text
                    link = url_elem.text.strip() if url_elem else ""
                    if "reestr-zalogov" in link or "залог" in text.lower(): cis_summary["is_pledged"] = True
                    if "такси" in text.lower() or "taxi" in text.lower(): cis_summary["is_taxi"] = True
                    cis_summary["records"].append({
                        "title": title_elem.text.strip() if title_elem else "",
                        "snippet": text.strip()[:100]
                    })
    except Exception:
        pass
    return cis_summary

class PDFReport(FPDF):
    def header(self):
        self.set_fill_color(30, 41, 59)
        self.rect(0, 0, 210, 25, 'F')
        self.set_font("Helvetica", "B", 14)
        self.set_text_color(255, 255, 255)
        self.cell(0, 5, "VEHICLE ACCIDENT & HISTORY REPORT", align="L", new_x="LMARGIN", new_y="NEXT")
        self.set_font("Helvetica", "", 9)
        self.set_text_color(203, 213, 225)
        self.cell(0, 8, "Aggregated VIN Records: US Auctions & CIS Footprint", align="L", new_x="LMARGIN", new_y="NEXT")
        self.ln(8)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(148, 163, 184)
        self.cell(0, 10, f"Page {self.page_no()} | VIN Aggregator API Pro", align="C")

def generate_pdf_report(data: dict) -> io.BytesIO:
    pdf = PDFReport()
    pdf.add_page()
    
    vin = data.get("vin", "UNKNOWN")
    specs = data.get("vehicle_specs", {})
    auction = data.get("auction_details", {})
    cis = data.get("cis_details", {})
    
    # VIN Block
    pdf.set_fill_color(241, 245, 249)
    pdf.rect(10, 28, 190, 12, 'F')
    pdf.set_xy(13, 30.5)
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(15, 23, 42)
    pdf.cell(22, 6, "VIN CODE:")
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(37, 99, 235)
    pdf.cell(0, 6, vin)
    pdf.ln(12)
    
    # Section 1
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(15, 23, 42)
    pdf.cell(0, 6, "1. Auction & Damage Records", new_x="LMARGIN", new_y="NEXT")
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(4)
    
    start_y = pdf.get_y()
    
    has_img = False
    if auction.get("photo_bytes"):
        try:
            img_io = io.BytesIO(auction["photo_bytes"])
            pdf.image(img_io, x=10, y=start_y, w=60)
            has_img = True
        except Exception:
            has_img = False
            
    if not has_img:
        pdf.set_fill_color(248, 250, 252)
        pdf.rect(10, start_y, 60, 40, 'F')
        pdf.set_xy(15, start_y + 17)
        pdf.set_font("Helvetica", "I", 8)
        pdf.set_text_color(148, 163, 184)
        pdf.cell(50, 5, "No Image Available")

    pdf.set_xy(75, start_y)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(100, 116, 139)
    pdf.cell(32, 5, "Primary Damage:")
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(220, 38, 38)
    pdf.cell(0, 5, str(auction.get("primary_damage", "N/A")), new_x="LMARGIN", new_y="NEXT")
    
    pdf.set_x(75)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(100, 116, 139)
    pdf.cell(32, 5, "Odometer at Sale:")
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(15, 23, 42)
    pdf.cell(0, 5, str(auction.get("odometer_at_sale", "N/A")), new_x="LMARGIN", new_y="NEXT")

    pdf.set_x(75)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(100, 116, 139)
    pdf.cell(32, 5, "Pledge / Bank Risk:")
    pdf.set_font("Helvetica", "B", 8)
    if cis.get("is_pledged"):
        pdf.set_text_color(220, 38, 38)
        pdf.cell(0, 5, "RISK: Found in Pledge Register", new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.set_text_color(16, 185, 129)
        pdf.cell(0, 5, "CLEAN (No Pledges Found)", new_x="LMARGIN", new_y="NEXT")

    pdf.set_x(75)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(100, 116, 139)
    pdf.cell(32, 5, "Taxi Usage Check:")
    pdf.set_font("Helvetica", "B", 8)
    if cis.get("is_taxi"):
        pdf.set_text_color(220, 38, 38)
        pdf.cell(0, 5, "WARNING: Commercial / Taxi Record", new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.set_text_color(16, 185, 129)
        pdf.cell(0, 5, "CLEAN (No Taxi Licenses)", new_x="LMARGIN", new_y="NEXT")

    pdf.set_y(start_y + 44)

    # Section 2 Specs
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(15, 23, 42)
    pdf.cell(0, 6, "2. Vehicle Specifications", new_x="LMARGIN", new_y="NEXT")
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(3)
    
    specs_data = [
        [("Make", specs.get("make")), ("Model", specs.get("model"))],
        [("Year", specs.get("year")), ("Body", specs.get("body_class"))],
        [("Engine HP", specs.get("engine_hp")), ("Fuel", specs.get("fuel_type"))]
    ]
    for row in specs_data:
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_text_color(100, 116, 139)
        pdf.cell(25, 4.5, f"{row[0][0]}:")
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(15, 23, 42)
        pdf.cell(65, 4.5, str(row[0][1]))
        
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_text_color(100, 116, 139)
        pdf.cell(25, 4.5, f"{row[1][0]}:")
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(15, 23, 42)
        pdf.cell(65, 4.5, str(row[1][1]), new_x="LMARGIN", new_y="NEXT")
        
    pdf.ln(4)

    # Section 3 Glossary
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(15, 23, 42)
    pdf.cell(0, 6, "3. Terminology & Glossary", new_x="LMARGIN", new_y="NEXT")
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(3)

    for term, definition in GLOSSARY_EN.items():
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_text_color(37, 99, 235)
        pdf.cell(40, 4, f"{term}:")
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(71, 85, 105)
        pdf.cell(0, 4, definition, new_x="LMARGIN", new_y="NEXT")

    pdf_output = io.BytesIO()
    pdf_output.write(pdf.output())
    pdf_output.seek(0)
    return pdf_output

@app.get("/api/v1/vin/{vin}")
async def get_vin_history(vin: str = Path(..., min_length=17, max_length=17)):
    vin = vin.upper()
    async with httpx.AsyncClient(follow_redirects=True) as client:
        specs, auction, cis = await asyncio.gather(
            decode_vin_basic(client, vin),
            check_deep_auction_data(client, vin),
            search_cis_deep_footprint(client, vin)
        )

    auction["damage_ru"] = translate_damage(auction.get("primary_damage"))
    return {
        "status": "success",
        "vin": vin,
        "vehicle_specs": specs,
        "auction_details": {
            "found": auction["found"],
            "primary_damage": auction["primary_damage"],
            "damage_ru": auction["damage_ru"],
            "odometer_at_sale": auction["odometer_at_sale"],
            "photo_url": auction["photo_url"]
        },
        "cis_details": cis
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
        
    auction["damage_ru"] = translate_damage(auction.get("primary_damage"))
    data = {
        "vin": vin,
        "vehicle_specs": specs,
        "auction_details": auction,
        "cis_details": cis
    }
    
    try:
        pdf_stream = generate_pdf_report(data)
        headers = {"Content-Disposition": f"attachment; filename=VIN_Report_{vin}.pdf"}
        return StreamingResponse(pdf_stream, media_type="application/pdf", headers=headers)
    except Exception as e:
        print(f"PDF Generation error: {e}")
        return {"error": "Failed to generate PDF"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)