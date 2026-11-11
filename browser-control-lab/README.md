# Local Browser Control Lab

macOS 로컬 환경에서 Playwright 기반 Chromium 테스트 세션을 만들고, 웹 관리자 대시보드에서 중앙 제어하는 시스템입니다.
테스트 대상은 **이 서버가 직접 제공하는 localhost 테스트 페이지로 한정**됩니다.

> 범위 밖(구현하지 않음): 외부 서비스 조회수·시청시간·추천 알고리즘 조작, 계정 로그인 자동화, 탐지 회피, 임의 외부 URL 호출.

## 구성

```
browser-control-lab/
├── backend/                FastAPI + Playwright + SQLite + psutil
│   ├── app/
│   │   ├── main.py             REST API, WebSocket(/ws), 앱 수명주기
│   │   ├── session_manager.py  세션 생성·시작·중지·재시작, 자원 가드, 정리
│   │   ├── testpages.py        자체 테스트 페이지 (/lab/*)
│   │   ├── procs.py            Chromium 프로세스 탐색·종료, 고아 프로세스 복구
│   │   ├── security.py         localhost 전용 URL 정책, 로그 비밀값 마스킹
│   │   ├── db.py               SQLite (세션·실행 이력·로그)
│   │   └── config.py           LAB_* 환경변수 설정
│   └── tests/                  pytest (실제 Chromium 사용)
├── frontend/               Next.js 15 + TypeScript + Tailwind CSS 4 대시보드
├── scripts/
│   ├── setup.sh                최초 설치
│   ├── dev.sh                  백엔드 + 대시보드 동시 실행
│   ├── e2e_dashboard.py        대시보드 UI 통합 테스트 (10개 세션)
│   └── perf_test.py            단계별 성능 테스트 (1 → 5 → 10 → 25)
└── reports/                성능 측정 결과, 대시보드 스크린샷
```

## 설치 (macOS)

필요: Python 3.10+, Node.js 18.18+ (권장 20+), npm

```bash
cd browser-control-lab
./scripts/setup.sh
```

`setup.sh`가 하는 일: `backend/.venv` 생성 → `pip install -r backend/requirements.txt` → `playwright install chromium` → `frontend`에서 `npm install` → `frontend/.env.local` 생성.

수동 설치:

```bash
python3 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements.txt
backend/.venv/bin/python -m playwright install chromium
cd frontend && npm install && cp .env.example .env.local
```

## 실행

```bash
./scripts/dev.sh
```

- 대시보드: http://127.0.0.1:3000
- API 문서(Swagger): http://127.0.0.1:8000/docs
- 테스트 페이지 목록: http://127.0.0.1:8000/lab

`Ctrl+C`를 누르면 두 서버가 함께 종료되고, 백엔드는 자신이 띄운 Chromium을 모두 정리한 뒤 종료합니다.

각각 따로 실행:

```bash
cd backend && .venv/bin/python -m app.main
```

```bash
cd frontend && npm run dev
```

운영 빌드로 대시보드를 띄우려면 `npm run build && npm start` 를 사용합니다.

## 사용법

1. **세션 등록**: 세션 수와 시나리오를 고른 뒤 `세션 등록`을 누릅니다. 최대 100개까지 등록됩니다.
2. **전체 시작 / 전체 중지 / 전체 삭제**: 상단 버튼으로 일괄 제어합니다.
3. **개별 제어**: 표의 각 행에서 `시작`, `중지`, `재시작`, `✕`(삭제)를 누릅니다. 다른 세션에는 영향이 없습니다.
4. **모니터링**: 전체·실행 중·오류 세션 수, CPU/RAM(임계치 표시), Chromium 프로세스 수, 세션별 실행시간·반복 횟수·로딩 시간·TTFB·격리 확인·메모리·오류 수, 최근 로그가 WebSocket으로 1초마다 갱신됩니다. WebSocket이 끊기면 2초 간격 폴링으로 자동 전환합니다.

### 테스트 시나리오 (모두 localhost)

| 시나리오 | 페이지 | 용도 |
|---|---|---|
| `basic` | `/lab/basic` | HTML + JS + 로컬 asset 로딩 |
| `heavy` | `/lab/heavy` | DOM 2,000개 렌더링 부하 |
| `slow` | `/lab/slow?delay_ms=800` | 느린 서버 응답 |
| `flaky` | `/lab/flaky?fail_rate=0.3` | 30% 확률 HTTP 500 → 간헐 오류 기록 |
| `error` | `/lab/error` | 항상 HTTP 500 → 연속 5회 실패 시 세션 `오류` 상태로 종료 |

세션은 시나리오 이름만 받습니다. URL을 직접 입력하는 기능은 없습니다.

### 세션 1회 반복에서 하는 일

1. 테스트 페이지로 이동하고 로딩 시간(`load` 이벤트까지)과 TTFB(Navigation Timing)를 측정합니다.
2. HTTP 4xx/5xx나 타임아웃이 나면 오류로 기록합니다(DB 로그 + 세션 `last_error`).
3. **격리 확인**: 자기 세션 ID를 `localStorage`와 쿠키에 쓰고, 서버의 `/lab/whoami`로 쿠키를 되읽어 자기 값만 보이는지 확인합니다.
4. `LAB_ITERATION_INTERVAL`초(기본 3초) 쉰 뒤 반복합니다.

## 설계 메모

- **세션 격리**: 세션마다 별도의 Chromium 프로세스(별도 프로필, 쿠키, 저장소)를 사용합니다.
- **프로세스 추적**: 모든 Chromium에 `--blc-lab-marker=<서버PID>:<세션ID>` 인자를 붙입니다. 세션 중지 시 브라우저를 정상 종료한 다음, 같은 마커를 가진 프로세스와 그 자식 프로세스가 남아 있으면 강제 종료하고 0개인지 확인합니다.
- **고아 프로세스 복구**: 서버가 시작할 때 마커의 서버 PID가 이미 죽은 Chromium을 찾아 종료합니다. 이전 실행에서 `running` 상태로 남은 세션은 `대기`로, 끝나지 않은 실행 이력은 `recovered_after_crash`로 정리합니다. 동시에 실행 중인 다른 랩 인스턴스의 브라우저는 건드리지 않습니다.
- **자원 가드**: 다음 중 하나에 해당하면 새 세션 실행을 거부합니다(HTTP 429, 대시보드에 사유 표시).
  - 실행 중 + 시작 중인 세션 수 ≥ `LAB_MAX_CONCURRENT`
  - CPU(3초 이동평균) ≥ `LAB_CPU_THRESHOLD`
  - RAM ≥ `LAB_RAM_THRESHOLD`

  `전체 시작`은 3개씩 나눠 시작하고, 묶음마다 브라우저가 뜬 뒤 CPU/RAM을 다시 측정합니다. 그래서 한 번에 모두 허용된 뒤에야 과부하가 드러나는 일이 없습니다.
- **네트워크 제한(2중)**:
  1. 페이지 요청은 Playwright route 가드가 검사해 localhost가 아니면 차단하고 로그를 남깁니다.
  2. Chromium 자체도 `--no-proxy-server`와 `--host-resolver-rules="MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1"`로 실행하고, 백그라운드 네트워킹·컴포넌트 업데이트·동기화를 끕니다. 따라서 브라우저 내부 서비스도 외부 호스트에 접속하지 못합니다.
- **로그 보안**: 모든 로그는 저장 전에 마스킹합니다. 대상은 `password=`, `token`, `api_key`, `Authorization: Bearer …`, `cookie`, URL 안의 `user:pass@` 형식입니다.
- **바인딩**: 백엔드와 대시보드 모두 `127.0.0.1`에만 바인딩합니다.

## 설정 (환경변수)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `LAB_PORT` | `8000` | 백엔드 포트 |
| `LAB_MAX_SESSIONS` | `100` | 등록 가능한 최대 세션 수(상한 100) |
| `LAB_MAX_CONCURRENT` | 자동 | 동시 실행 한도. 기본값은 `min(25, RAM(MB)/300, 코어수×3)` |
| `LAB_CPU_THRESHOLD` | `85` | 이 CPU%를 넘으면 신규 실행 차단 |
| `LAB_RAM_THRESHOLD` | `85` | 이 RAM%를 넘으면 신규 실행 차단 |
| `LAB_ITERATION_INTERVAL` | `3` | 세션 반복 간격(초) |
| `LAB_PAGE_TIMEOUT_MS` | `15000` | 페이지 로딩 타임아웃 |
| `LAB_HEADLESS` | `1` | `0`이면 브라우저 창 표시 |
| `LAB_CHROMIUM_EXECUTABLE` | (없음) | 특정 Chromium 바이너리 사용 |
| `LAB_DB_PATH` | `backend/data/lab.sqlite3` | SQLite 경로 |
| `NEXT_PUBLIC_API_URL` | `http://127.0.0.1:8000` | 대시보드가 접속할 API (`frontend/.env.local`) |

예: 25개 이상 테스트하기

```bash
cd backend && LAB_MAX_CONCURRENT=25 .venv/bin/python -m app.main
```

## 테스트

단위 + API + 크래시 복구 테스트 (실제 Chromium 사용, 약 30초):

```bash
cd backend && .venv/bin/pytest -q
```

대시보드 UI 통합 테스트 (백엔드와 대시보드가 실행 중이고 세션이 0개여야 함):

```bash
backend/.venv/bin/python scripts/e2e_dashboard.py --sessions 10
```

단계별 성능 테스트 (결과는 `reports/perf-*.md|json`):

```bash
backend/.venv/bin/python scripts/perf_test.py --hold 30 --max 25
```

`perf_test.py`는 1 → 5 → 10 단계를 실행합니다. 25 단계는 두 조건을 모두 만족할 때만 진행합니다.

- 10 단계에서 측정한 CPU/RAM을 25개로 선형 추정했을 때 75% 미만이고, 오류가 0건
- 25가 `LAB_MAX_CONCURRENT` 이하

### pytest 테스트 항목

- localhost 외 URL 차단(`127.0.0.1.evil.com`, `localhost@evil.com`, `file://` 등)과 비밀값 마스킹
- 세션 시작 → 측정 → 격리 확인 → 재시작 → 중지 후 프로세스 0개 확인
- 10개 세션 일괄 시작, 1개만 중지해도 나머지 9개는 유지, 전체 중지 후 프로세스 0개 확인
- 오류 시나리오: HTTP 500 기록, 연속 실패 시 `오류` 상태, 프로세스 정리
- 동시 실행 한도, CPU 임계치, RAM 임계치 초과 시 실행 차단(429)
- 세션 100개 등록 가능, 101번째는 거부(409)
- 화이트리스트에 없는 시나리오나 URL은 거부(422)
- 서버 SIGKILL 후 재시작하면 고아 Chromium 종료, 세션 상태 복구, 실행 이력 정리

## API 요약

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/state` | 대시보드 스냅샷(요약, 시스템, 세션, 최근 로그) |
| WS | `/ws` | `/api/state`를 1초마다 푸시 |
| POST | `/api/sessions` | `{count, scenario}`로 세션 등록 |
| DELETE | `/api/sessions` / `/api/sessions/{id}` | 전체 삭제 / 개별 삭제 (실행 중이면 먼저 중지) |
| POST | `/api/sessions/{id}/start` · `/stop` · `/restart` | 개별 제어 |
| POST | `/api/sessions/start-all` · `/stop-all` | 일괄 제어 |
| GET | `/api/logs?level=&session_id=` | 로그 조회 |
| GET | `/api/runs` | 실행 이력(시작·종료·사유·반복·오류·평균 로딩) |

## 알려진 제한

- 개발과 검증은 Linux 컨테이너(4코어, 16GB)에서 했습니다. 코드는 macOS 전용 API를 쓰지 않으며 psutil과 Playwright 모두 macOS를 지원하지만, **실제 Mac에서는 아직 실행해 보지 않았습니다.**
- 세션 메모리는 프로세스별 USS(고유 메모리) 합계입니다. macOS에서 권한 문제로 USS를 읽지 못하면 RSS로 대신하며, 이 경우 공유 메모리가 포함되어 값이 더 크게 보입니다.
- 이 프로젝트는 로컬 전용입니다. 외부 서버 배포 설정은 포함하지 않았습니다.
