import os
import sys
import json
import logging
from pathlib import Path
from mcp.server.models import InitializationOptions
import mcp.types as types
from mcp.server import NotificationOptions, Server
import mcp.server.stdio
from pydantic import AnyUrl
from typing import Any, Dict, List
import sys

sys.dont_write_bytecode = True

# 절대 임포트 사용

from nlp_processor import NLPProcessor

from cad_controller import CADController
# 도면 읽기 / 미리보기 렌더링
import base64
import cad_reader
import cad_worker


# Windows 환경에서 UTF-8 인코딩 설정
if sys.platform == "win32" and os.environ.get('PYTHONIOENCODING') is None:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# 로그 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
			os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cad_mcp.log'), encoding='utf-8'
		)
    ]
)


logger = logging.getLogger('mcp_cad_server')
logger.info("CAD MCP 서버 시작")


PROMPT_TEMPLATE = """
어시스턴트의 목표는 CAD MCP 서비스 사용법을 시연하는 것이다. 이 서비스를 통해 대화 안에서 바로 CAD 소프트웨어를 제어하여 도형을 만들고 수정할 수 있다.

<cad-mcp>

프롬프트:
이 서버는 "cad-assistant"라는 이름의 사전 정의된 프롬프트를 제공하여, 사용자가 자연어 명령으로 CAD를 제어하도록 돕는다. 사용자는 다양한 도형을 그리거나, 도형 요소를 수정하거나, 도면을 저장하도록 요청할 수 있다.

리소스:
이 서버는 현재 CAD 도면 상태를 나타내는 "drawing://current" 리소스를 제공한다.

도구:

이 서버는 다음과 같은 기본 작도 도구를 제공한다:
"draw_line": CAD에서 직선을 그린다
"draw_circle": CAD에서 원을 그린다
"draw_arc": CAD에서 호를 그린다
"draw_ellipse": CAD에서 타원을 그린다
"draw_polyline": CAD에서 폴리라인을 그린다
"draw_rectangle": CAD에서 사각형을 그린다
"draw_text": CAD에 텍스트를 추가한다
"draw_hatch": CAD에서 해치(채우기)를 그린다
"save_drawing": 현재 도면을 저장한다
"add_dimension": CAD에 선형 치수를 추가한다
"process_command": 자연어 명령을 처리하여 CAD 작업으로 변환한다

</cad-mcp>

친근한 방식으로 시연을 시작하라. 예: "안녕하세요! 오늘은 CAD MCP 서비스 사용법을 보여드리겠습니다. 이 서비스를 이용하면 화면을 직접 조작하지 않고도 대화 안에서 바로 CAD 소프트웨어를 제어할 수 있습니다. 시작해 볼까요!"
"""

# 추후 구현 예정
# "erase_entity": 지정한 엔티티 삭제
# "move_entity": 지정한 엔티티 이동
# "rotate_entity": 지정한 엔티티 회전
# "scale_entity": 지정한 엔티티 축척 조정

class Config:

    def __init__(self):
        # config.json 파일을 직접 읽는다
        config_path = os.path.join(os.path.dirname(__file__), 'config.json')
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = json.load(f)
        logger.info("설정 파일 로드 성공")

    @property
    def server_name(self) -> str:
        return self.config['server']['name']

    @property
    def server_version(self) -> str:
        return self.config['server']['version']

class CADService:
    def __init__(self):
        """CAD 서비스 초기화"""
        # COM 호출은 전용 스레드에서 실행한다. 캐디안이 멈춰도 MCP 서버는
        # 계속 응답하고, 타임아웃 시 Claude에게 상황을 알릴 수 있다.
        self.worker = cad_worker.CADWorker()
        self.controller = cad_worker.ControllerProxy(self.worker)
        self.nlp_processor = NLPProcessor()
        self.drawing_state = {
            "entities": [],
            "current_layer": "0",
            "last_command": "",
            "last_result": ""
        }
        logger.info("CAD 서비스 초기화 완료 (COM 전용 스레드)")

    def start_cad(self):
        """CAD 시작"""
        return self.worker.run(lambda c: c.start_cad(),
                               timeout=cad_worker.CONNECT_TIMEOUT)

    def cad_status(self):
        """CAD 연결 상태 점검 (막혀 있어도 빠르게 반환)"""
        return self.worker.health_check()

    def reconnect(self):
        """막힌 COM 연결을 버리고 새로 연결

        restart()는 새 작업 스레드로 교체할 뿐 실제로 캐디안에 다시
        붙지는 않는다. health_check()의 probe는 is_running()만 보고
        start_cad()를 호출하지 않으므로, 여기서 부르지 않으면
        재연결 직후의 health_check는 항상 document: "not_connected"만
        보고한다(실제 재연결 성공 여부와 무관하게) - 이러면 재연결이
        실패한 것처럼 보이는 오해를 산다. 그래서 restart 직후 이 자리에서
        직접 start_cad()를 시도해 실제 연결 결과가 반영되게 한다.
        """
        self.worker.restart()
        try:
            self.worker.run(lambda c: c.start_cad(),
                            timeout=cad_worker.CONNECT_TIMEOUT)
        except Exception as e:
            logger.warning(f"재연결 중 start_cad 실패: {e}")
        return self.worker.health_check(timeout=cad_worker.CONNECT_TIMEOUT)
    
    def draw_line(self, start_point, end_point, layer=None, color=None, lineweight=None):
        """직선 그리기"""
        if not self.controller.is_running():
            self.start_cad()
        
        # 현재 레이어 또는 지정된 레이어 사용
        current_layer = layer or self.drawing_state["current_layer"]
        
        result = self.controller.draw_line(start_point, end_point, current_layer, color, lineweight)
        if result:
            self.drawing_state["entities"].append({
                "type": "line",
                "start": start_point,
                "end": end_point,
                "layer": current_layer,
                "color": color,
                "lineweight": lineweight
            })
            self.drawing_state["last_command"] = f"{start_point}에서 {end_point}까지 직선 그리기"
            self.drawing_state["last_result"] = "성공"
        else:
            self.drawing_state["last_result"] = "실패"

        return result

    def draw_circle(self, center, radius, layer=None, color=None, lineweight=None):
        """원 그리기"""
        if not self.controller.is_running():
            self.start_cad()
        
        # 현재 레이어 또는 지정된 레이어 사용
        current_layer = layer or self.drawing_state["current_layer"]
        
        result = self.controller.draw_circle(center, radius, current_layer, color, lineweight)
        if result:
            self.drawing_state["entities"].append({
                "type": "circle",
                "center": center,
                "radius": radius,
                "layer": current_layer,
                "color": color,
                "lineweight": lineweight
            })
            self.drawing_state["last_command"] = f"원 그리기, 중심점{center}, 반지름{radius}, 레이어{current_layer}"
            self.drawing_state["last_result"] = "성공"
        else:
            self.drawing_state["last_result"] = "실패"

        return result

    def draw_arc(self, center, radius, start_angle, end_angle, layer=None, color=None, lineweight=None):
        """호 그리기"""
        if not self.controller.is_running():
            self.start_cad()
        
        # 현재 레이어 또는 지정된 레이어 사용
        current_layer = layer or self.drawing_state["current_layer"]
        
        result = self.controller.draw_arc(center, radius, start_angle, end_angle, current_layer, color, lineweight)

        if result:
            self.drawing_state["entities"].append({
                "type": "arc",
                "center": center,
                "radius": radius,
                "start_angle": start_angle,
                "end_angle": end_angle,
                "layer": current_layer,
                "color": color,
                "lineweight": lineweight
            })
            self.drawing_state["last_command"] = f"호 그리기, 중심점{center}, 반지름{radius}, 시작각도{start_angle}, 끝각도{end_angle}, 레이어{current_layer}"
            self.drawing_state["last_result"] = "성공"
        else:
            self.drawing_state["last_result"] = "실패"

        return result

    def draw_ellipse(self, center, major_axis, minor_axis, rotation=0, layer=None, color=None, lineweight=None):
        """타원 그리기"""
        if not self.controller.is_running():
            self.start_cad()
        
        # 현재 레이어 또는 지정된 레이어 사용
        current_layer = layer or self.drawing_state["current_layer"]
        
        result = self.controller.draw_ellipse(center, major_axis, minor_axis, rotation, current_layer, color, lineweight)

        if result:
            self.drawing_state["entities"].append({
                "type": "ellipse",
                "center": center,
                "major_axis": major_axis,
                "minor_axis": minor_axis,
                "rotation": rotation,
                "layer": current_layer,
                "color": color,
                "lineweight": lineweight
            })
            self.drawing_state["last_command"] = f"타원 그리기, 중심점{center}, 장축{major_axis}, 단축{minor_axis}, 회전각도{rotation}, 레이어{current_layer}"
            self.drawing_state["last_result"] = "성공"
        else:
            self.drawing_state["last_result"] = "실패"

        return result

    def draw_polyline(self, points, closed=False, layer=None, color=None, lineweight=None):
        """폴리라인 그리기"""
        if not self.controller.is_running():
            self.start_cad()
        
        # 현재 레이어 또는 지정된 레이어 사용
        current_layer = layer or self.drawing_state["current_layer"]
        
        result = self.controller.draw_polyline(points, closed, current_layer, color, lineweight)
        if result:
            self.drawing_state["entities"].append({
                "type": "polyline",
                "points": points,
                "closed": closed,
                "layer": current_layer,
                "color": color,
                "lineweight": lineweight
            })
            self.drawing_state["last_command"] = f"폴리라인 그리기, 점집합{points}, {'닫힘' if closed else '열림'}, 레이어{current_layer}"
            self.drawing_state["last_result"] = "성공"
        else:
            self.drawing_state["last_result"] = "실패"

        return result

    def draw_rectangle(self, corner1, corner2, layer=None, color=None, lineweight=None):
        """사각형 그리기"""
        if not self.controller.is_running():
            self.start_cad()
        
        # 현재 레이어 또는 지정된 레이어 사용
        current_layer = layer or self.drawing_state["current_layer"]
        
        result = self.controller.draw_rectangle(corner1, corner2, current_layer, color, lineweight)
        if result:
            self.drawing_state["entities"].append({
                "type": "rectangle",
                "corner1": corner1,
                "corner2": corner2,
                "layer": current_layer,
                "color": color,
                "lineweight": lineweight
            })
            self.drawing_state["last_command"] = f"사각형 그리기, 대각점{corner1}와 {corner2}, 레이어{current_layer}"
            self.drawing_state["last_result"] = "성공"
        else:
            self.drawing_state["last_result"] = "실패"

        return result

    def draw_text(self, position, text, height=2.5, rotation=0, layer=None, color=None):
        """텍스트 추가"""
        if not self.controller.is_running():
            self.start_cad()
        
        # 현재 레이어 또는 지정된 레이어 사용
        current_layer = layer or self.drawing_state["current_layer"]
        
        result = self.controller.draw_text(position, text, height, rotation, current_layer, color)
        if result:
            self.drawing_state["entities"].append({
                "type": "text",
                "position": position,
                "text": text,
                "height": height,
                "rotation": rotation,
                "layer": current_layer,
                "color": color
            })
            self.drawing_state["last_command"] = f"텍스트 '{text}' 추가, 위치{position}, 높이{height}, 회전{rotation}"
            self.drawing_state["last_result"] = "성공"
        else:
            self.drawing_state["last_result"] = "실패"

        return result

    def draw_hatch(self, points, pattern_name="SOLID", scale=1.0, layer=None, color=None):
        """해치(채우기) 그리기"""
        if not self.controller.is_running():
            self.start_cad()
        
        # 현재 레이어 또는 지정된 레이어 사용
        current_layer = layer or self.drawing_state["current_layer"]
        
        result = self.controller.draw_hatch(points, pattern_name, scale, current_layer, color)
        if result:
            self.drawing_state["entities"].append({
                "type": "hatch",
                "points": points,
                "pattern_name": pattern_name,
                "scale": scale,
                "layer": current_layer,
                "color": color
            })
            self.drawing_state["last_command"] = f"해치 그리기, 점집합{points}, 패턴{pattern_name}, 비율{scale}, 레이어{current_layer}"
            self.drawing_state["last_result"] = "성공"
        else:
            self.drawing_state["last_result"] = "실패"

        return result

    def add_dimension(self, start_point, end_point, text_position=None,textheight=5, layer=None, color=None):
        """선형 치수 추가"""
        if not self.controller.is_running():
            self.start_cad()
        
        # 현재 레이어 또는 지정된 레이어 사용
        current_layer = layer or self.drawing_state["current_layer"]
        
        result = self.controller.add_dimension(start_point, end_point, text_position, textheight,current_layer, color)
        if result:
            self.drawing_state["entities"].append({
                "type": "dimension",
                "start": start_point,
                "end": end_point,
                "text_position": text_position,
                "textheight": textheight,
                "layer": current_layer,
                "color": color
            })
            self.drawing_state["last_command"] = f"{start_point}에서 {end_point}까지 치수 추가"
            self.drawing_state["last_result"] = "성공"
        else:
            self.drawing_state["last_result"] = "실패"

        return result


    def save_drawing(self, file_path):
        """도면 저장"""
        if not self.controller.is_running():
            return False

        result = self.controller.save_drawing(file_path)
        if result:
            self.drawing_state["last_command"] = f"{file_path}에 도면 저장"
            self.drawing_state["last_result"] = "성공"
        else:
            self.drawing_state["last_result"] = "실패"
        
        return result
    
    # ------------------------------------------------------------------
    # 도면 읽기 (읽기 전용)
    #
    # 캐디안은 Export와 화면 캡처가 모두 막혀 있으므로, COM으로 엔티티를
    # 읽어 파이썬이 직접 이미지를 그린다. cad_reader 모듈이 그 역할을 한다.
    # ------------------------------------------------------------------

    def _doc_and_lib(self):
        """작업 스레드 안에서만 호출할 것. COM 객체는 스레드를 넘길 수 없다."""
        raise RuntimeError("_doc_and_lib는 작업 스레드 내부에서만 사용합니다")

    @staticmethod
    def _lib_of(controller):
        try:
            return controller.app.Library
        except Exception:
            return None

    @staticmethod
    def _ensure(controller):
        if not controller.is_running():
            controller.start_cad()
        if not controller.is_running():
            raise RuntimeError("CAD에 연결할 수 없습니다")
        return controller.doc, CADService._lib_of(controller)

    def get_drawing_info(self) -> Dict[str, Any]:
        """도면 범위, 레이어, 엔티티 수 등 전역 정보"""
        def job(controller):
            doc, lib = CADService._ensure(controller)
            # Regen만으로는 EXTMIN/EXTMAX가 갱신되지 않는다. 읽기 직전에
            # zoom_extents로 범위를 재계산해야 정확한 값이 나온다.
            controller.zoom_extents()
            return cad_reader.drawing_info(doc, lib)
        return self.worker.run(job)

    def list_entities(self, layer=None, entity_type=None, limit=100,
                      include_geometry=False):
        """엔티티 목록. (표시할 목록, 전체 개수) 반환

        include_geometry=False(기본)면 points/loops 좌표 배열을 빼고 요약만
        반환한다(cad_reader.compact_entity). 큰 도면에서 좌표까지 다
        붙이면 응답이 토큰 한도를 넘길 수 있다 — 실제로 217개 엔티티에서
        56,570자로 초과된 적이 있다(history.md 2026-09-04 참고).
        """
        def job(controller):
            doc, lib = CADService._ensure(controller)
            return cad_reader.read_entities(doc, lib)

        entities = self.worker.run(job)

        if layer:
            entities = [e for e in entities if e.get('Layer') == layer]
        if entity_type:
            key = str(entity_type).lower()
            entities = [e for e in entities
                        if key in str(e.get('ObjectName', '')).lower()
                        or key in str(e.get('EntityName', '')).lower()]

        total = len(entities)
        try:
            limit = max(1, int(limit))
        except (TypeError, ValueError):
            limit = 100
        page = entities[:limit]
        if not include_geometry:
            page = [cad_reader.compact_entity(e) for e in page]
        return page, total

    def preview_drawing(self, width=800, height=600, show_labels=False,
                        zoom_to=None):
        """미리보기 PNG 바이트와 요약 텍스트 반환

        COM 읽기는 작업 스레드에서, 이미지 렌더링은 여기서 한다.
        렌더링은 순수 파이썬이라 CAD 상태와 무관하다. 기본 해상도를
        800x600으로 낮춰뒀다 — 일상적인 확인에는 충분하고, 세밀하게 봐야
        하면 호출 시 width/height를 키우면 된다(최대 2000).
        """
        def job(controller):
            doc, lib = CADService._ensure(controller)
            entities = cad_reader.read_entities(doc, lib)
            try:
                expanded = cad_reader.expand_blocks(entities, doc, lib)
            except Exception as e:
                logger.warning(f"블록 펼치기 실패, 원본 사용: {e}")
                expanded = entities
            return entities, expanded

        entities, expanded = self.worker.run(job)
        summary = cad_reader.summarize(entities)

        bounds = None
        if zoom_to and len(zoom_to) == 4:
            bounds = tuple(float(v) for v in zoom_to)

        png = cad_reader.render_png(
            expanded,
            width=max(200, min(int(width), 2000)),
            height=max(200, min(int(height), 2000)),
            show_labels=bool(show_labels),
            bounds=bounds)
        return png, summary

    # ------------------------------------------------------------------
    # 편집 / 블록
    # ------------------------------------------------------------------

    def edit_entity(self, handle, operation, **kwargs) -> bool:
        """핸들로 지목한 엔티티를 편집"""
        if not self.controller.is_running():
            self.start_cad()

        op = str(operation).lower()
        c = self.controller

        if op == "move":
            return c.move_entity(handle, kwargs["offset"])
        if op == "rotate":
            return c.rotate_entity(handle, kwargs.get("base_point", (0, 0)),
                                   kwargs["angle"])
        if op == "scale":
            return c.scale_entity(handle, kwargs.get("base_point", (0, 0)),
                                  kwargs["factor"])
        if op == "copy":
            new_handle = c.copy_entity(handle, kwargs.get("offset"))
            return new_handle if new_handle else False
        if op == "mirror":
            return c.mirror_entity(handle, kwargs["point1"], kwargs["point2"],
                                   kwargs.get("make_copy", True))
        if op == "offset":
            return c.offset_entity(handle, kwargs["distance"])
        if op == "delete":
            return c.delete_entity(handle)

        raise ValueError(f"지원하지 않는 편집 작업: {operation}")

    def array_entity(self, handle, mode, **kwargs) -> bool:
        """엔티티 배열 복제"""
        if not self.controller.is_running():
            self.start_cad()

        if str(mode).lower() in ("polar", "원형"):
            return self.controller.array_polar(
                handle,
                kwargs["count"],
                kwargs.get("center", (0, 0)),
                kwargs.get("included_angle", 360.0),
                kwargs.get("rotate_objects", True))

        return self.controller.array_rectangular(
            handle,
            kwargs.get("rows", 1),
            kwargs.get("columns", 1),
            kwargs.get("row_spacing", 0.0),
            kwargs.get("column_spacing", 0.0))

    def list_blocks(self):
        if not self.controller.is_running():
            self.start_cad()
        return self.controller.list_blocks()

    def create_block(self, name, entities, attributes=None, base_point=(0, 0)):
        if not self.controller.is_running():
            self.start_cad()
        return self.controller.create_block(name, entities, attributes,
                                            base_point)

    def insert_block(self, name, position, scale=1.0, rotation=0.0,
                     attributes=None, layer=None):
        if not self.controller.is_running():
            self.start_cad()
        return self.controller.insert_block(name, position, scale, rotation,
                                            attributes, layer)

    def set_block_attributes(self, handle, attributes):
        if not self.controller.is_running():
            self.start_cad()
        return self.controller.set_block_attributes(handle, attributes)

    # ------------------------------------------------------------------
    # 배치 작도 / 삭제
    # ------------------------------------------------------------------

    def draw_entities(self, entities):
        """여러 도형을 한 번의 호출로 그린다. 각 항목은 create_block의
        entities와 같은 형식 (type + 좌표 키 + layer/color 등)."""
        if not self.controller.is_running():
            self.start_cad()
        handles = self.controller.draw_entities(entities)
        ok = sum(1 for h in handles if h)
        self.drawing_state["last_command"] = f"배치 작도 {len(entities)}개 요청"
        self.drawing_state["last_result"] = f"{ok}/{len(entities)}개 성공"
        return handles

    def delete_by_layer(self, layer):
        """레이어의 모든 엔티티를 삭제. 삭제된 개수 반환"""
        if not self.controller.is_running():
            self.start_cad()
        count = self.controller.delete_by_layer(layer)
        self.drawing_state["last_command"] = f"레이어 '{layer}' 삭제"
        self.drawing_state["last_result"] = f"{count}개 삭제"
        return count

    def delete_by_window(self, x1, y1, x2, y2, mode="crossing"):
        """사각형 범위에 걸리는 엔티티를 삭제. 삭제된 개수 반환"""
        if not self.controller.is_running():
            self.start_cad()
        count = self.controller.delete_by_window(x1, y1, x2, y2, mode)
        self.drawing_state["last_command"] = f"윈도우 삭제 ({x1},{y1})-({x2},{y2})"
        self.drawing_state["last_result"] = f"{count}개 삭제"
        return count

    def process_command(self, command: str) -> Dict[str, Any]:
        """자연어 명령 처리"""
        if not self.controller.is_running():
            self.start_cad()
        # NLP 처리기로 명령 파싱
        parsed_command = self.nlp_processor.process_command(command)
        command_type = parsed_command.get("type")
        try:
            # 기본 작도 명령 처리
            if command_type == "draw_line":
                start_point = parsed_command.get("start_point")
                end_point = parsed_command.get("end_point")
                # 색상 매개변수 가져오기
                color = parsed_command.get("color")
                # 명령에서 색상 이름을 추출해 RGB 값으로 변환 시도
                color_rgb = self.nlp_processor.extract_color_from_command(command)
                if color_rgb is not None:
                    color = color_rgb  # 명령에서 추출한 색상을 우선 사용
                # 선 굵기 매개변수 가져오기
                lineweight = parsed_command.get("lineweight")
                result = self.draw_line(start_point, end_point, None, color, lineweight)
                return {
                    "success": result is not None,
                    "message": "직선이 그려졌습니다" if result else "직선 그리기 실패",
                    "entity_id": result if result else None
                }

            elif command_type == "draw_circle":
                center = parsed_command.get("center")
                radius = parsed_command.get("radius")
                # 색상 매개변수 가져오기
                color = parsed_command.get("color")
                # 명령에서 색상 이름을 추출해 RGB 값으로 변환 시도
                color_rgb = self.nlp_processor.extract_color_from_command(command)
                if color_rgb is not None:
                    color = color_rgb  # 명령에서 추출한 색상을 우선 사용
                # 선 굵기 매개변수 가져오기
                lineweight = parsed_command.get("lineweight")
                result = self.draw_circle(center, radius, None, color, lineweight)
                return {
                    "success": result is not None,
                    "message": "원이 그려졌습니다" if result else "원 그리기 실패",
                    "entity_id": result if result else None
                }

            elif command_type == "draw_arc":
                center = parsed_command.get("center")
                radius = parsed_command.get("radius")
                start_angle = parsed_command.get("start_angle")
                end_angle = parsed_command.get("end_angle")
                # 필요한 인자가 모두 존재하고 유효한지 확인
                if center is None or radius is None or start_angle is None or end_angle is None:
                    return {
                        "success": False,
                        "message": "호 그리기 실패: 필요한 인자 누락",
                        "error": "필요한 인자 누락: 중심점, 반지름, 시작각도 또는 끝각도"
                    }
                # 색상 매개변수 가져오기
                color = parsed_command.get("color")
                # 명령에서 색상 이름을 추출해 RGB 값으로 변환 시도
                color_rgb = self.nlp_processor.extract_color_from_command(command)
                if color_rgb is not None:
                    color = color_rgb  # 명령에서 추출한 색상을 우선 사용
                # 선 굵기 매개변수 가져오기
                lineweight = parsed_command.get("lineweight")
                result = self.draw_arc(center, radius, start_angle, end_angle, None, color, lineweight)

                return {
                    "success": result is not None,
                    "message": "호가 그려졌습니다" if result else "호 그리기 실패",
                    "entity_id": result if result else None
                }

            elif command_type == "draw_ellipse":
                center = parsed_command.get("center")
                major_axis = parsed_command.get("major_axis")
                minor_axis = parsed_command.get("minor_axis")
                rotation = parsed_command.get("rotation", 0)  # 기본 회전각도 0
                # 필요한 인자가 모두 존재하고 유효한지 확인
                if center is None or major_axis is None or minor_axis is None:
                    return {
                        "success": False,
                        "message": "타원 그리기 실패: 필요한 인자 누락",
                        "error": "필요한 인자 누락: 중심점, 장축 또는 단축"
                    }
                # 색상 매개변수 가져오기
                color = parsed_command.get("color")
                # 명령에서 색상 이름을 추출해 RGB 값으로 변환 시도
                color_rgb = self.nlp_processor.extract_color_from_command(command)
                if color_rgb is not None:
                    color = color_rgb  # 명령에서 추출한 색상을 우선 사용
                # 선 굵기 매개변수 가져오기
                lineweight = parsed_command.get("lineweight")
                result = self.draw_ellipse(center, major_axis, minor_axis, rotation, None, color, lineweight)

                return {
                    "success": result is not None,
                    "message": "타원이 그려졌습니다" if result else "타원 그리기 실패",
                    "entity_id": result if result else None
                }

            elif command_type == "draw_rectangle":
                corner1 = parsed_command.get("corner1")
                corner2 = parsed_command.get("corner2")
                # 색상 매개변수 가져오기
                color = parsed_command.get("color")
                # 명령에서 색상 이름을 추출해 RGB 값으로 변환 시도
                color_rgb = self.nlp_processor.extract_color_from_command(command)
                if color_rgb is not None:
                    color = color_rgb  # 명령에서 추출한 색상을 우선 사용
                # 선 굵기 매개변수 가져오기
                lineweight = parsed_command.get("lineweight")
                result = self.draw_rectangle(corner1, corner2, None, color, lineweight)

                return {
                    "success": result is not None,
                    "message": "사각형이 그려졌습니다" if result else "사각형 그리기 실패",
                    "entity_id": result if result else None
                }

            elif command_type == "draw_text":
                position = parsed_command.get("position")
                text = parsed_command.get("text")
                height = parsed_command.get("height", 2.5)
                rotation = parsed_command.get("rotation", 0)
                # 색상 매개변수 가져오기
                color = parsed_command.get("color")
                # 명령에서 색상 이름을 추출해 RGB 값으로 변환 시도
                color_rgb = self.nlp_processor.extract_color_from_command(command)
                if color_rgb is not None:
                    color = color_rgb  # 명령에서 추출한 색상을 우선 사용
                result = self.draw_text(position, text, height, rotation, None, color)

                return {
                    "success": result is not None,
                    "message": "텍스트가 추가되었습니다" if result else "텍스트 추가 실패",
                    "entity_id": result if result else None
                }

            elif command_type == "draw_hatch":
                points = parsed_command.get("points")
                pattern_name = parsed_command.get("pattern_name", "SOLID")
                scale = parsed_command.get("scale", 1.0)
                # 필요한 인자가 모두 존재하고 유효한지 확인
                if points is None or len(points) < 3:
                    return {
                        "success": False,
                        "message": "해치 그리기 실패: 필요한 인자 누락",
                        "error": "해치 경계는 최소 3개의 점이 필요합니다"
                    }
                # 색상 매개변수 가져오기 (인덱스 또는 RGB 값일 수 있음)
                color = parsed_command.get("color")  # 색상 매개변수 가져오기
                # 명령에서 색상 이름을 추출해 RGB 값으로 변환 시도
                color_rgb = self.nlp_processor.extract_color_from_command(command)
                if color_rgb is not None:
                    color = color_rgb  # 명령에서 추출한 색상을 우선 사용
                result = self.draw_hatch(points, pattern_name, scale, None, color)

                return {
                    "success": result is not None,
                    "message": "해치가 그려졌습니다" if result else "해치 그리기 실패",
                    "entity_id": result if result else None
                }

            # 치수 처리
            elif command_type == "add_dimension":
                start_point = parsed_command.get("start_point")
                end_point = parsed_command.get("end_point")
                text_position = parsed_command.get("text_position")
                result = self.controller.add_dimension(start_point, end_point, text_position)
                return {
                    "success": result is not None,
                    "message": "치수가 추가되었습니다" if result else "치수 추가 실패",
                    "entity_id": result if result else None
                }

            elif command_type == "save":
                file_path = parsed_command.get("file_path")
                result = self.save_drawing(file_path)

                return {
                    "success": result,
                    "message": f"도면이 {file_path}에 저장되었습니다" if result else f"{file_path}에 도면 저장 실패"
                }


            # 레이어 작업 처리
            elif command_type == "create_layer":
                layer_name = parsed_command.get("layer_name")
                color = parsed_command.get("color", 7)  # 기본 흰색
                result = self.controller.create_layer(layer_name)  # , color
                return {
                    "success": result,
                    "message": f"레이어 {layer_name}이(가) 생성되었습니다" if result else f"레이어 {layer_name} 생성 실패"
                }

        except Exception as e:
            return {
                "success": False,
                "message": f"명령 처리 중 오류 발생: {str(e)}"
            }

async def main():
    """메인 진입점 함수"""
    logger.info("CAD MCP 서버 시작")

    # 설정 로드
    config = Config()
    cad_service = CADService()
    server = Server(config.server_name)

    # 핸들러 등록
    logger.debug("핸들러 등록")

    @server.list_resources()
    async def handle_list_resources() -> list[types.Resource]:
        logger.debug("list_resources 요청 처리")
        return [
            types.Resource(
                uri=AnyUrl("drawing://current"),
                name="현재 CAD 도면",
                description="현재 CAD 도면의 상태",
                mimeType="application/json",
            )
        ]

    @server.read_resource()
    async def handle_read_resource(uri: AnyUrl) -> str:
        logger.debug(f"read_resource 요청 처리, URI: {uri}")
        if uri.scheme != "drawing":
            logger.error(f"지원하지 않는 URI 스킴: {uri.scheme}")
            raise ValueError(f"지원하지 않는 URI 스킴: {uri.scheme}")

        path = str(uri).replace("drawing://", "")
        if not path or path != "current":
            logger.error(f"알 수 없는 리소스 경로: {path}")
            raise ValueError(f"알 수 없는 리소스 경로: {path}")

        return json.dumps(cad_service.drawing_state)


    @server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        """사용 가능한 도구 목록"""
        return [
            types.Tool(
                name="draw_line",
                description="CAD에서 직선을 그린다",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "start_point": {
                            "type": "array",
                            "description": "시작점 좌표 [x, y, z]",
                            "items": {"type": "number"},
                            "minItems": 2,
                            "maxItems": 3
                        },
                        "end_point": {
                            "type": "array",
                            "description": "끝점 좌표 [x, y, z]",
                            "items": {"type": "number"},
                            "minItems": 2,
                            "maxItems": 3
                        },
                        "layer": {"type": "string", "description": "레이어 이름 (선택)"},
                        "color": {"type": "string", "description": "색상 이름 (선택)"},
                        "lineweight": {"type": "number", "description": "선 굵기 (선택)"}
                    },
                    "required": ["start_point", "end_point"],
                },
            ),

            types.Tool(
                name="draw_circle",
                description="CAD에서 원을 그린다",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "center": {
                            "type": "array",
                            "description": "중심점 좌표 [x, y, z]",
                            "items": {"type": "number"},
                            "minItems": 2,
                            "maxItems": 3
                        },
                        "radius": {"type": "number", "description": "원의 반지름"},
                        "layer": {"type": "string", "description": "레이어 이름 (선택)"},
                        "color": {"type": "string", "description": "색상 이름 (선택)"},
                        "lineweight": {"type": "number", "description": "선 굵기 (선택)"}
                    },
                    "required": ["center", "radius"],
                },
            ),

            types.Tool(
                name="draw_arc",
                description="CAD에서 호를 그린다",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "center": {
                            "type": "array",
                            "description": "중심점 좌표 [x, y, z]",
                            "items": {"type": "number"},
                            "minItems": 2,
                            "maxItems": 3
                        },
                        "radius": {"type": "number", "description": "호의 반지름"},
                        "start_angle": {"type": "number", "description": "시작 각도(도)"},
                        "end_angle": {"type": "number", "description": "끝 각도(도)"},
                        "layer": {"type": "string", "description": "레이어 이름 (선택)"},
                        "color": {"type": "string", "description": "색상 이름 (선택)"},
                        "lineweight": {"type": "number", "description": "선 굵기 (선택)"}
                    },
                    "required": ["center", "radius", "start_angle", "end_angle"],
                },
            ),

            types.Tool(
                name="draw_ellipse",
                description="CAD에서 타원을 그린다",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "center": {
                            "type": "array",
                            "description": "타원 중심 좌표 [x, y, z]",
                            "items": {"type": "number"},
                            "minItems": 2,
                            "maxItems": 3
                        },
                        "major_axis": {"type": "number", "description": "장축 길이"},
                        "minor_axis": {"type": "number", "description": "단축 길이"},
                        "rotation": {"type": "number", "description": "회전 각도(도) (선택)"},
                        "layer": {"type": "string", "description": "레이어 이름 (선택)"},
                        "color": {"type": "string", "description": "색상 이름 (선택)"},
                        "lineweight": {"type": "number", "description": "선 굵기 (선택)"}
                    },
                    "required": ["center", "major_axis", "minor_axis"],
                },
            ),

            types.Tool(
                name="draw_polyline",
                description="CAD에서 폴리라인을 그린다",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "points": {
                            "type": "array",
                            "description": "점 집합 [[x1, y1, z1], [x2, y2, z2], ...]",
                            "items": {
                                "type": "array",
                                "items": {"type": "number"},
                                "minItems": 2,
                                "maxItems": 3
                            },
                            "minItems": 2
                        },
                        "closed": {"type": "boolean", "description": "닫힘 여부"},
                        "layer": {"type": "string", "description": "레이어 이름 (선택)"},
                        "color": {"type": "string", "description": "색상 이름 (선택)"},
                        "lineweight": {"type": "number", "description": "선 굵기 (선택)"}
                    },
                    "required": ["points"],
                },
            ),

            types.Tool(
                name="draw_rectangle",
                description="CAD에서 사각형을 그린다",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "corner1": {
                            "type": "array",
                            "description": "첫 번째 모서리 좌표 [x, y, z]",
                            "items": {"type": "number"},
                            "minItems": 2,
                            "maxItems": 3
                        },
                        "corner2": {
                            "type": "array",
                            "description": "두 번째 모서리 좌표 [x, y, z]",
                            "items": {"type": "number"},
                            "minItems": 2,
                            "maxItems": 3
                        },
                        "layer": {"type": "string", "description": "레이어 이름 (선택)"},
                        "color": {"type": "string", "description": "색상 이름 (선택)"},
                        "lineweight": {"type": "number", "description": "선 굵기 (선택)"}
                    },
                    "required": ["corner1", "corner2"],
                },
            ),

            types.Tool(
                name="draw_text",
                description="CAD에 텍스트를 추가한다",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "position": {
                            "type": "array",
                            "description": "삽입점 좌표 [x, y, z]",
                            "items": {"type": "number"},
                            "minItems": 2,
                            "maxItems": 3
                        },
                        "text": {"type": "string", "description": "텍스트 내용"},
                        "height": {"type": "number", "description": "텍스트 높이"},
                        "rotation": {"type": "number", "description": "회전 각도(도)"},
                        "layer": {"type": "string", "description": "레이어 이름 (선택)"},
                        "color": {"type": "string", "description": "색상 이름 (선택)"}
                    },
                    "required": ["position", "text"],
                },
            ),
           
            types.Tool(
                name="draw_hatch",
                description="CAD에서 해치(채우기)를 그린다",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "points": {
                            "type": "array",
                            "description": "채우기 경계 점 집합 [[x1, y1, z1], [x2, y2, z2], ...]",
                            "items": {
                                "type": "array",
                                "items": {"type": "number"},
                                "minItems": 2,
                                "maxItems": 3
                            },
                            "minItems": 3
                        },
                        "pattern_name": {"type": "string", "description": "채우기 패턴 이름 (선택, 기본값 SOLID)"},
                        "scale": {"type": "number", "description": "채우기 패턴 비율 (선택, 기본값 1.0)"},
                        "layer": {"type": "string", "description": "레이어 이름 (선택)"},
                        "color": {"type": "string", "description": "색상 이름 (선택)"}
                    },
                    "required": ["points"],
                },
            ),

            types.Tool(
                name="add_dimension",
                description="CAD에 선형 치수를 추가한다",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "start_point": {"type": "array", "description": "시작점 좌표 [x, y, z]", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "end_point": {"type": "array", "description": "끝점 좌표 [x, y, z]", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "text_position": {"type": "array", "description": "텍스트 위치 좌표 [x, y, z], 선택", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "textheight": {"type": "number", "description": "치수 문자 높이, 선택"},
                        "layer": {"type": "string", "description": "레이어 이름, 선택"},
                        "color": {"type": "string", "description": "색상 이름, 선택"}
                    },
                    "required": ["start_point", "end_point"],
                },
            ),

            types.Tool(
                name="save_drawing",
                description="현재 도면을 저장한다",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "저장 경로"}
                    },
                    "required": ["file_path"],
                },
            ),

            types.Tool(
                name="process_command",
                description="자연어 명령을 처리하여 CAD 작업으로 변환한다",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "자연어 명령"}
                    },
                    "required": ["command"],
                },
            ),

            # ---------------- 읽기 도구 ----------------
            types.Tool(
                name="cad_status",
                description=(
                    "CAD 연결이 살아 있는지 빠르게 확인한다. CAD가 멈춰 있어도 "
                    "즉시 반환하므로, 다른 도구가 응답하지 않을 때 먼저 호출한다."
                ),
                inputSchema={"type": "object", "properties": {}},
            ),

            types.Tool(
                name="cad_reconnect",
                description=(
                    "막힌 COM 연결을 버리고 캐디안에 다시 연결한다. cad_status가 "
                    "responsive=false를 보고한 뒤, 사용자가 캐디안에서 ESC를 눌러 "
                    "명령을 취소한 다음에 호출한다."
                ),
                inputSchema={"type": "object", "properties": {}},
            ),

            types.Tool(
                name="get_drawing_info",
                description=(
                    "현재 도면의 전역 정보를 읽는다. 엔티티 개수, 도면 범위"
                    "(EXTMIN/EXTMAX), 레이어 목록, 현재 레이어, 단위. "
                    "도형을 그리기 전에 현재 상태를 확인하거나, 그린 뒤 "
                    "범위가 의도대로인지 검증할 때 사용."
                ),
                inputSchema={"type": "object", "properties": {}},
            ),

            types.Tool(
                name="list_entities",
                description=(
                    "도면에 있는 엔티티 목록을 읽는다. 각 항목은 종류"
                    "(ObjectName), 핸들, 레이어, 색상을 포함한다. 그린 결과의 "
                    "숫자를 검산하거나 특정 도형을 찾을 때 사용. 기본으로는 "
                    "좌표 배열(폴리라인 points, 해치 loops)을 빼고 요약만 "
                    "반환한다 — 엔티티가 많으면 좌표까지 포함한 응답이 통째로 "
                    "실패할 수 있다. 전체 도면이 의도대로 그려졌는지 눈으로 "
                    "확인하려면 이 도구로 다 덤프하지 말고 preview_drawing "
                    "(이미지, 엔티티 수와 무관하게 비용 고정)을 먼저 쓰고, "
                    "이 도구는 layer/entity_type으로 좁힌 소수 엔티티의 정확한 "
                    "좌표가 필요할 때만 include_geometry=true와 함께 쓴다."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "layer": {"type": "string",
                                  "description": "이 레이어의 엔티티만 (생략 시 전체)"},
                        "entity_type": {"type": "string",
                                        "description": "종류 필터. 예: Circle, Line, Text, Polyline"},
                        "limit": {"type": "integer",
                                  "description": "최대 반환 개수 (기본 100)"},
                        "include_geometry": {
                            "type": "boolean",
                            "description": (
                                "true면 points/loops 좌표 배열까지 포함한 "
                                "전체 정보를 반환 (기본 false, 요약만)"
                            ),
                        },
                    },
                },
            ),

            types.Tool(
                name="preview_drawing",
                description=(
                    "현재 도면을 PNG 이미지로 렌더링해서 반환한다. 도면을 눈으로 "
                    "확인해 도형 배치, 겹침, 축척 오류를 찾을 때 사용. 이미지 "
                    "하나의 비용은 엔티티 수와 무관하게 거의 고정이므로, 배치 "
                    "작도 후 전체가 의도대로 그려졌는지 확인할 때는 "
                    "list_entities로 다 덤프하는 대신 이 도구를 우선 쓴다. "
                    "zoom_to로 특정 영역만 확대할 수 있고, show_labels로 각 "
                    "엔티티의 핸들을 표시할 수 있다."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "width": {"type": "integer", "description": "이미지 폭 (기본 800, 최대 2000)"},
                        "height": {"type": "integer", "description": "이미지 높이 (기본 600, 최대 2000)"},
                        "show_labels": {"type": "boolean",
                                        "description": "엔티티 핸들 표시 (기본 false)"},
                        "zoom_to": {
                            "type": "array",
                            "items": {"type": "number"},
                            "description": "확대할 영역 [x0, y0, x1, y1] (생략 시 전체)"
                        },
                    },
                },
            ),

            # ---------------- 편집 도구 ----------------
            types.Tool(
                name="edit_entity",
                description=(
                    "이미 그려진 도형을 핸들로 지목해 편집한다. 핸들은 "
                    "list_entities로 얻는다. operation에 따라 필요한 인자가 다르다: "
                    "move는 offset, rotate는 base_point와 angle, scale은 "
                    "base_point와 factor, copy는 offset(선택), mirror는 point1과 "
                    "point2, offset은 distance, delete는 추가 인자 없음."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "handle": {"type": "string", "description": "대상 엔티티 핸들"},
                        "operation": {
                            "type": "string",
                            "enum": ["move", "rotate", "scale", "copy",
                                     "mirror", "offset", "delete"],
                        },
                        "offset": {"type": "array", "items": {"type": "number"},
                                   "description": "이동/복사 변위 [dx, dy]"},
                        "base_point": {"type": "array", "items": {"type": "number"},
                                       "description": "회전/축척 기준점 [x, y]"},
                        "angle": {"type": "number", "description": "회전 각도(도)"},
                        "factor": {"type": "number", "description": "축척 배율"},
                        "point1": {"type": "array", "items": {"type": "number"},
                                   "description": "대칭축 시작점"},
                        "point2": {"type": "array", "items": {"type": "number"},
                                   "description": "대칭축 끝점"},
                        "make_copy": {"type": "boolean",
                                      "description": "대칭 시 원본 유지 (기본 true)"},
                        "distance": {"type": "number",
                                     "description": "평행복사 거리. 부호가 방향"},
                    },
                    "required": ["handle", "operation"],
                },
            ),

            types.Tool(
                name="array_entity",
                description=(
                    "도형을 배열 복제한다. mode가 polar면 중심점 기준 원형 배열"
                    "(count, center, included_angle), rectangular면 격자 배열"
                    "(rows, columns, row_spacing, column_spacing). "
                    "볼트홀 배치나 규칙적 반복에 쓴다."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "handle": {"type": "string"},
                        "mode": {"type": "string", "enum": ["polar", "rectangular"]},
                        "count": {"type": "integer",
                                  "description": "원형 배열 총 개수(원본 포함)"},
                        "center": {"type": "array", "items": {"type": "number"},
                                   "description": "원형 배열 중심 [x, y]"},
                        "included_angle": {"type": "number",
                                           "description": "원형 배열 전체 각도(도). 기본 360"},
                        "rotate_objects": {"type": "boolean",
                                           "description": "복제본을 함께 회전 (기본 true)"},
                        "rows": {"type": "integer"},
                        "columns": {"type": "integer"},
                        "row_spacing": {"type": "number"},
                        "column_spacing": {"type": "number"},
                    },
                    "required": ["handle", "mode"],
                },
            ),

            # ---------------- 블록 도구 ----------------
            types.Tool(
                name="list_blocks",
                description=(
                    "정의된 블록 목록과 각 블록의 속성 태그를 보여준다. "
                    "블록을 삽입하기 전에 이름과 태그를 확인할 때 사용."
                ),
                inputSchema={"type": "object", "properties": {}},
            ),

            types.Tool(
                name="create_block",
                description=(
                    "재사용할 블록(심볼)을 정의한다. entities에 도형 목록을, "
                    "attributes에 인스턴스마다 값을 바꿀 속성 태그를 넣는다. "
                    "좌표는 블록 기준점(base_point) 기준의 상대 좌표로 쓴다. "
                    "정의해 두면 insert_block으로 위치와 속성값만 바꿔 반복 배치할 수 "
                    "있고, 나중에 블록 정의만 고치면 모든 인스턴스가 함께 바뀐다. "
                    "entities의 각 항목은 type이 line, circle, arc, ellipse, "
                    "rectangle, polyline, text 중 하나이고 해당 좌표 키를 갖는다."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "블록 이름"},
                        "base_point": {"type": "array", "items": {"type": "number"},
                                       "description": "삽입 기준점 [x, y] (기본 [0,0])"},
                        "entities": {
                            "type": "array",
                            "description": (
                                "도형 목록. 예: "
                                "[{\"type\":\"rectangle\",\"corner1\":[0,0],"
                                "\"corner2\":[40,20],\"color\":2}, "
                                "{\"type\":\"circle\",\"center\":[20,10],\"radius\":5}, "
                                "{\"type\":\"text\",\"position\":[2,22],"
                                "\"text\":\"고정문구\",\"height\":4}]"
                            ),
                            "items": {"type": "object"},
                        },
                        "attributes": {
                            "type": "array",
                            "description": (
                                "인스턴스마다 바뀌는 속성. 예: "
                                "[{\"tag\":\"NAME\",\"prompt\":\"장비명\","
                                "\"default\":\"미지정\",\"position\":[2,6],"
                                "\"height\":5}]"
                            ),
                            "items": {"type": "object"},
                        },
                    },
                    "required": ["name", "entities"],
                },
            ),

            types.Tool(
                name="insert_block",
                description=(
                    "정의된 블록을 도면에 배치한다. attributes로 이 인스턴스만의 "
                    "속성값을 지정한다. 예: attributes={\"NAME\": \"라우터\"}. "
                    "삽입된 인스턴스의 핸들을 반환하므로 나중에 편집할 수 있다."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "블록 이름"},
                        "position": {"type": "array", "items": {"type": "number"},
                                     "description": "삽입 위치 [x, y]"},
                        "scale": {"type": "number", "description": "축척 (기본 1.0)"},
                        "rotation": {"type": "number", "description": "회전 각도(도)"},
                        "attributes": {"type": "object",
                                       "description": "속성 태그별 값. 예: {\"NAME\": \"라우터\"}"},
                        "layer": {"type": "string", "description": "배치할 레이어"},
                    },
                    "required": ["name", "position"],
                },
            ),

            types.Tool(
                name="set_block_attributes",
                description=(
                    "이미 배치된 블록 인스턴스의 속성값을 바꾼다. 핸들은 "
                    "list_entities로 얻는다. 라벨만 수정할 때 사용."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "handle": {"type": "string"},
                        "attributes": {"type": "object",
                                       "description": "속성 태그별 새 값"},
                    },
                    "required": ["handle", "attributes"],
                },
            ),

            # ---------------- 배치 작도 / 삭제 ----------------
            types.Tool(
                name="draw_entities",
                description=(
                    "여러 도형을 한 번의 호출로 그린다. entities의 각 항목은 "
                    "create_block과 같은 형식: type이 line, circle, arc, "
                    "ellipse, rectangle, polyline, text 중 하나이고 해당 좌표 "
                    "키와 layer/color/lineweight를 가질 수 있다. 도형 하나마다 "
                    "따로 호출을 왕복하는 대신, 자료(그리드/치수/실명 등)에서 "
                    "뽑아낸 도형 목록을 한 번에 넘길 때 사용한다. 반환값은 "
                    "entities와 같은 순서의 핸들 목록이며, 실패한 항목은 "
                    "null로 남는다."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "entities": {
                            "type": "array",
                            "description": (
                                "도형 목록. 예: "
                                "[{\"type\":\"line\",\"start\":[0,0],\"end\":[40,0],"
                                "\"layer\":\"외벽\"}, "
                                "{\"type\":\"circle\",\"center\":[20,10],\"radius\":5}, "
                                "{\"type\":\"text\",\"position\":[2,22],"
                                "\"text\":\"101\",\"height\":4}]"
                            ),
                            "items": {"type": "object"},
                        },
                        "verbose": {
                            "type": "boolean",
                            "description": (
                                "true면 항목마다 핸들을 한 줄씩 나열 (기본 "
                                "false, 성공/실패 개수와 실패 항목만 표시). "
                                "이후에 개별 핸들을 수정/삭제할 계획이면 켠다."
                            ),
                        },
                    },
                    "required": ["entities"],
                },
            ),

            types.Tool(
                name="delete_by_layer",
                description=(
                    "지정한 레이어의 모든 엔티티를 삭제한다. 자료가 바뀌어 "
                    "도면을 다시 그려야 할 때, 이전에 그린 것을 레이어 단위로 "
                    "한 번에 지우는 용도. 삭제된 개수를 반환한다."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "layer": {"type": "string", "description": "삭제할 레이어 이름"},
                    },
                    "required": ["layer"],
                },
            ),

            types.Tool(
                name="delete_by_window",
                description=(
                    "사각형 범위 [x1,y1]-[x2,y2]에 걸리는 엔티티를 삭제한다. "
                    "mode가 crossing(기본)이면 범위와 조금이라도 겹치는 것까지, "
                    "inside면 범위 안에 완전히 들어온 것만 지운다. 특정 구역만 "
                    "다시 그릴 때 레이어 구분 없이 그 구역만 지우는 용도. "
                    "삭제된 개수를 반환한다."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "x1": {"type": "number"},
                        "y1": {"type": "number"},
                        "x2": {"type": "number"},
                        "y2": {"type": "number"},
                        "mode": {"type": "string", "enum": ["crossing", "inside"],
                                 "description": "crossing(기본) 또는 inside"},
                    },
                    "required": ["x1", "y1", "x2", "y2"],
                },
            ),
        ]

    @server.call_tool()
    async def handle_call_tool(
        name: str, arguments: dict[str, Any] | None
    ) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
        """도구 실행 요청 처리"""
        try:
            # 읽기 도구는 인자가 없어도 정상이다
            READ_ONLY_TOOLS = ("get_drawing_info", "list_entities", "preview_drawing",
                               "list_blocks", "cad_status", "cad_reconnect")
            if not arguments:
                if name not in READ_ONLY_TOOLS:
                    raise ValueError("인자가 없습니다")
                arguments = {}

            if name == "draw_line":
                start_point = arguments.get("start_point")
                end_point = arguments.get("end_point")
                layer = arguments.get("layer")
                color = arguments.get("color") 
                lineweight = arguments.get("lineweight")
                
                # 명령에서 색상 이름을 추출해 RGB 값으로 변환 시도
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color)
                if color_rgb is not None:
                    color = color_rgb  # 명령에서 추출한 색상을 우선 사용
                
                if not start_point or not end_point:
                    raise ValueError("시작점 또는 끝점 좌표가 없습니다")
                
                
                result = cad_service.draw_line(start_point, end_point, layer, color, lineweight)
                return [types.TextContent(type="text", text=str(result))]

            elif name == "draw_circle":
                center = arguments.get("center")
                radius = arguments.get("radius")
                layer = arguments.get("layer")
                color = arguments.get("color")
                lineweight = arguments.get("lineweight")
                
                # 명령에서 색상 이름을 추출해 RGB 값으로 변환 시도
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color)
                if color_rgb is not None:
                    color = color_rgb  # 명령에서 추출한 색상을 우선 사용
                
                if not center or radius is None:
                    raise ValueError("중심점 좌표 또는 반지름이 없습니다")
                
                
                result = cad_service.draw_circle(center, radius, layer, color, lineweight)
                return [types.TextContent(type="text", text=str(result))]

            elif name == "draw_arc":
                center = arguments.get("center")
                radius = arguments.get("radius")
                start_angle = arguments.get("start_angle")
                end_angle = arguments.get("end_angle")
                layer = arguments.get("layer")
                color = arguments.get("color")
                lineweight = arguments.get("lineweight")
                
                # 명령에서 색상 이름을 추출해 RGB 값으로 변환 시도
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color)
                if color_rgb is not None:
                    color = color_rgb  # 명령에서 추출한 색상을 우선 사용
                
                # 각 인자를 개별적으로 검사해 구체적인 오류 메시지를 제공
                error_msgs = []
                if not center:
                    error_msgs.append("중심점 좌표")
                if radius is None:
                    error_msgs.append("반지름")
                if start_angle is None:
                    error_msgs.append("시작 각도")
                if end_angle is None:
                    error_msgs.append("끝 각도")
                
                if error_msgs:
                    raise ValueError(f"필요한 인자 누락: {', '.join(error_msgs)}")                
                
                result = cad_service.draw_arc(center, radius, start_angle, end_angle, layer, color, lineweight)
                return [types.TextContent(type="text", text=str(result))]

            elif name == "draw_ellipse":
                center = arguments.get("center")
                major_axis = arguments.get("major_axis")
                minor_axis = arguments.get("minor_axis")
                rotation = arguments.get("rotation")
                layer = arguments.get("layer")
                color = arguments.get("color")
                lineweight = arguments.get("lineweight")
                
                # 명령에서 색상 이름을 추출해 RGB 값으로 변환 시도
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color)
                if color_rgb is not None:
                    color = color_rgb  # 명령에서 추출한 색상을 우선 사용
                
                # 각 인자를 개별적으로 검사해 구체적인 오류 메시지를 제공
                error_msgs = []
                if not center:
                    error_msgs.append("중심점 좌표")
                if major_axis is None:
                    error_msgs.append("장축")
                if minor_axis is None:
                    error_msgs.append("단축")
                
                if error_msgs:
                    raise ValueError(f"필요한 인자 누락: {', '.join(error_msgs)}")
                
                result = cad_service.draw_ellipse(center, major_axis, minor_axis, rotation, layer, color, lineweight)
                return [types.TextContent(type="text", text=str(result))]

            elif name == "draw_polyline":
                points = arguments.get("points")
                closed = arguments.get("closed", False)
                layer = arguments.get("layer")
                color = arguments.get("color")
                lineweight = arguments.get("lineweight")
                
                # 명령에서 색상 이름을 추출해 RGB 값으로 변환 시도
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color)
                if color_rgb is not None:
                    color = color_rgb  # 명령에서 추출한 색상을 우선 사용
                
                if not points or len(points) < 2:
                    raise ValueError("점 집합이 없거나 점 개수가 부족합니다")
                
                result = cad_service.draw_polyline(points, closed, layer, color, lineweight)
                return [types.TextContent(type="text", text=str(result))]

            elif name == "draw_rectangle":
                corner1 = arguments.get("corner1")
                corner2 = arguments.get("corner2")
                layer = arguments.get("layer")
                color = arguments.get("color")
                lineweight = arguments.get("lineweight")
                
                # 명령에서 색상 이름을 추출해 RGB 값으로 변환 시도
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color)
                if color_rgb is not None:
                    color = color_rgb  # 명령에서 추출한 색상을 우선 사용
                
                if not corner1 or not corner2:
                    raise ValueError("모서리 좌표가 없습니다")
                
                result = cad_service.draw_rectangle(corner1, corner2, layer, color, lineweight)
                return [types.TextContent(type="text", text=str(result))]


            elif name == "draw_text":
                position = arguments.get("position")
                text = arguments.get("text")
                height = arguments.get("height", 2.5)
                rotation = arguments.get("rotation", 0)
                layer = arguments.get("layer")
                color = arguments.get("color")
                
                # 명령에서 색상 이름을 추출해 RGB 값으로 변환 시도
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color)
                if color_rgb is not None:
                    color = color_rgb  # 명령에서 추출한 색상을 우선 사용
                
                if not position or not text:
                    raise ValueError("삽입점 좌표 또는 텍스트 내용이 없습니다")
                
                result = cad_service.draw_text(position, text, height, rotation, layer, color)
                return [types.TextContent(type="text", text=str(result))]


            elif name == "draw_hatch":
                points = arguments.get("points")
                pattern_name = arguments.get("pattern_name", "SOLID")
                scale = arguments.get("scale", 1.0)
                layer = arguments.get("layer")
                color = arguments.get("color")
                
                # 명령에서 색상 이름을 추출해 RGB 값으로 변환 시도
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color)
                if color_rgb is not None:
                    color = color_rgb  # 명령에서 추출한 색상을 우선 사용
                
                if not points or len(points) < 3:
                    raise ValueError("점 집합이 없거나 점 개수가 부족합니다. 채우기 경계는 최소 3개의 점이 필요합니다")
                
                result = cad_service.draw_hatch(points, pattern_name, scale, layer, color)
                return [types.TextContent(type="text", text=str(result))]

            elif name == "save_drawing":
                file_path = arguments.get("file_path")
                
                if not file_path:
                    raise ValueError("저장 경로가 없습니다")
                
                result = cad_service.save_drawing(file_path)
                return [types.TextContent(type="text", text=str(result))]

            elif name == "add_dimension":
                start_point = arguments.get("start_point")
                end_point = arguments.get("end_point")
                text_position = arguments.get("text_position")
                layer = arguments.get("layer")
                color = arguments.get("color")
                textheight = arguments.get("textheight")                

                # 명령에서 색상 이름을 추출해 RGB 값으로 변환 시도
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color)
                if color_rgb is not None:
                    color = color_rgb  # 명령에서 추출한 색상을 우선 사용

                # 각 인자를 개별적으로 검사해 구체적인 오류 메시지를 제공
                error_msgs = []
                if not start_point:
                    error_msgs.append("시작점 좌표")
                if not end_point:
                    error_msgs.append("끝점 좌표")

                if error_msgs:
                    raise ValueError(f"필요한 인자 누락: {', '.join(error_msgs)}")                

                result = cad_service.add_dimension(start_point, end_point, text_position, textheight, layer, color)
                return [types.TextContent(type="text", text=str(result))]

            elif name == "process_command":
                command = arguments.get("command")                

                if not command:
                    raise ValueError("명령이 없습니다")

                result = cad_service.process_command(command)
                return [types.TextContent(type="text", text=str(result))]

            # ---------------- 읽기 도구 ----------------
            elif name == "cad_status":
                status = cad_service.cad_status()
                return [types.TextContent(
                    type="text",
                    text=json.dumps(status, ensure_ascii=False, indent=2))]

            elif name == "cad_reconnect":
                status = cad_service.reconnect()
                return [types.TextContent(
                    type="text",
                    text="재연결 시도 결과:\n"
                         + json.dumps(status, ensure_ascii=False, indent=2))]

            elif name == "get_drawing_info":
                info = cad_service.get_drawing_info()
                return [types.TextContent(
                    type="text",
                    text=json.dumps(info, ensure_ascii=False, indent=2))]

            elif name == "list_entities":
                entities, total = cad_service.list_entities(
                    layer=arguments.get("layer"),
                    entity_type=arguments.get("entity_type"),
                    limit=arguments.get("limit", 100),
                    include_geometry=arguments.get("include_geometry", False))

                lines = [f"조건에 맞는 엔티티 {total}개"
                         + (f" (아래는 {len(entities)}개만 표시)"
                            if len(entities) < total else "")]
                for e in entities:
                    lines.append(json.dumps(e, ensure_ascii=False))
                return [types.TextContent(type="text", text="\n".join(lines))]

            elif name == "preview_drawing":
                png, summary = cad_service.preview_drawing(
                    width=arguments.get("width", 800),
                    height=arguments.get("height", 600),
                    show_labels=arguments.get("show_labels", False),
                    zoom_to=arguments.get("zoom_to"))

                return [
                    types.ImageContent(
                        type="image",
                        mimeType="image/png",
                        data=base64.b64encode(png).decode("ascii")),
                    types.TextContent(type="text", text=summary),
                ]

            # ---------------- 편집 / 블록 ----------------
            elif name == "edit_entity":
                handle = arguments.get("handle")
                operation = arguments.get("operation")
                if not handle or not operation:
                    raise ValueError("handle과 operation은 필수입니다")

                extras = {k: v for k, v in arguments.items()
                          if k not in ("handle", "operation")}
                result = cad_service.edit_entity(handle, operation, **extras)

                if result is False:
                    text = f"편집 실패: {operation} (핸들 {handle})"
                elif isinstance(result, str):
                    text = f"복사 완료. 새 핸들: {result}"
                else:
                    text = f"편집 완료: {operation} (핸들 {handle})"
                return [types.TextContent(type="text", text=text)]

            elif name == "array_entity":
                handle = arguments.get("handle")
                mode = arguments.get("mode")
                if not handle or not mode:
                    raise ValueError("handle과 mode는 필수입니다")

                extras = {k: v for k, v in arguments.items()
                          if k not in ("handle", "mode")}
                ok = cad_service.array_entity(handle, mode, **extras)
                return [types.TextContent(
                    type="text",
                    text=("배열 복제 완료" if ok else "배열 복제 실패")
                         + f" ({mode}, 핸들 {handle})")]

            elif name == "list_blocks":
                blocks = cad_service.list_blocks()
                if not blocks:
                    text = "정의된 블록이 없습니다."
                else:
                    text = json.dumps(blocks, ensure_ascii=False, indent=2)
                return [types.TextContent(type="text", text=text)]

            elif name == "create_block":
                block_name = arguments.get("name")
                entities = arguments.get("entities")
                if not block_name or not entities:
                    raise ValueError("name과 entities는 필수입니다")

                ok = cad_service.create_block(
                    block_name, entities,
                    arguments.get("attributes"),
                    arguments.get("base_point", (0, 0)))
                return [types.TextContent(
                    type="text",
                    text=(f"블록 '{block_name}' 정의 완료" if ok
                          else f"블록 '{block_name}' 정의 실패 (이미 존재하는지 확인)"))]

            elif name == "insert_block":
                block_name = arguments.get("name")
                position = arguments.get("position")
                if not block_name or not position:
                    raise ValueError("name과 position은 필수입니다")

                handle = cad_service.insert_block(
                    block_name, position,
                    arguments.get("scale", 1.0),
                    arguments.get("rotation", 0.0),
                    arguments.get("attributes"),
                    arguments.get("layer"))
                return [types.TextContent(
                    type="text",
                    text=(f"블록 '{block_name}' 삽입 완료. 핸들: {handle}" if handle
                          else f"블록 '{block_name}' 삽입 실패"))]

            elif name == "set_block_attributes":
                handle = arguments.get("handle")
                attributes = arguments.get("attributes")
                if not handle or not attributes:
                    raise ValueError("handle과 attributes는 필수입니다")

                ok = cad_service.set_block_attributes(handle, attributes)
                return [types.TextContent(
                    type="text",
                    text=(f"핸들 {handle} 속성 변경 완료" if ok
                          else f"핸들 {handle} 속성 변경 실패 (태그 이름 확인)"))]

            # ---------------- 배치 작도 / 삭제 ----------------
            elif name == "draw_entities":
                entities = arguments.get("entities")
                if not entities:
                    raise ValueError("entities는 필수입니다")

                handles = cad_service.draw_entities(entities)
                ok = sum(1 for h in handles if h)
                lines = [f"배치 작도: {len(entities)}개 중 {ok}개 성공"]

                if arguments.get("verbose", False):
                    for i, h in enumerate(handles):
                        lines.append(f"  [{i}] {h if h else '실패'}")
                else:
                    failed = [i for i, h in enumerate(handles) if not h]
                    if failed:
                        lines.append(f"실패한 인덱스: {failed}")
                    if ok:
                        lines.append("개별 핸들이 필요하면 verbose=true로 다시 "
                                     "호출하거나 list_entities로 조회하세요.")

                return [types.TextContent(type="text", text="\n".join(lines))]

            elif name == "delete_by_layer":
                layer = arguments.get("layer")
                if not layer:
                    raise ValueError("layer는 필수입니다")

                count = cad_service.delete_by_layer(layer)
                return [types.TextContent(
                    type="text",
                    text=f"레이어 '{layer}' 엔티티 {count}개 삭제 완료")]

            elif name == "delete_by_window":
                x1, y1 = arguments.get("x1"), arguments.get("y1")
                x2, y2 = arguments.get("x2"), arguments.get("y2")
                mode = arguments.get("mode", "crossing")
                if None in (x1, y1, x2, y2):
                    raise ValueError("x1, y1, x2, y2는 필수입니다")

                count = cad_service.delete_by_window(x1, y1, x2, y2, mode)
                return [types.TextContent(
                    type="text",
                    text=f"윈도우 ({x1},{y1})-({x2},{y2}) {mode}: {count}개 삭제 완료")]

            else:
                raise ValueError(f"알 수 없는 도구: {name}")

        except cad_worker.CADBusy as e:
            logger.error(f"CAD 응답 없음: {e}")
            return [types.TextContent(
                type="text",
                text=(f"CAD가 응답하지 않습니다.\n\n{e}\n\n"
                      "cad_status 도구로 상태를 확인하고, 사용자에게 캐디안 창에서 "
                      "ESC를 두어 번 누르도록 안내한 뒤 cad_reconnect를 호출하세요."))]

        except Exception as e:
            return [types.TextContent(type="text", text=f"오류: {str(e)}")]

    @server.list_prompts()
    async def handle_list_prompts() -> list[types.Prompt]:
        logger.debug("list_prompts 요청 처리")
        return [
            types.Prompt(
                name="cad-assistant",
                description="자연어로 CAD를 제어하는 어시스턴트",
                arguments=[],
            )
        ]

    @server.get_prompt()
    async def handle_get_prompt(name: str, arguments: dict[str, str] | None) -> types.GetPromptResult:
        logger.debug(f"get_prompt 요청 처리, 이름: {name}, 인자: {arguments}")
        if name != "cad-assistant":
            logger.error(f"알 수 없는 프롬프트: {name}")
            raise ValueError(f"알 수 없는 프롬프트: {name}")

        prompt = PROMPT_TEMPLATE
        logger.debug("프롬프트 템플릿 생성")

        return types.GetPromptResult(
            description="CAD 어시스턴트 프롬프트 템플릿",
            messages=[
                types.PromptMessage(
                    role="user",
                    content=types.TextContent(type="text", text=prompt.strip()),
                )
            ],
        )

    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        logger.info("서버가 stdio 전송을 사용하여 실행 중")
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name=config.server_name,
                server_version=config.server_version,
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())