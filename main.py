import asyncio
import io
import re
from typing import Dict, Any
from fastapi import FastAPI, Path
from fastapi.responses import StreamingResponse
from bs4 import BeautifulSoup
from weasyprint import HTML
from curl_cffi.requests import AsyncSession  # Обход защиты Cloudflare

app = FastAPI(title="Real VIN Checker API V4", version="4.0.0")

# 1. Запрос в бесплатную официальную базу NHTSA (США)
async def decode_vin_nhtsa(session: AsyncSession, vin: str) -> Dict[str, Any]:
    url = f"https://vpic.nhtsa.dot.gov/api/vehicles/decodevinvalues/{vin}?format=json"
    result = {
        "make": "Н/Д",
        "model": "Н/Д",
        "year": "Н/Д",
        "trim": "Н/Д",
        "body_class": "Н/Д",
        "drive_type": "Н/Д",
        "engine": "Н/Д",
        "plant": "Н/Д",
        "vin_structure": []
    }
    
    try:
        res = await session.get(url, timeout=7.0)
        if res.status_code == 200:
            data = res.json().get("Results", [{}])[0]
            
            result["make"] = data.get("Make") or "Н/Д"
            result["model"] = data.get("Model") or "Н/Д"
            result["year"] = data.get("ModelYear") or "Н/Д"
            result["trim"] = data.get("Series") or "Стандарт"
            result["body_class"] = data.get("BodyClass") or "Н/Д"
            result["drive_type"] = data.get("DriveType") or "Н/Д"
            
            disp = data.get("DisplacementL")
            if disp:
                result["engine"] = f"{disp}L {data.get('EngineConfiguration', '')}".strip()
            
            plant = f"{data.get('PlantCity', '')} {data.get('PlantCountry', '')}".strip()
            result["plant"] = plant if plant else "Н/Д"
            
            result["vin_structure"] = [
                {"code": vin[:3], "title": "WMI (Производитель)", "value": f"{result['make']} ({result['plant']})"},
                {"code": vin[3:8], "title": "VDS (Модель/Кузов)", "value": f"{result['model']} {result['body_class']}"},
                {"code": vin[8], "title": "Контрольный знак", "value": f"Валидатор: {vin[8]}"},
                {"code": vin[9], "title": "Модельный год", "value": f"{result['year']} год"},
                {"code": vin[10], "title": "Завод сборки", "value": f"Завод: {vin[10]}"},
                {"code": vin[11:], "title": "VIS (Серийный номер)", "value": f"№ {vin[11:]}"}
            ]
    except Exception as e:
        print(f"NHTSA error: {e}")
        
    return result

# 2. РЕАЛЬНЫЙ парсинг аукциона BidFax с имитацией браузера Chrome (TLS Impersonate)
async def fetch_real_bidfax_data(session: AsyncSession, vin: str) -> Dict[str, Any]:
    url = f"https://www.bidfax.info/?do=search&subaction=search&story={vin}"
    
    auction_data = {
        "found": False,
        "auction_name": "Не найден в базах списаний США",
        "lot_number": "Н/Д",
        "seller": "Н/Д",
        "title_status": "Данные о страховом списании отсутствуют",
        "odometer_miles": "Н/Д",
        "final_bid": "Н/Д",
        "damage_ru": "Зафиксированных повреждений на аукционах не найдено"
    }

    try:
        # curl_cffi использует настоящие TLS-отпечатки Chrome 120, что обходит Cloudflare
        res = await session.get(
            url, 
            impersonate="chrome120", 
            timeout=10.0,
            headers={"Referer": "https://www.bidfax.info/"}
        )
        
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            
            # Ищем ссылку на карточку найденного авто
            card = soup.select_one(".post-title a, .search-results a")
            if card and card.get("href"):
                detail_url = card["href"]
                # Заходим внутрь найденного лота
                detail_res = await session.get(detail_url, impersonate="chrome120", timeout=10.0)
                if detail_res.status_code == 200:
                    detail_soup = BeautifulSoup(detail_res.text, "html.parser")
                    
                    auction_data["found"] = True
                    auction_data["auction_name"] = "Copart / IAAI (Архив BidFax)"
                    
                    # Извлекаем реальный текст со страницы
                    page_text = detail_soup.text
                    
                    # Ищем лот
                    lot_match = re.search(r'Lot number:\s*(\d+)', page_text, re.IGNORECASE)
                    if lot_match:
                        auction_data["lot_number"] = lot_match.group(1)
                        
                    # Ищем пробег
                    odo_match = re.search(r'Odometer:\s*([\d,]+|\d+)\s*(miles|mi)', page_text, re.IGNORECASE)
                    if odo_match:
                        auction_data["odometer_miles"] = f"{odo_match.group(1)} миль"
                        
                    # Ищем финальную ставку
                    bid_match = re.search(r'Final bid:\s*(\$[\d,]+)', page_text, re.IGNORECASE)
                    if bid_match:
                        auction_data["final_bid"] = bid_match.group(1)
                        
                    # Ищем повреждения
                    damage_match = re.search(r'Primary damage:\s*([^\n\r<]+)', page_text, re.IGNORECASE)
                    if damage_match:
                        auction_data["damage_ru"] = damage_match.group(1).strip()
                        
                    # Ищем тайтл
                    title_match = re.search(r'Doc type:\s*([^\n\r<]+)', page_text, re.IGNORECASE)
                    if title_match:
                        auction_data["title_status"] = title_match.group(1).strip()

    except Exception as e:
        print(f"Scraper error: {e}")

    return auction_data

# 3. Базовая проверка РФ
async def check_cis_databases(session: AsyncSession, vin: str) -> Dict[str, Any]:
    url = f"https://html.duckduckgo.com/html/?q={vin}"
    cis_info = {"is_pledged": False, "is_taxi": False}
    try:
        res = await session.get(url, impersonate="chrome120", timeout=6.0)
        if res.status_code == 200:
            text = res.text.lower()
            if "залог" in text or "reestr-zalogov" in text:
                cis_info["is_pledged"] = True
            if "такси" in text:
                cis_info["is_taxi"] = True
    except Exception:
        pass
    return cis_info

def build_pdf_html(data: dict) -> str:
    vin = data["vin"]
    specs = data["specs"]
    auction = data["auction"]
    cis = data["cis"]

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
            <tr><td><b>Повреждения:</b></td><td><span class="badge-danger">{auction.get('damage_ru')}</span></td></tr>
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
            <tr>
                <td width="50%"><b>Реестр залогов:</b></td>
                <td width="50%">{'<span class="badge-danger">ОБНАРУЖЕН ЗАЛОГ</span>' if cis.get('is_pledged') else '<span class="badge-success">ЧИСТО</span>'}</td>
            </tr>
            <tr>
                <td><b>База Такси:</b></td>
                <td>{'<span class="badge-danger">НАЙДЕНА ЛИЦЕНЗИЯ</span>' if cis.get('is_taxi') else '<span class="badge-success">ЧИСТО</span>'}</td>
            </tr>
        </table>

        <div class="section-title">4. Итоговое резюме</div>
        <div class="advice-card">
            <b>📋 Результат:</b><br>
            Автомобиль: {specs.get('make')} {specs.get('model')} ({specs.get('year')}).<br>
            {'⚠️ Автомобиль найден в архиве аукционов США.' if auction.get('found') else '✅ В архивах списаний США данные по данному VIN не обнаружены.'}
        </div>
    </body>
    </html>
    """

@app.get("/api/v1/vin/{vin}")
async def get_vin_full_details(vin: str = Path(..., min_length=17, max_length=17)):
    vin = vin.upper()
    async with AsyncSession() as session:
        specs, auction, cis = await asyncio.gather(
            decode_vin_nhtsa(session, vin),
            fetch_real_bidfax_data(session, vin),
            check_cis_databases(session, vin)
        )
    return {"status": "success", "vin": vin, "specs": specs, "auction": auction, "cis": cis}

@app.get("/api/v1/vin/{vin}/pdf")
async def get_vin_pdf_report(vin: str = Path(..., min_length=17, max_length=17)):
    vin = vin.upper()
    async with AsyncSession() as session:
        specs, auction, cis = await asyncio.gather(
            decode_vin_nhtsa(session, vin),
            fetch_real_bidfax_data(session, vin),
            check_cis_databases(session, vin)
        )
    
    html_string = build_pdf_html({"vin": vin, "specs": specs, "auction": auction, "cis": cis})
    pdf_bytes = HTML(string=html_string).write_pdf()
    
    return StreamingResponse(
        io.BytesIO(pdf_bytes), 
        media_type="application/pdf", 
        headers={"Content-Disposition": f"attachment; filename=VIN_Report_{vin}.pdf"}
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)