r"""CAD MCP 서버 진입점.

구조
    MCP 요청(비동기) -> 도구 핸들러(별도 스레드, asyncio.to_thread)
                     -> CADService.run() -> CADWorker 작업 큐 -> COM (작업 스레드)

    핸들러를 이벤트 루프 밖 스레드에서 돌리는 이유: 예전에는 핸들러가 이벤트
    루프 위에서 COM 응답을 동기로 기다려, 한 호출이 막히면 그동안 cad_status
    같은 다른 요청도 전혀 처리되지 않았다 (개선 요청 2-2).

    모든 호출에는 두 겹의 시간 제한이 있다.
      1) 작업 스레드 제한: 도구별 30~120초 (cad_worker)
      2) 서버 전체 상한: HARD_TIMEOUT초 — 미리보기 렌더링처럼 작업 스레드
         밖에서 일어나는 일까지 포함
    어느 쪽이든 넘기면 구조화된 오류를 돌려주고 서버는 계속 산다.

진단 장치
    - 도구 호출마다 시작/종료(소요 시간)를 cad_mcp.log에 남긴다. 서버가 죽으면
      마지막 "시작"만 있고 "종료"가 없는 줄이 범인이다.
    - 네이티브 크래시(COM 내부 접근 위반 등)는 파이썬 로그에 안 남으므로
      faulthandler로 cad_mcp_crash.log에 스택을 덤프한다.
"""
import asyncio
import base64
import faulthandler
import json
import logging
import logging.handlers
import os
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional

sys.dont_write_bytecode = True

import mcp.server.stdio  # noqa: E402
import mcp.types as types  # noqa: E402
from mcp.server import NotificationOptions, Server  # noqa: E402
from mcp.server.models import InitializationOptions  # noqa: E402
from pydantic import AnyUrl  # noqa: E402

import cad_reader  # noqa: E402
import cad_worker  # noqa: E402
from cad_controller import CADError, describe_error  # noqa: E402
from cad_worker import CADBusy, CONNECT_TIMEOUT, DEFAULT_TIMEOUT, LONG_TIMEOUT  # noqa: E402

SRC_DIR = os.path.dirname(os.path.abspath(__file__))
HARD_TIMEOUT = 280.0

# Windows 환경에서 UTF-8 인코딩 설정
if sys.platform == "win32" and os.environ.get('PYTHONIOENCODING') is None:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

_file_handler = logging.handlers.RotatingFileHandler(
    os.path.join(SRC_DIR, 'cad_mcp.log'), maxBytes=2 * 1024 * 1024,
    backupCount=3, encoding='utf-8')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(), _file_handler])
logger = logging.getLogger('mcp_cad_server')

# 네이티브 크래시 덤프 (파일 핸들은 프로세스 끝까지 열어 둔다)
_crash_file = open(os.path.join(SRC_DIR, 'cad_mcp_crash.log'), 'a', encoding='utf-8')
_crash_file.write(f"\n===== 서버 시작 {time.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()} =====\n")
_crash_file.flush()
faulthandler.enable(file=_crash_file, all_threads=True)


def _log_uncaught(exc_type, exc, tb):
    logger.critical("처리되지 않은 예외", exc_info=(exc_type, exc, tb))


def _log_thread_uncaught(args):
    logger.critical(f"스레드 '{args.thread.name if args.thread else '?'}' 처리되지 않은 예외",
                    exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


sys.excepthook = _log_uncaught
threading.excepthook = _log_thread_uncaught

with open(os.path.join(SRC_DIR, 'config.json'), 'r', encoding='utf-8') as _f:
    CONFIG = json.load(_f)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


# ======================================================================
# 서비스
# ======================================================================

class CADService:
    def __init__(self):
        self.worker = cad_worker.CADWorker()
        self.state = {"last_command": "", "last_result": ""}

    def run(self, fn: Callable, label: str, timeout: float = None,
            mutating: bool = False) -> Any:
        """작업 스레드에서 fn(controller) 실행

        mutating이면 실행 취소 표시(StartUndoMark/EndUndoMark)로 감싸서, undo
        1단계가 도구 호출 1번을 정확히 되돌리게 한다.
        """
        def job(c):
            if mutating:
                c.begin_undo()
            try:
                return fn(c)
            finally:
                if mutating:
                    c.end_undo()

        self.state["last_command"] = label
        try:
            result = self.worker.run(job, timeout=timeout, label=label)
            self.state["last_result"] = "성공"
            return result
        except Exception as e:
            self.state["last_result"] = f"실패: {describe_error(e)}"
            raise

    def status(self) -> Dict[str, Any]:
        info = self.worker.status()
        # 연결돼 있고 한가할 때만 가볍게 확인한다 (활성 문서 동기화 포함).
        # 연결이 없을 때 확인하려 들면 캐디안을 띄워 버릴 수 있어 하지 않는다.
        # 확인 요청은 다른 작업 뒤에 줄 서지 않고(0.5초 안에 시작 못 하면 생략),
        # 응답이 늦어도 스레드를 막힘으로 표시하지 않는다 — 상태 확인이
        # 그 자체로 서버를 막아서는 안 된다.
        if info.get("state") == "ready":
            try:
                self.worker.run(lambda c: c.doc_name, timeout=10.0, label="상태 확인",
                                queue_wait=0.5, mark_stuck=False)
                info = self.worker.status()
                info["probe"] = "ok"
            except Exception as e:
                info = self.worker.status()
                info["probe"] = describe_error(e)
        return info

    def reconnect(self) -> Dict[str, Any]:
        """작업 스레드를 새로 만들고 실제로 캐디안에 다시 붙는다"""
        self.worker.restart()
        try:
            self.worker.run(lambda c: c.doc_name, timeout=CONNECT_TIMEOUT, label="재연결")
        except Exception as e:
            logger.warning(f"재연결 실패: {describe_error(e)}")
        return self.worker.status()


# ======================================================================
# 도구 정의
# ======================================================================

PT = {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 3}
PTS = {"type": "array", "items": PT}
LAYER = {"type": "string", "description": "레이어 (생략 시 '0'). 없으면 만든다. 현재 레이어는 바꾸지 않는다"}
COLOR = {"type": ["string", "integer"],
         "description": "색: ACI 번호(1~255), BYLAYER, BYBLOCK, red/빨강 등. 생략 시 BYLAYER"}
LINEWEIGHT = {"type": "number", "description": "선두께(1/100mm: 13, 25, 50...). -1=BYLAYER"}
HANDLE = {"type": "string", "description": "엔티티 핸들"}
JUSTIFY = {"type": "string",
           "enum": ["left", "center", "right", "middle", "top_left", "top_center", "top_right",
                    "middle_left", "middle_center", "middle_right",
                    "bottom_left", "bottom_center", "bottom_right"],
           "description": "정렬. 지정하면 position이 그 정렬점이 된다 (방 이름은 middle_center)"}
ATTACH = {"type": "string",
          "enum": ["top_left", "top_center", "top_right", "middle_left", "middle_center",
                   "middle_right", "bottom_left", "bottom_center", "bottom_right"],
          "description": "MText 부착점 (position이 이 점이 된다). 기본 top_left"}
SELECTOR = {
    "type": "object",
    "description": ("대상 조건(모두 AND): window [x1,y1,x2,y2] + mode crossing(걸치면, 기본)|"
                    "inside(완전 포함), layers [..], types [line, polyline, text, mtext, "
                    "dimension, hatch, circle, arc, block], handles [..]. window 판정은 텍스트 "
                    "포함 실제 외곽(GetBoundingBox) 기준"),
    "properties": {
        "window": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
        "mode": {"type": "string", "enum": ["crossing", "inside"]},
        "layers": {"type": "array", "items": {"type": "string"}},
        "types": {"type": "array", "items": {"type": "string"}},
        "handles": {"type": "array", "items": {"type": "string"}},
    },
}
EDIT_PARAMS = {
    "offset": {"type": "array", "items": {"type": "number"},
               "description": "move/copy 변위 [dx, dy]"},
    "base_point": {"type": "array", "items": {"type": "number"},
                   "description": "rotate/scale 기준점 [x, y]"},
    "angle": {"type": "number", "description": "rotate 각도(도, 반시계)"},
    "factor": {"type": "number", "description": "scale 배율(>0)"},
    "point1": {"type": "array", "items": {"type": "number"}, "description": "mirror 축 첫 점"},
    "point2": {"type": "array", "items": {"type": "number"}, "description": "mirror 축 둘째 점"},
    "make_copy": {"type": "boolean", "description": "mirror 시 원본 유지 (기본 true)"},
    "distance": {"type": "number", "description": "offset 거리. 부호가 방향"},
    "layer": {"type": "string", "description": "set_properties: 옮길 레이어"},
    "color": COLOR,
    "lineweight": LINEWEIGHT,
    "linetype": {"type": "string", "description": "set_properties: 선종류 (BYLAYER, CONTINUOUS 등)"},
}
EDIT_OPS = ["move", "rotate", "scale", "copy", "mirror", "offset", "delete", "set_properties"]

SPEC_HELP = (
    "entities 항목 형식 (모두 layer/color/lineweight/linetype 지정 가능):\n"
    "line{start,end} circle{center,radius} arc{center,radius,start_angle,end_angle} "
    "ellipse{center,major_axis,minor_axis,rotation} rectangle{corner1,corner2} "
    "polyline{points,closed} text{position,text,height,rotation,justify} "
    "mtext{position,text,height,width,attachment} "
    "dimension{start,end,offset|text_position,orientation:aligned|horizontal|vertical,textheight} "
    "hatch{points,pattern_name,scale} "
    "wall{start,end,thickness,justify:center|left|right} — 벽 중심선(또는 한쪽 면)으로 닫힌 사각형 "
    "door{hinge,width,angle,swing:left|right} — angle은 경첩에서 닫힌 문짝이 향하는 방향(도), "
    "swing은 그 방향 기준 열리는 쪽. 문짝 선+1/4호 "
    "window{start,end,thickness,lines} — 벽 중심선 위 창 구간에 평행선 lines개(기본 3)")


class Tool:
    def __init__(self, name: str, description: str, properties: Dict[str, Any],
                 required: List[str], handler: Callable[[CADService, Dict[str, Any]], Any]):
        self.name = name
        self.description = description
        self.schema = {"type": "object", "properties": properties, "required": required}
        self.handler = handler


TOOLS: Dict[str, Tool] = {}


def tool(name: str, description: str, properties: Dict[str, Any] = None,
         required: List[str] = ()):
    def deco(fn):
        TOOLS[name] = Tool(name, description, properties or {}, list(required), fn)
        return fn
    return deco


def _opt(args: Dict[str, Any], *keys) -> Dict[str, Any]:
    return {k: args[k] for k in keys if args.get(k) is not None}


# ---------------- 작도 ----------------

@tool("draw_line", "직선을 그린다. 반환: 핸들",
      {"start_point": PT, "end_point": PT, "layer": LAYER, "color": COLOR, "lineweight": LINEWEIGHT},
      ["start_point", "end_point"])
def t_draw_line(svc, a):
    h = svc.run(lambda c: c.draw_line(a["start_point"], a["end_point"], a.get("layer"),
                                      a.get("color"), a.get("lineweight")), "draw_line", mutating=True)
    return f"직선 생성: {h}"


@tool("draw_circle", "원을 그린다. 반환: 핸들",
      {"center": PT, "radius": {"type": "number"}, "layer": LAYER, "color": COLOR,
       "lineweight": LINEWEIGHT}, ["center", "radius"])
def t_draw_circle(svc, a):
    h = svc.run(lambda c: c.draw_circle(a["center"], a["radius"], a.get("layer"),
                                        a.get("color"), a.get("lineweight")), "draw_circle", mutating=True)
    return f"원 생성: {h}"


@tool("draw_arc",
      "호를 그린다. 각도는 도 단위, 반시계 방향으로 start_angle에서 end_angle까지 "
      "(예: 270→0도 90° 호, 0→270은 270° 호). 반환: 핸들",
      {"center": PT, "radius": {"type": "number"}, "start_angle": {"type": "number"},
       "end_angle": {"type": "number"}, "layer": LAYER, "color": COLOR, "lineweight": LINEWEIGHT},
      ["center", "radius", "start_angle", "end_angle"])
def t_draw_arc(svc, a):
    h = svc.run(lambda c: c.draw_arc(a["center"], a["radius"], a["start_angle"], a["end_angle"],
                                     a.get("layer"), a.get("color"), a.get("lineweight")),
                "draw_arc", mutating=True)
    return f"호 생성: {h}"


@tool("draw_ellipse", "타원을 그린다. rotation은 장축 각도(도). 반환: 핸들",
      {"center": PT, "major_axis": {"type": "number"}, "minor_axis": {"type": "number"},
       "rotation": {"type": "number"}, "layer": LAYER, "color": COLOR, "lineweight": LINEWEIGHT},
      ["center", "major_axis", "minor_axis"])
def t_draw_ellipse(svc, a):
    h = svc.run(lambda c: c.draw_ellipse(a["center"], a["major_axis"], a["minor_axis"],
                                         a.get("rotation") or 0, a.get("layer"), a.get("color"),
                                         a.get("lineweight")), "draw_ellipse", mutating=True)
    return f"타원 생성: {h}"


@tool("draw_polyline", "폴리라인을 그린다. 반환: 핸들",
      {"points": PTS, "closed": {"type": "boolean"}, "layer": LAYER, "color": COLOR,
       "lineweight": LINEWEIGHT}, ["points"])
def t_draw_polyline(svc, a):
    h = svc.run(lambda c: c.draw_polyline(a["points"], bool(a.get("closed")), a.get("layer"),
                                          a.get("color"), a.get("lineweight")),
                "draw_polyline", mutating=True)
    return f"폴리라인 생성: {h}"


@tool("draw_rectangle", "사각형(닫힌 폴리라인)을 그린다. 반환: 핸들",
      {"corner1": PT, "corner2": PT, "layer": LAYER, "color": COLOR, "lineweight": LINEWEIGHT},
      ["corner1", "corner2"])
def t_draw_rectangle(svc, a):
    h = svc.run(lambda c: c.draw_rectangle(a["corner1"], a["corner2"], a.get("layer"),
                                           a.get("color"), a.get("lineweight")),
                "draw_rectangle", mutating=True)
    return f"사각형 생성: {h}"


@tool("draw_text", "한 줄 텍스트. justify로 정렬 (방 이름 가운데는 middle_center). 반환: 핸들",
      {"position": PT, "text": {"type": "string"}, "height": {"type": "number"},
       "rotation": {"type": "number", "description": "회전(도)"}, "justify": JUSTIFY,
       "layer": LAYER, "color": COLOR}, ["position", "text"])
def t_draw_text(svc, a):
    h = svc.run(lambda c: c.draw_text(a["position"], a["text"], a.get("height", 2.5),
                                      a.get("rotation") or 0, a.get("layer"), a.get("color"),
                                      a.get("justify")), "draw_text", mutating=True)
    return f"텍스트 생성: {h}"


@tool("draw_mtext",
      "여러 줄 텍스트(MText). width=0이면 자동 폭. 줄바꿈은 \\P. 반환: 핸들",
      {"position": PT, "text": {"type": "string"}, "height": {"type": "number"},
       "width": {"type": "number"}, "attachment": ATTACH,
       "rotation": {"type": "number", "description": "회전(도)"}, "layer": LAYER, "color": COLOR},
      ["position", "text"])
def t_draw_mtext(svc, a):
    h = svc.run(lambda c: c.draw_mtext(a["position"], a["text"], a.get("height", 2.5),
                                       a.get("width", 0), a.get("attachment"),
                                       a.get("rotation") or 0, a.get("layer"), a.get("color")),
                "draw_mtext", mutating=True)
    return f"MText 생성: {h}"


@tool("draw_hatch", "해치(채우기). 경계 폴리라인도 함께 만든다. 반환: 경계/해치 핸들",
      {"points": PTS, "pattern_name": {"type": "string", "description": "기본 SOLID"},
       "scale": {"type": "number"}, "layer": LAYER, "color": COLOR}, ["points"])
def t_draw_hatch(svc, a):
    r = svc.run(lambda c: c.draw_hatch(a["points"], a.get("pattern_name", "SOLID"),
                                       a.get("scale", 1.0), a.get("layer"), a.get("color")),
                "draw_hatch", mutating=True)
    return f"해치 생성: 경계 {r['boundary']}, 해치 {r['hatch']}"


@tool("add_dimension",
      "치수 하나. text_position(치수선 위치)을 주거나, offset(측정선에서 치수선까지 거리, 부호=방향)"
      "과 orientation(aligned|horizontal|vertical)을 준다. 여러 개면 add_dimension_chain이나 "
      "draw_entities의 dimension 타입을 쓴다. 반환: 핸들",
      {"start_point": PT, "end_point": PT, "text_position": PT, "offset": {"type": "number"},
       "orientation": {"type": "string", "enum": ["aligned", "horizontal", "vertical"]},
       "textheight": {"type": "number"}, "layer": LAYER, "color": COLOR},
      ["start_point", "end_point"])
def t_add_dimension(svc, a):
    h = svc.run(lambda c: c.add_dimension(a["start_point"], a["end_point"], a.get("text_position"),
                                          a.get("textheight"), a.get("layer"), a.get("color"),
                                          a.get("orientation", "aligned"), a.get("offset")),
                "add_dimension", mutating=True)
    return f"치수 생성: {h}"


@tool("add_dimension_chain",
      "연속 치수 + 전체 치수를 한 번에 (도면 한 면의 그리드 치수). points는 잴 점들(정렬은 자동). "
      "offset: 가장 바깥 점에서 치수선까지 거리(+면 위/오른쪽, -면 아래/왼쪽). overall_offset 생략 시 "
      "한 단 더 바깥에 전체 치수. 반환: 핸들",
      {"points": PTS, "orientation": {"type": "string", "enum": ["horizontal", "vertical"]},
       "offset": {"type": "number"}, "overall": {"type": "boolean", "description": "전체 치수 (기본 true)"},
       "overall_offset": {"type": "number"}, "textheight": {"type": "number"},
       "layer": LAYER, "color": COLOR}, ["points", "orientation"])
def t_add_dimension_chain(svc, a):
    r = svc.run(lambda c: c.add_dimension_chain(
        a["points"], a["orientation"], a.get("offset"), a.get("overall", True),
        a.get("overall_offset"), a.get("textheight"), a.get("layer"), a.get("color")),
        "add_dimension_chain", timeout=LONG_TIMEOUT, mutating=True)
    return f"연속 치수 {len(r['chain'])}개: {', '.join(r['chain'])}" + \
        (f" / 전체 치수: {r['overall']}" if r.get("overall") else "")


@tool("draw_entities",
      "여러 도형을 한 번의 호출로 그린다. 기본 응답은 항목 순서대로의 핸들 목록(실패 항목은 ✗)과 "
      "실패 사유. atomic=true면 하나라도 실패 시 이번 호출에서 만든 것을 모두 지운다. "
      "default_layer/default_color는 layer/color를 안 준 항목에 적용.\n" + SPEC_HELP,
      {"entities": {"type": "array", "items": {"type": "object"}},
       "atomic": {"type": "boolean"}, "default_layer": {"type": "string"},
       "default_color": COLOR,
       "verbose": {"type": "boolean", "description": "항목마다 한 줄로 자세히"}},
      ["entities"])
def t_draw_entities(svc, a):
    entities = a["entities"]
    # 캐디안은 도형 하나에 100~160ms가 든다 (AddLine만 ~64ms, 2026-10-12 실측).
    # 개수에 비례해 넉넉히 잡되 서버 전체 상한보다는 짧게.
    timeout = min(HARD_TIMEOUT - 30, DEFAULT_TIMEOUT + 0.5 * len(entities))
    r = svc.run(lambda c: c.draw_entities(entities, bool(a.get("atomic")), a.get("default_layer"),
                                          a.get("default_color")),
                f"draw_entities({len(entities)})", timeout=timeout, mutating=True)
    results = r["results"]
    ok = [x for x in results if "handles" in x]
    lines = [f"배치 작도: {len(entities)}개 중 {len(ok)}개 성공"]
    if r.get("rolled_back") is not None:
        lines[0] += (f" — atomic: {r['atomic_failed_at']}번에서 실패해 이미 만든 "
                     f"{r['rolled_back']}개를 지웠습니다")
    if a.get("verbose"):
        for x in results:
            lines.append(f"[{x['index']}] " + (",".join(x["handles"]) if "handles" in x
                                              else "✗ " + x["error"]))
    else:
        if r.get("rolled_back") is None:
            lines.append("handles: " + " | ".join(",".join(x["handles"]) if "handles" in x else "✗"
                                                  for x in results))
        for x in results:
            if "error" in x:
                lines.append(f"실패 [{x['index']}]: {x['error']}")
    return "\n".join(lines)


# ---------------- 편집 ----------------

def _edit_params(a: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in a.items() if k in EDIT_PARAMS and v is not None}


def _summarize_edit(op: str, r: Dict[str, Any]) -> str:
    results = r["results"]
    errs = [x for x in results if "error" in x]
    new = [h for x in results for h in ([x["new"]] if isinstance(x.get("new"), str)
                                        else (x.get("new") or []))]
    lines = [f"{op}: 대상 {r['matched']}개, 성공 {len(results) - len(errs)}개"]
    if new:
        lines.append("새 핸들: " + ", ".join(new))
    for x in errs[:20]:
        lines.append(f"실패 {x['handle']}: {x['error']}")
    if len(errs) > 20:
        lines.append(f"... 실패 {len(errs) - 20}개 더")
    return "\n".join(lines)


@tool("edit_entity",
      "핸들 하나를 편집. operation별 인자: move{offset} rotate{base_point,angle} "
      "scale{base_point,factor} copy{offset?} mirror{point1,point2,make_copy} offset{distance} "
      "delete{} set_properties{layer?,color?,lineweight?,linetype?}. 여러 개는 edit_entities",
      {"handle": HANDLE, "operation": {"type": "string", "enum": EDIT_OPS}, **EDIT_PARAMS},
      ["handle", "operation"])
def t_edit_entity(svc, a):
    op = a["operation"]
    r = svc.run(lambda c: c.edit_entity(a["handle"], op, **_edit_params(a)),
                f"edit_entity({op})", mutating=True)
    text = f"편집 완료: {op} (핸들 {a['handle']})"
    if r.get("new"):
        new = r["new"] if isinstance(r["new"], str) else ", ".join(r["new"])
        text += f" → 새 핸들 {new}"
    return text


@tool("edit_entities",
      "여러 엔티티에 같은 편집을 한 번에. 대상은 handles(목록) 또는 selector(영역/레이어/종류) 중 하나. "
      "예: 영역 전체를 x=32700 축으로 반전 → selector{window:[...],mode:inside}, operation mirror, "
      "point1 [32700,0], point2 [32700,1], make_copy false. 레이어 옮기기 → set_properties{layer}. "
      "인자는 edit_entity와 같다. 결과는 핸들별 성공/실패",
      {"operation": {"type": "string", "enum": EDIT_OPS},
       "handles": {"type": "array", "items": {"type": "string"}},
       "selector": SELECTOR, **EDIT_PARAMS}, ["operation"])
def t_edit_entities(svc, a):
    op = a["operation"]
    r = svc.run(lambda c: c.edit_entities(op, a.get("handles"), a.get("selector"), **_edit_params(a)),
                f"edit_entities({op})", timeout=LONG_TIMEOUT, mutating=True)
    return _summarize_edit(op, r)


@tool("edit_text",
      "Text/MText의 내용·높이·위치·정렬(Text는 justify, MText는 attachment)·폭을 바꾼다. "
      "위치 없이 justify만 바꾸면 지금 보이는 자리를 기준으로 정렬점을 잡는다",
      {"handle": HANDLE, "text": {"type": "string"}, "height": {"type": "number"},
       "position": PT, "justify": JUSTIFY, "attachment": ATTACH, "width": {"type": "number"},
       "rotation": {"type": "number"}}, ["handle"])
def t_edit_text(svc, a):
    r = svc.run(lambda c: c.edit_text(a["handle"], a.get("text"), a.get("height"),
                                      a.get("position"), a.get("justify"), a.get("attachment"),
                                      a.get("width"), a.get("rotation")),
                "edit_text", mutating=True)
    return "텍스트 수정: " + _dumps(cad_reader.round_floats(r, 1))


@tool("array_entity",
      "배열 복제. polar{count(원본 포함),center,included_angle,rotate_objects} 또는 "
      "rectangular{rows,columns,row_spacing,column_spacing}",
      {"handle": HANDLE, "mode": {"type": "string", "enum": ["polar", "rectangular"]},
       "count": {"type": "integer"}, "center": {"type": "array", "items": {"type": "number"}},
       "included_angle": {"type": "number"}, "rotate_objects": {"type": "boolean"},
       "rows": {"type": "integer"}, "columns": {"type": "integer"},
       "row_spacing": {"type": "number"}, "column_spacing": {"type": "number"}},
      ["handle", "mode"])
def t_array_entity(svc, a):
    if a["mode"] == "polar":
        svc.run(lambda c: c.array_polar(a["handle"], a["count"], a.get("center", (0, 0)),
                                        a.get("included_angle", 360.0), a.get("rotate_objects", True)),
                "array_polar", mutating=True)
    else:
        svc.run(lambda c: c.array_rectangular(a["handle"], a.get("rows", 1), a.get("columns", 1),
                                              a.get("row_spacing", 0.0), a.get("column_spacing", 0.0)),
                "array_rectangular", mutating=True)
    return f"배열 복제 완료 ({a['mode']}, 핸들 {a['handle']})"


@tool("insert_opening",
      "사각형 벽(꼭짓점 4개 닫힌 폴리라인)에 개구부를 낸다. 벽을 개구부 양옆 두 조각으로 다시 그리고 "
      "원래 벽은 지운다. kind=door면 문 기호(문짝+1/4호, swing_toward 쪽으로 열림, hinge_at에 가까운 "
      "문설주가 경첩), window면 창 평행선. center는 개구부 중심(벽 축 위의 점)",
      {"handle": HANDLE, "center": PT, "width": {"type": "number"},
       "kind": {"type": "string", "enum": ["none", "door", "window"]},
       "swing_toward": PT, "hinge_at": PT,
       "door_layer": {"type": "string"}, "window_layer": {"type": "string"},
       "window_lines": {"type": "integer"}}, ["handle", "center", "width"])
def t_insert_opening(svc, a):
    r = svc.run(lambda c: c.insert_opening(a["handle"], a["center"], a["width"], a.get("kind", "none"),
                                           a.get("swing_toward"), a.get("hinge_at"),
                                           a.get("door_layer"), a.get("window_layer"),
                                           a.get("window_lines", 3)),
                "insert_opening", mutating=True)
    return (f"개구부 완료: 벽 {r['removed']} → 조각 {', '.join(r['pieces']) or '없음'}"
            + (f" / 문 {', '.join(r['door'])}" if r["door"] else "")
            + (f" / 창 {', '.join(r['window'])}" if r["window"] else ""))


@tool("break_entity",
      "선/폴리라인을 두 점 사이에서 끊어 낸다(BREAK). 닫힌 폴리라인은 두 점 사이 두 경로 중 "
      "remove=short(기본)면 짧은 쪽을 지우고 나머지를 열린 폴리라인으로 남긴다. 외벽에 현관문 낼 때 등",
      {"handle": HANDLE, "point1": PT, "point2": PT,
       "remove": {"type": "string", "enum": ["short", "long"]}},
      ["handle", "point1", "point2"])
def t_break_entity(svc, a):
    r = svc.run(lambda c: c.break_entity(a["handle"], a["point1"], a["point2"],
                                         a.get("remove", "short")), "break_entity", mutating=True)
    return f"끊기 완료: {r['removed']} → {', '.join(r['pieces']) or '남은 조각 없음'}"


# ---------------- 삭제 ----------------

@tool("delete_by_layer", "레이어의 모든 엔티티를 삭제. 반환: 삭제 개수",
      {"layer": {"type": "string"}}, ["layer"])
def t_delete_by_layer(svc, a):
    n = svc.run(lambda c: c.delete_by_layer(a["layer"]), "delete_by_layer",
                timeout=LONG_TIMEOUT, mutating=True)
    return f"레이어 '{a['layer']}' 엔티티 {n}개 삭제"


@tool("delete_by_window",
      "사각형 범위에 걸리는(crossing, 기본) 또는 완전히 들어온(inside) 엔티티 삭제. 판정은 텍스트 포함 "
      "실제 외곽 기준. layers/types로 더 좁힐 수 있다. 반환: 삭제 개수",
      {"x1": {"type": "number"}, "y1": {"type": "number"}, "x2": {"type": "number"},
       "y2": {"type": "number"}, "mode": {"type": "string", "enum": ["crossing", "inside"]},
       "layers": {"type": "array", "items": {"type": "string"}},
       "types": {"type": "array", "items": {"type": "string"}}},
      ["x1", "y1", "x2", "y2"])
def t_delete_by_window(svc, a):
    n = svc.run(lambda c: c.delete_by_window(a["x1"], a["y1"], a["x2"], a["y2"],
                                             a.get("mode", "crossing"), a.get("layers"), a.get("types")),
                "delete_by_window", timeout=LONG_TIMEOUT, mutating=True)
    return f"윈도우 ({a['x1']},{a['y1']})-({a['x2']},{a['y2']}) {a.get('mode', 'crossing')}: {n}개 삭제"


# ---------------- 읽기 ----------------

@tool("cad_status",
      "연결 상태를 즉시 보고(막혀 있어도 바로 답한다). state: ready|busy(작업 중, 어떤 작업이 몇 초째)|"
      "stuck(막힘)|idle_not_connected(첫 작업 때 자동 연결)|not_started. 장애가 나면 재시작 전에 "
      "먼저 이걸 호출해 결과를 남긴다")
def t_cad_status(svc, a):
    return _dumps(svc.status())


@tool("cad_reconnect",
      "작업 스레드를 새로 만들고 캐디안에 다시 연결한다. stuck이 오래 안 풀릴 때, 캐디안에서 ESC를 "
      "누른 뒤 호출. 대기 중이던 요청은 취소된다")
def t_cad_reconnect(svc, a):
    return "재연결 결과: " + _dumps(svc.reconnect())


@tool("get_drawing_info",
      "도면 전역 정보: 문서명, 엔티티 수, 범위(EXTMIN/EXTMAX, 읽기 전에 전체 보기로 갱신), "
      "레이어 목록, 현재 레이어/색, 단위")
def t_get_drawing_info(svc, a):
    def job(c):
        c.zoom_extents()
        return cad_reader.drawing_info(c.doc, c.lib)
    info = svc.run(job, "get_drawing_info", timeout=LONG_TIMEOUT)
    return _dumps(cad_reader.round_floats(info, 1))


@tool("list_entities",
      "엔티티 목록. 기본은 좌표 배열(points/loops)을 뺀 요약, 숫자는 소수 1자리. 전체 모양 확인은 "
      "preview_drawing이 훨씬 싸다. window/layer/entity_type으로 좁히고, 정확한 좌표가 필요할 때만 "
      "include_geometry. fields로 필요한 키만 받을 수 있다(예: [\"Handle\",\"Layer\"]). "
      "텍스트에는 justify/attachment와 실제 외곽 bbox가 들어 있다",
      {"layer": {"type": "string"}, "entity_type": {"type": "string",
                                                    "description": "line, polyline, text, mtext, dimension, hatch, circle, arc, block"},
       "window": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
       "mode": {"type": "string", "enum": ["crossing", "inside"]},
       "limit": {"type": "integer", "description": "최대 개수 (기본 100)"},
       "include_geometry": {"type": "boolean"},
       "precision": {"type": "integer", "description": "소수 자릿수 (기본 1)"},
       "fields": {"type": "array", "items": {"type": "string"}}})
def t_list_entities(svc, a):
    selector = {}
    if a.get("layer"):
        selector["layers"] = [a["layer"]]
    if a.get("entity_type"):
        selector["types"] = [a["entity_type"]]
    if a.get("window"):
        selector["window"] = a["window"]
        selector["mode"] = a.get("mode", "crossing")

    limit = max(1, int(a.get("limit", 100)))

    def job(c):
        # 돌려줄 limit개까지만 상세히 읽는다 (엔티티 하나에 COM 속성 읽기 ~10번)
        if selector:
            targets = [e for _, e in c.select(selector)]
            return len(targets), cad_reader.read_entities(c.doc, c.lib, limit, entities=targets)
        return c.doc.ModelSpace.Count, cad_reader.read_entities(c.doc, c.lib, limit)

    total, page = svc.run(job, "list_entities", timeout=LONG_TIMEOUT)
    if not a.get("include_geometry"):
        page = [cad_reader.compact_entity(e) for e in page]
    fields = a.get("fields")
    if fields:
        page = [{k: e[k] for k in fields if k in e} for e in page]
    precision = int(a.get("precision", 1))
    lines = [f"조건에 맞는 엔티티 {total}개" + (f" (앞 {len(page)}개만 표시)" if len(page) < total else "")]
    lines += [_dumps(cad_reader.round_floats(e, precision)) for e in page]
    return "\n".join(lines)


@tool("get_bbox", "엔티티 실제 외곽 [minx,miny,maxx,maxy] (텍스트 폭 확인용)",
      {"handles": {"type": "array", "items": {"type": "string"}}}, ["handles"])
def t_get_bbox(svc, a):
    r = svc.run(lambda c: c.get_bbox(a["handles"]), "get_bbox", timeout=LONG_TIMEOUT)
    return "\n".join(_dumps(x) for x in r)


@tool("preview_drawing",
      "도면을 PNG로 렌더링. 비용이 엔티티 수와 거의 무관해서 전체 확인은 list_entities 대신 이것을 "
      "쓴다. zoom_to로 영역 확대, show_labels로 핸들 표시",
      {"width": {"type": "integer", "description": "기본 800, 최대 2000"},
       "height": {"type": "integer", "description": "기본 600, 최대 2000"},
       "show_labels": {"type": "boolean"},
       "zoom_to": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4}})
def t_preview_drawing(svc, a):
    def job(c):
        entities = cad_reader.read_entities(c.doc, c.lib)
        try:
            expanded = cad_reader.expand_blocks(entities, c.doc, c.lib)
        except Exception as e:
            logger.warning(f"블록 펼치기 실패, 원본 사용: {e}")
            expanded = entities
        return entities, expanded

    entities, expanded = svc.run(job, "preview_drawing", timeout=LONG_TIMEOUT)
    zoom = a.get("zoom_to")
    png = cad_reader.render_png(
        expanded,
        width=max(200, min(int(a.get("width", 800)), 2000)),
        height=max(200, min(int(a.get("height", 600)), 2000)),
        show_labels=bool(a.get("show_labels")),
        bounds=tuple(float(v) for v in zoom) if zoom else None)
    return [types.ImageContent(type="image", mimeType="image/png",
                               data=base64.b64encode(png).decode("ascii")),
            types.TextContent(type="text", text=cad_reader.summarize(entities))]


@tool("list_blocks", "정의된 블록과 속성 태그 목록")
def t_list_blocks(svc, a):
    blocks = svc.run(lambda c: c.list_blocks(), "list_blocks")
    return _dumps(blocks) if blocks else "정의된 블록이 없습니다."


# ---------------- 레이어 / 현재값 / 정리 ----------------

@tool("manage_layers",
      "레이어 관리. action: list(색·켜짐·잠금·엔티티 수) | create | delete(빈 레이어만) | rename | "
      "set(color/on/frozen/locked/lineweight 변경)",
      {"action": {"type": "string", "enum": ["list", "create", "delete", "rename", "set"]},
       "name": {"type": "string"}, "new_name": {"type": "string"}, "color": COLOR,
       "on": {"type": "boolean"}, "frozen": {"type": "boolean"}, "locked": {"type": "boolean"},
       "lineweight": LINEWEIGHT}, ["action"])
def t_manage_layers(svc, a):
    action = a["action"]
    r = svc.run(lambda c: c.layer_action(action, a.get("name"), a.get("new_name"), a.get("color"),
                                         a.get("on"), a.get("frozen"), a.get("locked"),
                                         a.get("lineweight")),
                f"manage_layers({action})", timeout=LONG_TIMEOUT, mutating=action != "list")
    if action == "list":
        return "\n".join(_dumps(x) for x in r["layers"])
    return _dumps(r)


@tool("set_current",
      "현재 레이어(CLAYER)·현재 색(CECOLOR) 설정. 테스트나 작업 후 color=BYLAYER로 되돌리는 데 쓴다. "
      "이 서버의 작도 도구는 현재값과 무관하게 레이어 '0'/BYLAYER가 기본이다",
      {"layer": {"type": "string"}, "color": COLOR})
def t_set_current(svc, a):
    r = svc.run(lambda c: c.set_current(a.get("layer"), a.get("color")), "set_current")
    return f"현재 레이어: {r['clayer']}, 현재 색: {r['cecolor']}"


@tool("purge",
      "쓰지 않는 항목 정리. kind: all(PURGE ALL) | layers(엔티티 없는 레이어만, 0/Defpoints/현재 "
      "레이어 제외) | blocks(삽입 안 된 블록 정의)",
      {"kind": {"type": "string", "enum": ["all", "layers", "blocks"]}})
def t_purge(svc, a):
    r = svc.run(lambda c: c.purge(a.get("kind", "all")), "purge", timeout=LONG_TIMEOUT, mutating=True)
    return (f"정리 완료: 레이어 {len(r['removed_layers'])}개 {r['removed_layers']}, "
            f"블록 {len(r['removed_blocks'])}개 {r['removed_blocks']}")


# ---------------- 문서 / 뷰 / 명령 ----------------

@tool("document",
      "문서 목록/전환. action: list | new(새 도면) | open(path) | activate(name) | save(이미 저장된 "
      "문서 덮어쓰기). 전환하면 이후 작업 대상이 바뀐다 (캐디안 탭도 함께 바뀜)",
      {"action": {"type": "string", "enum": ["list", "new", "open", "activate", "save"]},
       "name": {"type": "string"}, "path": {"type": "string"}}, ["action"])
def t_document(svc, a):
    action = a["action"]
    r = svc.run(lambda c: c.documents(action, a.get("name"), a.get("path")),
                f"document({action})", timeout=LONG_TIMEOUT)
    return _dumps(r)


@tool("save_drawing", "현재 도면을 경로에 저장 (저장 전 범위 갱신)",
      {"file_path": {"type": "string"}}, ["file_path"])
def t_save_drawing(svc, a):
    path = a["file_path"]
    if not os.path.isabs(path):
        base = CONFIG.get("output", {}).get("directory") or os.path.join(SRC_DIR, "..", "output")
        path = os.path.abspath(os.path.join(base, path))
    saved = svc.run(lambda c: c.save_drawing(path), "save_drawing", timeout=LONG_TIMEOUT)
    return f"저장 완료: {saved}"


@tool("zoom_extents", "전체 보기 + 재생성(REGEN). 갱신된 EXTMIN/EXTMAX를 돌려준다")
def t_zoom_extents(svc, a):
    r = svc.run(lambda c: c.extents(), "zoom_extents")
    return f"전체 보기 완료: {_dumps(r)}"


@tool("send_command",
      "캐디안 명령줄에 그대로 보낸다 (예: \"_.ZOOM\\n_E\\n\", \"-LAYER ...\"). 끝에 개행이 없으면 "
      "붙인다. wait초 안에 명령이 안 끝나면(추가 입력 대기) ESC로 취소한다. 전용 도구가 있으면 그걸 쓴다",
      {"command": {"type": "string"}, "wait": {"type": "number", "description": "기본 5초"}},
      ["command"])
def t_send_command(svc, a):
    wait = float(a.get("wait", 5.0))
    r = svc.run(lambda c: c.send_command(a["command"], True, wait), "send_command",
                timeout=wait + 20)
    return _dumps(r)


@tool("undo",
      "최근 작업 되돌리기. 이 서버의 작도/편집 도구 호출 1번 = 1단계 (사용자가 캐디안에서 직접 한 "
      "명령도 단계에 포함된다)",
      {"steps": {"type": "integer", "description": "기본 1"}})
def t_undo(svc, a):
    r = svc.run(lambda c: c.undo(a.get("steps", 1)), "undo", timeout=40)
    return f"되돌리기 {r['steps']}단계: 엔티티 수 변화 {r['entity_delta']:+d}" + \
        (f" ({r['note']})" if r.get("note") else "")


# ---------------- 블록 ----------------

@tool("create_block",
      "블록(심볼) 정의. entities는 draw_entities와 같은 형식(블록 기준점 상대 좌표), attributes는 "
      "[{tag,prompt,default,position,height,invisible}]",
      {"name": {"type": "string"}, "base_point": {"type": "array", "items": {"type": "number"}},
       "entities": {"type": "array", "items": {"type": "object"}},
       "attributes": {"type": "array", "items": {"type": "object"}}}, ["name", "entities"])
def t_create_block(svc, a):
    r = svc.run(lambda c: c.create_block(a["name"], a["entities"], a.get("attributes"),
                                         a.get("base_point", (0, 0))),
                "create_block", timeout=LONG_TIMEOUT, mutating=True)
    text = f"블록 '{r['block']}' 정의 완료: 도형 {r['entities']}개"
    if r["errors"]:
        text += "\n실패: " + "; ".join(r["errors"])
    return text


@tool("insert_block", "블록 배치. attributes={TAG: 값}. 반환: 핸들",
      {"name": {"type": "string"}, "position": PT, "scale": {"type": "number"},
       "rotation": {"type": "number"}, "attributes": {"type": "object"}, "layer": LAYER},
      ["name", "position"])
def t_insert_block(svc, a):
    h = svc.run(lambda c: c.insert_block(a["name"], a["position"], a.get("scale", 1.0),
                                         a.get("rotation", 0.0), a.get("attributes"), a.get("layer")),
                "insert_block", mutating=True)
    return f"블록 '{a['name']}' 삽입: {h}"


@tool("set_block_attributes", "배치된 블록의 속성값 변경",
      {"handle": HANDLE, "attributes": {"type": "object"}}, ["handle", "attributes"])
def t_set_block_attributes(svc, a):
    n = svc.run(lambda c: c.set_block_attributes(a["handle"], a["attributes"]),
                "set_block_attributes", mutating=True)
    return f"핸들 {a['handle']} 속성 {n}개 변경"


# ======================================================================
# 실행
# ======================================================================

def _summarize_args(args: Dict[str, Any]) -> str:
    text = _dumps(args)
    return text if len(text) <= 300 else text[:300] + f"...({len(text)}자)"


def _error_result(kind: str, message: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=f"오류[{kind}]: {message}")],
                                isError=True)


def _with_notices(service: CADService, content: List[Any]) -> List[Any]:
    notices = service.worker.pop_notices()
    if notices:
        content = list(content) + [types.TextContent(type="text",
                                                     text="\n".join("※ " + n for n in notices))]
    return content


async def main():
    logger.info("CAD MCP 서버 시작")
    service = CADService()
    server = Server(CONFIG['server']['name'])

    @server.list_resources()
    async def handle_list_resources() -> list[types.Resource]:
        return [types.Resource(uri=AnyUrl("drawing://current"), name="현재 CAD 도면",
                               description="마지막 작업과 연결 상태", mimeType="application/json")]

    @server.read_resource()
    async def handle_read_resource(uri: AnyUrl) -> str:
        if str(uri) != "drawing://current":
            raise ValueError(f"알 수 없는 리소스: {uri}")
        return _dumps({**service.state, "status": service.worker.status()})

    @server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        return [types.Tool(name=t.name, description=t.description, inputSchema=t.schema)
                for t in TOOLS.values()]

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict[str, Any] | None):
        spec = TOOLS.get(name)
        if spec is None:
            return _error_result("UNKNOWN_TOOL", f"알 수 없는 도구: {name}")
        args = arguments or {}
        t0 = time.monotonic()
        logger.info(f"도구 시작: {name} {_summarize_args(args)}")
        status = "실패"
        try:
            result = await asyncio.wait_for(asyncio.to_thread(spec.handler, service, args),
                                            timeout=HARD_TIMEOUT)
            if isinstance(result, str):
                result = [types.TextContent(type="text", text=result)]
            status = "성공"
            return _with_notices(service, result)
        except asyncio.TimeoutError:
            return _error_result("TIMEOUT", f"서버 처리 시간이 {HARD_TIMEOUT:.0f}초를 넘었습니다. "
                                            "cad_status로 상태를 확인하세요.")
        except CADBusy as e:
            return _error_result("CAD_BUSY", str(e))
        except KeyError as e:
            # 필수 인자는 JSON 스키마로 이미 검증되므로, 여기 오는 건 draw spec의
            # 빠진 키나 설정(config.json) 누락이다
            return _error_result("MISSING_VALUE", f"필요한 값이 없습니다(인자 항목 또는 설정): {e}")
        except (ValueError, TypeError) as e:
            return _error_result("INVALID_ARGS", str(e))
        except CADError as e:
            return _error_result("CAD_ERROR", str(e))
        except Exception as e:
            logger.exception(f"도구 {name} 처리 중 예외")
            return _error_result("COM_ERROR", describe_error(e))
        finally:
            logger.info(f"도구 종료: {name} {status} ({time.monotonic() - t0:.2f}s)")

    @server.list_prompts()
    async def handle_list_prompts() -> list[types.Prompt]:
        return [types.Prompt(name="cad-assistant",
                             description="자연어로 CAD를 제어하는 도우미", arguments=[])]

    @server.get_prompt()
    async def handle_get_prompt(name: str, arguments: dict[str, str] | None) -> types.GetPromptResult:
        if name != "cad-assistant":
            raise ValueError(f"알 수 없는 프롬프트: {name}")
        text = ("CAD MCP 도구로 캐디안을 제어합니다. 여러 도형은 draw_entities로 한 번에, 여러 편집은 "
                "edit_entities로 한 번에 처리하고, 결과 확인은 preview_drawing을 우선 쓰세요.")
        return types.GetPromptResult(description="CAD 도우미 프롬프트",
                                     messages=[types.PromptMessage(
                                         role="user", content=types.TextContent(type="text", text=text))])

    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        logger.info("서버가 stdio 전송으로 실행 중")
        await server.run(
            read_stream, write_stream,
            InitializationOptions(
                server_name=CONFIG['server']['name'],
                server_version=CONFIG['server']['version'],
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(), experimental_capabilities={})))


if __name__ == "__main__":
    asyncio.run(main())
