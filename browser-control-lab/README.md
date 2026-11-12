# Local Browser Control Lab

macOS 로컬 환경에서 Playwright 기반 Chromium 테스트 세션을 만들고, 웹 관리자 대시보드에서 중앙 제어하는 시스템입니다.
테스트 대상은 **이 서버가 직접 제공하는 localhost 테스트 페이지로 한정**됩니다.

> 범위 밖(구현하지 않음): 외부 서비스 조회수·시청시간·추천 알고리즘 조작, 계정 로그인 자동화, 탐지 회피, 임의 외부 URL 호출.
> `custom` 시나리오는 **본인이 소유하거나 테스트 권한이 있는 대상**에만 동작합니다. localhost는 항상 허용하고, 외부 도메인은 allowlist 등록과 소유권 확인을 모두 거쳐야 합니다. 외부 미디어/SNS 플랫폼은 항상 차단합니다. 자세한 내용은 [사용자 지정 URL 테스트](#사용자-지정-url-테스트)를 참고하세요.

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
   이미 등록된 세션이 있으면 바로 등록하지 않고 선택 창을 띄웁니다.
   - **교체**: 기존 세션을 중지·삭제하고 정확히 N개만 남깁니다.
   - **추가**: 기존 세션에 더해 총 개수가 늘어납니다. 동시 실행 한도를 넘으면 미리 경고합니다.
   - **취소**

   API에서는 `POST /api/sessions` 본문에 `"replace": true`를 넣으면 교체가 됩니다.
2. **전체 시작 / 전체 중지 / 전체 삭제**: 상단 버튼으로 일괄 제어합니다.
3. **개별 제어**: 표의 각 행에서 `시작`, `중지`, `재시작`, `✕`(삭제)를 누릅니다. 다른 세션에는 영향이 없습니다.
4. **모니터링**: 아래 항목이 WebSocket으로 1초마다 갱신됩니다. WebSocket이 끊기면 2초 간격 폴링으로 자동 전환합니다.
   - 전체·실행 중·오류 세션 수, CPU/RAM(임계치 표시), Chromium 프로세스 수
   - 세션별 실행시간·반복 횟수·로딩 시간·TTFB·격리 확인·메모리(USS)·오류 수
   - 최근 로그
5. **메모리 패널** (아래 [메모리 측정](#메모리-측정) 참고)
   - **시스템 메모리 구성**: macOS는 앱/와이어드/압축/캐시, Linux는 사용/캐시/여유
   - **테스트 브라우저 메모리**: 브라우저 USS 합계, 전체 RAM 대비 %, RSS 합계(비교용), 나머지 시스템 사용량, 백엔드 서버 RSS
   - **메모리 압력 · Swap**: 압력 등급, Swap 사용량, Swap in/out 속도, 압축 메모리

## 사용자 지정 URL 테스트

대시보드의 테스트 시나리오에서 `custom — 사용자 지정 URL`을 선택하면 설정 패널이 열립니다.

| 항목 | 설명 |
|---|---|
| 테스트 URL | 영상 파일 URL(`.mp4` `.webm` `.m4v` `.ogv` `.ogg` `.mov`) 또는 영상이 있는 페이지 URL. `http://`/`https://`만 허용. **입력한 값은 그대로 유지됩니다**(자동 치환 없음, 새로고침해도 이 브라우저에 남음). |
| 모드 | **Page Load**: URL을 열고, 지정한 간격마다 다시 로드합니다. **Video Playback**: 페이지 로드 후 첫 번째 HTML5 `<video>`를 찾아 재생합니다. |
| 새로고침 간격(초) | Page Load 전용. 최소 `LAB_CUSTOM_MIN_RELOAD_S`(기본 2초) |
| Muted / Loop | Video Playback 전용. Loop를 켜면 영상이 끝날 때마다 처음부터 다시 재생하고 반복 횟수를 셉니다. 끄면 한 번 재생 후 `ended` 상태로 유지됩니다. |
| URL 확인 | 백엔드가 URL을 검사한 뒤, 세션과 같은 네트워크 정책을 적용한 일회용 Chromium으로 **한 번 열어** 결과를 표시합니다(아래 참고). 입력값은 바뀌지 않습니다. |

이후 세션 수를 정하고 **세션 등록 → 전체 시작 / 전체 중지** 순서로 기존과 똑같이 사용합니다.

**등록 시점 고정**
- 세션은 등록 당시의 URL과 재생 옵션을 자기 설정으로 저장합니다. 응답의 `custom.url`에는 입력한 그대로, `nav_url`에는 정규화된 값이 들어갑니다.
- 이후 입력창을 바꿔도 기존 세션은 바뀌지 않습니다. 입력이 기존 세션 설정과 다르면 대시보드에 다음 안내가 표시됩니다: "URL이 변경되었습니다. 기존 등록 세션에는 적용되지 않습니다. 새 설정을 적용하려면 세션을 다시 등록하세요."
- 등록 성공 메시지와 세션 표에 등록된 URL이 그대로 표시됩니다.

### 직접 영상 파일 URL과 페이지 URL

| 종류 | 판별 | 재생 방식 |
|---|---|---|
| 직접 영상 파일 | URL 경로가 영상 확장자로 끝남 | 랩이 내부 플레이어 페이지 `/lab/player?src=<URL>`를 만들고, `<video src>`에 그 URL을 넣어 재생합니다. 영상 요청 자체는 대상 호스트로 가므로 allowlist·SSRF 규칙이 그대로 적용됩니다. 플레이어도 `src`를 다시 검증하고, URL을 HTML이 아닌 JSON으로 넣어 스크립트 주입을 막습니다. |
| 페이지 | 그 외 | 페이지를 열고 첫 번째 HTML5 `<video>`를 찾습니다. 리다이렉트 결과가 영상 파일이어도(Chromium이 띄우는 미디어 문서에 `<video>`가 있음) 동일하게 재생합니다. |

### URL 확인 결과

`POST /api/custom/probe`가 돌려주는 항목입니다.
- 입력 URL, 최종 URL(리다이렉트 경로 포함), 허용 여부, 차단 이유
- HTTP status, Content-Type
- 직접 영상 URL 여부, HTML 페이지 여부, HTML5 video 발견 여부
- 영상 정보: duration, readyState, networkState, 미디어 오류
- 소유권 확인 결과(외부 도메인)

동작 방식:
- 확인용 브라우저는 세션과 같은 allowlist·DNS 고정·차단 프록시 정책으로 동작하고, 끝나면 바로 종료·정리됩니다.
- 한 번에 하나만 실행되며, RAM 85% 또는 메모리 압력 `warn` 이상이면 실행하지 않습니다(HTTP 429).
- 리다이렉트가 허용되지 않은 곳으로 향하면 그 목적지를 차단 이유에 표시합니다.

세션 표의 **대상 · 페이지 / 영상** 칸에는 다음 정보가 표시됩니다.
- 현재 URL
- 페이지 상태(로딩 중/로드됨/오류)
- 페이지 로드 횟수
- 영상: 있음/없음, `playing` · `paused` · `ended` · `error`, currentTime / duration, 반복 횟수
- 마지막 오류

기존 측정 항목(CPU, RAM, 메모리 압력, Swap, 브라우저 메모리, 프로세스 수, 세션별 메모리, 오류 수)과 보호 로직은 custom 세션에도 그대로 적용됩니다.

### 로컬 테스트 페이지

| 경로 | 용도 |
|---|---|
| `/lab/video` | 2초 WebM(VP9) HTML5 video. Video Playback 모드 확인용 |
| `/lab/redirect-local` | 같은 호스트로 302 (허용) |
| `/lab/redirect-external` | 외부 호스트로 302 (차단되는지 확인) |
| `/lab/redirect-ip` | 클라우드 메타데이터 IP(169.254.169.254)로 302 (차단되는지 확인) |
| `/lab/static/test-2s.webm`, `test-2s-h264.mp4`, `test-2s-vp9.mp4` | 직접 영상 파일 (2초) |
| `/lab/redirect-video` | 같은 호스트의 `test-2s.webm`으로 302 |

대시보드의 예시 링크는 새 탭에서 열리기만 하며, 입력창 값을 바꾸지 않습니다.

### 내 영상 URL 등록 (대시보드, 재시작 불필요)

1. 시나리오를 `custom`으로 바꾸고 영상 URL(예: `https://video.my-site.com/clip.mp4`)을 넣은 뒤 **URL 확인**을 누릅니다.
2. 아직 허용되지 않은 도메인이면 **내 서버 등록** 상자가 나옵니다. 표시된 토큰만 들어 있는 텍스트 파일을 `https://<호스트>/.well-known/blc-lab-verify.txt`에 올립니다.
3. **확인 후 등록**을 누르면 토큰 파일을 실제로 읽어 확인합니다. 맞으면 도메인이 등록되고(`backend/data/registered_hosts.json`), 바로 영상 확인 결과가 표시됩니다. 이후 세션을 등록·시작하면 재생됩니다.

- 이 경로의 소유권 확인은 `LAB_CUSTOM_REQUIRE_VERIFICATION` 값과 관계없이 항상 수행합니다.
- 외부 미디어/SNS 플랫폼, 사설 IP(옵트인 없을 때), 잘못된 URL은 등록되지 않습니다. 등록 후에도 SSRF·리다이렉트 검사는 그대로 적용됩니다.
- 등록한 도메인은 대시보드 "대시보드에서 등록한 도메인"의 **해제**로 지웁니다.

### YouTube 단일 세션 호환성 테스트

YouTube 영상 URL이 Playwright Chromium에서 열리고 재생되는지 **브라우저 1개로 한 번만** 확인하는 기능입니다. 조회수·시청시간을 반복해서 만드는 용도로는 쓸 수 없도록 서버에서 제한합니다.

- 대상: `youtube.com/watch?v=…`, `youtu.be/…`, `youtube.com/shorts/…`, `/live/…` 영상 URL. 채널·목록 페이지는 거부합니다.
- 서버 측 강제 제한(API 직접 호출 포함):
  - 세션 수는 1개만 가능합니다. YouTube 세션은 1개만 등록되고, 두 번째 등록은 409로 거부합니다.
  - mode는 `video`만 가능하고 `loop=false`만 허용합니다. 위반하면 400으로 거부합니다.
  - 페이지는 1회만 엽니다. 재생은 1회이고, 끝나면 브라우저를 닫고 세션이 `stopped`가 됩니다.
  - 새로고침, 재시작, 재시도는 하지 않습니다. 실패하면 바로 `error`로 끝납니다. 다음 영상으로 자동 이동하면 그 영상은 재생하지 않고 종료합니다.
  - **전체 시작**에서는 제외합니다. 개별 시작만 가능합니다.
  - 실행 간 **쿨다운은 10분**입니다. 쿨다운 중 시작이나 재시작은 429로 거부합니다.
- 네트워크: YouTube·Google 도메인 목록(youtube.com, googlevideo.com, ytimg.com 등)만 열립니다. IP 리터럴과 그 밖의 호스트는 기존처럼 차단합니다.
- 대시보드 결과 패널 항목은 다음과 같습니다.
  - 입력/최종 URL, page loaded, player 발견 여부, playback state, 현재 시간과 duration
  - 실행시간, Chromium 프로세스 수, 세션 메모리(최대값 포함)
  - CPU, RAM, Memory Pressure, Swap, 마지막 오류
- 이 예외는 YouTube 단일 세션에만 적용됩니다. allowlist 등록, URL 확인(probe), 다중 세션에서는 YouTube를 포함한 모든 제3자 플랫폼을 계속 차단합니다.

### allowlist 설정 (`lab.env`)

```
LAB_CUSTOM_ALLOWED_HOSTS=test.my-site.com,staging.my-site.com   # 정확한 호스트명, 쉼표 구분, 와일드카드 불가
LAB_CUSTOM_RESOURCE_HOSTS=cdn.my-site.com                       # 하위 리소스(CDN 등)만 허용, 페이지로는 불가
LAB_CUSTOM_ALLOW_PRIVATE_NETWORK=1                              # 사설 IP 대상(자체 LAN·스테이징)일 때만
LAB_CUSTOM_HOST_OVERRIDES=staging.my-site.com=192.168.0.20      # 선택: 호스트→IP 고정
LAB_CUSTOM_REQUIRE_VERIFICATION=1                               # 기본값. 외부 도메인 소유권 확인
```

`lab.env`를 고친 뒤에는 백엔드를 다시 시작해야 반영됩니다. 잘못된 항목과 차단 플랫폼 항목은 무시되고, 그 사유가 대시보드와 로그에 표시됩니다.

**소유권 확인**
- 외부 도메인(localhost 제외)은 세션을 처음 시작할 때 `http(s)://<호스트>/.well-known/blc-lab-verify.txt`를 읽습니다. 이 파일에 대시보드에 표시된 토큰이 들어 있어야 합니다.
- 리다이렉트는 허용하지 않습니다.
- 확인 결과는 1시간 동안 캐시합니다.
- 확인에 실패하면 세션은 테스트를 시작하지 않고 `오류`로 끝납니다.
- 토큰은 `backend/data/verification_token.txt`에 저장됩니다. 공개되어도 되는 값입니다.

### 안전 설계 (SSRF 방어 포함)

1. **백엔드 검증**: 등록할 때와 시작할 때마다 검사하므로, 프론트엔드를 우회한 직접 API 호출도 같은 규칙으로 막힙니다.
   - `http`/`https`만 허용합니다(`file:`, `javascript:`, `data:` 등 차단).
   - URL 안의 계정 정보, 공백, 백슬래시, 2048자 초과 URL을 거부합니다.
   - localhost가 아닌 IP 직접 지정은 거부합니다.
   - allowlist에 없는 호스트는 거부합니다.
2. **외부 플랫폼 차단**: YouTube, Twitch, TikTok, Instagram, Facebook, X, 치지직, SOOP(아프리카TV), 네이버·카카오 TV, Vimeo, Netflix, Spotify 등 외부 미디어/SNS 플랫폼은 allowlist에 넣어도 거부합니다. 여러 자동 브라우저로 반복 재생하면 그 플랫폼의 조회수·시청시간을 조작하게 되기 때문입니다. 브라우저 안에서 이들 도메인으로 가는 하위 요청(임베드 플레이어 등)도 차단됩니다.
3. **DNS 고정**: allowlist 호스트는 백엔드가 직접 이름을 조회하고, 결과가 공인 IP인지 확인합니다. 이어 `--host-resolver-rules`로 브라우저가 바로 그 IP에만 접속하게 고정합니다. 나머지 모든 이름은 조회 결과가 없는 것(NOTFOUND)으로 처리되어, DNS 리바인딩 공격을 막습니다.
4. **네트워크 수준 차단**: custom 세션의 모든 연결은 존재하지 않는 프록시로 보내지고, localhost와 allowlist 호스트만 직접 접속합니다. Playwright의 요청 가로채기는 **리다이렉트 경유 요청을 보지 못하고**, DNS 고정은 **IP 직접 주소에는 적용되지 않기** 때문입니다. 그래서 `169.254.169.254` 같은 주소로 가는 리다이렉트도 연결 단계에서 실패합니다. 차단된 요청은 URL과 함께 세션 로그에 남습니다.
5. **경로 검사**: 페이지 이동이 끝나면 최종 URL과 리다이렉트 경로 전체를 다시 검사합니다.
6. **기존 basic 계열 시나리오**는 localhost 전용 정책 그대로이며, custom 기능과 무관하게 동작합니다.
7. **탐지 회피 없음**: User-Agent는 Playwright 기본값(HeadlessChrome) 그대로입니다. 탐지를 피하는 설정이나 계정 로그인 기능은 넣지 않았습니다.

### Video Playback 동작

1. 페이지 로드 후 최대 10초 동안 `<video>`를 찾습니다. iframe 안의 영상은 대상이 아닙니다.
2. Muted 설정을 적용하고 `play()`를 호출합니다. 브라우저는 `--autoplay-policy=no-user-gesture-required`로 실행되므로 Muted를 꺼도 재생할 수 있습니다.
3. 1초마다 다음 값을 읽어 세션 표에 표시합니다.
   - 상태(`playing`/`paused`/`ended`/`error`), paused, ended
   - currentTime / duration
   - `readyState`(HAVE_NOTHING~HAVE_ENOUGH_DATA), `networkState`(NETWORK_EMPTY/IDLE/LOADING/NO_SOURCE)
   - 미디어 오류(MEDIA_ERR_*)
   - 반복 횟수, `currentSrc`
4. 다음 경우는 세션 오류로 기록합니다.
   - 영상이 없을 때
   - `play()`가 거부될 때
   - 미디어 오류(`error` 이벤트)가 날 때
   - 페이지를 잃었을 때

   오류 후에는 간격만큼 기다렸다가 페이지를 다시 로드합니다. 재생이 시작되지 않은 채 연속 5번 실패하면 세션은 `오류`로 종료됩니다.
5. 코덱: 현재 Playwright 번들 Chromium(빌드 153, Linux에서 확인)은 H.264/AAC MP4, VP9 MP4, WebM을 모두 재생합니다.
   - 이전 버전 README의 "H.264 미지원" 안내는 잘못된 내용이어서 정정합니다.
   - 재생할 수 없는 파일은 `MEDIA_ERR_SRC_NOT_SUPPORTED`와 원인 안내가 세션 오류로 기록됩니다.
   - Mac용 빌드에서 MP4가 이 오류로 실패하면 WebM이나 VP9 MP4로 확인하세요.

### API

```
POST /api/sessions        {"count": 2, "scenario": "custom",
                           "custom": {"url": "http://127.0.0.1:8000/lab/video", "mode": "video",
                                      "reload_interval_s": 10, "muted": true, "loop": true}}
                          → 201, 또는 400 {"detail": "URL not allowed: ..."}
POST /api/custom/validate {"url": "..."} → 200 {ok, url, host, local, resolved_ips, verification_required, verify_url} | 400   (빠른 검사, 접속 안 함)
POST /api/custom/probe    {"url": "..."} → 200 {input_url, final_url, redirects, allowed, blocked_reason, http_status, content_type,
                                                is_direct_video, is_html, video_found, video{duration, ready_state_name, ...}} | 429
GET  /lab/player?src=<URL> → 직접 영상 파일용 내부 플레이어 (src 재검증, 허용되지 않으면 400)
GET  /api/config          → custom: {allowed_hosts, resource_hosts, verification: {path, token}, local_test_pages, ...}
```

세션 응답에는 다음 필드가 추가됩니다.

```
custom: {url(입력 그대로), nav_url, kind(page|video_file), mode, reload_interval_s, muted, loop, registered_at},
current_url, page_state, page_load_count,
video: {present, state, paused, ended, current_time, duration, ready_state(_name), network_state(_name),
        error{code,name,message}, current_src, video_count, repeats, muted, loop}
```

## 메모리 측정

| 표시 항목 | macOS | Linux | 의미 |
|---|---|---|---|
| 시스템 RAM 사용 % (차단 기준) | `vm_stat`: 앱(Anonymous−Purgeable) + 와이어드 + 압축 | total − available | Activity Monitor의 "사용된 메모리"와 같은 방식 |
| psutil % (비교용) | (total − available)/total. available = free + inactive만 포함 | 위와 동일 | macOS에서는 캐시를 사용으로 일부 계산해 **더 높게** 나옴 |
| 테스트 브라우저 USS | 세션 프로세스 트리의 USS 합 | 동일 | 세션을 끄면 회수되는 메모리. 실제 비용에 가장 가까움 |
| 테스트 브라우저 RSS | 프로세스별 RSS 합 | 동일 | 공유 라이브러리를 프로세스마다 중복 계산. 실제보다 **2~8배 큼** |
| 메모리 압력 | `sysctl kern.memorystatus_vm_pressure_level` (1 정상 / 2 경고 / 4 위험) | PSI `/proc/pressure/memory` (some avg10 ≥10% 경고, ≥40% 또는 full ≥5% 위험) | OS가 메모리 부족으로 느려지는지 |
| Swap | `psutil.swap_memory()` (`vm.swapusage`) 사용량과 초당 in/out | 동일 | Swap out이 계속 늘면 RAM이 부족한 상태 |

- 새 세션 실행은 RAM 사용 %가 `LAB_RAM_THRESHOLD` 이상이거나, 메모리 압력이 `LAB_PRESSURE_BLOCK_LEVEL`(기본 `warn`) 이상이면 차단됩니다.
- 일부 프로세스의 USS를 읽지 못하면 RSS로 대체하고, 대시보드에 그 사실을 표시합니다(`memory_method`: `uss` / `uss+rss` / `rss`).
- 참고: 리눅스 컨테이너에서 6세션은 USS 합계 679 MB, RSS 합계 5,307 MB였습니다. RSS를 그대로 더하면 메모리를 크게 과대평가합니다.

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
- **PID 추적**: Chromium 보조 프로세스(렌더러, GPU, 네트워크)에는 마커가 없어서 브라우저 프로세스의 *자식*으로만 찾을 수 있습니다. 메인 프로세스가 먼저 죽으면 보조 프로세스는 부모를 잃고(macOS에서는 launchd로 재배치) 자식 검색에서 빠집니다. 이를 막기 위해 세션 실행 중 보이는 모든 PID를 시작 시각과 함께 메모리와 SQLite(`tracked_processes`)에 기록합니다. 정리할 때는 기록된 PID 중 시작 시각까지 같은 프로세스만 종료하므로, PID가 재사용된 다른 프로세스는 건드리지 않습니다.
- **전체 중지 검증**: `전체 중지`는 세션을 멈춘 뒤 마커 검색과 추적 PID로 남은 프로세스를 다시 확인해 `verified_clean`과 `remaining_processes`를 돌려줍니다. 대시보드에 "브라우저 프로세스 0개 확인"이 표시되고, `GET /api/processes`로 언제든 확인할 수 있습니다.
- **고아 프로세스 복구**: 서버가 시작할 때 마커의 서버 PID가 이미 죽은 Chromium을 찾아 종료합니다. 죽은 서버가 추적 PID로 남긴 프로세스도 함께 종료합니다. 이전 실행에서 `running` 상태로 남은 세션은 `대기`로, 끝나지 않은 실행 이력은 `recovered_after_crash`로 정리합니다. 동시에 실행 중인 다른 랩 인스턴스의 브라우저는 건드리지 않습니다.
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
| `LAB_ENV_FILE` | `<프로젝트>/lab.env` | 머신 설정 파일 경로 |
| `LAB_CUSTOM_ALLOWED_HOSTS` | (없음) | custom 시나리오 대상 외부 호스트(정확한 이름, 쉼표 구분) |
| `LAB_CUSTOM_RESOURCE_HOSTS` | (없음) | 하위 리소스 전용 허용 호스트 |
| `LAB_CUSTOM_ALLOW_PRIVATE_NETWORK` | `0` | `1`이면 allowlist 호스트가 사설 IP로 해석되어도 허용 |
| `LAB_CUSTOM_HOST_OVERRIDES` | (없음) | `호스트=IP` 고정 매핑 |
| `LAB_CUSTOM_REQUIRE_VERIFICATION` | `1` | 외부 도메인 소유권 확인 |
| `LAB_CUSTOM_MIN_RELOAD_S` | `2` | Page Load 최소 새로고침 간격(초) |
| `LAB_PORT` | `8000` | 백엔드 포트 |
| `LAB_MAX_SESSIONS` | `100` | 등록 가능한 최대 세션 수(상한 100) |
| `LAB_MAX_CONCURRENT` | 자동 (`lab.env`: 100) | 동시 실행 한도. 지정하지 않으면 `min(25, RAM(MB)/300, 코어수×3)` |
| `LAB_CPU_THRESHOLD` | `85` | 이 CPU%를 넘으면 신규 실행 차단 |
| `LAB_RAM_THRESHOLD` | `85` | 이 RAM 사용%(위 표의 OS 기준 값)를 넘으면 신규 실행 차단 |
| `LAB_PRESSURE_BLOCK_LEVEL` | `warn` | 메모리 압력이 이 등급 이상이면 신규 실행 차단 (`warn` / `critical` / `off`) |
| `LAB_ITERATION_INTERVAL` | `3` | 세션 반복 간격(초) |
| `LAB_PAGE_TIMEOUT_MS` | `15000` | 페이지 로딩 타임아웃 |
| `LAB_HEADLESS` | `1` | `0`이면 브라우저 창 표시 |
| `LAB_CHROMIUM_EXECUTABLE` | (없음) | 특정 Chromium 바이너리 사용 |
| `LAB_DB_PATH` | `backend/data/lab.sqlite3` | SQLite 경로 |
| `NEXT_PUBLIC_API_URL` | `http://127.0.0.1:8000` | 대시보드가 접속할 API (`frontend/.env.local`) |

### 머신 설정 파일 `lab.env`

백엔드는 시작할 때 프로젝트 루트의 `lab.env`를 읽습니다.
- 파일 위치는 `LAB_ENV_FILE`로 바꿀 수 있습니다.
- `LAB_*` 키만 적용하고, `#` 주석과 빈 줄은 무시합니다.
- **셸에 이미 설정된 `LAB_*` 환경변수가 파일보다 우선합니다.**
- 파일을 고친 뒤에는 백엔드를 다시 시작해야 반영됩니다.
- 테스트(pytest)는 `lab.env`와 셸 환경변수를 무시하고 고정값(동시 실행 10)으로 실행합니다.

현재 저장소의 `lab.env`는 전용 테스트 머신(Mac mini M4 / 16GB)용입니다.

```
LAB_MAX_CONCURRENT=100
```

이 머신의 자동 계산값은 `min(25, RAM MB/300, 코어×3)` = 25입니다. 25개, 50개가 정상 동작하는 것을 차례로 확인한 뒤 테스트 편의를 위해 100(최대 등록 수와 같음)으로 올렸습니다. 실제 실행 수는 보호 로직이 결정합니다. 이 값을 올려도 아래 보호 로직은 그대로 신규 실행을 차단합니다.
- RAM 사용 85% 이상
- 메모리 압력 `warn` 이상
- CPU 85% 이상

예: 일회성으로 다른 값 쓰기 (lab.env보다 우선)

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

단계별 성능 테스트 (세션이 0개인 상태에서 실행):

```bash
backend/.venv/bin/python scripts/perf_test.py --stages 1,3,6,10 --hold 30
```

- **실행 순서**: 유휴 기준선(세션 0개)을 먼저 측정합니다. 이어 단계마다 세션 N개로 교체 → 시작 → `--hold`초 동안 1초 간격 측정 → 전체 중지 → `/api/processes`로 남은 프로세스 0개 확인 순서로 진행합니다.
- **기록 항목**: CPU 평균·최대, RAM 사용%(OS 기준)와 psutil%, 테스트 브라우저 USS·RSS 합계와 세션당 값, 프로세스 수, 최악 메모리 압력, Swap 증가량, 로딩 시간, 오류, 중지 시간, 잔여 프로세스
- **용량 추정**: 세션 1개당 RAM·CPU 증가량을 선형 근사해 임계치 기준 최대 세션 수를 추정합니다.
- **자동 중단**: 10개를 넘는 단계, 그리고 직전 단계에서 메모리 압력이 경고 이상이었던 경우에는 아래 조건을 모두 만족해야 다음 단계로 갑니다.
  - 추정 CPU·RAM이 한도 미만
  - 메모리 압력 정상
  - Swap 증가량 0.25 GB 이하
  - 오류 0건
- **결과 파일**: `reports/perf-<시각>.md`, `.html`(그래프), `.csv`, `.json`(초 단위 시계열 포함)

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
| GET | `/api/processes` | 이 서버가 띄운 브라우저 프로세스 목록과 메모리 (마커 검색 + 추적 PID) |

## 알려진 제한

- 개발과 자동 테스트는 Linux 컨테이너(4코어, 16GB)에서 했습니다. macOS 6세션 실행은 사용자가 확인했습니다.
- macOS 메모리 경로(`vm_stat` 파싱, `sysctl` 압력)는 `vm_stat` 형식의 샘플 출력으로 단위 테스트했으며, 실제 Mac에서는 아직 검증하지 않았습니다.
- 세션 메모리는 USS 합계입니다. macOS에서 USS를 읽지 못하면 RSS로 대체하고 화면에 표시합니다.
- Activity Monitor의 프로세스별 "메모리"(phys_footprint)는 psutil로 읽을 수 없어, USS와 약간 다를 수 있습니다.
- 이 프로젝트는 로컬 전용입니다. 외부 서버 배포 설정은 포함하지 않았습니다.
