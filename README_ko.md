# CAD-MCP Server (CAD Model Context Protocol Server)

[English](/README_en.md) | [中文](/README_zh.md) | 한국어

> 이 저장소는 [daobataotie/CAD-MCP](https://github.com/daobataotie/CAD-MCP)의 fork입니다.
> 원본은 AutoCAD/GstarCAD(浩辰CAD)/ZWCAD(中望CAD)를 지원하며, 이 fork는
> **CADian(캐디안, IntelliCAD 계열)** 지원과 안정성 개선을 추가했습니다.

## 프로젝트 소개

CAD-MCP는 자연어 명령으로 CAD 소프트웨어를 직접 제어하는 MCP(Model
Context Protocol) 서버입니다. Claude 같은 LLM과 대화하면서 CAD 인터페이스를
직접 조작하지 않고도 도면을 그리고 수정할 수 있습니다.

## 이 fork에서 달라진 점

원본 대비 아래 내용이 추가/수정되었습니다. 자세한 진단 과정과 근거는
[history.md](history.md)(작업 이력)와 [spec.md](spec.md)(아키텍처 스펙)에
남겨두었습니다.

- **CADian(IntelliCAD 계열) 지원 추가** — 좌표를 COM 객체로 요구하는 캐디안
  타입 라이브러리 대응, 색상/선두께 속성명 자동 탐지, 폴리라인/해치 인자
  형태 자동 탐색 등.
- **COM 전용 작업 스레드 도입** — CAD 자동화는 STA(Single-Threaded
  Apartment) COM이라 호출 스레드가 엇갈리면 `RPC_E_WRONG_THREAD`가 나고,
  캐디안이 명령 대기 중이면 호출이 무한 블로킹됩니다. 전용 스레드 하나가
  COM 연결을 계속 소유하고, 다른 도구 호출은 타임아웃과 함께 큐를 통해
  처리되도록 재설계했습니다(`cad_worker.py`). 막힌 연결은 `cad_reconnect`
  도구로 안전하게 새로 붙습니다.
- **배치 작도 / 삭제 도구 추가** — `draw_entities`(도형 목록 한 번에 작도),
  `delete_by_layer`, `delete_by_window`(범위 삭제). 자료가 바뀌어 도면을
  다시 그려야 할 때 도형 하나마다 왕복하지 않아도 됩니다.
- **도면 범위(EXTMIN/EXTMAX) 자동 갱신** — 저장/조회 직전에 자동으로
  범위를 재계산해, 항상 (0,0,0)으로 나오던 문제를 해결했습니다.
- **응답 크기 최적화** — 엔티티가 많은 도면에서 좌표까지 전부 반환하면
  LLM 쪽 응답 한도를 넘겨버리는 문제가 있어, 기본 응답을 요약형으로
  바꾸고 필요할 때만 전체 좌표를 받도록 했습니다(`list_entities`의
  `include_geometry`, `draw_entities`의 `verbose`).
- **읽기 전용 도구 추가** — `cad_status`(연결 상태 즉시 확인),
  `get_drawing_info`, `list_entities`, `preview_drawing`(도면을 PNG로
  렌더링해 반환 — 캐디안은 Export/화면캡처가 막혀 있어 COM으로 읽은
  엔티티를 파이썬이 직접 그림).
- **편집/블록 도구 추가** — `edit_entity`(이동/회전/축척/복사/대칭/
  평행복사/삭제), `array_entity`(원형·격자 배열), `create_block`/
  `insert_block`/`set_block_attributes`(재사용 심볼).

## 기능

### CAD 제어

- **여러 CAD 지원**: AutoCAD, GstarCAD(浩辰CAD), ZWCAD(中望CAD), **CADian**
- **작도**: 직선, 원, 호, 타원, 폴리라인, 사각형, 텍스트, 해치(패턴 채움),
  치수, 블록(심볼) 삽입, 여러 도형 한 번에 작도
- **편집**: 이동/회전/축척/복사/대칭/평행복사/삭제, 원형·격자 배열 복제
- **삭제**: 핸들 지정 삭제, 레이어 단위 삭제, 사각형 범위 삭제
- **레이어 관리**: 생성 및 전환
- **읽기/검증**: 도면 정보 조회, 엔티티 목록 조회, PNG 미리보기 렌더링,
  연결 상태 확인
- **저장**: 현재 도면을 DWG로 저장(저장 전 도면 범위 자동 갱신)

### 자연어 처리 (`process_command`)

- 자연어 명령을 CAD 작업 파라미터로 파싱
- 텍스트에서 색상 정보 추출
- 도형/동작 키워드 인식

## MCP 도구 전체 목록

| 분류 | 도구 |
|---|---|
| 작도 | `draw_line`, `draw_circle`, `draw_arc`, `draw_ellipse`, `draw_polyline`, `draw_rectangle`, `draw_text`, `draw_hatch`, `add_dimension`, `draw_entities`(배치) |
| 삭제 | `delete_by_layer`, `delete_by_window` (`edit_entity`의 `delete` 연산으로 핸들 하나만 삭제도 가능) |
| 편집/배열 | `edit_entity`(move/rotate/scale/copy/mirror/offset/delete), `array_entity`(polar/rectangular) |
| 블록 | `create_block`, `insert_block`, `set_block_attributes`, `list_blocks` |
| 읽기 | `cad_status`, `get_drawing_info`, `list_entities`, `preview_drawing` |
| 연결 관리 | `cad_reconnect` |
| 기타 | `save_drawing`, `process_command` |

## 설치

### 요구 사항

- **Windows** (COM 자동화 기반이라 Windows 전용)
- **Python 3.10 이상** 권장
- 지원 CAD 소프트웨어 중 하나가 설치되어 있어야 함: AutoCAD, GstarCAD,
  ZWCAD, **CADian**
- Python 패키지: `pywin32`, `mcp`, `pydantic` (`requirements.txt` 참고)

### 1. 저장소 클론

```bash
git clone https://github.com/scck12/CAD-MCP.git
cd CAD-MCP
```

### 2. (권장) 가상환경 생성 후 의존성 설치

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

가상환경을 안 쓰면 시스템 Python에 바로 설치해도 됩니다. 다만 뒤에서
MCP 클라이언트 설정에 `command`로 넣을 Python 경로가 이 환경의 경로와
일치해야 합니다.

### 3. `src/config.json` 설정

```json
{
    "server": {
        "name": "CAD MCP Server",
        "version": "1.0.0"
    },
    "cad": {
        "type": "CADian",
        "startup_wait_time": 20,
        "command_delay": 0.5
    },
    "output": {
        "directory": "./output",
        "default_filename": "cad_drawing.dwg"
    }
}
```

- `cad.type`: 사용할 CAD 종류. `AutoCAD`, `GCAD`(GstarCAD/浩辰CAD),
  `ZWCAD`(中望CAD/中望CAD), `CADian`(또는 `ICAD`, `IntelliCAD`) 중 하나.
- `cad.startup_wait_time`: CAD가 설치돼 있지만 아직 실행 중이 아닐 때,
  새로 띄우고 기다리는 시간(초). 컴퓨터 사양에 따라 조정.
- `cad.command_delay`: `SendCommand` 폴백 사용 시 명령 사이 대기 시간(초).
- `output.directory`: `save_drawing` 호출 시 상대 경로만 줬을 때 기준이
  되는 기본 출력 폴더.

CAD 프로그램이 이미 실행 중이면 그 인스턴스에 바로 연결하고, 실행 중이
아니면 새로 띄웁니다.

### 4. 서버 단독 실행 확인 (선택)

MCP 클라이언트 없이 서버 자체가 정상 기동하는지만 먼저 확인하고 싶다면:

```powershell
python src\server.py
```

정상이면 표준입출력으로 MCP 프로토콜 메시지를 기다리는 상태가 됩니다
(터미널에 아무 것도 안 뜨는 게 정상). `Ctrl+C`로 종료.

## LLM 데스크톱 앱 연결 (MCP 설정)

아래 예시들은 전부 **자신의 실제 경로**로 바꿔서 써야 합니다. 특히
가상환경을 썼다면 `command`는 `python`이 아니라
`C:\\...\\CAD-MCP\\.venv\\Scripts\\python.exe`처럼 그 가상환경의
python.exe 전체 경로를 쓰는 걸 권장합니다(데스크톱 앱은 가상환경을
자동으로 활성화해주지 않습니다).

### Claude Desktop

설정 파일 위치:

- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

파일이 없으면 새로 만들고, 있으면 `mcpServers`에 아래처럼 추가합니다.

```json
{
    "mcpServers": {
        "CAD": {
            "command": "C:\\Users\\사용자명\\CAD-MCP\\.venv\\Scripts\\python.exe",
            "args": [
                "C:\\Users\\사용자명\\CAD-MCP\\src\\server.py"
            ]
        }
    }
}
```

저장 후 Claude Desktop을 완전히 종료했다가 다시 실행하면 CAD 도구들이
잡힙니다. (도구를 새로 추가/변경한 뒤에도 마찬가지로 완전 재시작이
필요한 경우가 많습니다 — MCP 클라이언트가 연결 시점에 도구 목록을
캐시하기 때문입니다.)

### Claude Code (CLI / 이 문서를 보고 있는 도구)

프로젝트 루트에 `.mcp.json`을 두면 그 프로젝트를 열 때 자동으로
연결됩니다.

```json
{
    "mcpServers": {
        "CAD": {
            "command": "C:\\Users\\사용자명\\CAD-MCP\\.venv\\Scripts\\python.exe",
            "args": [
                "C:\\Users\\사용자명\\CAD-MCP\\src\\server.py"
            ]
        }
    }
}
```

또는 CLI로 바로 등록할 수도 있습니다.

```bash
claude mcp add CAD -- "C:\Users\사용자명\CAD-MCP\.venv\Scripts\python.exe" "C:\Users\사용자명\CAD-MCP\src\server.py"
```

### Cursor

설정 파일 위치:

- 전역: `~/.cursor/mcp.json`
- 프로젝트 한정: `<프로젝트 폴더>/.cursor/mcp.json`

```json
{
    "mcpServers": {
        "CAD": {
            "command": "C:\\Users\\사용자명\\CAD-MCP\\.venv\\Scripts\\python.exe",
            "args": [
                "C:\\Users\\사용자명\\CAD-MCP\\src\\server.py"
            ]
        }
    }
}
```

Cursor 설정 화면(Settings → MCP)에서 직접 추가할 수도 있습니다:

![Cursor 설정](imgs/cursor_config.png)

### Windsurf

설정 파일 위치: `~/.codeium/windsurf/mcp_config.json`

형식은 Claude Desktop과 동일합니다(`mcpServers` 아래 동일한 구조로
추가).

### MCP Inspector (설치 없이 바로 테스트)

클라이언트 설정 없이 도구 목록/호출을 바로 테스트해보고 싶을 때:

```bash
npx -y @modelcontextprotocol/inspector C:\Users\사용자명\CAD-MCP\.venv\Scripts\python.exe C:\Users\사용자명\CAD-MCP\src\server.py
```

## 사용 예시

CAD 프로그램(예: 캐디안)을 먼저 켜두고, 연결된 LLM 대화창에서 자연어로
요청하면 됩니다.

> "(0,0)에서 (100,0)까지 직선 그려줘"
> "중심 (50,50), 반지름 20인 원을 '외벽' 레이어에 빨간색으로 그려줘"
> "지금 도면 상태 보여줘" (→ `preview_drawing`)
> "'내벽' 레이어 지우고 이 좌표들로 다시 그려줘" (→ `delete_by_layer` + `draw_entities`)

## 문제 해결

- **도구 호출이 `RPC_E_WRONG_THREAD`나 응답 없음으로 실패한다** →
  `cad_status` 도구로 상태를 먼저 확인하고, 캐디안 창에서 ESC를 두어 번
  누른 뒤 `cad_reconnect`를 호출하세요. 자세한 원인은
  [history.md](history.md) 참고.
- **새로 추가한 도구가 안 보인다** → MCP 클라이언트가 도구 목록을 연결
  시점에 캐시하는 경우가 많습니다. 서버 프로세스 재시작만으로는 부족할
  수 있으니, 클라이언트(Claude Desktop 등) 자체를 완전히 재시작하세요.
- **여러 `server.py` 프로세스가 중복 실행 중인 것 같다** → 작업 관리자나
  PowerShell(`Get-CimInstance Win32_Process -Filter "Name='python.exe'"`)로
  확인하고 정리하세요.

## 프로젝트 구조

```
CAD-MCP/
├── imgs/                    # 이미지/데모 영상
├── output/                  # 기본 저장 폴더 (save_drawing)
├── requirements.txt         # Python 의존성
├── spec.md                  # 아키텍처/도구 스펙 (계속 갱신됨)
├── history.md                # 진단/수정 이력 (계속 갱신됨)
├── CLAUDE.md                 # (AI 세션용) 이 저장소 작업 지침
└── src/
    ├── server.py             # MCP 서버 진입점, 도구 정의/라우팅
    ├── cad_worker.py         # COM 전용 작업 스레드
    ├── cad_controller.py     # 실제 COM 호출(작도/편집/삭제/블록)
    ├── cad_reader.py         # 읽기 전용: 엔티티 조회, PNG 렌더링
    ├── nlp_processor.py      # 자연어 명령 파서 (process_command용)
    └── config.json           # 설정 파일
```

## 라이선스

MIT License. 원본 프로젝트: [daobataotie/CAD-MCP](https://github.com/daobataotie/CAD-MCP)
