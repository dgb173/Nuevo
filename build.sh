#!/usr/bin/env bash
# Salir inmediatamente si un comando falla
set -o errexit

# 1. Instalar las dependencias de Python
pip install -r requirements.txt

# 2. Instalar los navegadores y sus dependencias de sistema operativo
# El flag --with-deps es CRÍTICO para que funcione en el entorno Linux de Render
python -m playwright install --with-deps