r"""E2E 회귀 테스트: 실제 MCP 서버를 띄워 격리된 캐디안 인스턴스에서 도구를 검증한다.

    .venv\Scripts\python.exe tests\e2e_isolated.py

- CAD_MCP_ISOLATED=1로 서버를 띄우면 서버는 사용자가 쓰는 캐디안이 아니라
  새 캐디안 프로세스(DispatchEx)와 새 도면에서만 작업한다. 사용자의 작업 도면은
  건드리지 않는다.
- 테스트가 끝나면 이번에 새로 뜬 Icad.exe만 종료한다.
- 각 항목은 실패해도 계속 진행하고 마지막에 요약한다. 하나라도 실패하면 종료 코드 1.
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "src", "server.py")
PY = sys.executable

RESULTS = []


def icad_pids():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Icad.exe", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True, encoding="cp949", errors="ignore").stdout
    return {int(m.group(1)) for m in re.finditer(r'"Icad\.exe","(\d+)"', out, re.I)}


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail and not cond else ""), flush=True)


class Client:
    def __init__(self, session):
        self.s = session

    async def call(self, name, args=None):
        res = await self.s.call_tool(name, args or {})
        texts = [c.text for c in res.content if getattr(c, "type", "") == "text"]
        return res.isError, "\n".join(texts), res

    async def ok(self, name, args=None):
        err, text, _ = await self.call(name, args)
        if err:
            raise AssertionError(f"{name} 실패: {text}")
        return text


def handles_of(text):
    line = next(l for l in text.splitlines() if l.startswith("handles:"))
    return [None if p.strip() == "✗" else p.strip().split(",")
            for p in line[len("handles:"):].split("|")]


def entities_of(text):
    return [json.loads(l) for l in text.splitlines()[1:] if l.startswith("{")]


async def run_tests():
    params = StdioServerParameters(command=PY, args=[SERVER],
                                   env={**os.environ, "CAD_MCP_ISOLATED": "1",
                                        "PYTHONIOENCODING": "utf-8"})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            c = Client(session)

            tools = {t.name for t in (await session.list_tools()).tools}
            for name in ("draw_entities", "edit_entities", "edit_text", "draw_mtext", "get_bbox",
                         "manage_layers", "set_current", "purge", "document", "send_command",
                         "zoom_extents", "undo", "add_dimension_chain", "insert_opening",
                         "break_entity"):
                check(f"도구 등록: {name}", name in tools)
            check("process_command 제거", "process_command" not in tools)

            st = json.loads(await c.ok("cad_status"))
            check("시작 전 상태는 not_started", st["state"] == "not_started", st)

            # --- 현재값 오염 상태를 만들어 두고 기본값이 그것을 무시하는지 본다 (요청 2-4)
            r = await c.ok("set_current", {"layer": "TEST_CURRENT", "color": 1})
            check("set_current 반영", "TEST_CURRENT" in r and "1" in r, r)

            specs = [
                {"type": "line", "start": [0, 0], "end": [1000, 0]},
                {"type": "rectangle", "corner1": [0, 100], "corner2": [500, 300]},
                {"type": "text", "position": [2000, 500], "text": "거실 28.00", "height": 300,
                 "justify": "middle_center"},
                {"type": "mtext", "position": [5000, 1000], "text": "PAX 옷장", "height": 250,
                 "layer": "가구"},
                {"type": "dimension", "start": [0, -500], "end": [3000, -500], "offset": -800,
                 "orientation": "horizontal", "textheight": 250},
                {"type": "wall", "start": [0, 3000], "end": [4000, 3000], "thickness": 200},
                {"type": "door", "hinge": [6000, 0], "width": 900, "angle": 0, "swing": "left",
                 "layer": "문"},
                {"type": "window", "start": [7000, 0], "end": [8500, 0], "thickness": 200},
                {"type": "hatch", "points": [[9000, 0], [9500, 0], [9500, 500]]},
                {"type": "nonsense"},
            ]
            t0 = time.monotonic()
            text = await c.ok("draw_entities", {"entities": specs})
            hs = handles_of(text)
            check("draw_entities 9/10 성공", "10개 중 9개 성공" in text, text)
            check("draw_entities 기본 응답에 핸들 목록", len(hs) == 10 and hs[9] is None, text)
            check("door는 핸들 2개", hs[6] and len(hs[6]) == 2, hs[6])
            check("window는 평행선 3개", hs[7] and len(hs[7]) == 3, hs[7])
            check("실패 사유 표시", "nonsense" in text, text)
            print(f"   draw_entities {time.monotonic() - t0:.1f}s", flush=True)

            line_h, rect_h, text_h, mtext_h = hs[0][0], hs[1][0], hs[2][0], hs[3][0]
            wall_h = hs[5][0]

            ents = entities_of(await c.ok("list_entities", {"fields": ["Handle", "Layer", "color"]}))
            by_h = {e["Handle"]: e for e in ents}
            check("색 미지정 → BYLAYER(256), CECOLOR=1 무시", by_h[line_h]["color"] == 256, by_h[line_h])
            check("레이어 미지정 → '0', CLAYER 무시", by_h[line_h]["Layer"] == "0", by_h[line_h])
            check("지정 레이어 적용", by_h[mtext_h]["Layer"] == "가구", by_h[mtext_h])

            info = json.loads(await c.ok("get_drawing_info"))
            check("작도해도 현재 레이어 유지", info.get("current_layer") == "TEST_CURRENT", info)
            check("EXTMIN/EXTMAX 갱신", info.get("extmax", [0, 0])[0] > 1000, info)

            # --- 텍스트 정렬 / bbox
            bb = json.loads((await c.ok("get_bbox", {"handles": [text_h]})).splitlines()[0])["bbox"]
            cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
            check("middle_center 텍스트가 position 중심", abs(cx - 2000) < 60 and abs(cy - 500) < 80, bb)
            ents = entities_of(await c.ok("list_entities", {"entity_type": "text"}))
            check("list_entities에 justify/bbox", ents and ents[0].get("justify") == "middle_center"
                  and ents[0].get("bbox"), ents)

            # --- 속성 일괄 변경 (요청 3-1)
            r = await c.ok("edit_entities", {"operation": "set_properties",
                                             "handles": [line_h, rect_h, "FFFFF"], "layer": "가구",
                                             "color": 3})
            check("set_properties 2개 성공 + 없는 핸들은 개별 실패", "성공 2개" in r and "FFFFF" in r, r)
            ents = entities_of(await c.ok("list_entities", {"layer": "가구", "fields": ["Handle", "color"]}))
            check("레이어 이동 확인", {line_h, rect_h} <= {e["Handle"] for e in ents}, ents)

            # --- 영역 미러: MText 부착점이 같이 뒤집혀 실제 위치가 맞는지 (요청 3-4)
            before = json.loads((await c.ok("get_bbox", {"handles": [mtext_h]})).splitlines()[0])["bbox"]
            r = await c.ok("edit_entities", {"operation": "mirror",
                                             "selector": {"window": [4900, 600, 7500, 1100], "mode": "inside"},
                                             "point1": [7000, 0], "point2": [7000, 1], "make_copy": False})
            after = json.loads((await c.ok("get_bbox", {"handles": [mtext_h]})).splitlines()[0])["bbox"]
            expect = [14000 - before[2], 14000 - before[0]]
            check("MText 영역 미러 위치", "대상 1개" in r and abs(after[0] - expect[0]) < 30
                  and abs(after[2] - expect[1]) < 30, (before, after, r))
            ents = entities_of(await c.ok("list_entities", {"entity_type": "mtext"}))
            check("미러 후 attachment 보고", ents and ents[0].get("attachment") == "top_right", ents)

            # --- 텍스트 수정 (요청 3-4)
            r = await c.ok("edit_text", {"handle": text_h, "height": 200, "text": "드레스룸"})
            check("edit_text 반영", "드레스룸" in r and "200" in r, r)
            err, t, _ = await c.call("edit_text", {"handle": mtext_h, "justify": "center"})
            check("MText에 justify=center → top_center 부착점", not err and "top_center" in t, t)

            # --- 목록은 limit만큼만 읽고 전체 개수는 정확히
            info_n = json.loads(await c.ok("get_drawing_info"))["entity_count"]
            t = await c.ok("list_entities", {"limit": 2})
            check("list_entities limit 2 + 전체 개수", f"엔티티 {info_n}개" in t and "앞 2개만" in t
                  and len(entities_of(t)) == 2, t[:200])

            # --- 개구부 (요청 5)
            r = await c.ok("insert_opening", {"handle": wall_h, "center": [2000, 3000], "width": 900,
                                              "kind": "door", "swing_toward": [2000, 5000]})
            m = re.search(r"조각 ([0-9A-F, ]+) / 문 ([0-9A-F, ]+)", r)
            check("insert_opening: 조각 2 + 문 2", m and len(m.group(1).split(",")) == 2
                  and len(m.group(2).split(",")) == 2, r)
            err, t, _ = await c.call("edit_entity", {"handle": wall_h, "operation": "delete"})
            check("원래 벽은 삭제됨", err and "CAD_ERROR" in t, t)

            quad_h = (await c.ok("draw_polyline", {"points": [[0, 9000], [1000, 9000], [1100, 9200], [100, 9200]],
                                                   "closed": True})).split(": ")[1]
            err, t, _ = await c.call("insert_opening", {"handle": quad_h, "center": [500, 9100], "width": 200})
            check("직사각형 아닌 벽은 insert_opening 거부", err and "직사각형이 아닙니다" in t, t)
            err, t, _ = await c.call("draw_line", {"start_point": [5, 9500], "end_point": [5, 9500]})
            if err:
                check("길이 0 선(캐디안이 생성 자체를 거부)", "CAD_ERROR" in t or "COM_ERROR" in t, t)
            else:
                zero_h = t.split(": ")[1]
                err, t, _ = await c.call("break_entity", {"handle": zero_h, "point1": [5, 9500],
                                                          "point2": [5, 9500]})
                check("길이 0 선 끊기는 CAD_ERROR", err and "CAD_ERROR" in t and "길이 0" in t, t)

            # --- 끊기 (요청 5)
            loop_h = (await c.ok("draw_rectangle", {"corner1": [0, 6000], "corner2": [3000, 7000]})).split(": ")[1]
            r = await c.ok("break_entity", {"handle": loop_h, "point1": [1000, 6000], "point2": [1900, 6000]})
            check("break_entity 닫힌 사각형 → 열린 조각 1", "→" in r and len(r.split("→")[1].split(",")) == 1, r)

            # --- 연속 치수 (요청 4)
            r = await c.ok("add_dimension_chain", {"points": [[0, 8000], [3000, 8000], [7200, 8000], [9200, 8000]],
                                                   "orientation": "horizontal", "offset": 800, "textheight": 250})
            check("연속 치수 3 + 전체 1", "연속 치수 3개" in r and "전체 치수" in r, r)

            # --- 레이어 관리 / purge (요청 3-6)
            await c.ok("manage_layers", {"action": "create", "name": "빈레이어", "color": 5})
            err, t, _ = await c.call("manage_layers", {"action": "delete", "name": "가구"})
            check("사용 중 레이어 삭제 거부", err and "엔티티" in t, t)
            r = await c.ok("manage_layers", {"action": "rename", "name": "빈레이어", "new_name": "빈레이어2"})
            check("레이어 이름 변경", "빈레이어2" in r, r)
            r = await c.ok("purge", {"kind": "layers"})
            check("purge layers가 빈 레이어 제거", "빈레이어2" in r, r)
            layers = [json.loads(l) for l in (await c.ok("manage_layers", {"action": "list"})).splitlines()]
            check("purge가 사용 중 레이어는 남김", any(x["name"] == "가구" for x in layers), layers)
            gagu = next((x for x in layers if x["name"] == "가구"), {})
            check("레이어 목록: 모델 공간 개수 + in_use", gagu.get("entities", 0) >= 3 and gagu.get("in_use"), gagu)
            r = await c.ok("set_current", {"layer": "0", "color": "BYLAYER"})
            check("현재값 원복", "BYLAYER" in r, r)

            # --- undo (요청 4)
            await c.ok("draw_line", {"start_point": [0, 20000], "end_point": [10, 20000]})
            r = await c.ok("undo", {"steps": 1})
            check("undo 1단계가 마지막 작도를 되돌림", "-1" in r, r)

            # --- atomic 롤백 (요청 2-1)
            text = await c.ok("draw_entities", {"atomic": True, "entities": [
                {"type": "line", "start": [0, 30000], "end": [1, 30000]},
                {"type": "line", "start": [0, 30010], "end": [1, 30010]},
                {"type": "circle", "center": [0, 30020]}]})
            check("atomic 실패 시 롤백", "atomic" in text and "2개를 지웠습니다" in text, text)
            ents = entities_of(await c.ok("list_entities", {"window": [-1, 29990, 5, 30030]}))
            check("롤백 후 남은 엔티티 없음", len(ents) == 0, ents)

            # --- 오류 표시
            err, t, _ = await c.call("draw_line", {"start_point": [0, 0], "end_point": [1, 1],
                                                   "color": "보랏빛무지개"})
            check("잘못된 색 이름은 INVALID_ARGS", err and "INVALID_ARGS" in t, t)

            # --- 문서 (요청 6-1)
            docs = json.loads(await c.ok("document", {"action": "list"}))
            first = next(d["name"] for d in docs["documents"] if d["active"])
            r = json.loads(await c.ok("document", {"action": "new"}))
            check("document new", r.get("created") and r["created"] != first, r)
            ents = entities_of(await c.ok("list_entities", {}))
            check("새 문서는 비어 있음", len(ents) == 0, len(ents))
            r = json.loads(await c.ok("document", {"action": "activate", "name": first}))
            check("document activate", r.get("activated") == first, r)

            # --- zoom / send_command
            r = await c.ok("zoom_extents")
            check("zoom_extents 범위 반환", "extmax" in r, r)
            r = json.loads(await c.ok("send_command", {"command": "_.REGEN"}))
            check("send_command 완료", r.get("finished") in (True, None), r)

            # --- 동시성: 긴 배치 중에도 cad_status는 즉시 답한다 (요청 2-2)
            many = [{"type": "line", "start": [i * 10, 40000], "end": [i * 10, 40100]} for i in range(250)]
            batch = asyncio.create_task(c.call("draw_entities", {"entities": many}))
            await asyncio.sleep(1.5)
            t0 = time.monotonic()
            st = json.loads(await c.ok("cad_status"))
            dt = time.monotonic() - t0
            check("배치 진행 중 cad_status 즉시 응답", dt < 3.0 and st["state"] == "busy", (dt, st))
            err, text, _ = await batch
            check("긴 배치 완료", not err and "250개 중 250개 성공" in text, text[:200])
            st = json.loads(await c.ok("cad_status"))
            check("배치 중 상태 확인이 막힘을 남기지 않음", not st["stuck"], st)

            st = json.loads(await c.ok("cad_status"))
            check("종료 전 상태 ready", st["state"] == "ready", st)


def worker_unit_tests():
    """COM 없이 작업 스레드의 시간 제한/막힘/자동 복구를 검증"""
    sys.path.insert(0, os.path.join(ROOT, "src"))
    import cad_worker
    w = cad_worker.CADWorker()
    t0 = time.monotonic()
    try:
        w.run(lambda c: time.sleep(4), timeout=1, label="느린 작업", connect=False)
        check("시간 초과 시 CADBusy", False)
    except cad_worker.CADBusy:
        check("시간 초과 시 CADBusy", 0.9 < time.monotonic() - t0 < 2.5)
    check("시간 초과 후 stuck", w.status()["state"] == "stuck", w.status())
    t0 = time.monotonic()
    try:
        w.run(lambda c: 1, timeout=5, connect=False)
        check("stuck 중에는 즉시 거절", False)
    except cad_worker.CADBusy:
        check("stuck 중에는 즉시 거절", time.monotonic() - t0 < 0.5)
    time.sleep(3.5)
    check("막힌 작업이 끝나면 자동 복구", w.status()["state"] != "stuck", w.status())
    notices = w.pop_notices()
    check("늦게 완료 알림", any("늦게" in n for n in notices), notices)
    check("복구 후 정상 실행", w.run(lambda c: 42, connect=False) == 42)
    # 큐 대기 시간은 실행 시간 제한에 포함되지 않는다
    import threading
    out = {}
    th = threading.Thread(target=lambda: out.update(a=w.run(lambda c: time.sleep(2) or "a",
                                                            timeout=5, connect=False)))
    th.start()
    time.sleep(0.2)
    out["b"] = w.run(lambda c: "b", timeout=1, connect=False)
    th.join()
    check("앞 작업 대기는 시간 제한에 포함 안 됨", out == {"a": "a", "b": "b"}, out)
    # 상태 확인용 요청: 앞 작업 뒤에 오래 줄 서지 않고, 시간 초과해도 막힘을 남기지 않는다
    th = threading.Thread(target=lambda: w.run(lambda c: time.sleep(2), timeout=5, connect=False))
    th.start()
    time.sleep(0.2)
    t0 = time.monotonic()
    try:
        w.run(lambda c: 1, timeout=10, connect=False, queue_wait=0.5, mark_stuck=False)
        check("probe는 큐에서 0.5초만 기다림", False)
    except cad_worker.CADBusy:
        check("probe는 큐에서 0.5초만 기다림", time.monotonic() - t0 < 1.5)
    th.join()
    try:
        w.run(lambda c: time.sleep(1.5), timeout=0.5, connect=False, mark_stuck=False)
    except cad_worker.CADBusy:
        pass
    check("mark_stuck=False 시간 초과는 막힘을 남기지 않음", not w.status()["stuck"], w.status())
    time.sleep(1.2)
    w.restart()
    check("restart 후 새 스레드", w.status()["thread_alive"], w.status())

    import cad_reader
    r = cad_reader.round_floats({"a": float("inf"), "b": [float("nan"), 1.25, 3.0]}, 1)
    check("round_floats가 inf/NaN을 None으로", r == {"a": None, "b": [None, 1.2, 3]}, r)


def main():
    print("== 작업 스레드 단위 테스트 ==", flush=True)
    worker_unit_tests()
    before = icad_pids()
    print("== E2E (격리된 캐디안) ==", flush=True)
    try:
        asyncio.run(run_tests())
    except Exception as e:
        check("E2E 실행", False, repr(e))
    finally:
        for pid in icad_pids() - before:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n결과: {len(RESULTS) - len(failed)}/{len(RESULTS)} 통과")
    for name, _, detail in failed:
        print(f"  FAIL {name}: {str(detail)[:400]}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
