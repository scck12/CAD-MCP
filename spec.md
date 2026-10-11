# CAD-MCP 사양

CADian(IntelliCAD 계열)을 COM 자동화로 제어하는 MCP 서버. 이 문서는 지금
시점의 아키텍처와 도구 목록을 있는 그대로 기술한다. 코드가 바뀌면 이 문서도
같은 커밋/작업 안에서 함께 갱신한다 — 오래된 스펙은 없느니만 못하다.

## 구성 파일

| 파일 | 역할 |
|---|---|
| `src/server.py` | MCP 진입점. 도구 레지스트리(`@tool`), 핸들러, `CADService`, 시간 제한/오류 포맷/로그 |
| `src/cad_worker.py` | COM 전용 작업 스레드(`CADWorker`), 작업 큐, 시간 제한, 막힘/자동 복구, 상태 보고 |
| `src/cad_controller.py` | 실제 COM 호출(`CADController`) — 연결/문서, 생성, 편집, 선택, 텍스트, 레이어, 정리, 도메인 도구, 블록 |
| `src/cad_reader.py` | 읽기 전용: 엔티티 → dict, 도면 정보, PNG 미리보기 |
| `src/colors.py` | 색상 인자 해석 (ACI 번호 / BYLAYER / 한·영·중 색 이름) |
| `src/config.json` | CAD 종류, 대기시간, 출력 경로. 선택: `drawing.default_layer`(기본 "0"), `drawing.door_layer`("문"), `drawing.window_layer`("창호") |
| `tests/e2e_isolated.py` | 회귀 테스트 (아래 "검증 방법") |

## 아키텍처

```
MCP 요청 ─ handle_call_tool (asyncio)
            └ asyncio.to_thread(도구 핸들러)        ← 이벤트 루프를 막지 않는다
                 └ CADService.run(fn, label, timeout, mutating)
                      └ CADWorker 큐 → 작업 스레드: controller.prepare() → fn(controller)
```

- **작업 스레드 하나가 COM을 소유한다.** CoInitialize, CADController 생성,
  모든 COM 호출이 그 스레드 안에서만 일어난다 (STA 규칙).
- **핸들러는 이벤트 루프 밖 스레드에서 돈다** (`asyncio.to_thread`). 예전에는
  핸들러가 이벤트 루프 위에서 COM 응답을 동기로 기다려, 한 호출이 막히는 동안
  `cad_status`조차 처리되지 않았다. mcp 1.29는 요청을 동시에 처리하므로 이제
  긴 배치 중에도 `cad_status`가 즉시 답한다 (E2E로 확인).
- **시간 제한 두 겹.** ① 작업 스레드: 도구별 30초(기본) / 120초(도면 스캔·대량)
  / 연결이 없으면 최소 90초. `draw_entities`는 30+0.5×개수초. ② 서버 전체 상한
  `HARD_TIMEOUT`=280초 — 미리보기 렌더링처럼 작업 스레드 밖 처리까지 포함.
- **실행 시간은 작업이 실제로 시작된 시점부터** 잰다. 큐에서 앞 작업을 기다린
  시간은 포함하지 않는다 (예전엔 포함돼서, 느린 작업 뒤에 줄 선 정상 작업이 시간
  초과로 처리되며 스레드 전체를 막힘으로 표시할 수 있었다).
- **시작 전에 포기한 작업은 취소 표시** → 나중에 스레드가 꺼내도 실행하지 않는다.
  "실패라고 했는데 나중에 도형이 생기는" 경로를 막는다.
- **실행 중 시간 초과 → 막힘(stuck).** 이후 요청은 즉시 `CADBusy`로 거절된다.
  막혔던 작업이 나중에라도 끝나면 막힘이 **자동으로 풀리고**, "늦게 완료됨"
  알림이 다음 도구 응답 끝에 `※`로 붙는다.
- **`cad_reconnect`** = `CADWorker.restart()`(새 스레드, 대기 중 요청은 취소
  통보) + 그 자리에서 실제 재연결 시도. 막힌 옛 스레드는 COM 호출이 풀리는 순간
  스스로 끝난다.
- **스레드 종료 순서**: 컨트롤러의 COM 참조를 먼저 놓고(`release()`) 그 다음
  `CoUninitialize`. 반대 순서면 내려간 아파트먼트에서 `Release`가 불려 프로세스가
  비정상 종료할 수 있다 (2026-10-12 이전 코드는 반대였다).
- **활성 문서 동기화**: 모든 작업 직전에 `controller.prepare()`가
  `Application.ActiveDocument`를 다시 읽는다. 사용자가 캐디안 탭을 바꾸면 작업
  대상도 바뀌고, 응답 끝에 "작업 대상 문서가 A → B로 바뀌었습니다" 알림이 붙는다.
- **실행 취소 단위**: 도형을 바꾸는 도구(`mutating=True`)는 `StartUndoMark`/
  `EndUndoMark`로 감싼다 → `undo` 1단계 = 도구 호출 1번.
- **`cad_status`는 COM을 건드리지 않는다.** 작업 스레드가 매 작업 후 기록한
  평문 상태(연결 여부, 문서 이름, 실행 중 작업과 경과 초, 막힘)만 읽는다. 연결돼
  있고 한가할 때만 10초짜리 가벼운 확인을 한 번 더 한다 — 이 확인은 큐에서 0.5초
  넘게 기다리지 않고(`queue_wait`), 시간을 넘겨도 스레드를 막힘으로 표시하지
  않는다(`mark_stuck=False`). 연결이 없을 때는 캐디안을 띄워 버릴 수 있어서
  확인하지 않는다.
- **새 엔티티는 현재 레이어(CLAYER)에 생긴다.** 대상 레이어가 CLAYER와 같으면
  Layer를 다시 쓰지 않는다(쓰기 1번 ~20ms). CLAYER는 작업 시작 때 한 번 읽어
  캐시한다.

### 절대 규칙: 작업 스레드 밖으로 살아있는 COM 객체를 내보내지 않는다

**2026-09-04 사고**: `draw_line` 등이 COM 엔티티 객체를 그대로 반환했고,
`server.py`가 다른 스레드에서 `str(result)`/`result.Handle`로 건드리는 순간(GC의
`Release()`만으로도) `RPC_E_WRONG_THREAD`가 났다. 도형은 정상 작도됐는데도
반환값 처리에서 실패했다.

**규칙**: `CADService.run()`에 넘기는 함수의 반환값은 **핸들 문자열 또는 plain
Python 값(dict/list/str/int/float/bool)만**. COM 객체가 필요한 내부 합성(해치
경계 등)은 컨트롤러 내부(`_build`, `_create`)에서 끝낸다. 헬퍼:
`CADController._handle_of(entity)`.

### 생성 규칙 (반환값과 도면이 어긋나지 않게)

- 모든 생성은 `CADController._create(space, spec)` 한 곳을 지난다: 만들기 →
  스타일 → 핸들 확인. 중간에 실패하면 이번에 만든 엔티티를 지우고 예외를 올린다.
- 레이어를 안 주면 `drawing.default_layer`("0"), 색을 안 주면 **BYLAYER(256)를
  명시**한다. 캐디안의 현재 레이어(CLAYER)·현재 색(CECOLOR)에 결과가 좌우되지
  않는다. (예전: 단일 도구는 7로 강제, draw_entities는 CECOLOR를 따라가서 빨강
  사고가 났다.)
- 레이어가 없으면 만들되 **현재 레이어는 바꾸지 않는다** (`_ensure_layer`). 예전
  `create_layer`는 도형 하나 그릴 때마다 ActiveLayer를 바꿔 테스트 레이어가 현재
  레이어로 남았다.
- 실패는 `None`으로 숨기지 않고 `CADError`(또는 COM 예외)로 올린다. 서버가
  `오류[CAD_ERROR|COM_ERROR|INVALID_ARGS|CAD_BUSY|TIMEOUT]: ...` 형식으로, COM
  예외는 HRESULT 이름(`RPC_E_WRONG_THREAD(0x8001010E)` 등)과 함께
  `isError=true`로 돌려준다.
- 연결 실패 시 사용자 문서를 닫거나 버리지 않는다. (예전 `start_cad`는 활성 문서
  읽기가 실패하면 열린 문서를 저장 없이 전부 닫았다.)

## CADian COM API의 알려진 함정

- **좌표 인자**: AutoCAD 계열은 `VT_ARRAY|VT_R8` 평탄화 배열, 캐디안은
  `Library.CreatePoint(x,y,z)` 같은 COM 객체. `_pt`/`_vec`/`_pts`가 갈아끼운다.
- **얼리 바인딩 필수**: 레이트 바인딩이면 인자 타입 정보가 없어 `AddCircle` 등이
  TypeError. `gencache.EnsureDispatch`로 승격한다.
- **속성 이름**: 색 `color`(소문자), 선두께 `Lineweight`, 선종류 `Linetype`.
  레이어 객체는 `color`, `LayerOn`, `Freeze`, `Lock`, `Lineweight`.
- **`GetBoundingBox(MinPoint, MaxPoint)`는 in/out 점 객체 두 개를 넘겨야 한다**
  (`VT_BYREF|VT_DISPATCH`). 인자 없이 부르면 `DISP_E_PARAMNOTFOUND`. 2026-09-04에
  "불안정하다"고 적었던 것은 이 시그니처를 몰라서였다 — 지금은 정상 동작하며
  텍스트 실제 폭까지 정확하다. `CADController._bbox`, `cad_reader.com_bbox`.
- **`Mirror`/`Copy` 반환값은 범용 `IIcadEntity2` 래퍼**라 `AttachmentPoint` 같은
  타입별 속성이 없다. `doc.HandleToObject(handle)`로 다시 꺼내면 정확한 타입으로
  온다 (`_retype`). `HandleToObject`·`ModelSpace` 순회 결과는 원래 정확한 타입이다.
- **미러 시 텍스트**: 캐디안 Mirror는 MText 부착점을 좌↔우로 뒤집고(1→3, 4→6)
  Text 위치도 맞게 옮긴다 (MIRRTEXT=0, 제자리/복사 모두 확인). 2026-10 작업에서
  "MText 미러 시 라벨이 글자 폭만큼 밀린다"고 본 것은 CAD 쪽 문제가 아니라,
  미리보기·목록이 MText를 항상 삽입점에서 왼쪽 정렬로 가정해 그린 탓이었다. 지금은
  bbox 기준으로 그리고 `list_entities`가 justify/attachment/bbox를 보고한다.
- **Text 정렬**: `Alignment`(0 left, 1 center, 2 right, 4 middle, 6~14 top/middle/
  bottom × left/center/right) 설정 후 `TextAlignmentPoint`를 지정한다. 0이면
  `InsertionPoint`를 쓴다.
- **MText 기본 높이가 0.2**다. 만든 뒤 `Height`를 꼭 지정한다.
- **레이어 `Delete()`는 사용 중이면 `E_FAIL`**로 거부된다 (안전장치로 동작).
  `PurgeAll()`도 사용 중 레이어는 남긴다.
- **`SetVariable("CECOLOR", "BYLAYER"|"1")`**은 문자열로 넘긴다.
- **실행 취소**: `StartUndoMark`/`EndUndoMark` + `SendCommand("_.UNDO\n1\n")`로
  묶음 단위 되돌리기가 된다.
- **`AddDimRotated(p1, p2, 치수선위치, 회전rad)`** 지원 — 수평(0)/수직(π/2) 치수.
- **`Regen(1)`만으로는 `EXTMIN`/`EXTMAX`가 갱신되지 않는다.** `ZoomExtents`가 필요
  (`zoom_extents()`: `ActiveViewport.ZoomExtents` → `Application.ZoomExtents` →
  `SendCommand`). `get_drawing_info`, `save_drawing`, `zoom_extents` 도구가 먼저
  호출한다.
- **작도 속도**: 도형 하나에 100~160ms (AddLine 자체 ~64ms, Layer 지정 ~20ms,
  CreatePoint 2개 ~15ms — 2026-10-12 실측). 화면 갱신을 끄는 COM 속성이 없다.
  `draw_entities` 시간 제한이 개수에 비례하는 이유다.
- **`zoom_to`로 좁은 영역을 확대하면 텍스트 폰트가 폭주**할 수 있다 (scale × 실제
  text_height → 수천 pt → Pillow `DecompressionBombError`). `_korean_font()`가
  200pt로 자른다 — 폰트 크기 계산을 새로 넣을 땐 이 함수를 거친다.

## MCP 도구 목록

모든 작도 도구: `layer`(기본 "0"), `color`(기본 BYLAYER, ACI 번호·BYLAYER·BYBLOCK·
red/빨강 등), `lineweight`(1/100mm, -1=BYLAYER). 반환은 핸들.

### 작도
- `draw_line`, `draw_circle`, `draw_arc`(도 단위, 반시계 start→end: 270→0은 90° 호),
  `draw_ellipse`, `draw_polyline`, `draw_rectangle`(꼭짓점 4개 + 닫힘)
- `draw_text(justify)` — 정렬 지정 시 position이 정렬점 (방 이름은 middle_center)
- `draw_mtext(width, attachment)` — width=0 자동 폭, 줄바꿈 `\P`
- `draw_hatch` — 경계 폴리라인 + 해치 핸들 둘 다 반환
- `add_dimension(text_position | offset+orientation)` — aligned/horizontal/vertical
- `add_dimension_chain(points, orientation, offset, overall, overall_offset)` —
  한 면의 연속 치수 + 전체 치수를 한 번에 (예전: 도면당 20~24회 호출)
- `draw_entities(entities, atomic, default_layer, default_color, verbose)` — 배치.
  spec 종류: line, circle, arc, ellipse, rectangle, polyline, text, mtext, dimension,
  hatch, **wall**{start,end,thickness,justify}, **door**{hinge,width,angle,swing},
  **window**{start,end,thickness,lines}. 기본 응답 = `handles: a | b,c | ✗ | ...`
  (항목 순서, door처럼 여러 개면 쉼표) + 실패 사유. `atomic=true`면 하나라도
  실패 시 이번에 만든 것 전부 삭제.

### 편집
- `edit_entity(handle, operation, ...)` — move, rotate, scale, copy, mirror, offset,
  delete, **set_properties**{layer, color, lineweight, linetype}
- `edit_entities(operation, handles | selector, ...)` — 같은 편집을 여러 개에.
  결과는 핸들별 성공/실패. selector = {window, mode(crossing|inside), layers,
  types, handles} (AND). 영역 반전·레이어 일괄 이동 등
- `edit_text(handle, text, height, position, justify, attachment, width, rotation)`
- `array_entity` (polar/rectangular)
- `insert_opening(handle, center, width, kind=none|door|window, swing_toward,
  hinge_at)` — 사각형 벽(꼭짓점 4 닫힌 폴리라인)을 두 조각으로 다시 그리고 문/창
  기호를 넣는다
- `break_entity(handle, point1, point2, remove=short|long)` — 선/폴리라인 끊기.
  닫힌 폴리라인은 짧은(또는 긴) 쪽 경로를 지우고 열린 폴리라인 하나로 남긴다.
  bulge(호 구간) 폴리라인은 거부

### 삭제
- `delete_by_layer(layer)`, `delete_by_window(x1,y1,x2,y2, mode, layers, types)` —
  판정은 GetBoundingBox 실제 외곽(텍스트 포함). 그 밖은 `edit_entities` + delete.
- 자료가 바뀌어 다시 그릴 때: 지우고(`delete_by_*`/`edit_entities`) →
  `draw_entities`.

### 읽기
- `cad_status` — state: ready | busy{job, seconds} | stuck{stuck_job} |
  idle_not_connected | not_started. 장애 시 재시작 전에 먼저 호출해 기록한다.
- `get_drawing_info` (EXTMIN/EXTMAX 갱신 후), `list_blocks`
- `list_entities(layer, entity_type, window, mode, limit=100, include_geometry,
  precision=1, fields)` — 기본은 좌표 배열 없는 요약 + 소수 1자리. 텍스트는
  justify/attachment/width/bbox 포함. 전체 확인은 `preview_drawing`이 싸다.
- `get_bbox(handles)` — 실제 외곽 [minx,miny,maxx,maxy]
- `preview_drawing(width=800, height=600, show_labels, zoom_to)` — 텍스트는 bbox
  기준으로 그린다.

### 레이어 / 현재값 / 정리
- `manage_layers(action=list|create|delete|rename|set, ...)` — list는 색·켜짐·잠금·
  모델 공간 엔티티 수·in_use(치수/블록 내부 포함 사용 여부). delete는 빈 레이어만.
- `set_current(layer, color)` — CLAYER / CECOLOR. 작업·테스트 후 원복용.
- `purge(kind=all|layers|blocks)` — all은 PURGE ALL, layers는 빈 레이어(0/
  Defpoints/현재 제외), blocks는 삽입 안 된 정의.

### 문서 / 뷰 / 명령
- `document(action=list|new|open|activate|save, name, path)`
- `save_drawing(file_path)` (상대 경로면 config output 폴더 기준)
- `zoom_extents` — 전체 보기 + REGEN, 갱신된 범위 반환
- `send_command(command, wait=5)` — 명령줄에 그대로. 끝나지 않으면(추가 입력
  대기) ESC로 취소하고 알려준다.
- `undo(steps=1)` — 도구 호출 1번 = 1단계

### 블록 / 연결
- `create_block`(entities는 draw_entities와 같은 spec), `insert_block`,
  `set_block_attributes`
- `cad_reconnect`

### 제거된 도구
- `process_command`(자연어 → 명령) — 원본 프로젝트의 중국어 키워드 파서로, 실제로
  "zoom extents", 범위 삭제 같은 요청에 `None`만 반환했다. 구조화된 도구가 모두
  있어 2026-10-12에 목록과 코드(`nlp_processor.py`)를 지웠다. 명령줄 접근이
  필요하면 `send_command`.

## 진단 장치

- `src/cad_mcp.log` (2MB × 4 회전): 도구마다 `도구 시작: 이름 {인자}` /
  `도구 종료: 이름 성공|실패 (초)`. 서버가 죽었다면 마지막 "시작"만 있고 "종료"가
  없는 줄이 범인이다.
- `src/cad_mcp_crash.log`: `faulthandler`가 네이티브 크래시(COM 내부 접근 위반
  등, 파이썬 로그에는 안 남는 것)의 모든 스레드 스택을 덤프한다.
- 응답 끝 `※` 알림: 문서 전환, 늦게 끝난 작업, 막힘 자동 해제.

## 검증 방법

CAD 쪽 변경은 `py_compile`만으로는 부족하다 — COM 동작은 런타임에만 드러난다.

1. **회귀 테스트** (사용자 문서를 건드리지 않음):
   `.venv\Scripts\python.exe tests\e2e_isolated.py`
   - 작업 스레드 단위 테스트(시간 초과/막힘/자동 복구/큐 대기 분리/재시작) +
     `CAD_MCP_ISOLATED=1`로 실제 서버를 띄워 **새 캐디안 인스턴스(DispatchEx)의 새
     도면**에서 도구 전체를 MCP 클라이언트로 호출한다. 끝나면 이번에 뜬 Icad.exe만
     종료한다. 2026-10-12 기준 73개 항목.
   - 새 도구/동작을 추가하면 여기 항목을 추가한다.
2. **사용자 캐디안에 대한 확인은 읽기 전용 도구로만** (`cad_status`,
   `list_entities`, `manage_layers list`, `document list`). 작업 도면에 테스트
   엔티티를 남기지 않는다.
3. 새 도구가 클라이언트 도구 목록에 안 잡히면: 클라이언트가 연결 시점에 목록을
   캐시한다. 서버 프로세스 재시작만으로는 부족할 수 있으니 MCP 재연결/앱 재시작을
   안내한다.
4. `server.py` 프로세스가 여러 개 중복 실행돼 있을 수 있다. `Get-CimInstance
   Win32_Process -Filter "Name='python.exe'"`로 확인한다.
5. COM API를 새로 쓸 때는 makepy 래퍼
   (`.venv\Lib\site-packages\win32com\gen_py\77E49E57-...\IIcad*.py`)에서
   시그니처를 먼저 확인한다 — in/out 인자, VT_DISPATCH 점 객체 여부가 여기 다
   나온다.
