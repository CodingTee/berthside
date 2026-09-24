@echo off
REM Start BerthSide in dev mode (http://127.0.0.1:8000/docs)
cd /d "%~dp0"
".venv\Scripts\python.exe" -m uvicorn app.main:app --reload --port 8000
pause
