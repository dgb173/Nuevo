# app.py - Servidor web principal (Flask)
from flask import Flask, render_template, abort, request, jsonify
import asyncio
import datetime
import re
import math
import threading
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import json
import os
import html

# Módulos del proyecto
from modules.estudio_scraper import (
    obtener_datos_completos_partido, 
    format_ah_as_decimal_string_of
)
from modules import cache_manager

app = Flask(__name__)

# --- Lógica de Scraping Actualizada ---
URL_NOWGOAL_BASE = "https://www.nowgoal.com/"
DATA_JS_URL = f"{URL_NOWGOAL_BASE}gf/data/bf_en-idn.js"

REQUEST_TIMEOUT_SECONDS = 15
_REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Referer": URL_NOWGOAL_BASE,
}

_requests_session = None
_requests_session_lock = threading.Lock()

def _get_shared_requests_session():
    global _requests_session
    with _requests_session_lock:
        if _requests_session is None:
            session = requests.Session()
            retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
            adapter = HTTPAdapter(max_retries=retries)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            session.headers.update(_REQUEST_HEADERS)
            
            proxy_url = os.environ.get('PROXY_URL')
            if proxy_url:
                proxies = {"http": proxy_url, "https": proxy_url}
                session.proxies.update(proxies)
                print("[INFO] Sesión de Requests configurada para usar proxy.")

            _requests_session = session
        return _requests_session

def _parse_handicap_to_float(text: str):
    if text is None: return None
    t = str(text).strip()
    if not t: return None
    if '/' in t:
        parts = [p for p in re.split(r"/", t) if p]
        try:
            nums = [float(p) for p in parts]
            if not nums: return None
            return sum(nums) / len(nums)
        except (ValueError, TypeError):
            return None
    try:
        return float(t.replace('+', ''))
    except (ValueError, TypeError):
        return None

def normalize_handicap_to_half_bucket_str(text: str):
    v = _parse_handicap_to_float(text)
    if v is None: return None
    sign = 1.0 if v >= 0 else -1.0
    av = abs(v)
    bucket = round(av * 4) / 4
    if (bucket * 100) % 50 != 0:
        bucket = math.floor(av) + 0.5
    else:
        bucket = round(av)
    final_value = sign * bucket
    return f"{final_value:.2f}".replace('.00', '.0').replace('.50', '.5').replace('.25', '.25').replace('.75', '.75')

def parse_js_data(js_content: str, handicap_filter=None):
    matches_str = re.findall(r'A\[\d+\]=\[(.*?)\];', js_content)
    if not matches_str:
        print("[ERROR] No se encontró la estructura de datos 'A[i]=[...]' en el fichero JS.")
        return []

    upcoming_matches = []
    now_utc = datetime.datetime.utcnow()

    for match_str in matches_str:
        try:
            cleaned_str = match_str.replace(',,', ',None,').replace(',,', ',None,')
            cleaned_str = re.sub(r'\[,', '[None,', cleaned_str)
            cleaned_str = re.sub(r',\]', ',None]', cleaned_str)
            py_tuple_str = f"({cleaned_str})"
            match_data = eval(py_tuple_str, {"__builtins__": {}, "None": None, "True": True, "False": False})

            state = int(match_data[8])
            if state != 0:
                continue

            match_id = match_data[0]
            home_team = match_data[4]
            away_team = match_data[5]
            time_str = match_data[6]
            
            match_time_gmt8 = datetime.datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S')
            match_time_utc = match_time_gmt8 - datetime.timedelta(hours=8)

            if match_time_utc < now_utc:
                continue

            handicap = str(match_data[21])
            goal_line = str(match_data[24])

            if not handicap:
                continue

            upcoming_matches.append({
                "id": match_id,
                "time_obj": match_time_utc,
                "home_team": home_team.strip(),
                "away_team": away_team.strip(),
                "handicap": handicap,
                "goal_line": goal_line
            })
        except (IndexError, ValueError, TypeError, SyntaxError) as e:
            print(f"[AVISO] Saltando fila con datos inesperados: {e} -> {match_str[:100]}...")
            continue
    
    if handicap_filter:
        try:
            target = normalize_handicap_to_half_bucket_str(handicap_filter)
            if target is not None:
                upcoming_matches = [m for m in upcoming_matches if normalize_handicap_to_half_bucket_str(m.get('handicap', '')) == target]
        except Exception:
            pass

    upcoming_matches.sort(key=lambda x: x['time_obj'])
    
    for match in upcoming_matches:
        match['time'] = (match['time_obj'] + datetime.timedelta(hours=2)).strftime('%H:%M')
        del match['time_obj']

    return upcoming_matches

async def get_main_page_matches_async(handicap_filter=None):
    session = _get_shared_requests_session()
    try:
        url = f"{DATA_JS_URL}?t={int(datetime.datetime.utcnow().timestamp())}"
        response = await asyncio.to_thread(session.get, url, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        js_content = response.text
        if not js_content: return []
        return parse_js_data(js_content, handicap_filter)
    except Exception as e:
        print(f"[ERROR] No se pudo obtener o procesar el fichero de datos JS: {e}")
        return []

@app.route('/')
def index():
    try:
        print("Recibida petición para Próximos Partidos...")
        hf = request.args.get('handicap')
        matches = asyncio.run(get_main_page_matches_async(handicap_filter=hf))
        print(f"Scraper finalizado. {len(matches)} partidos encontrados.")
        if not matches:
             return render_template('index.html', matches=[], error="No se encontraron partidos. El scraper podría estar en ejecución o no hay partidos disponibles.", page_mode='upcoming', page_title='Próximos Partidos')
        opts = sorted({normalize_handicap_to_half_bucket_str(m.get('handicap')) for m in matches if normalize_handicap_to_half_bucket_str(m.get('handicap')) is not None}, key=lambda x: float(x))
        return render_template('index.html', matches=matches, handicap_filter=hf, handicap_options=opts, page_mode='upcoming', page_title='Próximos Partidos')
    except Exception as e:
        print(f"ERROR en la ruta principal: {e}")
        error_message = html.escape(str(e))
        return render_template('index.html', matches=[], error=f"No se pudieron cargar los partidos: {error_message}", page_mode='upcoming', page_title='Próximos Partidos')

@app.route('/resultados')
def resultados():
    return render_template('index.html', matches=[], error="La página de resultados está temporalmente deshabilitada.", page_mode='finished', page_title='Resultados Finalizados')

@app.route('/api/matches')
def api_matches():
    try:
        matches = asyncio.run(get_main_page_matches_async(request.args.get('handicap')))
        return jsonify({'matches': matches})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/estudio/<string:match_id>')
def mostrar_estudio(match_id):
    # This is still broken because of the URL in estudio_scraper.py
    # I will fix this AFTER the main pages are working.
    return "La función de estudio está temporalmente deshabilitada mientras se repara."

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)