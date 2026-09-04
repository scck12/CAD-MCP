import logging
import re
import math
from typing import Any, Dict, List, Optional, Tuple
import json
import os
import os.path

# config.json 파일을 직접 읽음
config_path = os.path.join(os.path.dirname(__file__), 'config.json')
with open(config_path, 'r', encoding='utf-8') as f:
    config = json.load(f)

logger = logging.getLogger('nlp_processor')

class NLPProcessor:
    """자연어 처리기 클래스, 사용자 명령을 파싱하여 CAD 작업으로 변환한다"""

    def __init__(self):
        """
        NLP 프로세서 초기화
        """

        self.logger = logging.getLogger(__name__)
        self.logger.info("자연어 처리기가 초기화되었습니다")

        # 구버전 CAD는 RGB 색상 설정을 지원하지 않으므로, 호환성을 위해 인덱스로 색상을 설정한다

        # 상세 색상 이름 -> RGB 값 매핑
        self.color_rgb_map = {
            # 기본 색상
            "红色": 1,
            "黄色": 2,
            "绿色": 3,
            "青色": 4,
            "蓝色": 5,
            "洋红色": 6,
            "白色": 7,
            "灰色": 8,
            "浅灰色": 9,
            "黑色": 250,
            "棕色": 251,
            "橙色": 30,
            "紫色": 200,
            "粉色": 221,


            # 기본 색상 - 영문 키
            "Red": 1,
            "Yellow": 2,
            "Green": 3,
            "Cyan": 4,
            "Blue": 5,
            "Magenta": 6,
            "White": 7,
            "Gray": 8,
            "Light Gray": 9,
            "Black": 250,
            "Brown": 251,
            "Orange": 30,
            "Purple": 200,
            "Pink": 221,

        }

        # 확장 키워드 매핑
        self.shape_keywords = {
            # 기본 형상
            "直线": "line", "线": "line",
            "圆": "circle", "圆形": "circle",
            "弧": "arc", "圆弧": "arc",
            "矩形": "rectangle", "方形": "rectangle", "正方形": "square",
            "多段线": "polyline", "折线": "polyline",
            "文本": "text", "文字": "text",
            "标注": "dimension", "尺寸标注": "dimension",

        }

        # 동작 키워드 매핑
        self.action_keywords = {
            # 기본 동작
            "画": "draw", "绘制": "draw", "创建": "draw", "添加": "draw",
            "修改": "modify", "调整": "modify", "改变": "modify",
            "移动": "move", "旋转": "rotate", "缩放": "scale",
            "放大": "scale_up", "缩小": "scale_down",
            "删除": "erase", "擦除": "erase", "移除": "erase",
            "保存": "save",

            # 전문 조작
            "标注": "dimension",
            "填充": "hatch",
            "创建图层": "create_layer",
            "切换图层": "change_layer"
        }


    def extract_color_from_command(self, command: str) -> Optional[int]:

        if command is None:
            return 7

        try:
            num = int(command)
            if num >= 1 and num <= 255:
                return num
        except:
            pass

        # 명령을 소문자로 변환
        command = command.lower()

        # 색상 이름 매칭 시도
        for color_name in self.color_rgb_map.keys():
            if color_name.lower() in command:
                return self.color_rgb_map[color_name]

        # 색상 설명(예: "淡蓝色" 같은 표현) 매칭 시도
        color_pattern = r'([深浅淡]?[a-zA-Z\u4e00-\u9fa5]+色)'
        color_matches = re.findall(color_pattern, command)

        for color_match in color_matches:
            # 알려진 색상 이름인지 확인
            if color_match in self.color_rgb_map:
                return self.color_rgb_map[color_match]

        # 색상 정보를 찾지 못하면 기본값 7(흰색)을 반환
        return 7

    def process_command(self, command: str) -> Dict[str, Any]:
        """자연어 명령을 처리하고 결과를 반환한다"""
        self.logger.info(f"명령 처리: {command}")

        # 명령 파싱
        parsed_command = self.parse_command(command)

        # 파싱 결과 반환
        return parsed_command

    def parse_command(self, command: str) -> Dict[str, Any]:
        """자연어 명령을 파싱하여 CAD 작업 매개변수로 변환한다"""
        self.logger.info(f"명령 파싱: {command}")

        # 명령을 소문자로 변환하고 여분의 공백 제거
        command = command.lower().strip()

        # 명령 유형 식별 시도
        command_type = self._identify_command_type(command)
        self.logger.debug(f"식별된 명령 유형: {command_type}")

        # 명령 유형에 따라 서로 다른 처리 함수로 분기
        if command_type == "draw_line":
            return self._parse_draw_line(command)
        elif command_type == "draw_circle":
            return self._parse_draw_circle(command)
        elif command_type == "draw_arc":
            return self._parse_draw_arc(command)
        elif command_type == "draw_rectangle":
            return self._parse_draw_rectangle(command)
        elif command_type == "draw_polyline":
            return self._parse_draw_polyline(command)
        elif command_type == "draw_text":
            return self._parse_draw_text(command)
        elif command_type == "draw_hatch":
            return self._parse_draw_hatch(command)
        elif command_type == "save":
            return self._parse_save(command)
        else:
            # 기본적으로 오류 결과를 반환
            return {
                "type": "unknown",
                "error": "인식할 수 없는 명령 유형입니다",
                "original_command": command
            }

    def _identify_command_type(self, command: str) -> str:
        """명령 유형을 식별한다"""
        # 동작 유형 확인
        for action, action_type in self.action_keywords.items():
            if action in command:
                # 기본 도형 처리
                for shape, shape_type in self.shape_keywords.items():
                    if shape in command:
                        if action_type == "draw":
                            if shape_type == "line":
                                return "draw_line"
                            elif shape_type == "circle":
                                return "draw_circle"
                            elif shape_type == "arc":
                                return "draw_arc"
                            elif shape_type in ["rectangle", "square"]:
                                return "draw_rectangle"
                            elif shape_type == "polyline":
                                return "draw_polyline"
                            elif shape_type == "text":
                                return "draw_text"
                            elif shape_type == "dimension":
                                return "add_dimension"

        # 레이어 생성 명령인지 확인
        if "图层" in command and any(action in command for action in ["创建", "新建", "添加"]):
            return "create_layer"

        # 치수 표기 명령인지 확인
        if "标注" in command:
            return "add_dimension"

        if "保存" in command:
            return "save"

        # 인식할 수 없으면 알 수 없는 유형을 반환
        return "unknown"

    def _extract_coordinates(self, text: str) -> List[Tuple[float, float, float]]:
        """텍스트에서 좌표점을 추출한다"""
        # 좌표 형식 매칭: (x,y,z) 또는 (x,y) 또는 x,y,z 또는 x,y
        pattern = r'\(?\s*(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)(?:\s*,\s*(-?\d+\.?\d*))?\s*\)?'
        matches = re.finditer(pattern, text)

        coordinates = []
        for match in matches:
            x = float(match.group(1))
            y = float(match.group(2))
            z = float(match.group(3)) if match.group(3) else 0.0
            coordinates.append((x, y, z))

        return coordinates

    def _extract_numbers(self, text: str) -> List[float]:
        """텍스트에서 숫자를 추출한다"""
        pattern = r'(-?\d+\.?\d*)'
        matches = re.findall(pattern, text)
        return [float(match) for match in matches]

    def _parse_draw_line(self, command: str) -> Dict[str, Any]:
        """직선 그리기 명령을 파싱한다"""
        # 좌표 추출 시도
        coordinates = self._extract_coordinates(command)

        if len(coordinates) >= 2:
            # 좌표점이 두 개 이상 발견되면 앞의 두 개를 시작점과 끝점으로 사용
            return {
                "type": "draw_line",
                "start_point": coordinates[0],
                "end_point": coordinates[1]
            }
        else:
            # 충분한 좌표점을 찾지 못하면 기본값 사용 시도
            # 여기서 필요에 따라 기본 시작점과 끝점을 설정할 수 있다
            return {
                "type": "draw_line",
                "start_point": (0, 0, 0),
                "end_point": (100, 100, 0),
                "note": "명령에 충분한 좌표 정보가 없어 기본 좌표를 사용했습니다"
            }

    def _parse_draw_circle(self, command: str) -> Dict[str, Any]:
        """원 그리기 명령을 파싱한다"""
        # 좌표와 반지름 추출 시도
        coordinates = self._extract_coordinates(command)
        numbers = self._extract_numbers(command)

        # 반지름 추출
        radius = None
        radius_pattern = r'(?:半径|r|radius)[^\d]*?(-?\d+\.?\d*)'
        radius_match = re.search(radius_pattern, command, re.IGNORECASE)
        if radius_match:
            radius = float(radius_match.group(1))
        elif len(numbers) > 0:
            # 반지름이 명시적으로 지정되지 않았으면 찾은 첫 번째 숫자를 반지름으로 사용
            radius = numbers[0]
        else:
            # 기본 반지름
            radius = 50.0

        # 중심점 추출
        center = None
        if len(coordinates) > 0:
            center = coordinates[0]
        else:
            # 기본 중심점
            center = (0, 0, 0)

        return {
            "type": "draw_circle",
            "center": center,
            "radius": radius
        }

    def _parse_draw_arc(self, command: str) -> Dict[str, Any]:
        """호(원호) 그리기 명령을 파싱한다"""
        # 좌표, 반지름, 각도 추출 시도
        coordinates = self._extract_coordinates(command)
        numbers = self._extract_numbers(command)

        # 중심점 추출
        center = None
        if len(coordinates) > 0:
            center = coordinates[0]
        else:
            # 기본 중심점
            center = (0, 0, 0)

        # 반지름 추출
        radius = None
        radius_pattern = r'(?:半径|r|radius)[^\d]*?(-?\d+\.?\d*)'
        radius_match = re.search(radius_pattern, command, re.IGNORECASE)
        if radius_match:
            radius = float(radius_match.group(1))
        elif len(numbers) > 0:
            # 반지름이 명시적으로 지정되지 않았으면 찾은 첫 번째 숫자를 반지름으로 사용
            radius = numbers[0]
        else:
            # 기본 반지름
            radius = 50.0

        # 시작 각도와 끝 각도 추출
        start_angle = 0.0
        end_angle = 90.0

        start_angle_pattern = r'(?:起始角度|start angle)[^\d]*?(-?\d+\.?\d*)'
        start_angle_match = re.search(start_angle_pattern, command, re.IGNORECASE)
        if start_angle_match:
            start_angle = float(start_angle_match.group(1))

        end_angle_pattern = r'(?:结束角度|end angle)[^\d]*?(-?\d+\.?\d*)'
        end_angle_match = re.search(end_angle_pattern, command, re.IGNORECASE)
        if end_angle_match:
            end_angle = float(end_angle_match.group(1))

        # 필요한 모든 매개변수에 값이 있는지 확인
        if center is None:
            center = (0, 0, 0)
        if radius is None:
            radius = 50.0
        if start_angle is None:
            start_angle = 0.0
        if end_angle is None:
            end_angle = 90.0

        self.logger.debug(f"호 명령 파싱 결과: 중심점={center}, 반지름={radius}, 시작각도={start_angle}, 끝각도={end_angle}")

        return {
            "type": "draw_arc",
            "center": center,
            "radius": radius,
            "start_angle": start_angle,
            "end_angle": end_angle
        }

    def _parse_draw_rectangle(self, command: str) -> Dict[str, Any]:
        """직사각형 그리기 명령을 파싱한다"""
        # 좌표 추출 시도
        coordinates = self._extract_coordinates(command)

        if len(coordinates) >= 2:
            # 좌표점이 두 개 이상 발견되면 앞의 두 개를 대각선 모서리점으로 사용
            return {
                "type": "draw_rectangle",
                "corner1": coordinates[0],
                "corner2": coordinates[1]
            }
        else:
            # 충분한 좌표점을 찾지 못하면 너비와 높이 추출 시도
            width = 100.0
            height = 100.0

            width_pattern = r'(?:宽度|width)[^\d]*?(-?\d+\.?\d*)'
            width_match = re.search(width_pattern, command, re.IGNORECASE)
            if width_match:
                width = float(width_match.group(1))

            height_pattern = r'(?:高度|height)[^\d]*?(-?\d+\.?\d*)'
            height_match = re.search(height_pattern, command, re.IGNORECASE)
            if height_match:
                height = float(height_match.group(1))

            # 좌표점이 하나 발견되면 그것을 시작점으로 사용
            if len(coordinates) == 1:
                corner1 = coordinates[0]
                corner2 = (corner1[0] + width, corner1[1] + height, corner1[2])
            else:
                # 기본 시작점과 끝점
                corner1 = (0, 0, 0)
                corner2 = (width, height, 0)

            return {
                "type": "draw_rectangle",
                "corner1": corner1,
                "corner2": corner2
            }

    def _parse_draw_polyline(self, command: str) -> Dict[str, Any]:
        """다중선(폴리라인) 그리기 명령을 파싱한다"""
        # 좌표 추출 시도
        coordinates = self._extract_coordinates(command)

        # 닫힘 여부 확인
        closed = "闭合" in command or "封闭" in command

        if len(coordinates) >= 2:
            # 좌표점이 두 개 이상 발견되면 이를 폴리라인의 점으로 사용
            return {
                "type": "draw_polyline",
                "points": coordinates,
                "closed": closed
            }
        else:
            # 충분한 좌표점을 찾지 못하면 오류를 반환
            return {
                "type": "error",
                "message": "다중선을 그리려면 최소 두 개의 좌표점이 필요합니다"
            }

    def _parse_draw_text(self, command: str) -> Dict[str, Any]:
        """텍스트 그리기 명령을 파싱한다"""
        # 좌표 추출 시도
        coordinates = self._extract_coordinates(command)

        # 텍스트 내용 추출
        text_pattern = r'[文本内容|text|内容][：:]\s*[\"\'](.*?)[\"\']'
        text_match = re.search(text_pattern, command)

        text = ""
        if text_match:
            text = text_match.group(1)
        else:
            # 따옴표 안의 내용을 텍스트로 추출 시도
            quote_pattern = r'[\"\'](.*?)[\"\']'
            quote_match = re.search(quote_pattern, command)
            if quote_match:
                text = quote_match.group(1)
            else:
                # 따옴표 안의 내용을 찾지 못하면 기본 텍스트 사용
                text = "예시 텍스트"

        # 텍스트 높이 추출
        height = 2.5  # 기본 높이
        height_pattern = r'(?:高度|height)[^\d]*?(-?\d+\.?\d*)'
        height_match = re.search(height_pattern, command, re.IGNORECASE)
        if height_match:
            height = float(height_match.group(1))

        # 회전 각도 추출
        rotation = 0.0  # 기본 각도
        rotation_pattern = r'(?:旋转|角度|rotation)[^\d]*?(-?\d+\.?\d*)'
        rotation_match = re.search(rotation_pattern, command, re.IGNORECASE)
        if rotation_match:
            rotation = float(rotation_match.group(1))

        # 삽입점 추출
        position = None
        if len(coordinates) > 0:
            position = coordinates[0]
        else:
            # 기본 삽입점
            position = (0, 0, 0)

        return {
            "type": "draw_text",
            "position": position,
            "text": text,
            "height": height,
            "rotation": rotation
        }

    def _parse_draw_hatch(self, command: str) -> Dict[str, Any]:
        """해치(채우기) 그리기 명령을 파싱한다"""
        # 좌표점 집합 추출 시도
        coordinates = self._extract_coordinates(command)

        # 채우기 패턴 이름 추출
        pattern_name = "SOLID"  # 기본값은 단색 채우기
        pattern_patterns = [
            r'(?:图案|pattern)[^\w]*?["\'](.*?)["\']\'',
            r'(?:图案|pattern)[^\w]*?(\w+)'
        ]

        for pattern in pattern_patterns:
            pattern_match = re.search(pattern, command, re.IGNORECASE)
            if pattern_match:
                pattern_name = pattern_match.group(1).upper()
                break

        # 채우기 비율 추출
        scale = 1.0  # 기본 비율
        scale_pattern = r'(?:比例|缩放|scale)[^\d]*?(\d+\.?\d*)'
        scale_match = re.search(scale_pattern, command, re.IGNORECASE)
        if scale_match:
            scale = float(scale_match.group(1))

        # 채우기 경계를 정의할 충분한 점이 있는지 확인
        if len(coordinates) >= 3:
            self.logger.debug(f"채우기 명령 파싱 결과: 점집합={coordinates}, 패턴={pattern_name}, 비율={scale}")
            return {
                "type": "draw_hatch",
                "points": coordinates,
                "pattern_name": pattern_name,
                "scale": scale
            }
        else:
            # 충분한 점이 없으면 오류 정보를 반환
            self.logger.warning("채우기 명령 파싱 실패: 채우기 경계를 정의하려면 최소 3개의 점이 필요합니다")
            return {
                "type": "error",
                "message": "채우기를 그리려면 경계를 정의할 최소 3개의 점이 필요합니다"
            }


    def _parse_save(self, command: str) -> Dict[str, Any]:
        """저장 명령을 파싱한다"""
        # 파일 경로 추출 시도
        path_pattern = r'(?:路径|保存到|path)[^\w]*?[\"\'](.*?)[\"\']'
        path_match = re.search(path_pattern, command, re.IGNORECASE)

        if path_match:
            file_path = path_match.group(1)
        else:
            # 기본 파일 경로
            file_path = os.path.join(config["output"]["directory"], config["output"]["default_filename"])

        return {
            "type": "save",
            "file_path": file_path
        }
