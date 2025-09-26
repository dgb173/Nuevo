@echo off
echo Ejecutando script de prueba final...
py test_scraper.py > test_output.txt
echo Prueba completada. El resultado se ha guardado en test_output.txt
pause