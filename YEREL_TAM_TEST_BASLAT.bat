@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo PIYASA NABZI YAT/KAP v2.1 - YEREL TAM TARAMA
echo Klasor: %CD%
echo ============================================================
echo.

where py >nul 2>nul
if %errorlevel%==0 (
    set "PYTHON=py -3"
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo HATA: Python bulunamadi. Python 3.11+ kurulu olmali.
        pause
        exit /b 1
    )
    set "PYTHON=python"
)

echo Gerekli kutuphaneler kontrol ediliyor...
%PYTHON% -m pip install -r requirements.txt
if errorlevel 1 (
    echo HATA: Kutuphane kurulumu basarisiz.
    pause
    exit /b 1
)

echo.
echo Ayarlar:
echo - Batch boyutu: 60 fon
echo - Istek araligi: 1.35 saniye
echo - Her 65 istekte: 180 saniye mola
echo - En fazla: 40 batch
echo - Kayit: Her fondan sonra data/staging altina
echo.

for /L %%B in (1,1,40) do (
    echo ============================================================
    echo BATCH %%B / 40 BASLIYOR
    echo ============================================================

    %PYTHON% scripts\update_yat_kap_data.py ^
      --batch-size 60 ^
      --delay 1.35 ^
      --routine-request-limit 65 ^
      --routine-cooldown-seconds 180

    if errorlevel 1 (
        echo.
        echo UYARI: Batch hata ile sonlandi. Checkpoint dosyalari korunur.
        echo Yeniden calistirdiginizda kaldigi yerden devam eder.
        pause
        exit /b 1
    )

    set "RUN_STATUS="
    for /f "usebackq delims=" %%S in (`powershell -NoProfile -Command "$p='data/run_state.json'; if(Test-Path $p){try{(Get-Content $p -Raw | ConvertFrom-Json).status}catch{''}}"`) do set "RUN_STATUS=%%S"

    echo Durum: !RUN_STATUS!

    if /I "!RUN_STATUS!"=="PUBLISHED" goto :DONE
    if /I "!RUN_STATUS!"=="COMPLETE_WITH_UNRESOLVED" goto :UNRESOLVED

    echo Sonraki batch'e geciliyor...
    echo.
)

echo.
echo 40 batch sinirina ulasildi.
echo data\run_state.json dosyasini kontrol edin.
pause
exit /b 0

:DONE
echo.
echo ============================================================
echo TAMAMLANDI: Resmi veri yayinlandi.
echo data\yat_fund_enrichment.json
 echo ============================================================
pause
exit /b 0

:UNRESOLVED
echo.
echo ============================================================
echo TARAMA TAMAMLANDI, ANCAK COZULEMEYEN KAYITLAR VAR.
echo data\diagnostics\request_failures.json dosyasini kontrol edin.
echo ============================================================
pause
exit /b 0
