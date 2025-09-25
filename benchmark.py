import requests
import time
import threading
import subprocess
import psutil
import re
from bs4 import BeautifulSoup
import os
import sys

# --- CONFIGURACIÓN ---
NUM_REQUESTS_PREVIEW = [1, 10, 50]
NUM_REQUESTS_ANALYSIS = [1, 3, 5]
TARGET_HOST = "http://127.0.0.1:5000"

def get_valid_match_id():
    match_id = "2824469"
    print(f"[INFO] Usando el match_id proporcionado por el usuario: {match_id}")
    return match_id

class BenchmarkRunner:
    def __init__(self, match_id):
        self.match_id = match_id
        self.app_process = None
        self.monitor_thread = None
        self.monitoring = False
        self.max_cpu = 0
        self.max_ram = 0
        self.results = {}

    def _monitor_resources(self, p):
        self.max_cpu = 0
        self.max_ram = 0
        while not psutil.pid_exists(p.pid) and self.monitoring:
            time.sleep(0.1)
        if not psutil.pid_exists(p.pid):
            return
        main_process = psutil.Process(p.pid)
        while self.monitoring:
            try:
                all_processes = [main_process] + main_process.children(recursive=True)
                total_cpu = sum([proc.cpu_percent(interval=0.1) for proc in all_processes if proc.is_running()])
                total_ram = sum([proc.memory_info().rss for proc in all_processes if proc.is_running()])
                if total_cpu > self.max_cpu:
                    self.max_cpu = total_cpu
                if total_ram > self.max_ram:
                    self.max_ram = total_ram
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                break
            except Exception:
                break

    def _make_request(self, url, results_list):
        start_time = time.time()
        try:
            requests.get(url, timeout=300)
        except requests.exceptions.RequestException:
            pass
        end_time = time.time()
        results_list.append(end_time - start_time)

    def _run_concurrent_requests(self, url, num_concurrent):
        threads = []
        response_times = []
        for _ in range(num_concurrent):
            thread = threading.Thread(target=self._make_request, args=(url, response_times))
            threads.append(thread)
            thread.start()
        for thread in threads:
            thread.join()
        return sum(response_times) / len(response_times) if response_times else 0

    def run_scenario(self, scenario_name, endpoint, concurrent_levels, measure_resources=False):
        print(f"\n--- Iniciando Escenario: {scenario_name} ---")
        self.results[scenario_name] = []
        for level in concurrent_levels:
            print(f"Probando con {level} peticiones concurrentes...")
            if measure_resources:
                self.monitoring = True
                self.monitor_thread = threading.Thread(target=self._monitor_resources, args=(self.app_process,))
                self.monitor_thread.start()
                time.sleep(1)
            url = f"{TARGET_HOST}{endpoint}{self.match_id}"
            avg_time = self._run_concurrent_requests(url, level)
            if measure_resources:
                self.monitoring = False
                self.monitor_thread.join()
            record = {
                "concurrent_reqs": level,
                "avg_response_time_s": f"{avg_time:.2f}",
                "max_cpu_percent": f"{self.max_cpu:.2f}%" if measure_resources else "N/A",
                "max_ram_mb": f"{self.max_ram / (1024*1024):.2f} MB" if measure_resources else "N/A"
            }
            self.results[scenario_name].append(record)
            print(f"Resultado: {record}")

    def run(self):
        print("[INFO] Iniciando la aplicación Flask en un subproceso...")
        self.app_process = subprocess.Popen([sys.executable, "app.py"])
        print("[INFO] Esperando a que la app esté en línea...")
        for _ in range(30): # Intentar durante 30 segundos
            try:
                requests.get(TARGET_HOST, timeout=1)
                print("[OK] La aplicación está en línea.")
                ready = True
                break
            except requests.exceptions.ConnectionError:
                time.sleep(1)
        else:
            ready = False

        if not ready:
            print("[ERROR] La aplicación no se pudo iniciar. Abortando benchmark.")
            self.app_process.terminate()
            self.app_process.wait()
            return
        try:
            self.run_scenario("Vista Previa Rápida (requests)", "/api/preview/", NUM_REQUESTS_PREVIEW, measure_resources=False)
            self.run_scenario("Análisis Completo (Selenium)", "/api/analisis/", NUM_REQUESTS_ANALYSIS, measure_resources=True)

            # Escenario 3: Probar la caché
            print("\n--- Preparando Escenario: Prueba de Caché ---")
            print("Asegurando que el primer resultado está en caché...")
            # La primera ejecución de "Análisis Completo" ya ha cacheado el resultado.
            self.run_scenario(
                "Análisis Cacheado (requests)",
                "/api/analisis/",
                [50], # Solo probamos con 50 peticiones concurrentes
                measure_resources=False # No medimos recursos, solo tiempo de respuesta
            )

        finally:
            print("\n[INFO] Finalizando el benchmark y deteniendo la aplicación...")
            self.app_process.terminate()
            self.app_process.wait()
            print("[OK] Benchmark completado.")
            
    def print_report(self):
        report_str = "\n\n" + "="*50 + "\n"
        report_str += "INFORME FINAL DE BENCHMARKING\n"
        report_str += "="*50 + "\n"
        for scenario_name, records in self.results.items():
            report_str += f"\n### {scenario_name} ###\n"
            if not records:
                report_str += "No se obtuvieron resultados para este escenario.\n"
                continue
            headers = records[0].keys()
            header_line = "| " + " | ".join(headers) + " |\n"
            separator_line = "| " + " | ".join(["---"] * len(headers)) + " |\n"
            report_str += header_line
            report_str += separator_line
            for record in records:
                row_line = "| " + " | ".join(map(str, record.values())) + " |\n"
                report_str += row_line
        report_str += "\n" + "="*50 + "\n"
        print(report_str)

if __name__ == "__main__":
    match_id = get_valid_match_id()
    if match_id:
        runner = BenchmarkRunner(match_id)
        runner.run()
        runner.print_report()