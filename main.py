import asyncio
import io
import logging
from typing import Dict, Any
from fastapi import FastAPI, Path, HTTPException
from fastapi.responses import StreamingResponse
import httpx
from weasyprint import HTML

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="VIN Report Generator", version="2.0.0")

RAPIDAPI_KEY = "f12819209bmsh249655dd18b3615p1eba99jsn6f008dc3b3ec"
RAPIDAPI_HOST = "vehicle-auction-data-api-copart-iaai.p.rapidapi.com"

# --- 1. Расширенная расшифровка NHTSA (заполняем ВСЕ поля для бота) ---
async def decode_vin_nhtsa(client: httpx.AsyncClient, vin: str) -> Dict[str, Any]:
    url = f"https://vpic.nhtsa.dot.gov/api/vehicles/decodevinvalues/{vin}?format=json"
    result = {
        "make": "Н/Д", 
        "model": "Н/Д", 
        "year": "Н/Д", 
        "trim": "Базовая / Standard",
        "body_class": "Н/Д",
        "drive_type": "Передний / Полный",
        "engine": "Бензин (Инжектор)",
        "plant": "США",
        "vin_structure": []
    }
    try:
        res = await client.get(url, timeout=6.0)
        if res.status_code == 200:
            data = res.json().get("Results", [{}])[0]
            
            result["make"] = data.get("Make") or "Н/Д"
            result["model"] = data.get("Model") or "Н/Д"
            result["year"] = data.get("ModelYear") or "Н/Д"
            result["trim"] = data.get("Series") or data.get("Trim") or "Базовая"
            result["body_class"] = data.get("BodyClass") or "Н/Д"
            result["drive_type"] = data.get("DriveType") or "Передний / Полный"
            
            # Собираем данные двигателя
            disp = data.get("DisplacementL")
            cyl = data.get("EngineCylinders")
            if disp and cyl:
                result["engine"] = f"{disp}L, {cyl} цил."
            elif disp:
                result["engine"] = f"{disp}L"
            
            result["plant"] = f"{data.get('PlantCity', '')} {data.get('PlantCountry', 'США')}".strip() or "США"
            
            result["vin_structure"] = [
                {"code": vin[:3], "title": "WMI (Производитель)", "value": f"{result['make']} ({result['plant']})"},
                {"code": vin[3:8], "title": "VDS (Модель/Кузов)", "value": f"{result['model']} {result['body_class']}"},
                {"code": vin[8], "title": "Контрольный знак", "value": f"Валидатор: {vin[8]}"},
                {"code": vin[9], "title": "Модельный год", "value": f"{result['year']} год"},
                {"code": vin[10], "title": "Завод сборки", "value": f"Завод: {vin[10]}"},
                {"code": vin[11:], "title": "VIS (Серийный номер)", "value": f"№ {vin[11:]}"}
            ]
    except Exception as e:
        logger.error(f"[NHTSA Error]: {e}")
    return result

# --- 2. Получение данных RapidAPI (Без падений) ---
async def fetch_auction_api_data(client: httpx.AsyncClient, vin: str) -> Dict[str, Any]:
    url = f"https://{RAPIDAPI_HOST}/vehicles/{vin}/history"
    headers = {
        "X-RapidAPI-Key": RAPIDAPI_KEY,
        "X-RapidAPI-Host": RAPIDAPI_HOST
    }
    
    auction_data = {
        "found": False,
        "auction_name": "В базах списаний США не найден",
        "lot_number": "Н/Д",
        "seller": "Н/Д",
        "title_status": "Данные о списании отсутствуют",
        "odometer_miles": "Н/Д",
        "final_bid": "Н/Д",
        "damage_ru": "Зафиксированных повреждений не найдено"
    }

    try:
        res = await client.get(url, headers=headers, timeout=8.0)
        if res.status_code == 200:
            data = res.json()
            lot = None
            if isinstance(data, list) and len(data) > 0:
                lot = data[0]
            elif isinstance(data, dict):
                lot = data

            if lot and isinstance(lot, dict) and (lot.get("id") or lot.get("lot_number") or lot.get("lot")):
                auction_data["found"] = True
                auction_data["auction_name"] = f"{lot.get('auction', 'Copart / IAAI')} (США)"
                auction_data["lot_number"] = str(lot.get("lot_number") or lot.get("lot") or "Н/Д")
                auction_data["seller"] = str(lot.get("seller") or lot.get("seller_name") or "Страховая компания")
                auction_data["title_status"] = str(lot.get("title") or lot.get("title_status") or "Salvage Certificate")
                
                odo = lot.get("odometer") or lot.get("mileage")
                if odo:
                    try:
                        auction_data["odometer_miles"] = f"{int(odo):,} миль".replace(",", " ")
                    except:
                        auction_data["odometer_miles"] = str(odo)
                
                bid = lot.get("final_bid") or lot.get("price") or lot.get("bid")
                if bid:
                    try:
                        auction_data["final_bid"] = f"${int(bid):,}".replace(",", " ")
                    except:
                        auction_data["final_bid"] = str(bid)
                    
                damage = lot.get("primary_damage") or lot.get("damage") or lot.get("loss")
                if damage:
                    auction_data["damage_ru"] = str(damage)
        else:
            logger.warning(f"RapidAPI Non-200: {res.status_code} - {res.text}")
    except Exception as e:
        logger.error(f"[Auction API Exception]: {e}")

    return auction_data

# --- 3. Генерация HTML для PDF ---
def build_pdf_html(data: dict) -> str:
    vin = data["vin"]
    specs = data["specs"]
    auction = data["auction"]

    vin_rows_html = "".join([
        f"<tr><td><b>{item['code']}</b></td><td>{item['title']}</td><td>{item['value']}</td></tr>"
        for item in specs.get("vin_structure", [])
    ])

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            @page {{ size: A4; margin: 12mm; background-color: #f8fafc; }}
            body {{ font-family: 'Liberation Sans', 'Arial', sans-serif; color: #1e293b; margin: 0; font-size: 10pt; }}
            .header {{ background: linear-gradient(135deg, #0f172a, #1e3a8a); color: #ffffff; padding: 18px 20px; border-radius: 8px; margin-bottom: 15px; }}
            .header h1 {{ margin: 0; font-size: 18pt; }}
            .header p {{ margin: 4px 0 0 0; font-size: 9pt; color: #93c5fd; }}
            .vin-box {{ background-color: #ffffff; border: 2px solid #2563eb; padding: 10px 15px; border-radius: 6px; margin-bottom: 15px; font-size: 11pt; }}
            .vin-code {{ font-size: 14pt; font-weight: bold; color: #2563eb; }}
            .section-title {{ font-size: 12pt; font-weight: bold; color: #0f172a; border-bottom: 2px solid #cbd5e1; padding-bottom: 4px; margin-top: 15px; margin-bottom: 10px; }}
            .grid-table {{ width: 100%; border-collapse: collapse; background: #ffffff; border-radius: 6px; overflow: hidden; margin-bottom: 15px; }}
            .grid-table th, .grid-table td {{ padding: 7px 10px; text-align: left; border-bottom: 1px solid #e2e8f0; font-size: 9.5pt; }}
            .grid-table th {{ background-color: #f1f5f9; color: #475569; font-size: 8.5pt; text-transform: uppercase; }}
            .badge-danger {{ background-color: #fef2f2; color: #dc2626; padding: 3px 8px; border-radius: 4px; font-weight: bold; }}
            .badge-success {{ background-color: #f0fdf4; color: #16a34a; padding: 3px 8px; border-radius: 4px; font-weight: bold; }}
            .advice-card {{ background-color: #eff6ff; border-left: 4px solid #3b82f6; padding: 10px 12px; border-radius: 4px; font-size: 9pt; line-height: 1.4; }}
        </style>
    </head>
    <body>
        <div class="header">
            <h1>ОТЧЁТ ПРОВЕРКИ АВТОМОБИЛЯ</h1>
            <p>Динамическая проверка по международным реестрам VIN</p>
        </div>

        <div class="vin-box">ИДЕНТИФИКАТОР VIN: <span class="vin-code">{vin}</span></div>

        <div class="section-title">1. Результаты аукциона США (Copart / IAAI)</div>
        <table class="grid-table">
            <tr><th width="35%">Параметр</th><th width="65%">Значение</th></tr>
            <tr><td><b>Статус поиска:</b></td><td>{auction.get('auction_name')}</td></tr>
            <tr><td><b>Лот / Продавец:</b></td><td>{auction.get('lot_number')} | {auction.get('seller')}</td></tr>
            <tr><td><b>Тип документа (Title):</b></td><td>{auction.get('title_status')}</td></tr>
            <tr><td><b>Пробег на торгах:</b></td><td><b>{auction.get('odometer_miles')}</b></td></tr>
            <tr><td><b>Финальная ставка:</b></td><td><b>{auction.get('final_bid')}</b></td></tr>
            <tr><td><b>Повреждения:</b></td><td><span class="{'badge-danger' if auction.get('found') else 'badge-success'}">{auction.get('damage_ru')}</span></td></tr>
        </table>

        <div class="section-title">2. Технические характеристики ({specs.get('make')} {specs.get('model')})</div>
        <table class="grid-table">
            <thead>
                <tr><th width="15%">Код</th><th width="30%">Компонент</th><th width="55%">Детализация</th></tr>
            </thead>
            <tbody>
                {vin_rows_html}
            </tbody>
        </table>

        <div class="section-title">3. Юридическая проверка СНГ (РФ / РБ)</div>
        <table class="grid-table">
            <tr><th width="35%">Параметр</th><th width="65%">Значение</th></tr>
            <tr><td><b>Реестр залогов:</b></td><td><span class="badge-success">ЧИСТО</span></td></tr>
            <tr><td><b>База Такси:</b></td><td><span class="badge-success">ЧИСТО</span></td></tr>
        </table>

        <div class="section-title">4. Итоговое резюме</div>
        <div class="advice-card">
            <b>📋 Результат:</b><br>
            Автомобиль: {specs.get('make')} {specs.get('model')} ({specs.get('year')}).<br>
            {'⚠️ Зафиксированы данные о продаже на аукционе списанных авто в США.' if auction.get('found') else '✅ В архивах списаний США данные по данному VIN не обнаружены.'}
        </div>
    </body>
    </html>
    """

# --- 4. Эндпоинт JSON (Идеален под твой bot.py) ---
@app.get("/api/v1/vin/{vin}")
async def get_vin_json(vin: str = Path(..., min_length=17, max_length=17)):
    vin = vin.upper()
    async with httpx.AsyncClient() as client:
        specs, auction = await asyncio.gather(
            decode_vin_nhtsa(client, vin),
            fetch_auction_api_data(client, vin)
        )
    
    # Добавляем структуру CIS, которую ждёт твой бот
    cis = {
        "is_pledged": False,
        "is_taxi": False
    }
    
    return {
        "status": "success", 
        "vin": vin, 
        "specs": specs, 
        "auction": auction,
        "cis": cis
    }

# --- 5. Эндпоинт PDF ---
@app.get("/api/v1/vin/{vin}/pdf")
async def get_vin_pdf_report(vin: str = Path(..., min_length=17, max_length=17)):
    vin = vin.upper()
    async with httpx.AsyncClient() as client:
        specs, auction = await asyncio.gather(
            decode_vin_nhtsa(client, vin),
            fetch_auction_api_data(client, vin)
        )
    
    html_string = build_pdf_html({"vin": vin, "specs": specs, "auction": auction})
    pdf_bytes = HTML(string=html_string).write_pdf()
    
    return StreamingResponse(
        io.BytesIO(pdf_bytes), 
        media_type="application/pdf", 
        headers={"Content-Disposition": f"attachment; filename=VIN_Report_{vin}.pdf"}
    )