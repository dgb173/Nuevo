@echo off
echo Activando entorno virtual...
call .venv\Scripts\activate.bat

echo Iniciando la aplicacion Flask...
echo Abre tu navegador y ve a http://127.0.0.1:5000
py app.py

echo.
echo La aplicacion se ha detenido.
pause