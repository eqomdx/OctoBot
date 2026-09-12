@echo off
py -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if not exist .env copy .env.example .env
 echo.
 echo Setup complete. Edit .env, then run RUN_WINDOWS.bat
pause
