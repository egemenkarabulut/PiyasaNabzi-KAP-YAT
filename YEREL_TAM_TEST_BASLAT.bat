@echo off
setlocal
cd /d "%~dp0"
title Piyasa Nabzi YAT KAP Public Veri - Yerel Tam Test

where py >nul 2>nul
if %errorlevel%==0 (
  set "PYTHON=py"
) else (
  set "PYTHON=python"
)

echo Gerekli kutuphaneler kuruluyor...
%PYTHON% -m pip install -q -r requirements.txt
if errorlevel 1 (
  echo HATA: Kutuphaneler kurulamadi.
  pause
  exit /b 1
)

echo.
echo Tam KAP YAT taramasi baslatiliyor...
%PYTHON% scripts\update_public_data.py --workers 2 --delay 0.55
set "EXIT_CODE=%errorlevel%"

echo.
if "%EXIT_CODE%"=="0" (
  echo BASARILI: data klasoru guncellendi.
) else (
  echo BASARISIZ: Public veri yayinlanmadi. .run_output klasorunu kontrol edin.
)
pause
exit /b %EXIT_CODE%
