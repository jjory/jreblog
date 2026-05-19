# 🏠 JRE일본부동산 블로그 자동화 시스템

> 마이소크(物件図面) → Claude AI 분석 → 한국어 네이버 블로그 자동 생성.
> 한 번에 최대 5개 도면을 올려 블로그 5개를 일괄 생성합니다.

## 📌 시작하기

이 시스템은 **두 가지 방식**으로 사용할 수 있습니다.

### 방식 A — 클라우드 웹 서비스 (권장) ☁️
인터넷 어디서나 접속하는 웹사이트로 운영. PC에 설치 불필요.
- **[클라우드_배포_가이드.md](./클라우드_배포_가이드.md)** — GitHub + Streamlit Cloud 배포 (무료)
- 배포 후 주소: `https://jreblog.streamlit.app`

### 방식 B — 사무실 PC 로컬 실행 💻
사무실 PC 1대에서 실행 → 같은 네트워크에서 브라우저로 접속.
- **[사용설명서.md](./사용설명서.md)** — `.bat` 더블클릭 방식
- **[vscode_가이드.md](./vscode_가이드.md)** — VSCode 사용 방식

## ⚡ 빠른 시작 — 클라우드 배포 (방식 A)

1. GitHub에 **비공개 저장소** 생성 → 프로그램 파일 업로드
   (단, `.env`·`secrets.toml` 등 API 키 파일은 제외)
2. [share.streamlit.io](https://share.streamlit.io) 에서 GitHub 연동 후 앱 배포
3. "Advanced settings → Secrets"에 API 키·비밀번호 입력
4. 서브도메인을 `jreblog`로 설정 → 완료

자세한 내용은 **[클라우드_배포_가이드.md](./클라우드_배포_가이드.md)** 참고.

## ⚡ 빠른 시작 — 로컬 실행 (방식 B)

프로그램 폴더 위치: `G:\내 드라이브\0.사내공유\1.부동산_공유\블로그자동작성\property_blog`

1. **`설치하기.bat`** 더블클릭 (한 번만)
2. **`방화벽허용하기.bat`** 우클릭 → 관리자 권한으로 실행 (한 번만)
3. `.env` 파일에 API 키와 공용 비밀번호 설정 (`.env.example` 복사)
4. **`시작하기.bat`** 더블클릭 → 표시되는 LAN IP 주소를 직원에게 공유

직원 PC는 브라우저에서 서버 PC의 LAN 주소(예: `http://192.168.1.10:8501`)로 접속.

## 🔧 시스템 사양

| 항목 | 내용 |
|------|------|
| OS | Windows 10/11 (서버 PC) |
| 클라이언트 | Windows / Mac / 모바일 브라우저 |
| 언어 | Python 3.10+ |
| AI | Claude Opus 4.7 Vision |
| UI | Streamlit (웹 브라우저) |
| 발행 대상 | 네이버 블로그 |

## 💰 비용

- 프로그램: 무료
- 글 1건 생성: 약 170~200원 (Anthropic API)
- 월 50건 발행 시: 약 8,000~10,000원

## 🔐 보안

- 사무실 LAN 내부에서만 접속 가능 (외부 인터넷 차단)
- 공용 비밀번호로 1차 보호
- 각 직원이 본인 이름으로 입장 (작업 추적 가능)
- API 키는 서버 PC의 `.env` 파일에만 저장 (직원에게 노출 X)

## 📂 파일 구조

```
property_blog/  (G:\내 드라이브\0.사내공유\1.부동산_공유\블로그자동작성\ 안에 위치)
├── 사용설명서.md          ← `.bat` 방식 가이드 (일반)
├── vscode_가이드.md       ← VSCode 방식 가이드 (관리자)
├── SOLUTION.md            ← 설계 문서 (개발자용)
├── README.md              ← 이 파일
├── 설치하기.bat           ← 첫 설치 시 1회
├── 방화벽허용하기.bat     ← 첫 설치 시 1회 (관리자)
├── 시작하기.bat           ← 매일 아침 더블클릭
├── .env.example          ← 환경변수 템플릿
├── .vscode/              ← VSCode 설정 (F5 한 번에 실행)
│   ├── launch.json
│   ├── settings.json
│   └── extensions.json
├── requirements.txt
├── app.py                ← Streamlit UI (인증 포함)
├── src/
│   ├── analyzer.py       ← Claude Vision 도면 분석
│   ├── generator.py      ← 한국어 블로그 생성
│   └── naver_publisher.py ← 네이버 API
└── examples/
    ├── sample_output.html
    └── sample_property_data.json
```

## ❓ 문의

문제 발생 시 **사용설명서.md** 의 "7. 문제가 생기면" 섹션 참고.
