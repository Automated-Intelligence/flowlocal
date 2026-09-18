@echo off
rem Launch FlowLocal under its watchdog, no console window.
start "" "%~dp0venv\Scripts\pythonw.exe" "%~dp0supervisor.py"
