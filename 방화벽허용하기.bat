@echo off
chcp 65001 >nul

REM 관리자 권한 확인
net session >nul 2>&1
if errorlevel 1 (
    echo.
    echo ====================================================
    echo   [관리자 권한 필요]
    echo ====================================================
    echo.
    echo 이 스크립트는 Windows 방화벽 설정을 변경하기 때문에
    echo 관리자 권한으로 실행해야 합니다.
    echo.
    echo 사용 방법:
    echo   1. 이 파일을 우클릭
    echo   2. "관리자 권한으로 실행" 클릭
    echo.
    pause
    exit /b 1
)

echo.
echo ====================================================
echo   방화벽 설정 - 사무실 직원 PC가 접속할 수 있게 합니다
echo ====================================================
echo.

REM 기존 규칙 있으면 삭제 (중복 방지)
netsh advfirewall firewall delete rule name="부동산블로그 시스템 (8501)" >nul 2>&1

REM 포트 8501 허용 규칙 추가 (인바운드, TCP, 사설 네트워크만)
netsh advfirewall firewall add rule ^
    name="부동산블로그 시스템 (8501)" ^
    dir=in ^
    action=allow ^
    protocol=TCP ^
    localport=8501 ^
    profile=private ^
    description="사무실 직원 PC가 부동산 블로그 시스템에 접속하기 위한 포트"

if errorlevel 1 (
    echo.
    echo X 방화벽 규칙 추가 실패. 직접 설정하셔야 합니다.
    echo   제어판 - 방화벽 - 고급설정 - 인바운드 규칙 - 새 규칙
    echo.
) else (
    echo.
    echo OK 방화벽 설정 완료!
    echo.
    echo 이제 같은 사무실 네트워크의 직원 PC가 접속할 수 있습니다.
    echo 한 번만 실행하면 됩니다.
    echo.
)

pause
