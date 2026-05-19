@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo ====================================================
echo   부동산 블로그 자동화 시스템 (사무실 공용 모드)
echo ====================================================
echo.

REM 사무실 PC의 LAN IP 자동 감지
setlocal enabledelayedexpansion
set "LAN_IP="

for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /R /C:"IPv4"') do (
    set "ip=%%a"
    set "ip=!ip: =!"
    REM 사설 IP 대역만 선택 (192.168.x, 10.x, 172.16-31.x)
    echo !ip! | findstr /R /C:"^192\.168\." /C:"^10\." /C:"^172\.1[6-9]\." /C:"^172\.2[0-9]\." /C:"^172\.3[0-1]\." >nul
    if !errorlevel! equ 0 (
        if "!LAN_IP!"=="" set "LAN_IP=!ip!"
    )
)

if "!LAN_IP!"=="" set "LAN_IP=127.0.0.1"

echo  [이 PC에서 사용할 때]
echo    브라우저에서:  http://localhost:8501
echo.
echo  [다른 직원 PC에서 사용할 때]
echo    브라우저에서:  http://!LAN_IP!:8501
echo.
echo    같은 사무실 와이파이/유선랜에 연결돼 있어야 합니다.
echo    위 주소를 직원들에게 카카오톡으로 공유하세요.
echo.
echo ====================================================
echo.
echo  종료하려면 이 창에서 [Ctrl + C] 를 누르세요.
echo  이 창을 닫으면 모든 직원이 사용할 수 없게 됩니다.
echo.
echo ====================================================
echo.

REM Streamlit을 모든 네트워크 인터페이스(0.0.0.0)로 바인딩 → 다른 PC에서 접속 가능
streamlit run app.py --server.address 0.0.0.0 --server.port 8501 --browser.gatherUsageStats false

pause
