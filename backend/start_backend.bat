@echo off
REM Start the SDOC backend in dev mode (http://127.0.0.1:8000/docs)
cd /d "%~dp0"
python -m uvicorn app.main:app --reload --port 8000
pause
