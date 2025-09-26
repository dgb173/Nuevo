# modules/estudio_scraper.py
from modules.analisis_avanzado import generar_analisis_comparativas_indirectas
from modules.analisis_reciente import analizar_rendimiento_reciente_con_handicap, comparar_lineas_handicap_recientes
from modules.analisis_rivales import analizar_rivales_comunes, analizar_contra_rival_del_rival
from modules.funciones_resumen import generar_resumen_rendimiento_reciente
from modules.funciones_auxiliares import _calcular_estadisticas_contra_rival, _analizar_over_under, _analizar_ah_cubierto, _analizar_desempeno_casa_fuera
import os
import time
import re
import math
from bs4 import BeautifulSoup
import pandas as pd
from concurrent.futures import ThreadPoolExecutor
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from modules.utils import parse_ah_to_number_of, format_ah_as_decimal_string_of, check_handicap_cover, check_goal_line_cover, get_match_details_from_row_of, extract_final_score_of

BASE_URL_OF = "https://www.nowgoal.com"
PLAYWRIGHT_TIMEOUT_MS = 20000
PLACEHOLDER_NODATA = "*(No disponible)*"
PLAYWRIGHT_SELECT_DELAY_MS = 400


playwright_launch_args = [
    "--headless=new",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-features=IsolateOrigins,site-per-process"
]


def _start_playwright_context():
    proxy_url = os.environ.get('PROXY_URL')
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=True, args=playwright_launch_args)
    context_kwargs = {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/116.0.0.0 Safari/537.36",
        "viewport": {"width": 1920, "height": 1080}
    }
    if proxy_url:
        context_kwargs["proxy"] = {"server": proxy_url}
    context = browser.new_context(**context_kwargs)
    return playwright, browser, context


def _close_playwright_context(playwright, browser, context):
    try:
        if context is not None:
            context.close()
    except Exception:
        pass
    try:
        if browser is not None:
            browser.close()
    except Exception:
        pass
    try:
        if playwright is not None:
            playwright.stop()
    except Exception:
        pass


def _load_page_html(context, url, wait_selector=None, selects=None, timeout_ms=PLAYWRIGHT_TIMEOUT_MS):
    page = context.new_page()
    try:
        page.goto(url, wait_until="networkidle", timeout=timeout_ms)
        if wait_selector:
            page.wait_for_selector(wait_selector, timeout=timeout_ms)
        if selects:
            for select_id, value in selects.items():
                try:
                    page.select_option(f"#{select_id}", value)
                    page.wait_for_timeout(PLAYWRIGHT_SELECT_DELAY_MS)
                except PlaywrightTimeout:
                    pass
                except Exception:
                    pass
        return page.content()
    finally:
        page.close()



def parse_ah_to_number_of(ah_line_str: str):
    if not isinstance(ah_line_str, str): return None
    s = ah_line_str.strip().replace(' ', '')
    if not s or s in ['-', '?']: return None
    original_starts_with_minus = ah_line_str.strip().startswith('-')
    try:
        if '/' in s:
            parts = s.split('/')
            if len(parts) != 2: return None
            p1_str, p2_str = parts[0], parts[1]
            val1 = float(p1_str)
            val2 = float(p2_str)
            if val1 < 0 and not p2_str.startswith('-') and val2 > 0:
                 val2 = -abs(val2)
            elif original_starts_with_minus and val1 == 0.0 and \
                 (p1_str == "0" or p1_str == "-0") and \
                 not p2_str.startswith('-') and val2 > 0:
                val2 = -abs(val2)
            return (val1 + val2) / 2.0
        else:
            return float(s)
    except (ValueError, IndexError):
        return None

def format_ah_as_decimal_string_of(ah_line_str: str, for_sheets=False):
    if not isinstance(ah_line_str, str) or not ah_line_str.strip() or ah_line_str.strip() in ['-', '?']:
        return ah_line_str.strip() if isinstance(ah_line_str, str) and ah_line_str.strip() in ['-','?'] else '-'
    numeric_value = parse_ah_to_number_of(ah_line_str)
    if numeric_value is None:
        return ah_line_str.strip() if ah_line_str.strip() in ['-','?'] else '-'
    if numeric_value == 0.0: return "0"
    sign = -1 if numeric_value < 0 else 1
    abs_num = abs(numeric_value)
    mod_val = abs_num % 1
    if mod_val == 0.0: abs_rounded = abs_num
    elif mod_val == 0.25: abs_rounded = math.floor(abs_num) + 0.25
    elif mod_val == 0.5: abs_rounded = abs_num
    elif mod_val == 0.75: abs_rounded = math.floor(abs_num) + 0.75
    else:
        if mod_val < 0.25: abs_rounded = math.floor(abs_num)
        elif mod_val < 0.75: abs_rounded = math.floor(abs_num) + 0.5
        else: abs_rounded = math.ceil(abs_num)
    final_value_signed = sign * abs_rounded
    if final_value_signed == 0.0: output_str = "0"
    elif abs(final_value_signed - round(final_value_signed, 0)) < 1e-9 : output_str = str(int(round(final_value_signed, 0)))
    elif abs(final_value_signed - (math.floor(final_value_signed) + 0.5)) < 1e-9: output_str = f"{final_value_signed:.1f}"
    elif abs(final_value_signed - (math.floor(final_value_signed) + 0.25)) < 1e-9 or \
         abs(final_value_signed - (math.floor(final_value_signed) + 0.75)) < 1e-9: output_str = f"{final_value_signed:.2f}".replace(".25", ".25").replace(".75", ".75")
    else: output_str = f"{final_value_signed:.2f}"
    if for_sheets:
        return "'" + output_str.replace('.', ',') if output_str not in ['-','?'] else output_str
    return output_str

def check_handicap_cover(resultado_raw: str, ah_line_num: float, favorite_team_name: str, home_team_in_h2h: str, away_team_in_h2h: str, main_home_team_name: str):
    """
    Simula si un resultado histÃ³rico habrÃ­a cubierto la lÃ­nea de hÃ¡ndicap actual.
    Maneja correctamente el HÃ¡ndicap AsiÃ¡tico 0.
    """
    try:
        goles_h, goles_a = map(int, resultado_raw.split('-'))

        # --- LÃ“GICA ESPECIAL PARA HÃNDICAP 0 (DRAW NO BET) ---
        if ah_line_num == 0.0:
            # Simulamos la apuesta sobre el equipo local del partido principal
            if main_home_team_name.lower() == home_team_in_h2h.lower(): # Si nuestro local jugaba de local
                if goles_h > goles_a: return ("CUBIERTO", True)
                elif goles_a > goles_h: return ("NO CUBIERTO", False)
                else: return ("PUSH", None)
            else: # Si nuestro local jugaba de visitante
                if goles_a > goles_h: return ("CUBIERTO", True)
                elif goles_h > goles_a: return ("NO CUBIERTO", False)
                else: return ("PUSH", None)
        
        # --- LÃ“GICA ANTERIOR PARA HÃNDICAPS CON FAVORITO ---
        if favorite_team_name.lower() == home_team_in_h2h.lower():
            favorite_margin = goles_h - goles_a
        elif favorite_team_name.lower() == away_team_in_h2h.lower():
            favorite_margin = goles_a - goles_h
        else:
            return ("indeterminado", None)
        
        if favorite_margin - abs(ah_line_num) > 0.05:
            return ("CUBIERTO", True)
        elif favorite_margin - abs(ah_line_num) < -0.05:
            return ("NO CUBIERTO", False)
        else:
            return ("PUSH", None)

    except (ValueError, TypeError, AttributeError):
        return ("indeterminado", None)

def check_goal_line_cover(resultado_raw: str, goal_line_num: float):
    try:
        goles_h, goles_a = map(int, resultado_raw.split('-'))
        total_goles = goles_h + goles_a
        if total_goles > goal_line_num:
            return ("SUPERADA (Over)", True)
        elif total_goles < goal_line_num:
            return (f"<span style='color: red; font-weight: bold;'>NO SUPERADA (UNDER) </span>", False)
        else:
            return ("PUSH (Igual)", None)
    except (ValueError, TypeError):
        return ("indeterminado", None)

def _analizar_precedente_handicap(precedente_data, ah_actual_num, favorito_actual_name, main_home_team_name):
    """
    FunciÃ³n helper para generar la sÃ­ntesis de HÃ¡ndicap de UN solo precedente.
    VERSIÃ“N FINAL CORREGIDA Y MEJORADA: Unifica la lÃ³gica para todos los cambios de favoritismo,
    incluyendo desde/hacia una lÃ­nea de 0, y siempre muestra el movimiento de la lÃ­nea.
    """
    res_raw = precedente_data.get('res_raw')
    ah_raw = precedente_data.get('ah_raw')
    home_team_precedente = precedente_data.get('home')
    away_team_precedente = precedente_data.get('away')

    if not all([res_raw, res_raw != '?-?', ah_raw, ah_raw != '-']):
        return "<li><span class='ah-value'>HÃ¡ndicap:</span> No hay datos suficientes en este precedente.</li>"

    ah_historico_num = parse_ah_to_number_of(ah_raw)
    comparativa_texto = ""

    if ah_historico_num is not None and ah_actual_num is not None:
        formatted_ah_historico = format_ah_as_decimal_string_of(ah_raw)
        formatted_ah_actual = format_ah_as_decimal_string_of(str(ah_actual_num))
        line_movement_str = f"{formatted_ah_historico} â†’ {formatted_ah_actual}"
        
        # 1. Identificar al favorito del partido histÃ³rico.
        favorito_historico_name = None
        if ah_historico_num > 0:
            favorito_historico_name = home_team_precedente
        elif ah_historico_num < 0:
            favorito_historico_name = away_team_precedente
        
        # 2. LÃ³gica de comparaciÃ³n unificada
        if favorito_actual_name.lower() == (favorito_historico_name or "").lower():
            # El favorito es el mismo equipo (o ambos son 'Ninguno'), ahora comparamos la magnitud.
            if abs(ah_actual_num) > abs(ah_historico_num):
                comparativa_texto = f"El mercado considera a este equipo <strong>mÃ¡s favorito</strong> que en el precedente (movimiento: <strong style='color: green; font-size:1.2em;'>{line_movement_str}</strong>). "
            elif abs(ah_actual_num) < abs(ah_historico_num):
                comparativa_texto = f"El mercado considera a este equipo <strong>menos favorito</strong> que en el precedente (movimiento: <strong style='color: orange; font-size:1.2em;'>{line_movement_str}</strong>). "
            else:
                comparativa_texto = f"El mercado mantiene una lÃ­nea de <strong>magnitud idÃ©ntica</strong> a la del precedente (<strong>{formatted_ah_historico}</strong>). "
        else:
            # Los favoritos han cambiado (A->B, Ninguno->A, o A->Ninguno).
            if favorito_historico_name and favorito_actual_name != "Ninguno (lÃ­nea en 0)":
                # Caso 1: Cambio total de favorito de un equipo a otro.
                comparativa_texto = f"Ha habido un <strong>cambio total de favoritismo</strong>. En el precedente el favorito era '{favorito_historico_name}' (movimiento: <strong style='color: red; font-size:1.2em;'>{line_movement_str}</strong>). "
            elif not favorito_historico_name:
                # Caso 2: Se establece un favorito donde antes no lo habÃ­a (lÃ­nea 0).
                comparativa_texto = f"El mercado establece un favorito claro, considerÃ¡ndolo <strong>mucho mÃ¡s favorito</strong> que en el precedente (movimiento: <strong style='color: green; font-size:1.2em;'>{line_movement_str}</strong>). "
            else: # favorito_actual_name es "Ninguno (lÃ­nea en 0)"
                # Caso 3: Se elimina un favorito que antes existÃ­a.
                comparativa_texto = f"El mercado <strong>ha eliminado al favorito</strong> ('{favorito_historico_name}') que existÃ­a en el precedente (movimiento: <strong style='color: orange; font-size:1.2em;'>{line_movement_str}</strong>). "
    else:
        comparativa_texto = f"No se pudo realizar una comparaciÃ³n detallada (lÃ­nea histÃ³rica: <strong>{format_ah_as_decimal_string_of(ah_raw)}</strong>). "

    # 3. Simular el resultado del hÃ¡ndicap
    resultado_cover, cubierto = check_handicap_cover(res_raw, ah_actual_num, favorito_actual_name, home_team_precedente, away_team_precedente, main_home_team_name)
    
    if cubierto is True:
        cover_html = f"<span style='color: green; font-weight: bold;'>CUBIERTO âœ…</span>"
    elif cubierto is False:
        cover_html = f"<span style='color: red; font-weight: bold;'>NO CUBIERTO âŒ</span>"
    else: # PUSH o indeterminado
        cover_html = f"<span style='color: #6c757d; font-weight: bold;'>{resultado_cover.upper()} ðŸ¤”</span>"

    return f"<li><span class='ah-value'>HÃ¡ndicap:</span> {comparativa_texto}Con el resultado ({res_raw.replace('-' , ':')}), la lÃ­nea actual se habrÃ­a considerado {cover_html}.</li>"

def _analizar_precedente_goles(precedente_data, goles_actual_num):
    res_raw = precedente_data.get('res_raw')
    if not res_raw or res_raw == '?-?':
        return "<li><span class='score-value'>Goles:</span> No hay datos suficientes en este precedente.</li>"
    try:
        total_goles = sum(map(int, res_raw.split('-')))
        resultado_cover, _ = check_goal_line_cover(res_raw, goles_actual_num)
        # Simplificar el mensaje para que sea mÃ¡s claro
        if 'SUPERADA' in resultado_cover:
            cover_html = "<span style='color: green; font-weight: bold;'>OVER</span>"
        elif 'NO SUPERADA' in resultado_cover:
            cover_html = "<span style='color: red; font-weight: bold;'>UNDER</span>"
        else: # PUSH or indeterminado
            cover_html = f"<span style='color: #6c757d; font-weight: bold;'>{resultado_cover}</span>"
        return f"<li><span class='score-value'>Goles:</span> El partido tuvo <strong>{total_goles} goles</strong>, por lo que la lÃ­nea actual habrÃ­a resultado {cover_html}.</li>"
    except (ValueError, TypeError):
        return "<li><span class='score-value'>Goles:</span> No se pudo procesar el resultado del precedente.</li>"

def generar_analisis_completo_mercado(main_odds, h2h_data, home_name, away_name):
    ah_actual_str = format_ah_as_decimal_string_of(main_odds.get('ah_linea_raw', '-'))
    ah_actual_num = parse_ah_to_number_of(ah_actual_str)
    goles_actual_num = parse_ah_to_number_of(main_odds.get('goals_linea_raw', '-'))
    if ah_actual_num is None or goles_actual_num is None: return ""
    favorito_name, favorito_html = "Ninguno (lÃ­nea en 0)", "Ninguno (lÃ­nea en 0)"
    if ah_actual_num < 0:
        favorito_name, favorito_html = away_name, f"<span class='away-color'>{away_name}</span>"
    elif ah_actual_num > 0:
        favorito_name, favorito_html = home_name, f"<span class='home-color'>{home_name}</span>"
    titulo_html = f"<p style='margin-bottom: 12px;'><strong>ðŸ“Š AnÃ¡lisis de Mercado vs. HistÃ³rico H2H</strong><br><span style='font-style: italic; font-size: 0.9em;'>LÃ­neas actuales: AH {ah_actual_str} / Goles {goles_actual_num} | Favorito: {favorito_html}</span></p>"
    precedente_estadio = {
        'res_raw': h2h_data.get('res1_raw'), 'ah_raw': h2h_data.get('ah1'),
        'home': home_name, 'away': away_name, 'match_id': h2h_data.get('match1_id')
    }
    sintesis_ah_estadio = _analizar_precedente_handicap(precedente_estadio, ah_actual_num, favorito_name, home_name)
    sintesis_goles_estadio = _analizar_precedente_goles(precedente_estadio, goles_actual_num)
    analisis_estadio_html = (
        f"<div style='margin-bottom: 10px;'>"
        f"  <strong style='font-size: 1.05em;'>ðŸŸï¸ AnÃ¡lisis del Precedente en Este Estadio</strong>"
        f"  <ul style='margin: 5px 0 0 20px; padding-left: 0;'>{sintesis_ah_estadio}{sintesis_goles_estadio}</ul>"
        f"</div>"
    )
    precedente_general_id = h2h_data.get('match6_id')
    if precedente_estadio['match_id'] and precedente_general_id and precedente_estadio['match_id'] == precedente_general_id:
        analisis_general_html = (
            "<div style='margin-top: 10px;'>"
            "  <strong>âœˆï¸ AnÃ¡lisis del H2H General MÃ¡s Reciente</strong>"
            "  <p style='margin: 5px 0 0 20px; font-style: italic; font-size: 0.9em;'>"
            "    El precedente es el mismo partido analizado arriba."
            "  </p>"
            "</div>"
        )
    else:
        precedente_general = {
            'res_raw': h2h_data.get('res6_raw'),
            'ah_raw': h2h_data.get('ah6'),
            'home': h2h_data.get('h2h_gen_home'),
            'away': h2h_data.get('h2h_gen_away'),
            'match_id': precedente_general_id
        }
        sintesis_ah_general = _analizar_precedente_handicap(precedente_general, ah_actual_num, favorito_name, home_name)
        sintesis_goles_general = _analizar_precedente_goles(precedente_general, goles_actual_num)
        analisis_general_html = (
            f"<div>"
            f"  <strong style='font-size: 1.05em;'>âœˆï¸ AnÃ¡lisis del H2H General MÃ¡s Reciente</strong>"
            f"  <ul style='margin: 5px 0 0 20px; padding-left: 0;'>{sintesis_ah_general}{sintesis_goles_general}</ul>"
            f"</div>"
        )
    return f'''
    <div style="border-left: 4px solid #1E90FF; padding: 12px 15px; margin-top: 15px; background-color: #f0f2f6; border-radius: 5px; font-size: 0.95em;">
        {titulo_html}
        {analisis_estadio_html}
        {analisis_general_html}
    </div>
    '''

def _analizar_precedente_mercado_simplificado(precedente_data, ah_actual_num, favorito_actual_name, main_home_team_name):
    res_raw = precedente_data.get('res_raw')
    if not res_raw or res_raw == '?-?':
        return None

    ah_raw = precedente_data.get('ah_raw')
    home_team_precedente = precedente_data.get('home')
    away_team_precedente = precedente_data.get('away')

    line_movement_str = 'N/A'
    if ah_actual_num is not None:
        formatted_actual = format_ah_as_decimal_string_of(str(ah_actual_num))
        if ah_raw and ah_raw != '-':
            formatted_historico = format_ah_as_decimal_string_of(ah_raw)
            line_movement_str = f"{formatted_historico} -> {formatted_actual}"
        else:
            line_movement_str = f"Sin dato -> {formatted_actual}"
    elif ah_raw and ah_raw != '-':
        line_movement_str = format_ah_as_decimal_string_of(ah_raw)

    cover_status = 'NEUTRO'
    if ah_actual_num is not None:
        cover_status, _ = check_handicap_cover(
            res_raw,
            ah_actual_num,
            favorito_actual_name,
            home_team_precedente,
            away_team_precedente,
            main_home_team_name
        )

    cover_styles = {
        'CUBIERTO': "<span style='color: green; font-weight: bold;'>CUBIERTO</span>",
        'NO CUBIERTO': "<span style='color: red; font-weight: bold;'>NO CUBIERTO</span>",
        'NULO': "<span style='color: #000000; font-weight: bold;'>NULO</span>",
        'NEUTRO': "<span style='color: #6c757d; font-weight: bold;'>NEUTRO</span>"
    }
    cover_html = cover_styles.get(
        cover_status,
        f"<span style='color: #6c757d; font-weight: bold;'>{cover_status.upper()}</span>"
    )

    return {
        'resultado': res_raw.replace('-', ':'),
        'movimiento_linea': line_movement_str,
        'cobertura': cover_html
    }

def generar_analisis_mercado_simplificado(main_odds, h2h_data, home_name, away_name):
    ah_actual_str = format_ah_as_decimal_string_of(main_odds.get('ah_linea_raw', '-'))
    ah_actual_num = parse_ah_to_number_of(ah_actual_str)
    if ah_actual_num is None:
        return ""

    favorito_name = "Ninguno (lÃ­nea en 0)"
    if ah_actual_num < 0:
        favorito_name = away_name
    elif ah_actual_num > 0:
        favorito_name = home_name

    # Analizar precedente en el mismo estadio
    precedente_estadio_data = {
        'res_raw': h2h_data.get('res1_raw'), 'ah_raw': h2h_data.get('ah1'),
        'home': home_name, 'away': away_name, 'match_id': h2h_data.get('match1_id')
    }
    analisis_estadio = _analizar_precedente_mercado_simplificado(precedente_estadio_data, ah_actual_num, favorito_name, home_name)

    # Analizar precedente general mÃ¡s reciente
    precedente_general_data = {
        'res_raw': h2h_data.get('res6_raw'),
        'ah_raw': h2h_data.get('ah6'),
        'home': h2h_data.get('h2h_gen_home'),
        'away': h2h_data.get('h2h_gen_away'),
        'match_id': h2h_data.get('match6_id')
    }
    analisis_general = _analizar_precedente_mercado_simplificado(precedente_general_data, ah_actual_num, favorito_name, home_name)

    # Construir HTML
    html = '<div class="card"><div class="card-body">'
    html += '<h6 class="card-title">ðŸ“Š AnÃ¡lisis Mercado vs. H2H</h6>'

    if analisis_estadio:
        html += f"""
            <div class="mb-2">
                <strong>Precedente (este estadio):</strong>
                <div>Resultado: <span class="score-value">{analisis_estadio['resultado']}</span></div>
                <div>Mov. LÃ­nea: <span class="ah-value">{analisis_estadio['movimiento_linea']}</span></div>
                <div>Cobertura AH: {analisis_estadio['cobertura']}</div>
            </div>
        """
    else:
        html += "<p>No hay precedente H2H en este estadio.</p>"

    # Evitar duplicados si el precedente general es el mismo
    match1_id = h2h_data.get('match1_id')
    match6_id = h2h_data.get('match6_id')

    if match6_id:
        if analisis_general:
            if match1_id and match1_id != match6_id:
                html += "<hr>"
            html += f"""
                <div class="mt-2">
                    <strong>Precedente (H2H mÇ­s reciente):</strong>
                    <div>Resultado: <span class="score-value">{analisis_general['resultado']}</span></div>
                    <div>Mov. Lï¿½ï¿½nea: <span class="ah-value">{analisis_general['movimiento_linea']}</span></div>
                    <div>Cobertura AH: {analisis_general['cobertura']}</div>
                </div>
            """
        elif match1_id and match1_id != match6_id:
            html += "<hr><p>No hay precedente H2H general.</p>"

    html += '</div></div>'
    return html

def get_match_details_from_row_of(row_element, score_class_selector='score', source_table_type='h2h'):
    try:
        cells = row_element.find_all('td')
        home_idx, score_idx, away_idx, ah_idx = 2, 3, 4, 11
        if len(cells) <= ah_idx: return None
        date_span = cells[1].find('span', attrs={'name': 'timeData'})
        date_txt = date_span.get_text(strip=True) if date_span else ''
        def get_cell_txt(idx):
            a = cells[idx].find('a')
            return a.get_text(strip=True) if a else cells[idx].get_text(strip=True)
        home, away = get_cell_txt(home_idx), get_cell_txt(away_idx)
        if not home or not away: return None
        score_cell = cells[score_idx]
        score_span = score_cell.find('span', class_=lambda c: isinstance(c, str) and score_class_selector in c)
        score_raw_text = (score_span.get_text(strip=True) if score_span else score_cell.get_text(strip=True)) or ''
        m = re.search(r'(\d+)\s*-\s*(\d+)', score_raw_text)
        score_raw, score_fmt = (f"{m.group(1)}-{m.group(2)}", f"{m.group(1)}:{m.group(2)}") if m else ('?-?', '?:?')
        ah_cell = cells[ah_idx]
        ah_line_raw = (ah_cell.get('data-o') or ah_cell.text).strip()
        ah_line_fmt = format_ah_as_decimal_string_of(ah_line_raw) if ah_line_raw not in ['', '-'] else '-'
        return {
            'date': date_txt, 'home': home, 'away': away, 'score': score_fmt,
            'score_raw': score_raw, 'ahLine': ah_line_fmt, 'ahLine_raw': ah_line_raw or '-',
            'matchIndex': row_element.get('index'), 'vs': row_element.get('vs'),
            'league_id_hist': row_element.get('name')
        }
    except Exception:
        return None

def _colorear_stats(val1_str, val2_str):
    """Compara dos valores de estadÃ­sticas y devuelve strings con formato HTML para colorearlos."""
    try:
        val1 = int(val1_str)
        val2 = int(val2_str)
        if val1 > val2:
            return f'<span style="color: green; font-weight: bold;">{val1}</span>', f'<span style="color: red;">{val2}</span>'
        elif val2 > val1:
            return f'<span style="color: red;">{val1}</span>', f'<span style="color: green; font-weight: bold;">{val2}</span>'
        else:
            # Si son iguales, no se aplica color
            return val1_str, val2_str
    except (ValueError, TypeError):
        # Si no se pueden convertir a nÃºmeros (ej. texto), devolver los originales
        return val1_str, val2_str

def get_match_progression_stats_data(match_id: str) -> pd.DataFrame | None:
    if not match_id or not match_id.isdigit(): return None
    url = f"{BASE_URL_OF}/match/live-{match_id}"
    try:
        session = requests.Session()
        retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retries)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/116.0.0.0 Safari/537.36"})
        response = session.get(url, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'lxml')
        
        # Definir el orden especÃ­fico de las estadÃ­sticas (sin Yellow Cards)
        stat_order = ["Corners", "Shots", "Shots on Goal", "Attacks", "Dangerous Attacks", "Red Cards"]
        stat_titles = {stat: "-" for stat in stat_order}
        
        team_tech_div = soup.find('div', id='teamTechDiv_detail')
        if team_tech_div and (stat_list := team_tech_div.find('ul', class_='stat')):
            for li in stat_list.find_all('li'):
                if (title_span := li.find('span', class_='stat-title')) and (stat_title := title_span.get_text(strip=True)) in stat_titles:
                    values = [v.get_text(strip=True) for v in li.find_all('span', class_='stat-c')]
                    if len(values) == 2:
                        home_val, away_val = _colorear_stats(values[0], values[1])
                        stat_titles[stat_title] = {"Home": home_val, "Away": away_val}
        
        # Si no encontramos las tarjetas rojas en la secciÃ³n principal, las buscamos en la secciÃ³n de eventos
        if stat_titles["Red Cards"] == "-":
            red_cards = {"Home": 0, "Away": 0}
            events_table = soup.find('table', id='eventsTable')
            if events_table:
                # Buscar imÃ¡genes de tarjetas rojas
                red_card_images = events_table.find_all('img', alt='Red Card')
                for img in red_card_images:
                    # Determinar si es para el equipo local o visitante basado en la estructura de la tabla
                    parent_td = img.find_parent('td')
                    if parent_td:
                        # Si el td tiene style="text-align: right;", es para el equipo local
                        if "text-align: right;" in parent_td.get('style', ''):
                            red_cards["Home"] += 1
                        # Si el td tiene style="text-align: left;", es para el equipo visitante
                        elif "text-align: left;" in parent_td.get('style', ''):
                            red_cards["Away"] += 1
            stat_titles["Red Cards"] = red_cards
            
        # Eliminamos la extracciÃ³n de tarjetas amarillas segÃºn solicitud
        # Pasamos directamente a procesar Red Cards
            
        # Crear las filas respetando el orden definido
        table_rows = []
        for stat_name in stat_order:
            vals = stat_titles[stat_name]
            if isinstance(vals, dict):
                table_rows.append({
                    "Estadistica_EN": stat_name,
                    "Casa": vals.get('Home', '-'),
                    "Fuera": vals.get('Away', '-')
                })
        
        df = pd.DataFrame(table_rows)
        return df.set_index("Estadistica_EN") if not df.empty else df
    except requests.RequestException:
        return None

def get_rival_a_for_original_h2h_of(soup, league_id=None):
    if not soup or not (table := soup.find("table", id="table_v1")): return None, None, None
    for row in table.find_all("tr", id=re.compile(r"tr1_\d+")):
        if league_id and row.get("name") != str(league_id):
            continue
        if row.get("vs") == "1" and (key_id := row.get("index")):
            onclicks = row.find_all("a", onclick=True)
            if len(onclicks) > 1 and (rival_tag := onclicks[1]) and (rival_id_match := re.search(r"team\((\d+)\)", rival_tag.get("onclick", ""))):
                return key_id, rival_id_match.group(1), rival_tag.text.strip()
    return None, None, None

def get_rival_b_for_original_h2h_of(soup, league_id=None):
    if not soup or not (table := soup.find("table", id="table_v2")): return None, None, None
    for row in table.find_all("tr", id=re.compile(r"tr2_\d+")):
        if league_id and row.get("name") != str(league_id):
            continue
        if row.get("vs") == "1" and (key_id := row.get("index")):
            onclicks = row.find_all("a", onclick=True)
            if len(onclicks) > 0 and (rival_tag := onclicks[0]) and (rival_id_match := re.search(r"team\((\d+)\)", rival_tag.get("onclick", ""))):
                return key_id, rival_id_match.group(1), rival_tag.text.strip()
    return None, None, None


def get_h2h_details_for_original_logic_of(context, key_match_id, rival_a_id, rival_b_id, rival_a_name="Rival A", rival_b_name="Rival B"):
    if not all([context, key_match_id, rival_a_id, rival_b_id]):
        return {"status": "error", "resultado": "N/A (Datos incompletos para H2H)"}

    url = f"{BASE_URL_OF}/match/h2h-{key_match_id}"
    try:
        html = _load_page_html(context, url, wait_selector="#table_v2", selects={"hSelect_2": "8"})
    except PlaywrightTimeout:
        return {"status": "error", "resultado": "N/A (Timeout cargando H2H Col3)"}
    except Exception as e:
        return {"status": "error", "resultado": f"N/A (Error Playwright en H2H Col3: {type(e).__name__})"}

    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", id="table_v2")
    if not table:
        return {"status": "error", "resultado": "N/A (Tabla H2H Col3 no encontrada)"}

    for row in table.find_all("tr", id=re.compile(r"tr2_\d+")):
        links = row.find_all("a", onclick=True)
        if len(links) < 2:
            continue
        h_id_m = re.search(r"team\((\d+)\)", links[0].get("onclick", ""))
        a_id_m = re.search(r"team\((\d+)\)", links[1].get("onclick", ""))
        if not h_id_m or not a_id_m:
            continue
        if {h_id_m.group(1), a_id_m.group(1)} == {str(rival_a_id), str(rival_b_id)}:
            score_cell = row.find("td", class_="score")
            goles_home = goles_away = '-'
            if score_cell:
                marcador = score_cell.get_text(strip=True)
                if marcador and '-' in marcador:
                    goles_home, goles_away = marcador.split('-', 1)
            fecha = row.find("span", attrs={"name": "timeData"})
            return {
                "status": "found",
                "match_id": row.get("id", "").replace("tr2_", ""),
                "h2h_home_team_name": rival_a_name,
                "h2h_away_team_name": rival_b_name,
                "goles_home": goles_home.strip(),
                "goles_away": goles_away.strip(),
                "date": fecha.get_text(strip=True) if fecha else "",
                "handicap_line_raw": row.get('ah'),
                "ou_result": row.get('goal')
            }

    return {"status": "error", "resultado": "N/A (No se encontraron datos de H2H coincidentes)"}



def get_team_league_info_from_script_of(soup):
    script_tag = soup.find("script", string=re.compile(r"var _matchInfo = "))
    if not (script_tag and script_tag.string): return (None,) * 3 + ("N/A",) * 3
    content = script_tag.string
    def find_val(pattern):
        match = re.search(pattern, content)
        return match.group(1).replace("'", "") if match else None
    home_id = find_val(r"hId:\s*parseInt\('(\d+)'\)")
    away_id = find_val(r"gId:\s*parseInt\('(\d+)'\)")
    league_id = find_val(r"sclassId:\s*parseInt\('(\d+)'\)")
    home_name = find_val(r"hName:\s*'([^']*)'") or "N/A"
    away_name = find_val(r"gName:\s*'([^']*)'") or "N/A"
    league_name = find_val(r"lName:\s*'([^']*)'") or "N/A"
    return home_id, away_id, league_id, home_name, away_name, league_name

def get_match_datetime_from_script_of(soup):
    """
    Extrae fecha/hora del partido desde el script _matchInfo si estÃ¡ disponible.
    Devuelve dict con 'match_date', 'match_time' y 'match_datetime'.
    """
    result = {"match_date": None, "match_time": None, "match_datetime": None}
    try:
        script_tag = soup.find("script", string=re.compile(r"var _matchInfo = "))
        if not (script_tag and script_tag.string):
            return result
        content = script_tag.string

        def find_val(pattern):
            m = re.search(pattern, content)
            return m.group(1) if m else None

        # Posibles fuentes en el script
        match_time_txt = find_val(r"matchTime:\s*'([^']+)'")  # ej: 9/9/2025 5:00:00 PM
        start_date = find_val(r"startDate:\s*'([^']+)'")      # ej: 2025-09-09
        door_time = find_val(r"doorTime:\s*'([^']+)'")        # ej: 09:00:00.000+08:00

        normalized_date = None
        normalized_time = None

        # 1) Intentar usar matchTime completo si viene (m/d/Y h:m:s AM/PM)
        if match_time_txt:
            try:
                # Aceptar tanto m/d como mm/dd
                from datetime import datetime
                dt = None
                for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M"):
                    try:
                        dt = datetime.strptime(match_time_txt, fmt)
                        break
                    except Exception:
                        continue
                if dt:
                    normalized_date = dt.strftime("%Y-%m-%d")
                    normalized_time = dt.strftime("%H:%M")
            except Exception:
                pass

        # 2) Si no hay matchTime, combinar startDate + doorTime
        if not normalized_date and start_date:
            normalized_date = start_date
            if door_time:
                # Tomar HH:MM de door_time y omitir zona
                m = re.match(r"(\d{2}):(\d{2})", door_time)
                if m:
                    normalized_time = f"{m.group(1)}:{m.group(2)}"

        if normalized_date:
            result["match_date"] = normalized_date
            result["match_time"] = normalized_time
            result["match_datetime"] = f"{normalized_date} {normalized_time}".strip()
    except Exception:
        pass
    return result

def _parse_date_ddmmyyyy(d: str) -> tuple:
    m = re.search(r'(\d{2})-(\d{2})-(\d{4})', d or '')
    return (int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else (1900, 1, 1)

def extract_last_match_in_league_of(soup, table_id, team_name, league_id, is_home_game):
    if not soup or not (table := soup.find("table", id=table_id)): return None
    candidate_matches = []
    score_selector = 'fscore_1' if is_home_game else 'fscore_2'
    for row in table.find_all("tr", id=re.compile(rf"tr{table_id[-1]}_\d+")):
        if not (details := get_match_details_from_row_of(row, score_class_selector=score_selector, source_table_type='hist')):
            continue
        if league_id and details.get("league_id_hist") != str(league_id):
            continue
        is_team_home = team_name.lower() in details.get('home', '').lower()
        is_team_away = team_name.lower() in details.get('away', '').lower()
        if (is_home_game and is_team_home) or (not is_home_game and is_team_away):
            candidate_matches.append(details)
    if not candidate_matches: return None
    candidate_matches.sort(key=lambda x: _parse_date_ddmmyyyy(x.get('date', '')), reverse=True)
    last_match = candidate_matches[0]
    return {
        "date": last_match.get('date', 'N/A'), "home_team": last_match.get('home'),
        "away_team": last_match.get('away'), "score": last_match.get('score_raw', 'N/A').replace('-', ':'),
        "handicap_line_raw": last_match.get('ahLine_raw', 'N/A'), "match_id": last_match.get('matchIndex')
    }

def extract_bet365_initial_odds_of(soup):
    odds_info = {
        "ah_home_cuota": "N/A", "ah_linea_raw": "N/A", "ah_away_cuota": "N/A",
        "goals_over_cuota": "N/A", "goals_linea_raw": "N/A", "goals_under_cuota": "N/A"
    }
    if not soup: return odds_info
    bet365_row = soup.select_one("tr#tr_o_1_8[name='earlyOdds'], tr#tr_o_1_31[name='earlyOdds']")
    if not bet365_row: return odds_info
    tds = bet365_row.find_all("td")
    if len(tds) >= 11:
        odds_info["ah_home_cuota"] = tds[2].get("data-o", tds[2].text).strip()
        odds_info["ah_linea_raw"] = tds[3].get("data-o", tds[3].text).strip()
        odds_info["ah_away_cuota"] = tds[4].get("data-o", tds[4].text).strip()
        odds_info["goals_over_cuota"] = tds[8].get("data-o", tds[8].text).strip()
        odds_info["goals_linea_raw"] = tds[9].get("data-o", tds[9].text).strip()
        odds_info["goals_under_cuota"] = tds[10].get("data-o", tds[10].text).strip()
    return odds_info

def extract_standings_data_from_h2h_page_of(soup, team_name):
    data = {"name": team_name, "ranking": "N/A", "total_pj": "N/A", "total_v": "N/A",
            "total_e": "N/A", "total_d": "N/A", "total_gf": "N/A", "total_gc": "N/A",
            "specific_pj": "N/A", "specific_v": "N/A", "specific_e": "N/A",
            "specific_d": "N/A", "specific_gf": "N/A", "specific_gc": "N/A",
            "specific_type": "N/A"}
    if not soup or not team_name:
        return data
    standings_section = soup.find("div", id="porletP4")
    if not standings_section:
        return data
    team_table_soup = None
    is_home_table = False
    home_div = standings_section.find("div", class_="home-div")
    if home_div and team_name.lower() in home_div.get_text(strip=True).lower():
        team_table_soup = home_div.find("table", class_="team-table-home")
        is_home_table = True
        data["specific_type"] = "Est. como Local (en Liga)"
    else:
        guest_div = standings_section.find("div", class_="guest-div")
        if guest_div and team_name.lower() in guest_div.get_text(strip=True).lower():
            team_table_soup = guest_div.find("table", class_="team-table-guest")
            is_home_table = False
            data["specific_type"] = "Est. como Visitante (en Liga)"
    if not team_table_soup:
        return data
    header_link = team_table_soup.find("a")
    if header_link:
        full_text = header_link.get_text(separator=" ", strip=True)
        rank_match = re.search(r'\[.*?-(\d+)\]', full_text)
        if rank_match:
            data["ranking"] = rank_match.group(1)
    all_rows = team_table_soup.find_all("tr", align="center")
    is_ft_section = False
    for row in all_rows:
        header_cell = row.find("th")
        if header_cell:
            header_text = header_cell.get_text(strip=True)
            if "FT" in header_text:
                is_ft_section = True
            elif "HT" in header_text:
                is_ft_section = False
            continue
        if is_ft_section and len(cells := row.find_all("td")) >= 7:
            row_type_element = cells[0].find("span") or cells[0]
            row_type = row_type_element.get_text(strip=True)
            stats = [cell.get_text(strip=True) for cell in cells[1:7]]
            pj, v, e, d, gf, gc = stats
            if row_type == "Total":
                data.update({"total_pj": pj, "total_v": v, "total_e": e,
                            "total_d": d, "total_gf": gf, "total_gc": gc})
            specific_row_needed = "Home" if is_home_table else "Away"
            if row_type == specific_row_needed:
                data.update({"specific_pj": pj, "specific_v": v, "specific_e": e,
                            "specific_d": d, "specific_gf": gf, "specific_gc": gc})
    return data

def extract_over_under_stats_from_div_of(soup, team_type: str):
    default_stats = {"over_pct": 0, "under_pct": 0, "push_pct": 0, "total": 0}
    if not soup:
        return default_stats
    table_id = "table_v1" if team_type == 'home' else "table_v2"
    table = soup.find("table", id=table_id)
    if not table:
        return default_stats
    y_bar = table.find("ul", class_="y-bar")
    if not y_bar:
        return default_stats
    ou_group = None
    for group in y_bar.find_all("li", class_="group"):
        if "Over/Under Odds" in group.get_text():
            ou_group = group
            break
    if not ou_group:
        return default_stats
    try:
        total_text = ou_group.find("div", class_="tit").find("span").get_text(strip=True)
        total_match = re.search(r'\((\d+)\s*games\)', total_text)
        total = int(total_match.group(1)) if total_match else 0
        values = ou_group.find_all("span", class_="value")
        if len(values) == 3:
            over_pct_text = values[0].get_text(strip=True).replace('%', '')
            push_pct_text = values[1].get_text(strip=True).replace('%', '')
            under_pct_text = values[2].get_text(strip=True).replace('%', '')
            return {"over_pct": float(over_pct_text), "under_pct": float(under_pct_text), "push_pct": float(push_pct_text), "total": total}
    except (ValueError, TypeError, AttributeError):
        return default_stats
    return default_stats

def extract_h2h_data_of(soup, home_name, away_name, league_id=None):
    results = {'ah1': '-', 'res1': '?:?', 'res1_raw': '?-?', 'match1_id': None, 'ah6': '-', 'res6': '?:?', 'res6_raw': '?-?', 'match6_id': None, 'h2h_gen_home': "Local (H2H Gen)", 'h2h_gen_away': "Visitante (H2H Gen)"}
    if not soup or not home_name or not away_name or not (h2h_table := soup.find("table", id="table_v3")): return results
    all_matches = []
    for r in h2h_table.find_all("tr", id=re.compile(r"tr3_\d+")):
        if (d := get_match_details_from_row_of(r, score_class_selector='fscore_3', source_table_type='h2h')):
            if not league_id or (d.get('league_id_hist') and d.get('league_id_hist') == str(league_id)):
                all_matches.append(d)
    if not all_matches: return results
    all_matches.sort(key=lambda x: _parse_date_ddmmyyyy(x.get('date', '')), reverse=True)
    most_recent = all_matches[0]
    results.update({'ah6': most_recent.get('ahLine', '-'), 'res6': most_recent.get('score', '?:?'), 'res6_raw': most_recent.get('score_raw', '?-?'), 'match6_id': most_recent.get('matchIndex'), 'h2h_gen_home': most_recent.get('home'), 'h2h_gen_away': most_recent.get('away')})
    for d in all_matches:
        if d['home'].lower() == home_name.lower() and d['away'].lower() == away_name.lower():
            results.update({'ah1': d.get('ahLine', '-'), 'res1': d.get('score', '?:?'), 'res1_raw': d.get('score_raw', '?-?'), 'match1_id': d.get('matchIndex')})
            break
    return results

def extract_comparative_match_of(soup, table_id, main_team, opponent, league_id, is_home_table):
    if not opponent or opponent == "N/A" or not main_team or not (table := soup.find("table", id=table_id)): return None
    score_selector = 'fscore_1' if is_home_table else 'fscore_2'
    for row in table.find_all("tr", id=re.compile(rf"tr{table_id[-1]}_\d+")):
        if not (details := get_match_details_from_row_of(row, score_class_selector=score_selector, source_table_type='hist')): continue
        if league_id and details.get('league_id_hist') and details.get('league_id_hist') != str(league_id): continue
        h, a = details.get('home','').lower(), details.get('away','').lower()
        main, opp = main_team.lower(), opponent.lower()
        if (main == h and opp == a) or (main == a and opp == h):
            return {"score": details.get('score', '?:?'), "ah_line": details.get('ahLine', '-'), "localia": 'H' if main == h else 'A', "home_team": details.get('home'), "away_team": details.get('away'), "match_id": details.get('matchIndex')}
    return None

def extract_indirect_comparison_data(soup):
    """
    Extrae los datos de los dos paneles de Comparativas Indirectas.
    """
    data = {"comp1": None, "comp2": None}
    comparativas_divs = soup.select("div.football-history-list > div.content") # Asumiendo una estructura de selectores; ajustar si es necesario.

    if len(comparativas_divs) < 2:
        return data

    def parse_comparison_box(box_soup):
        try:
            # TÃ­tulo: "Yangon United FC U21 vs. Ãšlt. Rival de Dagon FC U21"
            title = box_soup.find("div", class_="title").get_text(strip=True)
            main_team_name = title.split(' vs. ')[0]

            # Resultado: "0 : 1"
            res_text = box_soup.find(string=re.compile(r"Res\s*:")).find_next("span").get_text(strip=True)
            res_raw = res_text.replace(' ', '').replace(':', '-')

            # HÃ¡ndicap AsiÃ¡tico: "AH: 4"
            ah_text = box_soup.find(string=re.compile(r"AH\s*:")).find_next("span").get_text(strip=True)
            ah_num = parse_ah_to_number_of(ah_text)

            # LocalÃ­a: "H" o "A"
            localia_text = box_soup.find(string=re.compile(r"LocalÃ­a de")).find_next("span").get_text(strip=True)

            # EstadÃ­sticas
            stats = {}
            stats_table = box_soup.find("table") # Asumiendo que las stats estÃ¡n en una tabla
            rows = stats_table.find_all("tr")
            
            # Ejemplo de extracciÃ³n de estadÃ­sticas (ajustar a la estructura real del HTML)
            # Esto es un placeholder, el cÃ³digo real podrÃ­a necesitar ser mÃ¡s robusto
            stats['tiros_casa'] = rows[0].find_all('td')[0].text.strip()
            stats['tiros_fuera'] = rows[0].find_all('td')[2].text.strip()
            stats['tiros_puerta_casa'] = rows[1].find_all('td')[0].text.strip()
            stats['tiros_puerta_fuera'] = rows[1].find_all('td')[2].text.strip()
            stats['ataques_casa'] = rows[2].find_all('td')[0].text.strip()
            stats['ataques_fuera'] = rows[2].find_all('td')[2].text.strip()
            stats['ataques_peligrosos_casa'] = rows[3].find_all('td')[0].text.strip()
            stats['ataques_peligrosos_fuera'] = rows[3].find_all('td')[2].text.strip()

            return {
                "main_team": main_team_name,
                "resultado": res_text,
                "resultado_raw": res_raw,
                "ah_raw": ah_text,
                "ah_num": ah_num,
                "localia": localia_text,
                "stats": stats
            }
        except Exception:
            return None

    data["comp1"] = parse_comparison_box(comparativas_divs[0])
    data["comp2"] = parse_comparison_box(comparativas_divs[1])

    return data

# --- FUNCIÃ“N PRINCIPAL DE EXTRACCIÃ“N ---


def obtener_datos_completos_partido(match_id: str):
    """Scrapea y arma el analisis completo para el ID de partido dado."""
    if not match_id or not match_id.isdigit():
        return {"error": "ID de partido invalido."}

    playwright = browser = context = None
    try:
        playwright, browser, context = _start_playwright_context()
        main_page_url = f"{BASE_URL_OF}/match/h2h-{match_id}"
        try:
            main_page_html = _load_page_html(
                context,
                main_page_url,
                wait_selector="#table_v1",
                selects={"hSelect_1": "8", "hSelect_2": "8", "hSelect_3": "8"}
            )
        except PlaywrightTimeout:
            return {"error": "No se pudo cargar la pagina principal del partido (timeout)."}
        except Exception as exc:
            return {"error": f"No se pudo cargar la pagina principal del partido: {type(exc).__name__}"}

        soup_completo = BeautifulSoup(main_page_html, "lxml")
        datos = {"match_id": match_id}
        datos['final_score'] = extract_final_score_of(soup_completo)

        home_id, away_id, league_id, home_name, away_name, league_name = get_team_league_info_from_script_of(soup_completo)
        dt_info = get_match_datetime_from_script_of(soup_completo)
        datos.update({
            "home_name": home_name,
            "away_name": away_name,
            "league_name": league_name,
            "match_date": dt_info.get("match_date"),
            "match_time": dt_info.get("match_time"),
            "match_datetime": dt_info.get("match_datetime"),
        })

        with ThreadPoolExecutor(max_workers=8) as executor:
            future_home_standings = executor.submit(extract_standings_data_from_h2h_page_of, soup_completo, home_name)
            future_away_standings = executor.submit(extract_standings_data_from_h2h_page_of, soup_completo, away_name)
            future_home_ou = executor.submit(extract_over_under_stats_from_div_of, soup_completo, 'home')
            future_away_ou = executor.submit(extract_over_under_stats_from_div_of, soup_completo, 'away')
            future_main_odds = executor.submit(extract_bet365_initial_odds_of, soup_completo)
            future_h2h_data = executor.submit(extract_h2h_data_of, soup_completo, home_name, away_name, None)
            future_last_home = executor.submit(extract_last_match_in_league_of, soup_completo, "table_v1", home_name, league_id, True)
            future_last_away = executor.submit(extract_last_match_in_league_of, soup_completo, "table_v2", away_name, league_id, False)

            datos["home_standings"] = future_home_standings.result()
            datos["away_standings"] = future_away_standings.result()
            datos["home_ou_stats"] = future_home_ou.result()
            datos["away_ou_stats"] = future_away_ou.result()
            main_match_odds_data = future_main_odds.result()
            h2h_data = future_h2h_data.result()
            datos["main_match_odds_data"] = main_match_odds_data
            datos["h2h_data"] = h2h_data
            last_home_match = future_last_home.result()
            last_away_match = future_last_away.result()

        key_id_a, rival_a_id, rival_a_name = get_rival_a_for_original_h2h_of(soup_completo, league_id)
        _, rival_b_id, rival_b_name = get_rival_b_for_original_h2h_of(soup_completo, league_id)
        if key_id_a and rival_a_id and rival_b_id:
            details_h2h_col3 = get_h2h_details_for_original_logic_of(
                context,
                key_id_a,
                rival_a_id,
                rival_b_id,
                rival_a_name,
                rival_b_name
            )
        else:
            details_h2h_col3 = {"status": "error", "resultado": "N/A (H2H incompleto)"}

        with ThreadPoolExecutor(max_workers=8) as executor:
            comp_L_vs_UV_A = extract_comparative_match_of(soup_completo, "table_v1", home_name, (last_away_match or {}).get('home_team'), league_id, True)
            comp_V_vs_UL_H = extract_comparative_match_of(soup_completo, "table_v2", away_name, (last_home_match or {}).get('away_team'), league_id, False)

            datos["market_analysis_html"] = generar_analisis_completo_mercado(main_match_odds_data, h2h_data, home_name, away_name)
            datos["main_match_odds"] = {
                "ah_linea": format_ah_as_decimal_string_of(main_match_odds_data.get('ah_linea_raw', '?')),
                "goals_linea": format_ah_as_decimal_string_of(main_match_odds_data.get('goals_linea_raw', '?'))
            }

            match_ids_to_fetch_stats = {
                'last_home': (last_home_match or {}).get('match_id'),
                'last_away': (last_away_match or {}).get('match_id'),
                'h2h_col3': (details_h2h_col3 or {}).get('match_id') if isinstance(details_h2h_col3, dict) else None,
                'comp_L_vs_UV_A': (comp_L_vs_UV_A or {}).get('match_id'),
                'comp_V_vs_UL_H': (comp_V_vs_UL_H or {}).get('match_id'),
                'h2h_stadium': h2h_data.get('match1_id'),
                'h2h_general': h2h_data.get('match6_id')
            }

            stats_futures = {key: executor.submit(get_match_progression_stats_data, match_id)
                             for key, match_id in match_ids_to_fetch_stats.items() if match_id}
            stats_results = {key: future.result() for key, future in stats_futures.items()}

        datos['last_home_match'] = {'details': last_home_match, 'stats': stats_results.get('last_home')}
        datos['last_away_match'] = {'details': last_away_match, 'stats': stats_results.get('last_away')}
        datos['h2h_col3'] = {'details': details_h2h_col3, 'stats': stats_results.get('h2h_col3')}
        datos['comp_L_vs_UV_A'] = {'details': comp_L_vs_UV_A, 'stats': stats_results.get('comp_L_vs_UV_A')}
        datos['comp_V_vs_UL_H'] = {'details': comp_V_vs_UL_H, 'stats': stats_results.get('comp_V_vs_UL_H')}
        datos['h2h_stadium'] = {'details': h2h_data, 'stats': stats_results.get('h2h_stadium')}
        datos['h2h_general'] = {'details': h2h_data, 'stats': stats_results.get('h2h_general')}

        indirect_comparison_data = extract_indirect_comparison_data(soup_completo)
        datos["advanced_analysis_html"] = generar_analisis_comparativas_indirectas(indirect_comparison_data)

        current_ah_line = parse_ah_to_number_of(main_match_odds_data.get('ah_linea_raw', '0'))
        rendimiento_local = analizar_rendimiento_reciente_con_handicap(soup_completo, home_name, True)
        datos["rendimiento_local_handicap"] = rendimiento_local
        rendimiento_visitante = analizar_rendimiento_reciente_con_handicap(soup_completo, away_name, False)
        datos["rendimiento_visitante_handicap"] = rendimiento_visitante

        if current_ah_line is not None:
            comparacion_local = comparar_lineas_handicap_recientes(soup_completo, home_name, current_ah_line, True)
            datos["comparacion_lineas_local"] = comparacion_local
            comparacion_visitante = comparar_lineas_handicap_recientes(soup_completo, away_name, current_ah_line, False)
            datos["comparacion_lineas_visitante"] = comparacion_visitante

        rivales_comunes = analizar_rivales_comunes(soup_completo, home_name, away_name)
        datos["rivales_comunes"] = rivales_comunes

        rival_local_rival = (last_away_match or {}).get('home_team', 'N/A')
        rival_visitante_rival = (last_home_match or {}).get('away_team', 'N/A')
        if rival_local_rival != 'N/A' and rival_visitante_rival != 'N/A':
            analisis_contra_rival = analizar_contra_rival_del_rival(
                soup_completo, home_name, away_name, rival_local_rival, rival_visitante_rival
            )
            datos["analisis_contra_rival_del_rival"] = analisis_contra_rival

        resumen_rendimiento = generar_resumen_rendimiento_reciente(soup_completo, home_name, away_name, current_ah_line)
        datos["resumen_rendimiento_reciente"] = resumen_rendimiento

        from modules.funciones_auxiliares import (
            _calcular_estadisticas_contra_rival,
            _analizar_over_under,
            _analizar_ah_cubierto,
            _analizar_desempeno_casa_fuera,
            _contar_victorias_h2h,
            _analizar_over_under_h2h,
            _contar_over_h2h,
            _contar_victorias_h2h_general
        )

        datos["_calcular_estadisticas_contra_rival"] = _calcular_estadisticas_contra_rival
        datos["_analizar_over_under"] = _analizar_over_under
        datos["_analizar_ah_cubierto"] = _analizar_ah_cubierto
        datos["_analizar_desempeno_casa_fuera"] = _analizar_desempeno_casa_fuera
        datos["_contar_victorias_h2h"] = _contar_victorias_h2h
        datos["_analizar_over_under_h2h"] = _analizar_over_under_h2h
        datos["_contar_over_h2h"] = _contar_over_h2h
        datos["_contar_victorias_h2h_general"] = _contar_victorias_h2h_general

        return datos

    except PlaywrightTimeout:
        return {"error": "La fuente de datos (Nowgoal) tardo demasiado en responder."}
    except Exception as e:
        print(f"ERROR CRITICO en el scraper: {e}")
        return {"error": f"Error durante el scraping: {e}"}
    finally:
        _close_playwright_context(playwright, browser, context)

def obtener_datos_preview_rapido(match_id: str):
    """Vista previa usando Playwright para replicar el flujo completo pero sin Selenium."""
    if not match_id or not match_id.isdigit():
        return {"error": "ID de partido invalido."}

    playwright = browser = context = None
    try:
        playwright, browser, context = _start_playwright_context()
        url = f"{BASE_URL_OF}/match/h2h-{match_id}"
        try:
            html = _load_page_html(
                context,
                url,
                wait_selector="#table_v1",
                selects={"hSelect_1": "8", "hSelect_2": "8", "hSelect_3": "8"}
            )
        except PlaywrightTimeout:
            return {"error": "La fuente de datos (Nowgoal) tardo demasiado en responder."}
        except Exception as exc:
            return {"error": f"No se pudo cargar la pagina del partido: {type(exc).__name__}"}

        soup = BeautifulSoup(html, 'lxml')
        _, _, league_id, home_name, away_name, _ = get_team_league_info_from_script_of(soup)
        dt_info = get_match_datetime_from_script_of(soup)

        main_odds = extract_bet365_initial_odds_of(soup)
        ah_line_raw = main_odds.get('ah_linea_raw', '-')
        ah_line_num = parse_ah_to_number_of(ah_line_raw)
        favorito_actual = None
        if ah_line_num is not None:
            if ah_line_num > 0:
                favorito_actual = home_name
            elif ah_line_num < 0:
                favorito_actual = away_name

        def analizar_rendimiento(tabla_id, equipo_nombre):
            tabla = soup.find("table", id=tabla_id)
            if not tabla:
                return {"wins": 0, "draws": 0, "losses": 0, "total": 0}
            partidos = tabla.find_all("tr", id=re.compile(rf"tr{tabla_id[-1]}_\d+"), limit=8)
            victorias = 0
            for r in partidos:
                celdas = r.find_all("td")
                if len(celdas) < 5:
                    continue
                resultado_span = celdas[5].find("span")
                if resultado_span and 'win' in resultado_span.get('class', []):
                    victorias += 1
            return {"wins": victorias, "total": len(partidos)}

        rendimiento_local = analizar_rendimiento("table_v1", home_name)
        rendimiento_visitante = analizar_rendimiento("table_v2", away_name)

        h2h_stats = {"home_wins": 0, "away_wins": 0, "draws": 0}
        last_h2h_cover = "DESCONOCIDO"
        try:
            h2h_data = extract_h2h_data_of(soup, home_name, away_name, None)
            h2h_table = soup.find("table", id="table_v3")
            if h2h_table:
                partidos_h2h = h2h_table.find_all("tr", id=re.compile(r"tr3_\d+"), limit=8)
                for r in partidos_h2h:
                    tds = r.find_all("td")
                    if len(tds) < 5:
                        continue
                    home_h2h = tds[2].get_text(strip=True)
                    resultado_raw = tds[3].get_text(strip=True)
                    try:
                        goles_h, goles_a = map(int, resultado_raw.split("-"))
                        es_local_en_h2h = home_name.lower() in home_h2h.lower()
                        if goles_h == goles_a:
                            h2h_stats["draws"] += 1
                        elif (es_local_en_h2h and goles_h > goles_a) or (not es_local_en_h2h and goles_a > goles_h):
                            h2h_stats["home_wins"] += 1
                        else:
                            h2h_stats["away_wins"] += 1
                    except (ValueError, IndexError):
                        continue
            res_raw = None
            h_home = None
            h_away = None
            if h2h_data.get('res1_raw') and h2h_data.get('res1_raw') != '?-?':
                res_raw = h2h_data['res1_raw']
                h_home = home_name
                h_away = away_name
            elif h2h_data.get('res6_raw') and h2h_data.get('res6_raw') != '?-?':
                res_raw = h2h_data['res6_raw']
                h_home = h2h_data.get('h2h_gen_home', home_name)
                h_away = h2h_data.get('h2h_gen_away', away_name)
            if favorito_actual and (ah_line_num is not None) and res_raw:
                ct, _ = check_handicap_cover(res_raw.replace(':', '-'), ah_line_num, favorito_actual, h_home, h_away, home_name)
                last_h2h_cover = ct
        except Exception:
            pass

        recent_indirect = {"last_home": None, "last_away": None, "h2h_col3": None}
        try:
            last_home = extract_last_match_in_league_of(soup, "table_v1", home_name, league_id, True)
            last_away = extract_last_match_in_league_of(soup, "table_v2", away_name, league_id, False)

            def _df_to_rows(df):
                rows = []
                try:
                    if df is not None and not df.empty:
                        for idx, row in df.iterrows():
                            label = idx.replace('Shots on Goal', 'Tiros a Puerta').replace('Shots', 'Tiros').replace('Dangerous Attacks', 'Ataques Peligrosos').replace('Attacks', 'Ataques')
                            rows.append({"label": label, "home": row.get('Casa', ''), "away": row.get('Fuera', '')})
                except Exception:
                    pass
                return rows

            recent_indirect["last_home"] = {
                "details": last_home,
                "stats_rows": _df_to_rows((last_home or {}).get('stats'))
            }
            recent_indirect["last_away"] = {
                "details": last_away,
                "stats_rows": _df_to_rows((last_away or {}).get('stats'))
            }

            key_id_a, rival_a_id, rival_a_name = get_rival_a_for_original_h2h_of(soup, league_id)
            _, rival_b_id, rival_b_name = get_rival_b_for_original_h2h_of(soup, league_id)
            if key_id_a and rival_a_id and rival_b_id:
                h2h_col3 = get_h2h_details_for_original_logic_of(context, key_id_a, rival_a_id, rival_b_id, rival_a_name, rival_b_name)
            else:
                h2h_col3 = {"status": "error", "resultado": "N/A (H2H incompleto)"}
            recent_indirect["h2h_col3"] = h2h_col3
        except Exception:
            pass

        indirect = {
            "home_better": 0,
            "away_better": 0,
            "draws": 0,
            "samples": []
        }
        try:
            panels = extract_indirect_comparison_data(soup)
            if panels:
                for key in ['comp1', 'comp2']:
                    comp = panels.get(key)
                    if not comp or not comp.get('stats'):
                        continue
                    home_info = comp['stats'].get('home_indirect') or {}
                    away_info = comp['stats'].get('away_indirect') or {}
                    rv = comp.get('rival_name', 'Rival')
                    if home_info.get('margin') is None or away_info.get('margin') is None:
                        continue
                    if home_info["margin"] > away_info["margin"]:
                        indirect["home_better"] += 1
                        verdict = "home"
                    elif home_info["margin"] < away_info["margin"]:
                        indirect["away_better"] += 1
                        verdict = "away"
                    else:
                        indirect["draws"] += 1
                        verdict = "draw"
                    indirect["samples"].append({
                        "rival": rv,
                        "home_margin": home_info["margin"],
                        "away_margin": away_info["margin"],
                        "verdict": verdict
                    })
        except Exception:
            pass

        indirect_panels = extract_indirect_comparison_data(soup)
        ataques_peligrosos = {}
        favorite_da = None
        try:
            if indirect_panels and indirect_panels.get("comp1"):
                c1 = indirect_panels["comp1"]
                ap_home = int(c1['stats'].get('ataques_peligrosos_casa', 0) or 0)
                ap_away = int(c1['stats'].get('ataques_peligrosos_fuera', 0) or 0)
                own_ap, rival_ap = (ap_away, ap_home) if c1.get('localia') == 'A' else (ap_home, ap_away)
                ataques_peligrosos['team1'] = {
                    "name": c1['main_team'],
                    "own": own_ap,
                    "rival": rival_ap,
                    "very_superior": bool((own_ap - rival_ap) >= 5)
                }
            if indirect_panels and indirect_panels.get("comp2"):
                c2 = indirect_panels["comp2"]
                ap_home = int(c2['stats'].get('ataques_peligrosos_casa', 0) or 0)
                ap_away = int(c2['stats'].get('ataques_peligrosos_fuera', 0) or 0)
                own_ap, rival_ap = (ap_away, ap_home) if c2.get('localia') == 'A' else (ap_home, ap_away)
                ataques_peligrosos['team2'] = {
                    "name": c2['main_team'],
                    "own": own_ap,
                    "rival": rival_ap,
                    "very_superior": bool((own_ap - rival_ap) >= 5)
                }
            fav_name = (favorito_actual or '').lower()
            for key in ['team1', 'team2']:
                if key in ataques_peligrosos and ataques_peligrosos[key]['name'].lower() == fav_name:
                    favorite_da = {
                        "name": ataques_peligrosos[key]['name'],
                        "very_superior": ataques_peligrosos[key]['very_superior'],
                        "own": ataques_peligrosos[key]['own'],
                        "rival": ataques_peligrosos[key]['rival']
                    }
                    break
        except Exception:
            pass

        result = {
            "home_team": home_name,
            "away_team": away_name,
            "recent_form": {
                "home": rendimiento_local,
                "away": rendimiento_visitante,
            },
            "recent_indirect": recent_indirect,
            "handicap": {
                "ah_line": format_ah_as_decimal_string_of(ah_line_raw),
                "favorite": favorito_actual or "",
                "cover_on_last_h2h": last_h2h_cover
            },
            "dangerous_attacks": ataques_peligrosos,
            "favorite_dangerous_attacks": favorite_da,
            "h2h_indirect": indirect,
            "h2h_stats": h2h_stats
        }

        return result

    except PlaywrightTimeout:
        return {"error": "La fuente de datos (Nowgoal) tardo demasiado en responder."}
    except Exception as e:
        print(f"ERROR en scraper preview rapido para {match_id}: {e}")
        return {"error": f"No se pudieron obtener los datos de la vista previa: {type(e).__name__}"}
    finally:
        _close_playwright_context(playwright, browser, context)
def obtener_datos_preview_ligero(match_id: str):
    """
    Vista previa LIGERA (solo on-click): usa requests + BeautifulSoup.
    Devuelve el mismo esquema que la versiÃ³n 'rÃ¡pida' con Selenium, pero sin abrir navegador.
    """
    if not match_id or not match_id.isdigit():
        return {"error": "ID de partido invÃ¡lido."}

    url = f"{BASE_URL_OF}/match/h2h-{match_id}"
    try:
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/116.0.0.0 Safari/537.36"})
        response = session.get(url, timeout=5)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'lxml')

        # Equipos
        _, _, league_id, home_name, away_name, _ = get_team_league_info_from_script_of(soup)
        dt_info = get_match_datetime_from_script_of(soup)

        # LÃ­nea AH (Bet365 inicial)
        main_odds = extract_bet365_initial_odds_of(soup)
        ah_line_raw = main_odds.get('ah_linea_raw', '-')
        ah_line_num = parse_ah_to_number_of(ah_line_raw)
        favorito_actual = None
        if ah_line_num is not None:
            if ah_line_num > 0:
                favorito_actual = home_name
            elif ah_line_num < 0:
                favorito_actual = away_name

        # Rendimiento reciente (Ãºltimos 8)
        def analizar_rendimiento(tabla_id, equipo_nombre):
            tabla = soup.find("table", id=tabla_id)
            if not tabla:
                return {"wins": 0, "draws": 0, "losses": 0, "total": 0}
            partidos = tabla.find_all("tr", id=re.compile(rf"tr{tabla_id[-1]}_\\d+"), limit=8)
            victorias = 0
            for r in partidos:
                celdas = r.find_all("td")
                if len(celdas) < 5:
                    continue
                resultado_span = celdas[5].find("span")
                if resultado_span and 'win' in resultado_span.get('class', []):
                    victorias += 1
            return {"wins": victorias, "total": len(partidos)}

        rendimiento_local = analizar_rendimiento("table_v1", home_name)
        rendimiento_visitante = analizar_rendimiento("table_v2", away_name)

        # H2H directo (usar funciÃ³n existente para coherencia)
        h2h_stats = {"home_wins": 0, "away_wins": 0, "draws": 0}
        last_h2h_cover = "DESCONOCIDO"
        try:
            h2h_data = extract_h2h_data_of(soup, home_name, away_name, None)
            h2h_table = soup.find("table", id="table_v3")
            if h2h_table:
                partidos_h2h = h2h_table.find_all("tr", id=re.compile(r"tr3_\\d+"), limit=8)
                for r in partidos_h2h:
                    tds = r.find_all("td")
                    if len(tds) < 5:
                        continue
                    home_h2h = tds[2].get_text(strip=True)
                    resultado_raw = tds[3].get_text(strip=True)
                    try:
                        goles_h, goles_a = map(int, resultado_raw.split("-"))
                        es_local_en_h2h = home_name.lower() in home_h2h.lower()
                        if goles_h == goles_a:
                            h2h_stats["draws"] += 1
                        elif (es_local_en_h2h and goles_h > goles_a) or (not es_local_en_h2h and goles_a > goles_h):
                            h2h_stats["home_wins"] += 1
                        else:
                            h2h_stats["away_wins"] += 1
                    except (ValueError, IndexError):
                        continue
            # Cobertura del favorito en el Ãºltimo H2H disponible
            res_raw = None
            h_home = None
            h_away = None
            if h2h_data.get('res1_raw') and h2h_data.get('res1_raw') != '?-?':
                res_raw = h2h_data['res1_raw']
                h_home = home_name
                h_away = away_name
            elif h2h_data.get('res6_raw') and h2h_data.get('res6_raw') != '?-?':
                res_raw = h2h_data['res6_raw']
                h_home = h2h_data.get('h2h_gen_home', home_name)
                h_away = h2h_data.get('h2h_gen_away', away_name)
            if favorito_actual and (ah_line_num is not None) and res_raw:
                ct, _ = check_handicap_cover(res_raw.replace(':', '-'), ah_line_num, favorito_actual, h_home, h_away, home_name)
                last_h2h_cover = ct
        except Exception:
            pass

        # Rendimiento Reciente (Ãºltimos partidos) y H2H Rivales (Col3) con peticiones ligeras
        recent_indirect = {"last_home": None, "last_away": None, "h2h_col3": None}
        try:
            # Ãšltimos partidos
            last_home = extract_last_match_in_league_of(soup, "table_v1", home_name, league_id, True)
            last_away = extract_last_match_in_league_of(soup, "table_v2", away_name, league_id, False)
            def _df_to_rows(df):
                rows = []
                try:
                    if df is not None and not df.empty:
                        for idx, row in df.iterrows():
                            label = idx.replace('Shots on Goal', 'Tiros a Puerta').replace('Shots', 'Tiros').replace('Dangerous Attacks', 'Ataques Peligrosos').replace('Attacks', 'Ataques')
                            rows.append({"label": label, "home": row.get('Casa', ''), "away": row.get('Fuera', '')})
                except Exception:
                    pass
                return rows
            if last_home:
                lh_stats = get_match_progression_stats_data(str(last_home.get('match_id')))
                recent_indirect["last_home"] = {
                    "home": last_home.get('home_team'),
                    "away": last_home.get('away_team'),
                    "score": last_home.get('score'),
                    "ah": format_ah_as_decimal_string_of(last_home.get('handicap_line_raw', '-') or '-'),
                    "ou": "-",
                    "stats_rows": _df_to_rows(lh_stats),
                    "date": last_home.get('date')
                }
            if last_away:
                la_stats = get_match_progression_stats_data(str(last_away.get('match_id')))
                recent_indirect["last_away"] = {
                    "home": last_away.get('home_team'),
                    "away": last_away.get('away_team'),
                    "score": last_away.get('score'),
                    "ah": format_ah_as_decimal_string_of(last_away.get('handicap_line_raw', '-') or '-'),
                    "ou": "-",
                    "stats_rows": _df_to_rows(la_stats),
                    "date": last_away.get('date')
                }
            # H2H Rivales (Col3) sin Selenium: cargar la pÃ¡gina del key_id_a
            key_id_a, rival_a_id, rival_a_name = get_rival_a_for_original_h2h_of(soup, league_id)
            _, rival_b_id, rival_b_name = get_rival_b_for_original_h2h_of(soup, league_id)
            if key_id_a and rival_a_id and rival_b_id:
                key_url = f"{BASE_URL_OF}/match/h2h-{key_id_a}"
                session2 = requests.Session()
                session2.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/116.0.0.0 Safari/537.36"})
                key_resp = session2.get(key_url, timeout=6)
                key_resp.raise_for_status()
                soup_key = BeautifulSoup(key_resp.text, 'lxml')
                table = soup_key.find("table", id="table_v2")
                if table:
                    for row in table.find_all("tr", id=re.compile(r"tr2_\\d+")):
                        links = row.find_all("a", onclick=True)
                        if len(links) < 2:
                            continue
                        m_h = re.search(r"team\((\d+)\)", links[0].get("onclick", ""))
                        m_a = re.search(r"team\((\d+)\)", links[1].get("onclick", ""))
                        if not (m_h and m_a):
                            continue
                        if {m_h.group(1), m_a.group(1)} == {str(rival_a_id), str(rival_b_id)}:
                            score_span = row.find("span", class_="fscore_2")
                            if not score_span or '-' not in score_span.text:
                                break
                            score_txt = score_span.text.strip().split("(")[0].strip()
                            try:
                                g_h, g_a = score_txt.split('-', 1)
                            except Exception:
                                break
                            tds = row.find_all("td")
                            ah_raw = "-"
                            if len(tds) > 11:
                                cell = tds[11]
                                ah_raw = (cell.get("data-o") or cell.text).strip() or "-"
                            match_id_col3 = row.get('index')
                            score_line = f"{links[0].text.strip()} {g_h}:{g_a} {links[1].text.strip()}"
                            col3_stats = get_match_progression_stats_data(str(match_id_col3))
                            # Fecha si existe
                            date_txt = None
                            try:
                                date_span = tds[1].find('span', attrs={'name': 'timeData'}) if len(tds) > 1 else None
                                date_txt = date_span.get_text(strip=True) if date_span else None
                            except Exception:
                                date_txt = None
                            recent_indirect["h2h_col3"] = {
                                "score_line": score_line,
                                "ah": format_ah_as_decimal_string_of(ah_raw or '-'),
                                "ou": "-",
                                "stats_rows": _df_to_rows(col3_stats),
                                "date": date_txt
                            }
                            break
        except Exception:
            pass

        # H2H indirecto ligero (rivales comunes)
        indirect = {"home_better": 0, "away_better": 0, "draws": 0, "samples": []}
        try:
            table_v1 = soup.find("table", id="table_v1")
            table_v2 = soup.find("table", id="table_v2")
            def _parse_score_to_tuple(score_text):
                try:
                    gh, ga = map(int, score_text.strip().split("-"))
                    return gh, ga
                except Exception:
                    return None
            def _find_match_info(table, rival_name_lower, team_name_ref):
                if not table:
                    return None
                rows = table.find_all("tr", id=re.compile(r"tr[12]_\\d+"))
                for r in rows:
                    tds = r.find_all("td")
                    if len(tds) < 5:
                        continue
                    home_t = tds[2].get_text(strip=True)
                    away_t = tds[4].get_text(strip=True)
                    if away_t.lower() == rival_name_lower or home_t.lower() == rival_name_lower:
                        score_text = tds[3].get_text(strip=True)
                        score = _parse_score_to_tuple(score_text)
                        if not score:
                            continue
                        gh, ga = score
                        if home_t.lower() == team_name_ref.lower():
                            margin = gh - ga
                        elif away_t.lower() == team_name_ref.lower():
                            margin = ga - gh
                        else:
                            margin = gh - ga
                        return {"rival": rival_name_lower, "margin": margin}
                return None
            if table_v1 and table_v2:
                rivals_home = set()
                for r in table_v1.find_all("tr", id=re.compile(r"tr1_\\d+")):
                    tds = r.find_all("td")
                    if len(tds) >= 5:
                        rivals_home.add(tds[4].get_text(strip=True).lower())
                rivals_away = set()
                for r in table_v2.find_all("tr", id=re.compile(r"tr2_\\d+")):
                    tds = r.find_all("td")
                    if len(tds) >= 5:
                        rivals_away.add(tds[2].get_text(strip=True).lower())
                common = [rv for rv in rivals_home.intersection(rivals_away) if rv and rv != '?']
                common = common[:3]
                for rv in common:
                    home_info = _find_match_info(table_v1, rv, home_name)
                    away_info = _find_match_info(table_v2, rv, away_name)
                    if not home_info or not away_info:
                        continue
                    if home_info["margin"] > away_info["margin"]:
                        indirect["home_better"] += 1
                        verdict = "home"
                    elif home_info["margin"] < away_info["margin"]:
                        indirect["away_better"] += 1
                        verdict = "away"
                    else:
                        indirect["draws"] += 1
                        verdict = "draw"
                    indirect["samples"].append({
                        "rival": rv,
                        "home_margin": home_info["margin"],
                        "away_margin": away_info["margin"],
                        "verdict": verdict
                    })
        except Exception:
            pass

        # Ataques peligrosos (comparativas indirectas)
        indirect_panels = extract_indirect_comparison_data(soup)
        ataques_peligrosos = {}
        favorite_da = None
        try:
            if indirect_panels and indirect_panels.get("comp1"):
                c1 = indirect_panels["comp1"]
                ap_home = int(c1['stats'].get('ataques_peligrosos_casa', 0) or 0)
                ap_away = int(c1['stats'].get('ataques_peligrosos_fuera', 0) or 0)
                own_ap, rival_ap = (ap_away, ap_home) if c1.get('localia') == 'A' else (ap_home, ap_away)
                ataques_peligrosos['team1'] = {
                    "name": c1['main_team'],
                    "own": own_ap,
                    "rival": rival_ap,
                    "very_superior": bool((own_ap - rival_ap) >= 5)
                }
            if indirect_panels and indirect_panels.get("comp2"):
                c2 = indirect_panels["comp2"]
                ap_home = int(c2['stats'].get('ataques_peligrosos_casa', 0) or 0)
                ap_away = int(c2['stats'].get('ataques_peligrosos_fuera', 0) or 0)
                own_ap, rival_ap = (ap_away, ap_home) if c2.get('localia') == 'A' else (ap_home, ap_away)
                ataques_peligrosos['team2'] = {
                    "name": c2['main_team'],
                    "own": own_ap,
                    "rival": rival_ap,
                    "very_superior": bool((own_ap - rival_ap) >= 5)
                }
            fav_name = (favorito_actual or '').lower()
            for key in ['team1','team2']:
                if key in ataques_peligrosos and ataques_peligrosos[key]['name'].lower() == fav_name:
                    favorite_da = {
                        "name": ataques_peligrosos[key]['name'],
                        "very_superior": ataques_peligrosos[key]['very_superior'],
                        "own": ataques_peligrosos[key]['own'],
                        "rival": ataques_peligrosos[key]['rival']
                    }
                    break
        except Exception:
            pass

        result = {
            "home_team": home_name,
            "away_team": away_name,
            "recent_form": {
                "home": rendimiento_local,
                "away": rendimiento_visitante,
            },
            "recent_indirect": recent_indirect,
            "handicap": {
                "ah_line": format_ah_as_decimal_string_of(ah_line_raw),
                "favorite": favorito_actual or "",
                "cover_on_last_h2h": last_h2h_cover
            },
            "dangerous_attacks": ataques_peligrosos,
            "favorite_dangerous_attacks": favorite_da,
            "h2h_indirect": indirect,
            "h2h_stats": h2h_stats
        }
        # AÃ±adir campos de fecha/hora del partido a la respuesta
        result.update({
            "match_date": dt_info.get("match_date"),
            "match_time": dt_info.get("match_time"),
            "match_datetime": dt_info.get("match_datetime"),
        })
        return result
    except requests.Timeout:
        return {"error": "La fuente de datos (Nowgoal) tardÃ³ demasiado en responder."}
    except Exception as e:
        print(f"ERROR en scraper preview ligero para {match_id}: {e}")
        return {"error": f"No se pudieron obtener los datos de la vista previa (ligera): {type(e).__name__}"}

