@echo off
cd /d "%~dp0"
pyw -3 builder_app.py
if errorlevel 1 py -3 builder_app.py
if errorlevel 1 python builder_app.py
