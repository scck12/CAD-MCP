# CAD-MCP 작업 이력

시간 순으로 추가한다(최신이 아래). 항목마다 무엇을, 왜, 어떻게 확인했는지
남긴다 — "고쳤다"만 쓰지 않는다. `spec.md`가 바뀌면 그 사실도 같이 적는다.

## 2026-09-04 — COM 전용 작업 스레드 도입 (세션 시작 시점에 이미 반영됨)

`cad_worker.py`(단일 STA 스레드 + 명령 큐), `cad_controller.py`의 캐디안
호환 계층(좌표를 COM 객체로 넘기는 `_pt`/`_vec`/`_pts`, 속성명 캐시,
`AddHatch`/`AddPolyline` 인자 탐색 등)이 이 세션이 시작된 시점에 이미
작업 트리에 반영되어 있었다(`git status`상 미커밋 상태). 캐디안 자동화가
STA COM이라 다른 스레드에서 호출하면 `RPC_E_WRONG_THREAD`가 나는 문제에
대한 대응. 이 시점 기준 상세 배경은 대화 로그 참고, 코드 상태는 `spec.md`
참고.

## 2026-09-04 — 작도 도구 전체가 RPC_E_WRONG_THREAD로 실패하는 문제 진단/수정

**증상**: `draw_line` 등 쓰기 계열 도구를 호출하면 매번
`(-2147417842, '응용 프로그램이 다른 스레드를 위해 배열된 인터페이스를
호출했습니다')` 에러가 남. 읽기 계열(`cad_status`, `get_drawing_info`)은
정상. `cad_reconnect` 후에도 동일.

**진단**: 라이브 서버에 `draw_line`을 직접 호출해 재현한 뒤,
`list_entities`로 확인해보니 **도형은 실제로 정상 작도되어 있었고**
(`AcDbLine`, handle `522`), `cad_status`도 `thread_alive: true, stuck:
false`로 멀쩡했다. 즉 COM 호출 자체와 작업 스레드는 문제가 없었다. 원인은
`CADController.draw_line`(그리고 `draw_circle`/`draw_arc`/`draw_ellipse`/
`draw_polyline`/`draw_text`/`draw_hatch`/`add_dimension`)이 작업 스레드가
만든 **살아있는 COM 엔티티 객체를 그대로 반환**하고, `server.py`가 그걸
`str(result)`나 `result.Handle`로 다른 스레드(asyncio)에서 건드리는
순간이었다.

**수정**: `cad_controller.py`에 `_handle_of(entity)` 헬퍼를 추가하고, 위
메서드들이 엔티티 대신 핸들 문자열(또는 실패 시 `None`)을 반환하도록
변경. `draw_hatch`는 내부적으로 폴리라인 COM 객체가 그대로 필요(
`AppendOuterLoop`에 넘김)해서 그 부분만 새 비공개 메서드 `_make_polyline`
으로 분리하고, 공개 `draw_polyline`은 핸들만 반환하는 얇은 래퍼로 바꿈.
`server.py`의 `process_command` 8곳에서 `result.Handle` → `result`로 수정
(이미 핸들 문자열이므로).

**검증**: 중복 실행 중이던 `server.py` 프로세스 4개를 정리 → 앱이 자동
재기동 → `draw_line`/`draw_circle`/`draw_polyline`/`draw_hatch`/
`add_dimension`을 전용 임시 레이어(`MCP_TEST_TMP`)에서 실제 호출해 모두
에러 없이 핸들을 반환하는 것을 확인, `list_entities`로 좌표까지 검증한
뒤 삭제로 정리(엔티티 수 85개로 원복).

**스펙 반영**: `spec.md`의 "절대 규칙" 섹션에 이 규칙을 못박음 — 작업
스레드 밖으로 살아있는 COM 객체를 절대 내보내지 않는다.

## 2026-09-04 — draw_arc 오류 원인 파악

**증상 보고**: "draw_arc 기능이 안되고있음".

**진단**: 위 항목과 같은 근본 원인(엔티티 객체가 스레드 경계를 넘어감)이었다.
`draw_arc`도 예외 없이 같은 패턴으로 `arc` 객체를 그대로 반환하고 있었다.
위 수정에서 `draw_arc`도 함께 고쳐졌고, 서버 재시작 이후 실제 호출로
재검증(center/radius/start_angle/end_angle 모두 정확) → 별도 수정 불필요,
이미 해결됨을 확인.

## 2026-09-04 — 삭제 도구(delete_by_layer, delete_by_window) 및 배치 작도(draw_entities) 추가

**요청 배경**: 자료가 바뀌면 도면을 다시 그려야 하는데, 지우는 방법이
MCP에 없어서 매번 캐디안에서 손으로 정리해야 했음.

**구현**:
- `CADController.delete_by_layer(layer)` — `doc.ModelSpace`를 스냅샷
  리스트로 받아(라이브 컬렉션을 순회하며 삭제하면 흔들릴 수 있어서)
  `entity.Layer == layer`인 것만 `Delete()`. 삭제 개수 반환.
- `CADController.delete_by_window(x1,y1,x2,y2,mode)` — 처음에는
  `GetBoundingBox()`와 `SelectionSets.Select()`를 시도했으나, 별도
  probe 스크립트로 확인한 결과 캐디안에서 둘 다 `DISP_E_PARAMNOTFOUND`
  (필수 매개변수 없음)로 실패함을 확인. 대신 이미 검증된
  `cad_reader.read_entity()`의 좌표 추출 로직을 재사용해 파이썬에서 직접
  바운딩 박스를 계산하고 겹침을 판정(`_entity_bbox` 헬퍼, `cad_reader.
  bounds_of`의 점 수집 로직과 동일). `mode="crossing"`(일부 겹침, 기본)
  / `"inside"`(완전 포함).
- `CADController.draw_entities(entities)` — 기존 `create_block`이 쓰던
  `_draw_into(space, spec)`를 `ModelSpace`에 바로 적용하는 배치 버전.
  항목별로 실패해도 나머지는 계속 진행하고, 실패한 자리는 `None`으로
  남겨 인덱스를 유지.
- `server.py`: `CADService.draw_entities/delete_by_layer/delete_by_window`
  래퍼와 대응하는 MCP 도구 3개(`draw_entities`, `delete_by_layer`,
  `delete_by_window`) 등록.

**검증**: 서버 프로세스 재시작만으로는 새 도구가 이 세션의 MCP 도구
목록에 안 잡혔다(클라이언트가 연결 시점에 도구 목록을 캐시하는 것으로
보임 — 앱을 완전히 재연결해야 반영될 가능성이 높음). 대신 `sys.path`에
`src`를 넣고 `CADController`를 별도 프로세스에서 직접 import해(같은
캐디안 인스턴스에 별도 COM 연결, 안전함) 실제로 검증:
- `draw_entities`로 4개 성공 + 의도적으로 잘못된 타입 1개가 `None`으로
  실패하는 것 확인.
- `delete_by_window(crossing)`로 범위 안 엔티티 5개는 지워지고 범위 밖
  엔티티는 살아남는 것 확인.
- `delete_by_layer`로 남은 엔티티 정리, 0개로 원복 확인.
- 테스트에 사용한 임시 레이어/엔티티는 스크립트 안에서 전부 정리됨.

**후속 조치 필요**: 이 앱에서 MCP 서버를 재연결(또는 앱 재시작)해야 새
도구 3개가 실제로 호출 가능해진다. `spec.md`의 "검증 방법" 섹션에 이
제약을 남겨둠.

## 2026-09-04 — EXTMIN/EXTMAX가 항상 (0,0,0)으로 나오는 문제 수정

**증상**: `get_drawing_info`가 `extmin`/`extmax`를 계속 `[0,0,0]`으로
보고함. 사용자는 캐디안에서 수동으로 REGEN 후 저장하면 정리된다고 보고.

**진단**: `CADController.refresh_view()`가 매 작도 후 `doc.Regen(1)`을
이미 호출하고 있었는데도 `EXTMIN`/`EXTMAX` 시스템 변수는 갱신되지
않았다. `Regen`은 화면 재묘화일 뿐 도면 범위 재계산과는 별개이고,
실제로 범위를 다시 계산하는 것은 `ZoomExtents` 계열 호출이다(기존
코드에 `CADController.zoom_extents()`가 이미 있었지만 어디서도 자동
호출되지 않고 있었음).

**수정**: `CADService.get_drawing_info`가 `cad_reader.drawing_info`를
읽기 직전에 `controller.zoom_extents()`를 호출. `CADController.
save_drawing`도 `doc.SaveAs()` 직전에 `self.zoom_extents()`를 호출하도록
변경(저장 파일에도 정확한 범위가 들어가도록).

**검증**: 별도 프로세스 probe로 `zoom_extents()` 호출 후
`doc.GetVariable("EXTMIN"/"EXTMAX")`가 실제 도면 형상에 맞는 값(예:
`EXTMIN (-16131.9, -11.0)` 근처)을 반환하는 것을 확인. 이후 실제
`mcp__CAD__get_drawing_info` 호출로도 `extmin`/`extmax`가 0이 아닌
실제 좌표로 나오는 것을 재확인.

## 2026-09-04 — spec.md / history.md 체계 도입

이 두 파일과 `CLAUDE.md`(향후 세션에 두 파일을 계속 갱신하라는 지시)를
추가. 목적: MCP 소스가 세션이 바뀔 때마다 매번 새로 진단하지 않도록,
아키텍처 판단 근거와 CADian COM API의 함정을 계속 누적한다.

## 2026-09-04 — 토큰 소비 최적화: list_entities 요약 기본화, preview_drawing 축소, draw_entities 응답 요약

**계기**: 사용자가 토큰 소비 관점에서 검토를 요청. 실제로
`list_entities`를 필터 없이 호출해보니 도면(217개 엔티티)에서
**56,570자, 응답이 토큰 한도를 초과해 통째로 실패**하는 것을 직접
재현해서 확인함. 원인은 폴리라인/해치의 `points`/`loops` 좌표 배열을
엔티티마다 전부 덤프하고 있었기 때문(엔티티 1개가 최대 300~400자).

**수정**:
- `cad_reader.compact_entity(e)` 추가 — `points`/`loops`를 빼고
  `point_count`만 남긴 요약 dict.
- `CADService.list_entities`에 `include_geometry`(기본 `False`) 추가.
  기본은 요약만 반환하고, 좌표가 필요하면 `include_geometry=true`로
  명시 요청. 기본 `limit`도 500 → 100으로 낮춤(요약 모드라도 500개면
  여전히 클 수 있어서 안전망으로).
- `preview_drawing` 기본 해상도 1200x900 → 800x600. 이미지 하나의
  비용은 엔티티 수와 거의 무관(고정)하므로, "배치 작도가 의도대로
  됐나" 같은 전체 확인은 `list_entities`로 다 덤프하는 대신
  `preview_drawing`을 쓰도록 두 도구의 description에 서로 안내를 추가.
- `draw_entities` 응답에 `verbose`(기본 `False`) 추가. 기본은 "N개 중
  M개 성공 / 실패 인덱스"만 반환하고, 항목별 핸들 전체 나열은
  `verbose=true`일 때만.

**검증**: 서버 재시작 후 동일한 도면(217개 엔티티)에서 `list_entities`를
다시 필터 없이 호출 → 이번엔 정상 응답(100개로 잘리고 각 항목은
`point_count`만 있는 요약). `draw_entities`를 의도적 실패 항목 포함해서
호출 → "3개 중 2개 성공 / 실패한 인덱스: [2]"만 반환되는 것 확인.

**부수적으로 발견한 별개 버그**: `preview_drawing`에 좁은 `zoom_to`(가로
세로 25×30 단위)를 주고 확인하던 중 `Image size (1,297,835,816 pixels)
exceeds limit... decompression bomb` 에러로 크래시하는 것을 발견. 원인은
`cad_reader.render_png`가 `scale = 이미지 픽셀 / zoom_to 범위`로 계산하는데,
`zoom_to` 범위가 작으면 `scale`이 커지고, 그 상태에서 도면의 실제
`text_height`(치수 문자 등, 수백 단위인 경우가 흔함)를 곱하면 폰트 크기가
수천pt로 치솟아 Pillow가 글리프 래스터화 중 죽는 것이었다. `cad_reader.
_korean_font()`에 `_MAX_FONT_SIZE=200` clamp를 추가해 수정. 같은 `zoom_to`로
재호출해 정상적으로 이미지가 반환되는 것을 확인(빈 결과였지만 에러 없이
반환 — 해당 좌표가 실제 도면 범위 밖이라 예상된 결과).

**스펙 반영**: `spec.md`의 "MCP 도구 목록"에 새 파라미터들과 "언제 어떤
도구를 쓸지" 가이드를 추가, "알려진 함정"에 폰트 크기 폭주 항목 추가.

## 2026-09-04 — 소스 코드 주석/로그/도구 설명을 중국어·영어에서 한국어로 번역

**계기**: 원 저장소가 중국어 화자가 작성한 프로젝트라 `server.py`,
`cad_controller.py`, `nlp_processor.py`, `__init__.py`에 중국어 주석/
docstring/로그 메시지/MCP 도구 설명이 많이 남아 있었음(총 500줄 이상).
사용자가 전부 한국어로 번역해 달라고 요청.

**작업**: 파일별로 백그라운드 에이전트 3개를 병렬로 돌려 번역(server.py,
cad_controller.py, nlp_processor.py 각각 1개). `__init__.py`는 짧아서 직접
번역.

**중요한 예외 — nlp_processor.py는 전부 번역하면 안 된다**:
`NLPProcessor`(자연어 명령 파서, `process_command`가 사용)는 사용자가
입력한 **중국어 단어 자체를 매칭**해서 도형 종류/색상/동작을 인식한다.
아래는 주석이 아니라 매칭 로직이므로 번역하지 않고 그대로 남겨둠 —
번역하면 한국어 지원이 추가되는 게 아니라 중국어 명령 인식이 조용히
깨지기만 한다:
- `self.color_rgb_map` / `self.shape_keywords` / `self.action_keywords`
  딕셔너리의 키(예: `"红色"`, `"直线"`, `"画"` 등)
- `radius_pattern`, `width_pattern`, `height_pattern`, `text_pattern`,
  `rotation_pattern`, `scale_pattern`, `path_pattern`,
  `start_angle_pattern`, `end_angle_pattern`, `color_pattern`,
  `pattern_patterns` 등 중국어/영어 키워드를 매칭하는 정규식 문자열
- `"图层" in command`, `"标注" in command`, `"闭合" in command` 같은
  부분 문자열 검사

주석/docstring/로그 메시지(`self.logger.info(...)` 등)와, 매칭 키가
아니라 결과로 반환되는 사람이 읽는 문자열(`"error"`, `"note"`,
`"message"` 필드 값)은 정상적으로 한국어로 번역함.

`cad_controller.py`의 `APP_IDS` 딕셔너리에 있는 浩辰CAD(GstarCAD),
中望CAD(ZWCAD) 표시명도 실제 중국어 제품명이라 번역하지 않고 남김(주변
로그 문장은 한국어로 번역됨).

**검증**:
- 세 파일 모두 `py_compile` 통과.
- `nlp_processor.py`는 번역 후 실제 중국어 명령 파싱 스모크 테스트
  ("画一个圆心在(10,20)半径为30的圆" → `draw_circle`, 반지름 30 정상 인식,
  "红色" → 색상 인덱스 1 정상 인식)로 매칭 로직이 안 깨졌음을 직접 확인.
- 전체 파일에 대해 중국어 문자(U+4E00-U+9FFF) 잔존 여부를 정규식으로
  스캔 → 위에 나열한 의도적 예외들만 남고 나머지는 0건 확인. 에이전트가
  `color_rgb_map` 등 딕셔너리 바로 위의 짧은 라벨 주석(`# 基本颜色` 등)
  5개를 지나치게 조심해서 실수로 안 옮긴 게 있었는데, 이건 데이터가
  아니라 순수 주석이라 직접 마저 번역함.
- 서버 재시작 후 `get_drawing_info` 등 실제 도구 호출 및
  `cad_mcp.log` 확인으로, 새로 남는 로그가 전부 한국어로 나오는 것을
  실제 운영 환경에서 확인.
- 주의: 이 세션의 MCP 클라이언트가 도구 목록(설명 텍스트 포함)을 연결
  시점에 캐시하고 있어서, 서버 프로세스만 재시작해서는 이 세션에 보이는
  도구 설명이 갱신되지 않았다(소스 코드 자체는 확인 결과 정상 번역됨).
  MCP 서버 재연결/앱 재시작이 필요한 이미 알려진 제약(`spec.md` "검증
  방법" 참고)과 동일한 현상.

## 2026-09-06 — "scale이 서버를 죽였다 / draw_arc가 타임아웃됐다" 제보 조사

**제보**: 실제 도면 작업 세션에서 `edit_entity`의 scale이 서버를 멈추게
했고 `draw_arc`가 타임아웃됐다고 보고됨. delete(24회)·draw_polyline·
draw_rectangle(20회)은 한 번도 실패하지 않았다는 대비가 단서로 제시됨.

**1차 조사 (코드/재현)**: 라이브 도면에서 line/polyline/text/dimension/
hatch 5종 엔티티에 rotate/scale 직접 호출 → 전부 즉시 정상. 별도의 격리된
새 CADian 인스턴스(`DispatchEx`로 강제 새 프로세스, 기존 작업 문서는
안 건드림)에서 `draw_arc` 9개 엣지 케이스(역순 각도, 360도 초과, 반지름
0/음수/극단값 등)와 `scale_entity` 5개 엣지 케이스(factor 0/음수/극단값),
블록 인스턴스 rotate/scale까지 순차 재현 → 전부 0.3초 이내 정상(음수
반지름만 CADian이 즉시 예외로 거부 — 이미 안전하게 처리되는 정상 동작).
`edit_entity`의 7개 연산 전부 동일한 `ControllerProxy` →
`CADWorker.run()` 45초 타임아웃 경로를 거치므로 연산별 코드 차이도 없음.

**로그 분석**: `cad_mcp.log` 전체 이력에 CADBusy(45초 타임아웃) 에러가
단 한 번도 없었음. 반면 이 세션(Claude Code) 작업자가 직접 재시작한
것보다 훨씬 많은 서버 재시작이 로그에 있었음 — 다른 원인(다른 세션의
재연결 시도, 앱 자체의 동작 등)으로 서버 프로세스가 재시작되는 순간과
그 세션의 호출 타이밍이 겹쳐 "서버가 죽었다/타임아웃됐다"로 보였을
가능성이 1차 결론.

**사용자 반박 및 2차 가설**: 장애 당시 `cad_reconnect`가 2회 실행됐고
그중 1회가 `document: "not_connected"`를 반환한 것이 단서로 제시됨.
"`cad_reconnect`가 새 스레드를 띄우며 이전 COM 프록시를 정리하지 않아
이후 호출이 죽은 프록시를 잡고 무한 대기한다"는 가설(RPC_E_WRONG_THREAD와
같은 뿌리)이 제기됨.

**2차 검증**: 라이브 서버에서 `cad_reconnect`를 3회 연속 호출한 직후
scale을 시도(1회), 별도로 3회 연속 재연결 직후 draw_arc를 시도(1회) —
총 6회 반복. 매번 재연결 포함 2초 이내 정상 처리, 행 없음(로그 타임스탬프
실측: 00:51:29,747 재연결 시작 → 00:51:31,904 scale 완료). **스레드 간
COM 객체 공유 자체가 코드 구조상 불가능**함도 확인 — `CADWorker._loop`가
매번 새 스레드 안에서 자기만의 `CADController` 지역 변수를 만들고,
`ControllerProxy`는 항상 "현재" 워커 스레드에만 작업을 넘기므로 이전
스레드의 컨트롤러가 이후 호출에 섞여 들어갈 경로가 없음. 가설 재현 실패.

**그런데 실제로 다른 버그를 하나 발견함**: `CADWorker.health_check()`의
`probe`는 `controller.is_running()`만 확인하고 `start_cad()`는 호출하지
않는다. `restart()` 직후의 컨트롤러는 항상 비어 있는 새 상태이므로,
`cad_reconnect`(`restart()` + `health_check()`)는 **실제 재연결
성공/실패와 무관하게 항상 `document: "not_connected"`를 반환**한다 —
사용자가 관찰한 그 현상이 정확히 이것이었다. 재현: 라이브 서버에서
`cad_reconnect`를 6회 호출, 매번 `not_connected` 확인. 이건 행(hang)이
아니라 "재연결했다고 보고하지만 실제로는 재연결 시도조차 안 하는" 상태
보고 버그.

**수정**: `CADService.reconnect()`(`server.py`)가 `restart()` 직후
`worker.run(lambda c: c.start_cad(), timeout=CONNECT_TIMEOUT)`을 직접
호출해 실제 재연결을 시도한 뒤 `health_check()`를 반환하도록 변경.
`cad_status`(단순 상태 확인)는 의도적으로 그대로 둠 — 멈춘 상황에서도
안전하게 즉시 반환해야 하니 새 COM 호출을 걸면 안 됨.

**검증**: 서버 재시작 후 `cad_reconnect`를 3회 연속 호출 → 매번
`document: "step2.dwg"`로 정확히 보고되는 것을 확인(이전엔 매번
`not_connected`였음). 실제 작업 도면(엔티티 337개로 계속 진행 중)에는
영향 없음을 `get_drawing_info`로 확인.

**최종 결론**: scale/draw_arc 자체에는 버그가 없고(코드 경로 동일, 광범위
edge case 재현 실패), 사용자가 단서로 제시한 stale-proxy 가설도 재현 실패.
다만 조사 과정에서 `cad_reconnect`의 "not_connected 오보고" 버그를 찾아
수정함 — 원래 제보(서버가 죽었다/타임아웃)에 대한 직접 증거는 아니지만,
향후 재연결 후 상태 판단에 혼란을 줄 수 있었던 실제 결함.

## 2026-10-12 — 도면 작업 세션 개선 요청서(19개 항목) 반영

분양 평면도 3종 작도 세션이 정리한 `cad-mcp-개선요청.md`를 받아 처리. 서버를
대부분 다시 짰다(`server.py` 도구 레지스트리, `cad_worker.py`, `cad_controller.py`
전면 재작성, `colors.py` 신설, `nlp_processor.py`/`verify_cadian.py` 삭제).

**먼저 COM API를 확인했다** (추측으로 짜면 또 깨지므로): makepy 래퍼에서 시그니처를
읽고, 격리 캐디안 인스턴스(`DispatchEx`)에서 probe로 실제 동작을 확인. 사용자의
작업 도면(PID 571048)은 건드리지 않음. 확인된 것 → spec.md "알려진 함정"에 반영.

**요청별 진단/처리**

- 2-1A 유령 핸들 582: 로그 확인 결과 **2026-09-04 11:18:57 `핸들 582 삭제`**
  (edit_entity delete 성공)가 있고, 21:23의 두 번 실패는 이미 지운 핸들을 다시
  지우려던 것. 생성 결과가 틀어진 게 아니라 앞서 그 세션이 벽을 지운 것이었다.
  그래도 생성 경로를 `_create()` 하나로 모아 "생성→스타일→핸들 확인, 실패 시 만든
  것 삭제"로 고정했고, 이제 도구 시작/종료와 인자가 로그에 남아 같은 의문을 바로
  추적할 수 있다.
- 2-1B 에러인데 생성됨(4F8·506·514): 2026-09-04의 RPC_E_WRONG_THREAD 수정 이전
  현상. 추가로, 시간 초과 후 큐에 남아 있던 작업이 나중에 실행될 수 있던 경로를
  막았다(시작 전 포기한 작업은 취소 표시). 실행 중이던 작업이 늦게 끝나면 "늦게
  완료됨" 알림을 다음 응답에 붙인다.
- 2-2 무한 대기/서버 사망: 재현은 여전히 안 됐다(이전 조사 참고). 대신 구조적
  원인이 될 수 있는 것을 전부 고쳤다. ① 핸들러가 이벤트 루프 위에서 COM을 동기로
  기다려, 한 호출이 막히면 서버 전체가 응답 불가였다 → `asyncio.to_thread`.
  ② 타임아웃이 큐 대기까지 포함해 느린 작업 뒤의 정상 작업이 막힘을 유발할 수
  있었다 → 시작 시점 기준. ③ 스레드 종료 시 COM 참조를 쥔 채 `CoUninitialize`를
  불러, 재연결 후 옛 프록시 Release가 내려간 아파트먼트에서 불릴 수 있었다 → 참조
  먼저 해제(사용자 가설 "재연결이 이전 프록시를 정리하지 않는다"와 가장 가까운 실제
  결함). ④ 미리보기 렌더링 등 작업 스레드 밖 처리에 상한이 없었다 →
  `HARD_TIMEOUT` 280초. ⑤ 다음 장애 때 원인을 잡도록 faulthandler 크래시 덤프
  (`cad_mcp_crash.log`)와 도구 시작/종료 로그 추가. 기본 타임아웃 45→30초.
- 2-3 활성 문서 캐싱: 모든 작업 직전 `ActiveDocument`를 다시 읽어 바뀌면 대상
  교체 + 알림. `document` 도구(list/new/open/activate/save) 추가.
- 2-4 색 기본값: 원인 둘 — draw_entities는 색을 안 주면 CECOLOR를 따라갔고, 단일
  도구는 색 파서가 None을 7로 바꿨다. 이제 지정 안 하면 BYLAYER를 명시. 또
  `create_layer`가 도형마다 ActiveLayer를 바꾸던 부작용(MCP_TEST_TMP가 현재 레이어로
  남은 원인) 제거. E2E에서 CECOLOR=1, CLAYER=TEST_CURRENT로 오염시킨 뒤 기본값이
  BYLAYER/"0"이고 현재 레이어가 유지되는지 확인.
- 3-1/3-2/3-3: `set_properties` 연산, `edit_entities`(handles 또는 selector{window,
  mode, layers, types}) 추가.
- 3-4 텍스트: `draw_text.justify`, `draw_mtext`, `edit_text` 추가. **"MText 미러 시
  라벨이 글자 폭만큼 밀린다"는 CAD 문제가 아니었다** — probe로 캐디안 Mirror가 MText
  부착점(1→3, 4→6)과 Text 위치를 정확히 뒤집는 것을 확인(제자리·복사, 폭 0/2000).
  원인은 `list_entities`가 부착점을 안 알려주고 `preview_drawing`이 MText를 항상
  삽입점에서 왼쪽 정렬로 그린 것. 미러 후 삽입점이 오른쪽 끝으로 가니 미리보기에서
  글자가 오른쪽으로 밀려 보였다. 이제 Text/MText에 justify/attachment/width/bbox를
  보고하고, 미리보기는 bbox 기준으로 그린다.
- 3-5 bbox: `GetBoundingBox`는 in/out 점 객체 두 개를 넘겨야 동작한다(2026-09-04
  probe가 인자 없이 불러서 "불안정"으로 오판). `get_bbox` 도구, 선택 판정도 이걸로.
- 3-6: `manage_layers`, `set_current`, `purge`. 사용 중 레이어는 CADian이
  `E_FAIL`로 삭제를 거부하는 것도 확인.
- 3-7 process_command: 원본 중국어 키워드 파서라 실제 요청에 None만 반환 → 도구와
  `nlp_processor.py` 삭제, `send_command`(끝나지 않는 명령은 ESC로 취소) 추가.
- 3-8: `zoom_extents` 도구.
- 4 효율: `add_dimension_chain`, draw_entities에 dimension/wall/door/window 타입,
  기본 응답에 핸들 목록, `atomic` 롤백, list_entities precision/window/fields,
  `undo`(도구 호출 단위 실행 취소 마크).
- 5: `insert_opening`(벽 2조각 + 문/창 기호), `break_entity`(닫힌 폴리라인 짧은
  쪽 제거), draw_entities의 wall/door/window.
- 6-2 에러 메시지: `오류[종류]: ...` + HRESULT 이름, `isError=true`.
- 6-3 cad_status 불일치: COM 없이 상태 보고, state/note로 지연 연결을 명시.
- 6-4 draw_arc 규약을 도구 설명에 명시(반시계, 270→0은 90° 호).
- 그 외 발견: 연결 실패 시 열린 문서를 저장 없이 모두 닫던 코드 제거.

**성능 실측**: 도형 하나 100~160ms(AddLine ~64ms, Layer 지정 ~20ms) — CADian COM
내부 비용. draw_entities 시간 제한을 30+0.5×개수초로.

**검증**: `tests/e2e_isolated.py` 신설 — 작업 스레드 단위 8개 + 격리 캐디안에서
실제 서버 E2E 56개, **64/64 통과**(2회 실행). 사용자 캐디안에는 새 서버를 읽기
전용으로만 붙여 확인(cad_status, list_entities, manage_layers list, document list —
step2.dwg, 129개 엔티티, Text bbox/justify 정상).

**남은 것**: 사용자 MCP 클라이언트가 새 도구를 보려면 MCP 재연결/앱 재시작이
필요하다. 장애 재발 시 `cad_mcp_crash.log`와 로그의 마지막 "도구 시작"을 먼저 본다.

## 2026-10-12 — 위 작업 코드 리뷰 지적 10건 수정

- `cad_status` 확인 요청이 다른 작업 뒤에 최대 120초 줄 서고, 10초 넘으면 스레드를
  막힘으로 만들던 문제 → `worker.run(queue_wait=0.5, mark_stuck=False)`. 상태
  확인이 그 자체로 서버를 막지 않는다.
- `round_floats`가 inf/NaN에서 `int()` 예외 → None으로 (빈 도면 EXTMIN 등).
- `edit_text`에서 MText에 justify(center 등)를 주면 거부 → top_center 등 부착점으로
  매핑.
- `list_entities`가 limit과 무관하게 도면 전체(최대 1500개)를 상세히 읽던 것 →
  limit개만 읽고 전체 개수는 `ModelSpace.Count`/선택 결과 수.
- `GetBoundingBox`용 점 객체를 엔티티마다 2개씩 만들던 것 → 스캔당 한 쌍 재사용.
- 새 엔티티가 이미 현재 레이어에 생기는데도 Layer를 매번 쓰던 것(~20ms) → 같으면
  생략(CLAYER는 작업마다 한 번 읽어 캐시). 250개 배치 39.2초 → 26.7초.
- `manage_layers list`가 모델 공간을 두 번 순회 → 한 번에.
- `break_entity` 길이 0 선에서 ZeroDivisionError → CADError.
- 내부 KeyError가 "필수 인자 누락"으로 보이던 것 → `MISSING_VALUE`, save_drawing은
  config output이 없어도 기본 폴더 사용.
- `insert_opening`이 사다리꼴 벽을 말없이 직사각형으로 바꾸던 것 → 직사각형이
  아니면 거부.

검증: `tests/e2e_isolated.py`에 각 항목 회귀 테스트 추가, **73/73 통과**.
