# app.py - Servidor web principal (Flask)
from flask import Flask, render_template, abort, request
import asyncio
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
import datetime
import re
import math
import time
import json
import os
import html
import threading
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import pytz

# Â¡Importante! Importa tu nuevo mÃ³dulo de scraping
from modules.estudio_scraper import (
    obtener_datos_completos_partido, 
    format_ah_as_decimal_string_of, 
    obtener_datos_preview_rapido, 
    obtener_datos_preview_ligero, 
    generar_analisis_mercado_simplificado,
    check_handicap_cover,
    parse_ah_to_number_of
)
from flask import jsonify # AsegÃºrate de que jsonify estÃ¡ importado

app = Flask(__name__)

# --- MantÃ©n tu lÃ³gica para la pÃ¡gina principal ---
URL_NOWGOAL = "https://live20.nowgoal25.com/"
URL_NOWGOAL_BASE = "https://www.nowgoal.com/"
DATA_JS_URL = f"{URL_NOWGOAL_BASE}gf/data/bf_en-idn.js"

REQUEST_TIMEOUT_SECONDS = 12
_REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
    "Referer": URL_NOWGOAL_BASE,
}

MADRID_TZ = pytz.timezone("Europe/Madrid")
NOWGOAL_TZ = pytz.timezone("Asia/Shanghai")

_requests_session = None
_requests_session_lock = threading.Lock()
_requests_fetch_lock = threading.Lock()


_ANALYSIS_CACHE_TTL_SECONDS = 600  # 10 minutos
_analysis_cache = {}
_analysis_cache_lock = threading.Lock()
_analysis_processing = set()
_analysis_processing_lock = threading.Lock()


def _build_nowgoal_url(path: str | None = None) -> str:
    if not path:
        return URL_NOWGOAL
    base = URL_NOWGOAL.rstrip('/')
    suffix = path.lstrip('/')
    return f"{base}/{suffix}"


def _get_shared_requests_session():
    global _requests_session
    with _requests_session_lock:
        if _requests_session is None:
            session = requests.Session()
            retries = Retry(total=3, backoff_factor=0.4, status_forcelist=[500, 502, 503, 504])
            adapter = HTTPAdapter(max_retries=retries)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            session.headers.update(_REQUEST_HEADERS)

            proxy_url = os.environ.get('PROXY_URL')
            if proxy_url:
                proxy_url = proxy_url.strip()
                if proxy_url and proxy_url.upper() != 'URL_DE_TU_PROXY':
                    proxies = {"http": proxy_url, "https": proxy_url}
                    session.proxies.update(proxies)
                    print('[INFO] Requests session configured to use proxy.')
                else:
                    print('[INFO] PROXY_URL set but ignored (empty or placeholder).')

            _requests_session = session
        return _requests_session


def _fetch_nowgoal_html_sync(url: str) -> str | None:
    session = _get_shared_requests_session()
    try:
        with _requests_fetch_lock:
            response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        return response.text
    except Exception as exc:
        print(f"Error al obtener {url} con requests: {exc}")
        return None


async def _fetch_nowgoal_html(path: str | None = None, filter_state: int | None = None, requests_first: bool = True) -> str | None:
    target_url = _build_nowgoal_url(path)
    html_content = None

    if requests_first:
        try:
            html_content = await asyncio.to_thread(_fetch_nowgoal_html_sync, target_url)
        except Exception as exc:
            print(f"Error asincronico al lanzar la carga con requests ({target_url}): {exc}")
            html_content = None

    if html_content:
        return html_content

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                await page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(4000)
                if filter_state is not None:
                    try:
                        await page.evaluate("(state) => { if (typeof HideByState === 'function') { HideByState(state); } }", filter_state)
                        await page.wait_for_timeout(1500)
                    except Exception as eval_err:
                        print(f"Advertencia al aplicar HideByState({filter_state}) en {target_url}: {eval_err}")
                return await page.content()
            finally:
                await browser.close()
    except Exception as browser_exc:
        print(f"Error al obtener la pagina con Playwright ({target_url}): {browser_exc}")
    return None

def _parse_number_clean(s: str):
    if s is None:
        return None
    txt = str(s).strip()
    txt = txt.replace('âˆ’', '-')  # unicode minus
    txt = txt.replace(',', '.')
    txt = txt.replace('+', '')
    txt = txt.replace(' ', '')
    m = re.search(r"^[+-]?\d+(?:\.\d+)?$", txt)
    if m:
        try:
            return float(m.group(0))
        except ValueError:
            return None
    return None

def _parse_number(s: str):
    if s is None:
        return None
    # Normaliza separadores y signos
    txt = str(s).strip()
    txt = txt.replace('âˆ’', '-')  # minus unicode
    txt = txt.replace(',', '.')
    txt = txt.replace(' ', '')
    # Coincide con un nÃºmero decimal con signo
    m = re.search(r"^[+-]?\d+(?:\.\d+)?$", txt)
    if m:
        try:
            return float(m.group(0))
        except ValueError:
            return None
    return None

def _parse_handicap_to_float(text: str):
    if text is None:
        return None
    t = str(text).strip()
    if '/' in t:
        parts = [p for p in re.split(r"/", t) if p]
        nums = []
        for p in parts:
            v = _parse_number_clean(p)
            if v is None:
                return None
            nums.append(v)
        if not nums:
            return None
        return sum(nums) / len(nums)
    # Si viene como cadena normal (ej. "+0.25" o "-0,75")
    return _parse_number_clean(t.replace('+', ''))

def _bucket_to_half(value: float) -> float:
    if value is None:
        return None
    if value == 0:
        return 0.0
    sign = -1.0 if value < 0 else 1.0
    av = abs(value)
    base = math.floor(av + 1e-9)
    frac = av - base
    # Mapea 0.25/0.75/0.5 a .5, 0.0 queda .0
    def close(a, b):
        return abs(a - b) < 1e-6
    if close(frac, 0.0):
        bucket = float(base)
    elif close(frac, 0.5) or close(frac, 0.25) or close(frac, 0.75):
        bucket = base + 0.5
    else:
        # fallback: redondeo al mÃºltiplo de 0.5 mÃ¡s cercano
        bucket = round(av * 2) / 2.0
        # si cae justo en entero, desplazar a .5 para respetar la preferencia de .25/.75 â†’ .5
        f = bucket - math.floor(bucket)
        if close(f, 0.0) and (abs(av - (math.floor(bucket) + 0.25)) < 0.26 or abs(av - (math.floor(bucket) + 0.75)) < 0.26):
            bucket = math.floor(bucket) + 0.5
    return sign * bucket

def normalize_handicap_to_half_bucket_str(text: str):
    v = _parse_handicap_to_float(text)
    if v is None:
        return None
    b = _bucket_to_half(v)
    if b is None:
        return None
    # Formato con un decimal
    return f"{b:.1f}"



def _filter_matches_by_handicap(matches, handicap_filter):
    if not handicap_filter:
        return matches
    try:
        target = normalize_handicap_to_half_bucket_str(handicap_filter)
    except Exception:
        target = None
    if target is None:
        return matches
    filtered = []
    for match in matches:
        try:
            hv = normalize_handicap_to_half_bucket_str(match.get('handicap'))
        except Exception:
            hv = None
        if hv == target:
            filtered.append(match)
    return filtered


def parse_js_data(js_content: str):
    matches_str = re.findall(r"A\[\d+\]=\[(.*?)\];", js_content)
    if not matches_str:
        print("[ERROR] No se encontro la estructura de datos 'A[i]=[...]' en el fichero JS.")
        return []

    upcoming_matches = []
    now_utc = datetime.datetime.now(pytz.utc)

    for raw_str in matches_str:
        sanitized = raw_str.strip()
        while ',,' in sanitized:
            sanitized = sanitized.replace(',,', ',None,')
        if sanitized.startswith(','):
            sanitized = 'None' + sanitized
        if sanitized.endswith(','):
            sanitized = sanitized + 'None'

        try:
            match_tuple = eval(f"({sanitized})", {"__builtins__": {}, "None": None, "True": True, "False": False})
        except Exception as exc:
            print(f"[AVISO] Saltando fila con datos inesperados: {exc} -> {sanitized[:80]}...")
            continue

        try:
            state = int(match_tuple[8])
            if state != 0:
                continue

            match_id = match_tuple[0]
            home_team = str(match_tuple[4]).strip()
            away_team = str(match_tuple[5]).strip()
            time_str = str(match_tuple[6]).strip()
            if not all([match_id, home_team, away_team, time_str]):
                continue

            match_time_gmt8 = NOWGOAL_TZ.localize(datetime.datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S'))
            match_time_utc = match_time_gmt8.astimezone(pytz.utc)
            if match_time_utc < now_utc:
                continue

            handicap = match_tuple[21] if len(match_tuple) > 21 else None
            goal_line = match_tuple[25] if len(match_tuple) > 25 else None

            handicap_str = '' if handicap is None else str(handicap).strip()
            goal_line_str = '' if goal_line is None else str(goal_line).strip()

            if not handicap_str or handicap_str in {'N/A', 'None'}:
                continue
            if not goal_line_str or goal_line_str in {'N/A', 'None'}:
                continue

            upcoming_matches.append({
                'id': str(match_id),
                'time_obj': match_time_utc,
                'home_team': home_team,
                'away_team': away_team,
                'handicap': handicap_str,
                'goal_line': goal_line_str
            })
        except (IndexError, ValueError, TypeError) as exc:
            print(f"[AVISO] Error al procesar fila JS: {exc}")
            continue

    upcoming_matches.sort(key=lambda x: x['time_obj'])
    for match in upcoming_matches:
        time_utc = match['time_obj']
        match['time_utc'] = time_utc.strftime('%Y-%m-%d %H:%M')
        time_madrid = MADRID_TZ.normalize(time_utc.astimezone(MADRID_TZ))
        match['time'] = time_madrid.strftime('%Y-%m-%d %H:%M')
        match['time_madrid'] = match['time']
        del match['time_obj']

    return upcoming_matches

def parse_main_page_matches(html_content, limit=20, offset=0, handicap_filter=None):
    soup = BeautifulSoup(html_content, 'html.parser')
    match_rows = soup.find_all('tr', id=lambda x: x and x.startswith('tr1_'))
    upcoming_matches = []
    now_utc = datetime.datetime.now(pytz.utc)

    for row in match_rows:
        match_id = row.get('id', '').replace('tr1_', '')
        if not match_id: continue

        time_cell = row.find('td', {'name': 'timeData'})
        if not time_cell or not time_cell.has_attr('data-t'): continue
        
        try:
            match_time_utc = pytz.utc.localize(datetime.datetime.strptime(time_cell['data-t'], '%Y-%m-%d %H:%M:%S'))
        except (ValueError, IndexError):
            continue

        if match_time_utc < now_utc: continue

        home_team_tag = row.find('a', {'id': f'team1_{match_id}'})
        away_team_tag = row.find('a', {'id': f'team2_{match_id}'})
        odds_data = row.get('odds', '').split(',')
        handicap = odds_data[2] if len(odds_data) > 2 else "N/A"
        goal_line = odds_data[10] if len(odds_data) > 10 else "N/A"

        if handicap == "N/A":
            continue


        upcoming_matches.append({
            "id": match_id,
            "time_obj": match_time_utc,
            "home_team": home_team_tag.text.strip() if home_team_tag else "N/A",
            "away_team": away_team_tag.text.strip() if away_team_tag else "N/A",
            "handicap": handicap,
            "goal_line": goal_line
        })

    if handicap_filter:
        try:
            target = normalize_handicap_to_half_bucket_str(handicap_filter)
            if target is not None:
                filtered = []
                for m in upcoming_matches:
                    hv = normalize_handicap_to_half_bucket_str(m.get('handicap', ''))
                    if hv == target:
                        filtered.append(m)
                upcoming_matches = filtered
        except Exception:
            pass

    upcoming_matches.sort(key=lambda x: x['time_obj'])
    
    paginated_matches = upcoming_matches[offset:offset+limit]

    for match in paginated_matches:
        time_utc = match['time_obj']
        match['time_utc'] = time_utc.strftime('%Y-%m-%d %H:%M')
        time_madrid = MADRID_TZ.normalize(time_utc.astimezone(MADRID_TZ))
        match['time'] = time_madrid.strftime('%Y-%m-%d %H:%M')
        match['time_madrid'] = match['time']
        del match['time_obj']

    return paginated_matches

def parse_main_page_finished_matches(html_content, limit=20, offset=0, handicap_filter=None):
    soup = BeautifulSoup(html_content, 'html.parser')
    match_rows = soup.find_all('tr', id=lambda x: x and x.startswith('tr1_'))
    finished_matches = []
    for row in match_rows:
        match_id = row.get('id', '').replace('tr1_', '')
        if not match_id: continue

        state = row.get('state')
        if state is not None and state != "-1":
            continue

        cells = row.find_all('td')
        if len(cells) < 8: continue

        home_team_tag = row.find('a', {'id': f'team1_{match_id}'})
        away_team_tag = row.find('a', {'id': f'team2_{match_id}'})
        
        score_cell = cells[6]
        score_text = "N/A"
        if score_cell:
            b_tag = score_cell.find('b')
            if b_tag:
                score_text = b_tag.text.strip()
            else:
                score_text = score_cell.get_text(strip=True)

        if not re.match(r'^\d+\s*-\s*\d+$', score_text):
            continue

        odds_data = row.get('odds', '').split(',')
        handicap = odds_data[2] if len(odds_data) > 2 else "N/A"
        goal_line = odds_data[10] if len(odds_data) > 10 else "N/A"

        if handicap == "N/A":
            continue

        time_cell = row.find('td', {'name': 'timeData'})
        match_time_utc = datetime.datetime.now(pytz.utc)
        if time_cell and time_cell.has_attr('data-t'):
            try:
                match_time_utc = pytz.utc.localize(datetime.datetime.strptime(time_cell['data-t'], '%Y-%m-%d %H:%M:%S'))
            except (ValueError, IndexError):
                continue
        
        finished_matches.append({
            "id": match_id,
            "time_obj": match_time_utc,
            "home_team": home_team_tag.text.strip() if home_team_tag else "N/A",
            "away_team": away_team_tag.text.strip() if away_team_tag else "N/A",
            "score": score_text,
            "handicap": handicap,
            "goal_line": goal_line
        })

    if handicap_filter:
        try:
            target = normalize_handicap_to_half_bucket_str(handicap_filter)
            if target is not None:
                filtered = []
                for m in finished_matches:
                    hv = normalize_handicap_to_half_bucket_str(m.get('handicap', ''))
                    if hv == target:
                        filtered.append(m)
                finished_matches = filtered
        except Exception:
            pass

    finished_matches.sort(key=lambda x: x['time_obj'], reverse=True)
    
    paginated_matches = finished_matches[offset:offset+limit]

    for match in paginated_matches:
        time_utc = match['time_obj']
        match['time_utc'] = time_utc.strftime('%Y-%m-%d %H:%M')
        time_madrid = MADRID_TZ.normalize(time_utc.astimezone(MADRID_TZ))
        match['time'] = time_madrid.strftime('%Y-%m-%d %H:%M')
        match['time_madrid'] = match['time']
        del match['time_obj']

    return paginated_matches

async def get_main_page_matches_async(limit=20, offset=0, handicap_filter=None):
    session = _get_shared_requests_session()
    matches = []
    try:
        url = f"{DATA_JS_URL}?t={int(datetime.datetime.now(pytz.utc).timestamp())}"
        response = await asyncio.to_thread(session.get, url, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        js_content = response.text
        if js_content:
            matches = parse_js_data(js_content)
    except Exception as exc:
        print(f"[ERROR] No se pudo obtener o procesar el fichero de datos JS: {exc}")
        matches = []

    if matches:
        matches = _filter_matches_by_handicap(matches, handicap_filter)
        return matches[offset:offset + limit]

    print('[AVISO] Fallback a Playwright para cargar los partidos en vivo.')
    html_content = await _fetch_nowgoal_html(filter_state=3, requests_first=False)
    if not html_content:
        return []

    matches = parse_main_page_matches(html_content, limit, offset, handicap_filter)
    if matches:
        return matches

    html_content = await _fetch_nowgoal_html(filter_state=3, requests_first=False)
    if not html_content:
        return []
    return parse_main_page_matches(html_content, limit, offset, handicap_filter)

async def get_main_page_finished_matches_async(limit=20, offset=0, handicap_filter=None):
    html_content = await _fetch_nowgoal_html(path='football/results')
    if not html_content:
        html_content = await _fetch_nowgoal_html(path='football/results', requests_first=False)
        if not html_content:
            return []
    matches = parse_main_page_finished_matches(html_content, limit, offset, handicap_filter)
    if not matches:
        html_content = await _fetch_nowgoal_html(path='football/results', requests_first=False)
        if not html_content:
            return []
        matches = parse_main_page_finished_matches(html_content, limit, offset, handicap_filter)
    return matches

@app.route('/')
def index():
    try:
        print("Recibida peticiÃ³n para PrÃ³ximos Partidos...")
        hf = request.args.get('handicap')
        matches = asyncio.run(get_main_page_matches_async(handicap_filter=hf))
        print(f"Scraper finalizado. {len(matches)} partidos encontrados.")
        opts = sorted({
            normalize_handicap_to_half_bucket_str(m.get('handicap'))
            for m in matches if normalize_handicap_to_half_bucket_str(m.get('handicap')) is not None
        }, key=lambda x: float(x))
        return render_template('index.html', matches=matches, handicap_filter=hf, handicap_options=opts, page_mode='upcoming', page_title='PrÃ³ximos Partidos')
    except Exception as e:
        print(f"ERROR en la ruta principal: {e}")
        return render_template('index.html', matches=[], error=f"No se pudieron cargar los partidos: {e}", page_mode='upcoming', page_title='PrÃ³ximos Partidos')

@app.route('/resultados')
def resultados():
    try:
        print("Recibida peticiÃ³n para Partidos Finalizados...")
        hf = request.args.get('handicap')
        matches = asyncio.run(get_main_page_finished_matches_async(handicap_filter=hf))
        print(f"Scraper finalizado. {len(matches)} partidos encontrados.")
        opts = sorted({
            normalize_handicap_to_half_bucket_str(m.get('handicap'))
            for m in matches if normalize_handicap_to_half_bucket_str(m.get('handicap')) is not None
        }, key=lambda x: float(x))
        return render_template('index.html', matches=matches, handicap_filter=hf, handicap_options=opts, page_mode='finished', page_title='Resultados Finalizados')
    except Exception as e:
        print(f"ERROR en la ruta de resultados: {e}")
        return render_template('index.html', matches=[], error=f"No se pudieron cargar los partidos: {e}", page_mode='finished', page_title='Resultados Finalizados')

@app.route('/api/matches')
def api_matches():
    try:
        offset = int(request.args.get('offset', 0))
        limit = int(request.args.get('limit', 5))
        limit = min(limit, 50)
        matches = asyncio.run(get_main_page_matches_async(limit, offset, request.args.get('handicap')))
        return jsonify({'matches': matches})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/finished_matches')
def api_finished_matches():
    try:
        offset = int(request.args.get('offset', 0))
        limit = int(request.args.get('limit', 5))
        limit = min(limit, 50)
        matches = asyncio.run(get_main_page_finished_matches_async(limit, offset, request.args.get('handicap')))
        return jsonify({'matches': matches})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/proximos')
def proximos():
    try:
        print("Recibida peticiÃ³n. Ejecutando scraper de partidos...")
        hf = request.args.get('handicap')
        matches = asyncio.run(get_main_page_matches_async(25, 0, hf))
        print(f"Scraper finalizado. {len(matches)} partidos encontrados.")
        opts = sorted({
            normalize_handicap_to_half_bucket_str(m.get('handicap'))
            for m in matches if normalize_handicap_to_half_bucket_str(m.get('handicap')) is not None
        }, key=lambda x: float(x))
        return render_template('index.html', matches=matches, handicap_filter=hf, handicap_options=opts)
    except Exception as e:
        print(f"ERROR en la ruta principal: {e}")
        return render_template('index.html', matches=[], error=f"No se pudieron cargar los partidos: {e}")

# --- NUEVA RUTA PARA MOSTRAR EL ESTUDIO DETALLADO ---
@app.route('/estudio/<string:match_id>')
def mostrar_estudio(match_id):
    """
    Esta ruta se activa cuando un usuario visita /estudio/ID_DEL_PARTIDO.
    """
    print(f"Recibida peticiÃ³n para el estudio del partido ID: {match_id}")
    
    # Llama a la funciÃ³n principal de tu mÃ³dulo de scraping
    datos_partido = obtener_datos_completos_partido(match_id)
    
    if not datos_partido or "error" in datos_partido:
        # Si hay un error, puedes mostrar una pÃ¡gina de error
        print(f"Error al obtener datos para {match_id}: {datos_partido.get('error')}")
        abort(500, description=datos_partido.get('error', 'Error desconocido'))

    # Si todo va bien, renderiza la plantilla HTML pasÃ¡ndole los datos
    print(f"Datos obtenidos para {datos_partido['home_name']} vs {datos_partido['away_name']}. Renderizando plantilla...")
    return render_template('estudio.html', data=datos_partido, format_ah=format_ah_as_decimal_string_of)

# --- NUEVA RUTA PARA ANALIZAR PARTIDOS FINALIZADOS ---
@app.route('/analizar_partido', methods=['GET', 'POST'])
def analizar_partido():
    """
    Ruta para analizar partidos finalizados por ID.
    """
    if request.method == 'POST':
        match_id = request.form.get('match_id')
        if match_id:
            print(f"Recibida peticiÃ³n para analizar partido finalizado ID: {match_id}")
            
            # Llama a la funciÃ³n principal de tu mÃ³dulo de scraping
            datos_partido = obtener_datos_completos_partido(match_id)
            
            if not datos_partido or "error" in datos_partido:
                # Si hay un error, mostrarlo en la pÃ¡gina
                print(f"Error al obtener datos para {match_id}: {datos_partido.get('error')}")
                return render_template('analizar_partido.html', error=datos_partido.get('error', 'Error desconocido'))
            
            # --- ANÃLISIS SIMPLIFICADO ---
            # Extraer los datos necesarios para el anÃ¡lisis simplificado
            main_odds = datos_partido.get("main_match_odds_data")
            h2h_data = datos_partido.get("h2h_data")
            home_name = datos_partido.get("home_name")
            away_name = datos_partido.get("away_name")

            analisis_simplificado_html = ""
            if all([main_odds, h2h_data, home_name, away_name]):
                analisis_simplificado_html = generar_analisis_mercado_simplificado(main_odds, h2h_data, home_name, away_name)

            # Si todo va bien, renderiza la plantilla HTML pasÃ¡ndole los datos
            print(f"Datos obtenidos para {datos_partido['home_name']} vs {datos_partido['away_name']}. Renderizando plantilla...")
            return render_template('estudio.html', 
                                   data=datos_partido, 
                                   format_ah=format_ah_as_decimal_string_of,
                                   analisis_simplificado_html=analisis_simplificado_html)
        else:
            return render_template('analizar_partido.html', error="Por favor, introduce un ID de partido vÃ¡lido.")
    
    # Si es GET, mostrar el formulario
    return render_template('analizar_partido.html')

# --- NUEVA RUTA API PARA LA VISTA PREVIA RÃPIDA ---
@app.route('/api/preview/<string:match_id>')
def api_preview(match_id):
    """
    Endpoint para la vista previa. Llama al scraper LIGERO y RÃPIDO.
    Devuelve los datos en formato JSON.
    """
    try:
        # Por defecto usa la vista previa LIGERA (requests). Si ?mode=selenium, usa la completa.
        mode = request.args.get('mode', 'light').lower()
        if mode in ['full', 'selenium']:
            preview_data = obtener_datos_preview_rapido(match_id)
        else:
            preview_data = obtener_datos_preview_ligero(match_id)
        if "error" in preview_data:
            return jsonify(preview_data), 500
        return jsonify(preview_data)
    except Exception as e:
        print(f"Error en la ruta /api/preview/{match_id}: {e}")
        return jsonify({'error': 'OcurriÃ³ un error interno en el servidor.'}), 500


def _get_cached_analysis(match_id):
    with _analysis_cache_lock:
        entry = _analysis_cache.get(match_id)
        if not entry:
            return None
        if time.time() - entry['timestamp'] > _ANALYSIS_CACHE_TTL_SECONDS:
            del _analysis_cache[match_id]
            return None
        return entry


def _store_analysis_in_cache(match_id, data, status):
    with _analysis_cache_lock:
        _analysis_cache[match_id] = {
            'data': data,
            'status': status,
            'timestamp': time.time()
        }


def _analysis_background_worker(match_id):
    try:
        payload, error = _build_analysis_payload(match_id)
        if error:
            _store_analysis_in_cache(match_id, {'error': error}, 'error')
        else:
            _store_analysis_in_cache(match_id, payload, 'ok')
    except Exception as exc:
        _store_analysis_in_cache(match_id, {'error': f"Error interno: {exc}"}, 'error')
    finally:
        with _analysis_processing_lock:
            _analysis_processing.discard(match_id)


def _build_analysis_payload(match_id):
    datos = obtener_datos_completos_partido(match_id)
    if not datos or (isinstance(datos, dict) and datos.get('error')):
        return None, (datos or {}).get('error', 'No se pudieron obtener datos.')

    def df_to_rows(df):
        rows = []
        try:
            if df is not None and hasattr(df, 'iterrows'):
                for idx, row in df.iterrows():
                    label = str(idx)
                    label = label.replace('Shots on Goal', 'Tiros a Puerta')                                     .replace('Shots', 'Tiros')                                     .replace('Dangerous Attacks', 'Ataques Peligrosos')                                     .replace('Attacks', 'Ataques')
                    try:
                        home_val = row['Casa']
                    except Exception:
                        home_val = ''
                    try:
                        away_val = row['Fuera']
                    except Exception:
                        away_val = ''
                    rows.append({'label': label, 'home': home_val or '', 'away': away_val or ''})
        except Exception:
            pass
        return rows

    payload = {
        'home_team': datos.get('home_name', ''),
        'away_team': datos.get('away_name', ''),
        'final_score': datos.get('score'),
        'match_date': datos.get('match_date'),
        'match_time': datos.get('match_time'),
        'match_datetime': datos.get('match_datetime'),
        'recent_indirect_full': {
            'last_home': None,
            'last_away': None,
            'h2h_col3': None
        },
        'comparativas_indirectas': {
            'left': None,
            'right': None
        }
    }

    main_odds = datos.get("main_match_odds_data")
    home_name = datos.get("home_name")
    away_name = datos.get("away_name")
    ah_actual_num = parse_ah_to_number_of(main_odds.get('ah_linea_raw', ''))

    favorito_actual_name = "Ninguno (lÃ­nea en 0)"
    if ah_actual_num is not None:
        if ah_actual_num > 0:
            favorito_actual_name = home_name
        elif ah_actual_num < 0:
            favorito_actual_name = away_name

    def get_cover_status_vs_current(details):
        if not details or ah_actual_num is None:
            return 'NEUTRO'
        try:
            score_str = details.get('score', '').replace(' ', '').replace(':', '-')
            if not score_str or '?' in score_str:
                return 'NEUTRO'

            h_home = details.get('home_team')
            h_away = details.get('away_team')

            status, _ = check_handicap_cover(score_str, ah_actual_num, favorito_actual_name, h_home, h_away, home_name)
            return status
        except Exception:
            return 'NEUTRO'

    def analyze_h2h_rivals(home_result, away_result):
        if not home_result or not away_result:
            return None
        try:
            home_goals = list(map(int, home_result.get('score', '0-0').split('-')))
            away_goals = list(map(int, away_result.get('score', '0-0').split('-')))
            home_goal_diff = home_goals[0] - home_goals[1]
            away_goal_diff = away_goals[0] - away_goals[1]
            if home_goal_diff > away_goal_diff:
                return "Contra rivales comunes, el Equipo Local ha obtenido mejores resultados"
            elif away_goal_diff > home_goal_diff:
                return "Contra rivales comunes, el Equipo Visitante ha obtenido mejores resultados"
            else:
                return "Los rivales han tenido resultados similares"
        except Exception:
            return None

    def analyze_indirect_comparison(result, team_name):
        if not result:
            return None
        try:
            status = get_cover_status_vs_current(result)
            if status == 'CUBIERTO':
                return f"Contra este rival, {team_name} habrÃ­a cubierto el handicap"
            elif status == 'NO CUBIERTO':
                return f"Contra este rival, {team_name} no habrÃ­a cubierto el handicap"
            else:
                return f"Contra este rival, el resultado para {team_name} serÃ­a indeterminado"
        except Exception:
            return None

    last_home = (datos.get('last_home_match') or {})
    last_home_details = last_home.get('details') or {}
    if last_home_details:
        payload['recent_indirect_full']['last_home'] = {
            'home': last_home_details.get('home_team'),
            'away': last_home_details.get('away_team'),
            'score': (last_home_details.get('score') or '').replace(':', ' : '),
            'ah': format_ah_as_decimal_string_of(last_home_details.get('handicap_line_raw') or '-'),
            'ou': last_home_details.get('ouLine') or '-',
            'stats_rows': df_to_rows(last_home.get('stats')),
            'date': last_home_details.get('date'),
            'cover_status': get_cover_status_vs_current(last_home_details)
        }

    last_away = (datos.get('last_away_match') or {})
    last_away_details = last_away.get('details') or {}
    if last_away_details:
        payload['recent_indirect_full']['last_away'] = {
            'home': last_away_details.get('home_team'),
            'away': last_away_details.get('away_team'),
            'score': (last_away_details.get('score') or '').replace(':', ' : '),
            'ah': format_ah_as_decimal_string_of(last_away_details.get('handicap_line_raw') or '-'),
            'ou': last_away_details.get('ouLine') or '-',
            'stats_rows': df_to_rows(last_away.get('stats')),
            'date': last_away_details.get('date'),
            'cover_status': get_cover_status_vs_current(last_away_details)
        }

    h2h_col3 = (datos.get('h2h_col3') or {})
    h2h_col3_details = h2h_col3.get('details') or {}
    if h2h_col3_details and h2h_col3_details.get('status') == 'found':
        h2h_col3_details_adapted = {
            'score': f"{h2h_col3_details.get('goles_home')}:{h2h_col3_details.get('goles_away')}",
            'home_team': h2h_col3_details.get('h2h_home_team_name'),
            'away_team': h2h_col3_details.get('h2h_away_team_name')
        }
        payload['recent_indirect_full']['h2h_col3'] = {
            'home': h2h_col3_details.get('h2h_home_team_name'),
            'away': h2h_col3_details.get('h2h_away_team_name'),
            'score': f"{h2h_col3_details.get('goles_home')} : {h2h_col3_details.get('goles_away')}",
            'ah': format_ah_as_decimal_string_of(h2h_col3_details.get('handicap_line_raw') or '-'),
            'ou': h2h_col3_details.get('ou_result') or '-',
            'stats_rows': df_to_rows(h2h_col3.get('stats')),
            'date': h2h_col3_details.get('date'),
            'cover_status': get_cover_status_vs_current(h2h_col3_details_adapted),
            'analysis': analyze_h2h_rivals(last_home_details, last_away_details)
        }

    h2h_general = (datos.get('h2h_general') or {})
    h2h_general_details = h2h_general.get('details') or {}
    if h2h_general_details:
        score_text = h2h_general_details.get('res6') or ''
        cover_input = {
            'score': score_text,
            'home_team': h2h_general_details.get('h2h_gen_home'),
            'away_team': h2h_general_details.get('h2h_gen_away')
        }
        payload['recent_indirect_full']['h2h_general'] = {
            'home': h2h_general_details.get('h2h_gen_home'),
            'away': h2h_general_details.get('h2h_gen_away'),
            'score': score_text.replace(':', ' : '),
            'ah': h2h_general_details.get('ah6') or '-',
            'ou': h2h_general_details.get('ou_result6') or '-',
            'stats_rows': df_to_rows(h2h_general.get('stats')),
            'date': h2h_general_details.get('date'),
            'cover_status': get_cover_status_vs_current(cover_input) if score_text else 'NEUTRO'
        }

    comp_left = (datos.get('comp_L_vs_UV_A') or {})
    comp_left_details = comp_left.get('details') or {}
    if comp_left_details:
        payload['comparativas_indirectas']['left'] = {
            'title_home_name': datos.get('home_name'),
            'title_away_name': datos.get('away_name'),
            'home_team': comp_left_details.get('home_team'),
            'away_team': comp_left_details.get('away_team'),
            'score': (comp_left_details.get('score') or '').replace(':', ' : '),
            'ah': format_ah_as_decimal_string_of(comp_left_details.get('ah_line') or '-'),
            'ou': comp_left_details.get('ou_line') or '-',
            'localia': comp_left_details.get('localia') or '',
            'stats_rows': df_to_rows(comp_left.get('stats')),
            'cover_status': get_cover_status_vs_current(comp_left_details),
            'analysis': analyze_indirect_comparison(comp_left_details, datos.get('home_name'))
        }

    comp_right = (datos.get('comp_V_vs_UL_H') or {})
    comp_right_details = comp_right.get('details') or {}
    if comp_right_details:
        payload['comparativas_indirectas']['right'] = {
            'title_home_name': datos.get('home_name'),
            'title_away_name': datos.get('away_name'),
            'home_team': comp_right_details.get('home_team'),
            'away_team': comp_right_details.get('away_team'),
            'score': (comp_right_details.get('score') or '').replace(':', ' : '),
            'ah': format_ah_as_decimal_string_of(comp_right_details.get('ah_line') or '-'),
            'ou': comp_right_details.get('ou_line') or '-',
            'localia': comp_right_details.get('localia') or '',
            'stats_rows': df_to_rows(comp_right.get('stats')),
            'cover_status': get_cover_status_vs_current(comp_right_details),
            'analysis': analyze_indirect_comparison(comp_right_details, datos.get('away_name'))
        }

    h2h_data = datos.get("h2h_data")
    simplified_html = ""
    if all([main_odds, h2h_data, home_name, away_name]):
        simplified_html = generar_analisis_mercado_simplificado(main_odds, h2h_data, home_name, away_name)

    payload['simplified_html'] = simplified_html

    return payload, None


@app.route('/api/analisis/<string:match_id>')
def api_analisis(match_id):
    try:
        entry = _get_cached_analysis(match_id)
        if entry:
            if entry['status'] == 'ok':
                return jsonify(entry['data'])
            return jsonify(entry['data']), 500

        with _analysis_processing_lock:
            if match_id not in _analysis_processing:
                _analysis_processing.add(match_id)
                threading.Thread(target=_analysis_background_worker, args=(match_id,), daemon=True).start()

        return jsonify({'status': 'processing', 'message': 'Generando anÃ¡lisis. Vuelve a consultar en unos segundos.'}), 202
    except Exception as e:
        print(f"Error en la ruta /api/analisis/{match_id}: {e}")
        return jsonify({'error': 'OcurriÃ³ un error interno en el servidor.'}), 500


@app.route('/start_analysis_background', methods=['POST'])
def start_analysis_background():
    match_id = request.json.get('match_id')
    if not match_id:
        return jsonify({'status': 'error', 'message': 'No se proporcionÃ³ match_id'}), 400

    def analysis_worker(app, match_id):
        with app.app_context():
            print(f"Iniciando anÃ¡lisis en segundo plano para el ID: {match_id}")
            try:
                obtener_datos_completos_partido(match_id)
                print(f"AnÃ¡lisis en segundo plano finalizado para el ID: {match_id}")
            except Exception as e:
                print(f"Error en el hilo de anÃ¡lisis para el ID {match_id}: {e}")

    thread = threading.Thread(target=analysis_worker, args=(app, match_id))
    thread.start()

    return jsonify({'status': 'success', 'message': f'AnÃ¡lisis iniciado para el partido {match_id}'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True) # debug=True es Ãºtil para desarrollar



