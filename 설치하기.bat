@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo ========================================
echo   부동산 블로그 자동화 시스템 설치
echo ========================================
echo.
echo 필요한 부품들을 설치합니다.
echo 1~3분 정도 걸려요. 잠시 기다려주세요...
echo.

REM Python 확인
where python >nul 2>&1
if errorlevel 1 (
    echo X Python이 설치되어 있지 않습니다.
    echo.
    echo 1) https://www.python.org/downloads/ 에서 Python을 먼저 설치하세요
    echo 2) 설치 시 "Add Python to PATH" 체크박스를 반드시 체크!
    echo 3) 설치 후 이 파일을 다시 더블클릭하세요
    echo.
    pause
    exit /b 1
)

echo OK Python 확인됨
python --version
echo.

REM pip 업그레이드 및 패키지 설치
echo [부품 설치 중...]
python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt

if errorlevel 1 (
    echo.
    echo X 설치 중 오류가 발생했습니다.
    echo 사용설명서.md의 "문제가 생기면" 섹션을 확인하세요.
    echo.
    pause
    exit /b 1
)

echo.
echo ========================================
echo   설치 완료!
echo ========================================
echo.
echo 다음에 사용하실 때는 '시작하기.bat' 을
echo 더블클릭만 하시면 됩니다.
echo.

REM .env 파일이 없으면 안내
if not exist ".env" (
    echo [중요] 아직 API 키 설정이 안 됐어요.
    echo.
    echo   1. '.env.example' 파일을 복사해서 '.env' 라는 이름으로 만들어주세요
    echo   2. '.env' 파일을 열어 ANTHROPIC_API_KEY 부분에 본인 키를 입력하세요
    echo   3. 자세한 방법은 '사용설명서.md' 의 '단계 5' 참조
    echo.
)

pause
