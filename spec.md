# CAD-MCP 사양

CADian(IntelliCAD 계열)을 COM 자동화로 제어하는 MCP 서버. 이 문서는 지금
시점의 아키텍처와 도구 목록을 있는 그대로 기술한다. 코드가 바뀌면 이 문서도
같은 커밋/작업 안에서 함께 갱신한다 — 오래된 스펙은 없느니만 못하다.

## 구성 파일

| 파일 | 역할 |
|---|---|
| `src/server.py` | MCP 서버 진입점. 도구 정의(`handle_list_tools`)와 라우팅(`handle_call_tool`), `CADService`(비즈니스 로직 얇은 래퍼) |
| `src/cad_worker.py` | COM 전용 작업 스레드(`CADWorker`)와 `ControllerProxy` |
| `src/cad_controller.py` | 실제 COM 호출(`CADController`) — 도형 생성/편집/삭제/블록 |
| `src/cad_reader.py` | 읽기 전용: 엔티티 → dict 변환, 도면 정보, PNG 미리보기 렌더링 |
| `src/nlp_processor.py` | `process_command`(자연어 명령)용 파서 |
| `src/config.json` | CAD 종류(`cad.type`), 시작 대기시간, 출력 경로 |

## 아키텍처: COM 전용 작업 스레드

**문제**: CAD의 COM 호출은 동기 블로킹이다. 캐디안이 명령 대기 상태이거나
대화상자를 띄우고 있으면 호출이 무한정 반환되지 않는다. 또한 COM은
STA(Single-Threaded Apartment) 규칙을 따르므로, 한 스레드에서 만든 COM
객체를 다른 스레드에서 건드리면 `RPC_E_WRONG_THREAD`(0x8001010E)가 난다.

**해결**: `CADWorker`가 스레드 하나(`cad-com`)를 소유하고, 그 스레드 안에서만
`pythoncom.CoInitialize()`와 `CADController` 생성, 모든 COM 호출이 일어난다.
호출자(MCP 비동기 이벤트 루프)는 `worker.run(func)`으로 작업을 큐에 넣고
타임아웃을 걸어 기다린다. 시간 초과 시 `CADBusy`를 올리고 작업 스레드는
막힌 채로 버려둔다 — 서버 자체는 계속 응답 가능하다.

`ControllerProxy`는 `CADController`처럼 보이는 대리자다.
`CADService.controller.draw_line(...)` 같은 호출은 실제로는
`worker.run(lambda c: c.draw_line(...))`로 큐를 거쳐 작업 스레드에서
실행된다. `CADService`의 나머지 코드는 이 사실을 몰라도 된다.

`cad_reconnect`(`CADWorker.restart()`)는 막힌 스레드를 재사용하지 않는다.
진짜로 멈춘(블로킹된) 스레드는 풀어줄 방법이 없으므로, `_stuck` 플래그를
세우고 그 스레드는 자연 종료되게 버려둔 뒤 새 스레드 + 새
`CADController`로 교체한다. 새 스레드는 `GetActiveObject`로 이미 떠 있는
캐디안 프로세스에 다시 붙는다.

### 절대 규칙: 작업 스레드 밖으로 살아있는 COM 객체를 내보내지 않는다

**2026-09-04에 실제로 겪은 버그**: `draw_line` 등이 작업 스레드에서 만든
COM 엔티티 객체(`win32com` Dispatch)를 그대로 반환했다. 이 객체가 큐를
통해 asyncio 스레드로 넘어온 뒤 `server.py`가 `str(result)`나
`result.Handle`로 건드리는 순간(심지어 GC가 `Release()`를 호출하는 것만
으로도) `RPC_E_WRONG_THREAD`가 났다. 스레드 모델 자체는 멀쩡했고, 실제로
도형은 정상 작도됐는데도 반환값 처리 단계에서 매번 실패했다.

**규칙**: `CADController`의 공개 메서드 중 `ControllerProxy`를 거쳐 작업
스레드 밖으로 나가는 반환값은 **핸들 문자열 또는 plain Python 값(bool,
int, list/dict of primitives)만 허용**한다. 살아있는 COM 객체가 필요한
내부 합성(예: `draw_hatch`가 경계 폴리라인 COM 객체를 `AppendOuterLoop`에
넘겨야 하는 경우)은 `_make_polyline`처럼 언더스코어로 시작하는 비공개
메서드로 분리하고, 그 안에서만 엔티티를 다룬다. 새 draw_* / edit_* 메서드를
추가할 때 이 규칙을 반드시 지킨다. 헬퍼: `CADController._handle_of(entity)`.

## CADian COM API의 알려진 함정

- **좌표 인자**: AutoCAD 계열은 `VT_ARRAY|VT_R8` 평탄화 배열을 받지만,
  캐디안의 타입 라이브러리는 좌표를 `Library.CreatePoint(x,y,z)` 같은 COM
  객체로 선언한다. `CADController._pt`/`_vec`/`_pts`가 `cad_type`에 따라
  갈아끼운다.
- **얼리 바인딩 필수**: 캐디안은 레이트 바인딩 상태에서 인자 타입 정보가
  없어 `AddCircle` 등이 TypeError로 실패한다. `win32com.client.gencache.
  EnsureDispatch`로 승격해야 한다.
- **속성 이름이 CAD별로 다름**: 색상은 `Color`(AutoCAD)/`color`(캐디안,
  소문자), 선두께는 `LineWeight`/`Lineweight`. `_set_entity_prop`이 후보를
  순서대로 시도하고 성공한 이름을 캐시한다.
- **`GetBoundingBox()` / `SelectionSets.Select()`가 불안정**: 필수 매개변수
  오류(`DISP_E_PARAMNOTFOUND`)로 실패하는 경우가 많다(2026-09-04 probe로
  확인). 범위 판정이 필요하면(`delete_by_window`) 이미 검증된
  `cad_reader.read_entity()`의 좌표 추출 로직으로 파이썬에서 직접 바운딩
  박스를 계산한다.
- **`Regen(1)`만으로는 `EXTMIN`/`EXTMAX`가 갱신되지 않는다.** 도면 범위가
  실제로 재계산되려면 `ZoomExtents`가 필요하다(`ActiveViewport.
  ZoomExtents()` → 실패 시 `Application.ZoomExtents()` → 실패 시
  `SendCommand("_ZOOM\n_E\n")` 순으로 시도, `CADController.zoom_extents()`).
  `get_drawing_info`와 `save_drawing`은 값을 읽거나 저장하기 직전에
  `zoom_extents()`를 자동으로 호출한다.
- **`zoom_to`로 좁은 영역만 확대하면 텍스트 폰트 크기가 폭주할 수 있다.**
  `render_png`는 `scale = 이미지 픽셀 / (zoom_to 범위)`로 계산하는데,
  범위가 작으면 scale이 커지고, 그 상태에서 도면 실제 `text_height`(치수
  문자 등, 수백 단위인 경우가 흔함)를 곱하면 폰트 크기가 수천pt로
  치솟는다. 그 크기로 Pillow가 글리프를 래스터화하다
  `DecompressionBombError`로 죽는다(2026-09-04, 25×30 단위짜리 좁은
  `zoom_to`에서 실제 재현: "Image size 1,297,835,816 pixels exceeds
  limit"). `cad_reader._korean_font()`가 `_MAX_FONT_SIZE=200`으로 clamp해
  막아 놓았다 — 폰트 크기 계산을 새로 추가할 땐 이 clamp를 거치게 한다.

## MCP 도구 목록

### 작도
`draw_line`, `draw_circle`, `draw_arc`, `draw_ellipse`, `draw_polyline`,
`draw_rectangle`, `draw_text`, `draw_hatch`, `add_dimension` — 모두 성공 시
핸들 문자열, 실패 시 `None`/에러 텍스트를 반환한다.

`draw_entities(entities, verbose=False)` — 배치 작도. 항목마다 왕복하지
않고 도형 목록을 한 번에 넘긴다. 각 항목은 `create_block`의 `entities`와
같은 spec 형식(`type` + 좌표 키 + `layer`/`color`/`lineweight`). 실패한
항목은 결과 목록에서 `null`로 남고 나머지는 계속 진행한다. 내부적으로
`CADController._draw_into`를 재사용한다. 응답은 기본적으로 성공/실패
개수와 실패 인덱스만 반환한다(항목이 많으면 핸들을 다 나열하는 게
낭비라서) — 이후에 특정 핸들을 편집/삭제할 계획이면 `verbose=true`로
호출해 항목별 핸들을 받는다.

### 삭제
`edit_entity(handle, operation="delete")` — 핸들 하나 삭제(기존 편집 도구의
일부).

`delete_by_layer(layer)` — 레이어의 모든 엔티티 삭제. 삭제 개수 반환.

`delete_by_window(x1, y1, x2, y2, mode)` — 사각형 범위에 걸리는 엔티티
삭제. `mode="crossing"`(기본, 일부 겹침) 또는 `"inside"`(완전 포함). 삭제
개수 반환. `GetBoundingBox`를 안 쓰고 `cad_reader` 좌표 추출 기반으로 직접
판정한다(위 "알려진 함정" 참고).

자료가 바뀌어 다시 그려야 할 때 표준 흐름: `delete_by_layer` 또는
`delete_by_window`로 이전 산출물을 지운 뒤 `draw_entities`로 다시 그린다.

### 읽기
`cad_status`, `get_drawing_info`, `list_blocks`.

`list_entities(layer, entity_type, limit=100, include_geometry=False)` —
기본으로는 `points`/`loops` 좌표 배열을 뺀 요약만 반환한다
(`cad_reader.compact_entity`). 큰 도면에서 좌표까지 다 포함하면 응답
자체가 토큰 한도를 넘겨 실패할 수 있다(217개 엔티티에서 56,570자로 실제
초과 확인, 2026-09-04). 좌표가 필요하면 `include_geometry=true`로 명시
호출하거나 `layer`/`entity_type`으로 좁혀서 쓴다.

`preview_drawing(width=800, height=600, show_labels, zoom_to)` — 도면
전체를 이미지 하나로 렌더링. 비용이 엔티티 수와 거의 무관(고정)하므로,
배치 작도 후 "의도대로 그려졌나" 확인은 `list_entities`로 다 덤프하는
대신 이 도구를 우선 쓴다. `list_entities`는 특정 소수 엔티티의 정확한
좌표가 필요할 때 쓴다.

### 편집 / 배열
`edit_entity`(move/rotate/scale/copy/mirror/offset/delete),
`array_entity`(polar/rectangular).

### 블록
`create_block`, `insert_block`, `set_block_attributes`.

### 연결 관리
`cad_reconnect` — 막힌 COM 연결을 버리고 재연결(위 "절대 규칙" 참고).
`CADWorker.restart()`로 작업 스레드만 교체하는 게 아니라, 그 직후
`start_cad()`를 직접 호출해 실제로 캐디안에 재연결까지 시도한 뒤 상태를
반환한다 — 그냥 `restart()` + `health_check()`만 하면 새로 만든 컨트롤러가
아직 아무 것도 안 한 상태라 항상 `document: "not_connected"`만 나온다
(2026-09-06에 실제로 6회 연속 재현 확인). `cad_status`는 의도적으로 이
얕은 확인만 한다 — 멈춰 있는 상황에서도 안전하게 즉시 반환해야 하니
`start_cad()` 같은 새 COM 호출을 걸지 않는다. 이 차이를 헷갈리지 말 것.

### 기타
`save_drawing`(저장 전 `zoom_extents` 자동 호출), `process_command`(자연어
명령, 일부 명령 타입만 지원).

## 검증 방법

CAD 쪽 변경은 `py_compile`만으로는 충분하지 않다 — COM 호출의 실제 동작은
런타임에만 드러난다. 캐디안이 이미 떠 있는 상태에서:

1. 해당 MCP 도구(`mcp__CAD__*`)를 직접 호출해 결과를 확인한다.
2. 새로 추가한 도구가 아직 MCP 클라이언트의 도구 목록에 안 잡히면(클라이언트가
   연결 시점에 도구 목록을 캐시하므로, 서버 프로세스만 재시작해서는 반영되지
   않을 수 있다), `sys.path`에 `src`를 넣고 `CADController`를 직접 import해
   별도 프로세스로 검증한다 — 같은 캐디안 인스턴스에 별도 COM 연결로 붙는
   것은 안전하다(표준 프로세스 간 COM).
3. `list_entities`로 실제로 그려졌는지/지워졌는지 좌표까지 확인하고,
   테스트로 만든 엔티티는 반드시 정리한다(전용 임시 레이어 사용 권장).
4. 서버 프로세스가 여러 개 중복 실행되고 있을 수 있다(재시작 시마다 새
   프로세스가 뜨고 예전 것이 안 죽는 경우). `Get-CimInstance Win32_Process
   -Filter "Name='python.exe'"`로 `server.py` 커맨드라인을 가진 프로세스를
   확인하고 정리한다.
