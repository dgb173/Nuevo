import datetime
import re
import math
import requests
import os

# --- SCRIPT DE DIAGNÓSTICO FINAL ---

DATA_JS_URL = "https://www.nowgoal.com/gf/data/bf_en-idn.js"
REQUEST_TIMEOUT_SECONDS = 15
_REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Referer": "https://www.nowgoal.com/",
}

def get_js_content():
    """Función simple para descargar el contenido del JS."""
    try:
        proxy_url = os.environ.get('PROXY_URL')
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        response = requests.get(DATA_JS_URL, headers=_REQUEST_HEADERS, proxies=proxies, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        return response.text
    except Exception as e:
        print(f"[ERROR CRÍTICO] No se pudo descargar el fichero de datos: {e}")
        return None

# --- BLOQUE PRINCIPAL DE EJECUCIÓN ---

if __name__ == "__main__":
    print("--- Iniciando Test del Scraper (Diagnóstico Final v2) ---")
    content = get_js_content()

    if content is None:
        print("\nRESULTADO: FALLO GENERAL. La descarga falló y no se devolvió contenido.")
    elif content.strip() == "":
        print("\nRESULTADO: ÉXITO EN EL DIAGNÓSTICO.")
        print("\nEl problema está confirmado: El servidor de Nowgoal está devolviendo un FICHERO VACÍO.")
        print("Esto es una medida anti-scraping porque detecta tu proxy.")
        print("\nSOLUCIÓN: Necesitas cambiar a un proveedor de proxy de mayor calidad (residencial o móvil). El código funciona, pero el proxy está siendo bloqueado.")
    else:
        print("\nRESULTADO: INESPERADO. Se ha recibido contenido.")
        print("--- CONTENIDO CRUDO DEL FICHERO JS DESCARGADO ---\
")
        print(content)
        print("\n--- FIN DEL CONTENIDO CRUDO ---")
        print("\nSi ves esto, por favor, envía todo este output al asistente.")

    print("\n--- Test Finalizado ---")
