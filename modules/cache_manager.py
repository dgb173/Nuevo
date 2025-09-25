
import sqlite3
import json
import time
from datetime import datetime, timedelta

DB_PATH = "cache.db"
CACHE_DURATION_SECONDS = 86400  # 24 horas

def setup_database():
    """Crea la tabla de caché si no existe."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS analysis_cache (
            match_id TEXT PRIMARY KEY,
            data_json TEXT NOT NULL,
            timestamp REAL NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def get_from_cache(match_id: str):
    """
    Busca un resultado en la caché. Devuelve los datos si son encontrados y no han expirado.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT data_json, timestamp FROM analysis_cache WHERE match_id = ?", (match_id,))
        result = cursor.fetchone()
        conn.close()

        if result:
            data_json, timestamp = result
            cache_age = time.time() - timestamp
            if cache_age < CACHE_DURATION_SECONDS:
                print(f"[CACHE] HIT: Se encontró un resultado válido para el match_id {match_id}.")
                return json.loads(data_json)
            else:
                print(f"[CACHE] EXPIRED: El resultado para el match_id {match_id} ha expirado.")
                # Opcional: eliminar el registro expirado
                # delete_from_cache(match_id)
    except (sqlite3.OperationalError, json.JSONDecodeError):
        # La tabla podría no existir o los datos estar corruptos
        pass
    
    print(f"[CACHE] MISS: No se encontró un resultado válido para el match_id {match_id}.")
    return None

def set_to_cache(match_id: str, data: dict):
    """
    Guarda un resultado de análisis en la base de datos de caché.
    """
    try:
        data_json = json.dumps(data)
        current_timestamp = time.time()
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        # REPLACE INTO intentará un INSERT, y si falla por PRIMARY KEY, hará un UPDATE.
        cursor.execute(
            "REPLACE INTO analysis_cache (match_id, data_json, timestamp) VALUES (?, ?, ?)",
            (match_id, data_json, current_timestamp)
        )
        conn.commit()
        conn.close()
        print(f"[CACHE] SET: Se ha guardado el resultado para el match_id {match_id}.")
    except Exception as e:
        print(f"[CACHE ERROR] No se pudo guardar en caché para el match_id {match_id}: {e}")

def delete_from_cache(match_id: str):
    """Elimina un registro específico de la caché."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM analysis_cache WHERE match_id = ?", (match_id,))
        conn.commit()
        conn.close()
        print(f"[CACHE] DELETED: Se ha eliminado el registro para el match_id {match_id}.")
    except Exception as e:
        print(f"[CACHE ERROR] No se pudo eliminar de la caché para el match_id {match_id}: {e}")

# Inicializar la base de datos al importar el módulo
setup_database()
