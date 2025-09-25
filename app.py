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

# ¡Importante! Importa tu nuevo módulo de scraping
from modules.estudio_scraper import (
    obtener_datos_completos_partido, 
    format_ah_as_decimal_string_of, 
    obtener_datos_preview_rapido, 
    obtener_datos_preview_ligero, 
    generar_analisis_mercado_simplificado,
    check_handicap_cover,
    parse_ah_to_number_of
)
from modules import cache_manager # IMPORTAR EL GESTOR DE CACHÉ

app = Flask(__name__)

# --- Lógica de Scraping actualizada ---
URL_NOWGOAL_BASE = "https://www.nowgoal.com/"
# URL del fichero JS que contiene los datos de los partidos
DATA_JS_URL = f"{URL_NOWGOAL_BASE}gf/data/bf_en-idn.js"

REQUEST_TIMEOUT_SECONDS = 15
_REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
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
    if '/' in t:
        parts = [p for p in re.split(r"/", t) if p]
        nums = [float(p) for p in parts if p]
        if not nums: return None
        return sum(nums) / len(nums)
    return float(t.replace('+', ''))

def _bucket_to_half(value: float) -> float:
    if value is None: return None
    if value == 0: return 0.0
    sign = -1.0 if value < 0 else 1.0
    av = abs(value)
    base = math.floor(av)
    frac = av - base
    if abs(frac - 0.25) < 1e-6 or abs(frac - 0.75) < 1e-6:
        return sign * (base + 0.5)
    return sign * round(av * 2) / 2.0

def normalize_handicap_to_half_bucket_str(text: str):
    v = _parse_handicap_to_float(text)
    if v is None: return None
    b = _bucket_to_half(v)
    if b is None: return None
    return f"{b:.1f}"

def parse_js_data(js_content: str, handicap_filter=None):
    # El contenido JS es una asignación de variables. Buscamos la estructura de array.
    # Ejemplo: var A=new Array();A[0]=[...];A[1]=[...];
    # O puede ser: var matchData = [[...], [...]]
    match = re.search(r'=\s*(\[\[.*\].*\].*)\];', js_content, re.DOTALL)
    if not match:
        # Fallback para el formato A[0]=[...];A[1]=[...];
        matches_str = re.findall(r'\[([^\[\]]+)\];', js_content)
        if not matches_str:
            print("[ERROR] No se encontró la estructura de datos de partidos en el fichero JS.")
            return []
        
        # Limpiar y convertir a una lista de listas
        data_str = '[' + ','.join([f'[{m}]' for m in matches_str]) + ']'
    else:
        data_str = match.group(1)

    try:
        # Reemplazar comillas simples por dobles para que sea JSON válido
        # y manejar posibles comillas dentro de los nombres de equipos
        data_str = data_str.replace("\\'", "'\"") # Unescape escaped quotes
        data_str = re.sub(r"'(.*?)'", r"\"\1\"", data_str)
        all_matches_data = json.loads(data_str)
    except json.JSONDecodeError as e:
        print(f"[ERROR] Fallo al decodificar JSON del fichero JS: {e}")
        # Intentar un método más sucio si falla el JSON estricto
        try:
            all_matches_data = eval(data_str)
        except:
            print("[ERROR] Fallo también con eval(). No se pueden parsear los datos.")
            return []

    upcoming_matches = []
    now_utc = datetime.datetime.utcnow()

    # Indices basados en el análisis histórico de la estructura de Nowgoal
    # ID, LeagueID, Home, Away, Time, State, HomeScore, AwayScore, ..., Handicap(26), O/U(28)
    for match_data in all_matches_data:
        try:
            state = int(match_data[5])
            # Solo partidos no iniciados (estado 0)
            if state != 0:
                continue

            match_id = match_data[0]
            home_team = match_data[2]
            away_team = match_data[3]
            
            # La fecha/hora viene en formato "new Date(YYYY,MM,DD,HH,MM,SS)"
            time_str = match_data[4]
            m = re.search(r'(\d+),(\d+),(\d+),(\d+),(\d+),(\d+)', time_str)
            if not m: continue
            
            # Mes en JS es 0-11, en Python es 1-12
            year, month, day, hour, minute, sec = [int(g) for g in m.groups()]
            match_time = datetime.datetime(year, month + 1, day, hour, minute, sec)

            if match_time < now_utc: continue

            handicap = str(match_data[26])
            goal_line = str(match_data[28])

            if not handicap or not goal_line:
                continue

            upcoming_matches.append({
                "id": match_id,
                "time_obj": match_time,
                "home_team": home_team.strip(),
                "away_team": away_team.strip(),
                "handicap": handicap,
                "goal_line": goal_line
            })
        except (IndexError, ValueError, TypeError) as e:
            # print(f"Saltando fila de partido por error de parseo: {e} -> {match_data}")
            continue

    if handicap_filter:
        try:
            target = normalize_handicap_to_half_bucket_str(handicap_filter)
            if target is not None:
                upcoming_matches = [
                    m for m in upcoming_matches 
                    if normalize_handicap_to_half_bucket_str(m.get('handicap', '')) == target
                ]
        except Exception:
            pass

    upcoming_matches.sort(key=lambda x: x['time_obj'])
    
    for match in upcoming_matches:
        # Ajustar a la zona horaria deseada si es necesario, ej. +2 horas
        match['time'] = (match['time_obj'] + datetime.timedelta(hours=2)).strftime('%H:%M')
        del match['time_obj']

    return upcoming_matches

async def get_main_page_matches_async(handicap_filter=None):
    session = _get_shared_requests_session()
    try:
        # Añadir un timestamp para evitar caché agresiva
        url = f"{DATA_JS_URL}?t={int(datetime.datetime.utcnow().timestamp())}"
        response = await asyncio.to_thread(session.get, url, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        js_content = response.text
        
        if not js_content:
            print("[ERROR] El fichero de datos JS está vacío.")
            return []
            
        return parse_js_data(js_content, handicap_filter)

    except Exception as e:
        print(f"[ERROR] No se pudo obtener o procesar el fichero de datos JS: {e}")
        return []

@app.route('/')
def index():
    try:
        print("Recibida petición para Próximos Partidos...")
        hf = request.args.get('handicap')
        # La función ahora es mucho más simple
        matches = asyncio.run(get_main_page_matches_async(handicap_filter=hf))
        print(f"Scraper finalizado. {len(matches)} partidos encontrados.")
        
        if not matches:
             return render_template('index.html', matches=[], error="No se encontraron partidos. El scraper podría estar en ejecución o no hay partidos disponibles.", page_mode='upcoming', page_title='Próximos Partidos')

        opts = sorted({
            normalize_handicap_to_half_bucket_str(m.get('handicap'))
            for m in matches if normalize_handicap_to_half_bucket_str(m.get('handicap')) is not None
        }, key=lambda x: float(x))
        return render_template('index.html', matches=matches, handicap_filter=hf, handicap_options=opts, page_mode='upcoming', page_title='Próximos Partidos')
    except Exception as e:
        print(f"ERROR en la ruta principal: {e}")
        import html
        error_message = html.escape(str(e))
        return render_template('index.html', matches=[], error=f"No se pudieron cargar los partidos: {error_message}", page_mode='upcoming', page_title='Próximos Partidos')

# --- OTRAS RUTAS (sin cambios, pero se dejan por completitud) ---

@app.route('/resultados')
def resultados():
    # Esta ruta necesitaría una refactorización similar para los resultados,
    # pero por ahora la dejamos como está para centrarnos en el problema principal.
    return render_template('index.html', matches=[], error="La página de resultados está temporalmente deshabilitada.", page_mode='finished', page_title='Resultados Finalizados')

@app.route('/api/matches')
def api_matches():
    try:
        matches = asyncio.run(get_main_page_matches_async(request.args.get('handicap')))
        return jsonify({'matches': matches})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ... (El resto de las rutas de API y estudio se dejan como estaban, 
# ya que dependen de 'obtener_datos_completos_partido' que es un módulo separado 
# y probablemente ya maneja la nueva estructura o usa URLs de API diferentes)

@app.route('/estudio/<string:match_id>')
def mostrar_estudio(match_id):
    print(f"Recibida petición para el estudio del partido ID: {match_id}")
    cached_data = cache_manager.get_from_cache(match_id)
    if cached_data:
        return render_template('estudio.html', data=cached_data, format_ah=format_ah_as_decimal_string_of)
    datos_partido = obtener_datos_completos_partido(match_id)
    if not datos_partido or "error" in datos_partido:
        abort(500, description=datos_partido.get('error', 'Error desconocido'))
    cache_manager.set_to_cache(match_id, datos_partido)
    return render_template('estudio.html', data=datos_partido, format_ah=format_ah_as_decimal_string_of)

if __name__ == '__main__':
    # Para pruebas locales, puedes quitar el comentario de la siguiente línea
    # os.environ['PROXY_URL'] = 'URL_DE_TU_PROXY_LOCAL_SI_LA_NECESITAS'
    app.run(host='0.0.0.0', port=5000, debug=False)