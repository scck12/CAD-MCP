import logging
import math
import time
import os
import json
from typing import Any, Dict, List, Optional, Tuple, Union

# config.json 파일을 직접 읽는다
config_path = os.path.join(os.path.dirname(__file__), 'config.json')
with open(config_path, 'r', encoding='utf-8') as f:
    config = json.load(f)

try:
    import win32com.client
    # pythoncom은 pywin32의 일부이므로 별도 설치가 필요 없다
    import pythoncom
except ImportError:
    logging.error("win32com.client 또는 pythoncom을 임포트할 수 없습니다. pywin32 라이브러리가 설치되어 있는지 확인하세요")
    raise

logger = logging.getLogger('cad_controller')

# ----------------------------------------------------------------------
# CAD 종류별 COM ProgID 매핑
#   CADian(캐디안)은 IntelliCAD 기반이라 ProgID가 "ICAD.Application"이다.
# ----------------------------------------------------------------------
APP_IDS = {
    "autocad":    ("AutoCAD.Application", "AutoCAD"),
    "gcad":       ("GCAD.Application",    "浩辰CAD"),
    "gstarcad":   ("GCAD.Application",    "浩辰CAD"),
    "zwcad":      ("ZWCAD.Application",   "中望CAD"),
    "cadian":     ("ICAD.Application",    "CADian"),
    "icad":       ("ICAD.Application",    "CADian/IntelliCAD"),
    "intellicad": ("ICAD.Application",    "IntelliCAD"),
}

# 좌표를 COM 객체(VT_DISPATCH)로 넘겨야 하는 계열
DISPATCH_POINT_TYPES = ("cadian", "icad", "intellicad")


class CADController:
    """CAD 컨트롤러 클래스. CAD 응용 프로그램과의 상호작용을 담당한다"""

    def __init__(self):
        """CAD 컨트롤러 초기화"""
        self.app = None
        self.doc = None
        self.entities = {}  # 생성된 도형의 엔티티 참조 저장 (후속 수정용)
        # 설정 파일에서 매개변수 로드
        self.startup_wait_time = config["cad"]["startup_wait_time"]
        self.command_delay = config["cad"]["command_delay"]
        # CAD 종류 가져오기
        self.cad_type = config["cad"]["type"]
        # 유효한 선두께 값 목록
        self.valid_lineweights = [0, 5, 9, 13, 15, 18, 20, 25, 30, 35, 40, 50, 53, 60, 70, 80, 90, 100, 106, 120, 140, 158, 200, 211]
        # 다중 점 인자 생성 전략 캐시 (CADian 전용, 최초 1회만 탐색)
        self._pts_strategy = None
        # 엔티티 속성명 캐시 (CAD별로 Color/color, LineWeight/Lineweight 등이 다름)
        self._color_prop = None
        self._lineweight_prop = None
        # AddHatch 인자 조합 캐시 (PatternType, HatchObjectType)
        self._hatch_args = None
        logger.info(f"CAD 컨트롤러 초기화 완료 (type={self.cad_type})")

    # ==================================================================
    # CADian(IntelliCAD) 호환 계층
    #
    # AutoCAD / 浩辰 / 中望 계열은 좌표를 VT_ARRAY|VT_R8 배열로 받는다.
    # 반면 CADian의 타입 라이브러리는 좌표를 COM 객체로 선언하고 있다.
    #
    #   IIcadModelSpace.AddCircle -> InvokeTypes(..., ((9,1),(5,1)), Center, Radius)
    #                                                  ^^^^^ 9 = VT_DISPATCH
    #
    # 이 객체는 Application.Library(IIcadLibrary)가 만들어 준다.
    #   Library.CreatePoint(x, y, z)  -> Point 객체
    #   Library.CreateVector(x, y, z) -> Vector 객체
    #   Library.CreatePoints()        -> 빈 Points 컬렉션
    #   Library.ConvertArrayToPoints(arr) -> 배열을 Points 컬렉션으로
    #
    # 따라서 아래 헬퍼들이 cad_type에 따라 배열/객체를 갈아끼운다.
    # 기존 CAD(AutoCAD 등) 동작은 그대로 유지된다.
    # ==================================================================

    def _is_dispatch_point_cad(self) -> bool:
        """좌표를 COM 객체로 넘겨야 하는 CAD인지"""
        return self.cad_type.lower() in DISPATCH_POINT_TYPES

    @staticmethod
    def _normalize(point) -> Tuple[float, float, float]:
        """2D/3D 좌표를 (x, y, z) float 튜플로 정규화"""
        x = float(point[0])
        y = float(point[1])
        z = float(point[2]) if len(point) > 2 else 0.0
        return (x, y, z)

    @staticmethod
    def _variant_doubles(values) -> Any:
        """double 배열 VARIANT 생성"""
        return win32com.client.VARIANT(
            pythoncom.VT_ARRAY | pythoncom.VT_R8,
            [float(v) for v in values])

    def _pt(self, point) -> Any:
        """단일 점 인자 생성 (AddLine / AddCircle / AddText / AddDimAligned 등)"""
        x, y, z = self._normalize(point)
        if self._is_dispatch_point_cad():
            return self.app.Library.CreatePoint(x, y, z)
        return self._variant_doubles((x, y, z))

    def _vec(self, x: float, y: float, z: float = 0.0) -> Any:
        """벡터 인자 생성 (AddEllipse의 장축 벡터 등)"""
        if self._is_dispatch_point_cad():
            return self.app.Library.CreateVector(float(x), float(y), float(z))
        return self._variant_doubles((x, y, z))

    def _pts(self, points: List[Tuple[float, float, float]]) -> Any:
        """다중 점 인자 생성 (AddPolyline 등)

        AutoCAD 계열: [x1,y1,z1, x2,y2,z2, ...] 평탄화 배열
        CADian:       Points 컬렉션 객체
        """
        normalized = [self._normalize(p) for p in points]
        flat = [c for p in normalized for c in p]

        if not self._is_dispatch_point_cad():
            return self._variant_doubles(flat)

        lib = self.app.Library

        # 캐시된 전략이 있으면 그대로 사용
        if self._pts_strategy == "convert":
            return lib.ConvertArrayToPoints(self._variant_doubles(flat), True)
        if self._pts_strategy == "collection":
            return self._pts_collection(normalized)

        # 최초 1회: 어떤 방식이 통하는지 탐색하고 캐시
        # IIcadLibrary.ConvertArrayToPoints(ArrayOfDoubles, Is3DPoint) - 인자 2개 필수
        try:
            result = lib.ConvertArrayToPoints(self._variant_doubles(flat), True)
            self._pts_strategy = "convert"
            logger.info("CADian 점 배열 전략: ConvertArrayToPoints")
            return result
        except Exception as e:
            logger.info(f"ConvertArrayToPoints 사용 불가, CreatePoints로 전환: {e}")

        result = self._pts_collection(normalized)
        self._pts_strategy = "collection"
        logger.info("CADian 점 배열 전략: CreatePoints + Add")
        return result

    def _pts_collection(self, normalized: List[Tuple[float, float, float]]) -> Any:
        """빈 Points 컬렉션을 만들고 점을 하나씩 추가

        IIcadPoints.Add(x, y, z, Index=-1)
        점 객체가 아니라 좌표 3개를 받는다는 점이 CreatePoint와 다르다.
        """
        pts = self.app.Library.CreatePoints()
        for x, y, z in normalized:
            pts.Add(x, y, z)
        return pts

    @staticmethod
    def _handle_of(entity: Any) -> Optional[str]:
        """엔티티에서 핸들(문자열)만 추출한다.

        이 값만이 작업 스레드 경계를 안전하게 넘을 수 있다. COM 엔티티
        객체(win32com Dispatch)를 그대로 반환하면, 호출자가 나중에 다른
        스레드에서 그 값을 만지는 순간(str(), 속성 접근, 심지어 GC가
        Release()를 호출하는 것만으로도) RPC_E_WRONG_THREAD
        (0x8001010E, "응용 프로그램이 다른 스레드를 위해 배열된 인터페이스를
        호출했습니다")가 발생한다. 실제로 도형은 정상적으로 그려졌는데도
        이 반환값 처리 단계에서 매번 실패하는 것이 이 버그의 정체였다.
        """
        if entity is None:
            return None
        try:
            return str(entity.Handle)
        except Exception:
            return None

    def _send_command(self, command: str) -> bool:
        """SendCommand 폴백. COM API가 막힌 경우의 최후 수단

        앞뒤로 ESC를 보내 이전/이후 명령이 대기 상태로 남지 않게 한다.
        명령이 걸린 채로 남으면 그 뒤의 모든 COM 호출이 무한 대기한다.
        """
        try:
            try:
                self.doc.SendCommand("\x1b\x1b")
            except Exception:
                pass
            self.doc.SendCommand(command)
            time.sleep(self.command_delay)
            return True
        except Exception as e:
            logger.error(f"SendCommand 실패: {str(e)}")
            return False

    def _resolve_app_id(self) -> Tuple[str, str]:
        """설정된 CAD 종류에 대응하는 ProgID와 표시명 반환"""
        key = self.cad_type.lower()
        if key in APP_IDS:
            return APP_IDS[key]
        logger.warning(f"알 수 없는 CAD 종류 '{self.cad_type}', AutoCAD로 처리합니다")
        return APP_IDS["autocad"]

    @staticmethod
    def _wrap_early_bound(dispatch_obj: Any) -> Any:
        """가능하면 얼리 바인딩(makepy 래퍼)으로 승격

        CADian은 타입 라이브러리가 필수적이다. 레이트 바인딩 상태에서는
        인자 타입 정보가 없어 AddCircle 등이 TypeError로 실패한다.
        """
        try:
            return win32com.client.gencache.EnsureDispatch(dispatch_obj)
        except Exception as e:
            logger.warning(f"얼리 바인딩 승격 실패, 레이트 바인딩으로 계속: {e}")
            return dispatch_obj

    def start_cad(self) -> bool:
        """CAD를 시작하고 문서를 새로 만들거나 연다"""
        old_app = None
        try:
            # COM 초기화
            pythoncom.CoInitialize()

            # 기존 인스턴스 참조를 저장해 두었다가 나중에 정리한다
            if self.app is not None:
                old_app = self.app
                self.app = None
                self.doc = None

            app_id, app_name = self._resolve_app_id()

            try:
                # 실행 중인 CAD 인스턴스에 연결을 시도한다
                logger.info(f"기존 {app_name} 인스턴스에 연결 시도... (ProgID={app_id})")
                try:
                    raw = win32com.client.GetActiveObject(app_id)
                    self.app = self._wrap_early_bound(raw)
                    logger.info(f"실행 중인 {app_name} 인스턴스에 연결 성공")
                except Exception as e:
                    logger.info(f"실행 중인 {app_name} 인스턴스를 찾지 못해 새 인스턴스 시작을 시도합니다: {str(e)}")
                    raise

                # 현재 문서가 없으면 새 문서를 만든다
                try:
                    if self.app.Documents.Count == 0:
                        logger.info("새 문서 생성 중...")
                        self.doc = self.app.Documents.Add()
                    else:
                        logger.info("활성 문서 가져오는 중...")
                        self.doc = self.app.ActiveDocument
                except Exception as doc_ex:
                    # 문서 가져오기에 실패하면 강제로 새 문서를 만든다
                    logger.warning(f"문서 가져오기 실패, 새 문서 생성 시도: {str(doc_ex)}")
                    try:
                        # 열려 있는 모든 문서를 닫는다
                        for i in range(self.app.Documents.Count):
                            try:
                                self.app.Documents.Item(0).Close(False)  # 저장하지 않음
                            except:
                                pass

                        # 새 문서 생성
                        self.doc = self.app.Documents.Add()
                    except Exception as new_doc_ex:
                        logger.error(f"새 문서 생성 실패: {str(new_doc_ex)}")
                        raise

            except Exception as app_ex:
                # 연결에 실패하면 새 인스턴스를 시작한다
                logger.info(f"연결 실패, 새 CAD 인스턴스를 시작합니다: {str(app_ex)}")
                try:
                    logger.info(f"{app_name} 인스턴스 시작 중... (ProgID={app_id})")
                    try:
                        self.app = win32com.client.gencache.EnsureDispatch(app_id)
                    except Exception as gen_ex:
                        logger.warning(f"EnsureDispatch 실패, Dispatch로 재시도: {gen_ex}")
                        self.app = win32com.client.Dispatch(app_id)
                    self.app.Visible = True

                    # CAD 시작 대기
                    time.sleep(self.startup_wait_time)  # 설정된 대기 시간 사용

                    # 문서를 가져오거나 생성
                    logger.info("문서 가져오기 시도 중...")
                    try:
                        if self.app.Documents.Count == 0:
                            self.doc = self.app.Documents.Add()
                        else:
                            self.doc = self.app.ActiveDocument
                    except Exception:
                        self.doc = self.app.ActiveDocument
                except Exception as new_app_ex:
                    logger.error(f"새 CAD 인스턴스 시작 실패: {str(new_app_ex)}")
                    raise

            # 추가 안전 확인 및 대기
            time.sleep(2)  # CAD가 문서 생성을 처리할 시간을 더 준다

            if self.doc is None:
                raise Exception("유효한 Document 객체를 가져올 수 없습니다")

            # 문서 속성을 읽어 유효성을 검증한다
            try:
                name = self.doc.Name
                logger.info(f"문서 이름: {name}")
            except Exception as name_ex:
                logger.error(f"문서 이름을 읽을 수 없습니다: {str(name_ex)}")
                raise Exception("문서 객체가 유효하지 않습니다")

            # CADian 계열은 Library 객체가 반드시 필요하므로 여기서 미리 확인
            if self._is_dispatch_point_cad():
                try:
                    _ = self.app.Library
                    logger.info("CADian Library 객체 확인 완료 (좌표는 COM 객체로 전달)")
                except Exception as lib_ex:
                    logger.error(f"Library 객체를 가져올 수 없습니다: {lib_ex}")
                    raise Exception("CADian Library 객체 사용 불가")

            logger.info("CAD 시작 및 준비 완료")
            return True

        except Exception as e:
            logger.error(f"CAD 시작 실패: {str(e)}")
            return False
        finally:
            # 기존 인스턴스 정리
            if old_app is not None:
                try:
                    del old_app
                except:
                    pass

    def is_running(self) -> bool:
        """CAD가 실행 중인지 확인"""
        return self.app is not None and self.doc is not None

    def save_drawing(self, file_path: str) -> bool:
        """현재 도면을 지정한 경로에 저장"""
        if not self.is_running():
            logger.error("CAD가 실행 중이지 않아 도면을 저장할 수 없습니다")
            return False

        try:
            # 디렉터리 존재 확인
            directory = os.path.dirname(file_path)
            if directory:
                os.makedirs(directory, exist_ok=True)

            # Regen만으로는 EXTMIN/EXTMAX가 갱신되지 않는다. 저장 전
            # zoom_extents로 도면 범위를 먼저 재계산해 둔다.
            self.zoom_extents()

            # 파일 저장
            self.doc.SaveAs(file_path)
            logger.info(f"도면이 저장되었습니다: {file_path}")

            return True
        except Exception as e:
            logger.error(f"도면 저장 실패: {str(e)}")
            return False

    def refresh_view(self) -> None:
        """CAD 뷰 갱신"""
        if self.is_running():
            try:
                self.doc.Regen(1)  # acAllViewports = 1
            except Exception as e:
                logger.error(f"뷰 갱신 실패: {str(e)}")

    def validate_lineweight(self, lineweight) -> int:
        """유효한 선두께 값을 검증하여 반환

        제공된 선두께 값이 유효값 목록에 없으면 기본값 0을 반환한다

        Args:
            lineweight: 검증할 선두께 값

        Returns:
            유효한 선두께 값
        """
        if lineweight is None:
            return None

        # 선두께 값이 유효값 목록에 있는지 확인
        if lineweight in self.valid_lineweights:
            return lineweight
        else:
            logger.warning(f"선두께 값 {lineweight}가 유효하지 않아 기본값 0을 사용합니다")
            return 0

    # 엔티티 속성 이름이 CAD별로 다르다.
    #   AutoCAD 계열 : Color      / LineWeight
    #   CADian       : color(소문자) / Lineweight(소문자 w)
    # 성공한 이름을 캐시해 매 도형마다 예외가 발생하지 않게 한다.
    COLOR_PROPS = ("Color", "color", "ColorIndex")
    LINEWEIGHT_PROPS = ("LineWeight", "Lineweight")

    def _set_entity_prop(self, entity: Any, candidates: Tuple[str, ...],
                         cache_attr: str, value: Any, label: str) -> bool:
        """후보 속성명을 순서대로 시도하고 성공한 이름을 캐시"""
        cached = getattr(self, cache_attr)
        names = (cached,) + tuple(n for n in candidates if n != cached) if cached else candidates

        for name in names:
            try:
                setattr(entity, name, value)
                if cached != name:
                    setattr(self, cache_attr, name)
                    logger.info(f"{label} 속성명 확정: '{name}'")
                return True
            except Exception:
                continue

        logger.warning(f"{label} 설정 실패({value}): 후보 {candidates} 모두 사용 불가")
        return False

    def _apply_style(self, entity: Any, layer: str = None,
                     color: int = None, lineweight=None) -> None:
        """레이어 / 색상 / 선두께 공통 적용

        일부 속성은 CAD 종류에 따라 이름이 다르거나 없으므로
        하나가 실패해도 나머지는 계속 적용한다.
        """
        if entity is None:
            return

        if layer:
            try:
                self.create_layer(layer)
                entity.Layer = layer
            except Exception as e:
                logger.warning(f"레이어 설정 실패({layer}): {e}")

        if color is not None:
            self._set_entity_prop(entity, self.COLOR_PROPS,
                                  '_color_prop', color, "색상")

        if lineweight is not None:
            self._set_entity_prop(entity, self.LINEWEIGHT_PROPS,
                                  '_lineweight_prop',
                                  self.validate_lineweight(lineweight), "선두께")

    def draw_line(self, start_point: Tuple[float, float, float],
                  end_point: Tuple[float, float, float], layer: str = None,
                  color: int = None, lineweight=None) -> Optional[str]:
        """직선 그리기"""
        if not self.is_running():
            return None

        try:
            start_point = self._normalize(start_point)
            end_point = self._normalize(end_point)

            # 좌표 인자 생성 (CAD 종류에 따라 배열 또는 COM 객체)
            line = self.doc.ModelSpace.AddLine(self._pt(start_point), self._pt(end_point))

            self._apply_style(line, layer, color, lineweight)

            # 뷰 갱신
            self.refresh_view()

            logger.debug(f"직선 그리기 완료: 시작점{start_point}, 끝점{end_point}, 레이어{layer if layer else '기본값'}, 색상{color if color is not None else '기본값'}")
            return self._handle_of(line)

        except Exception as e:
            logger.error(f"직선 그리기 중 오류 발생: {str(e)}")
            return None

    def draw_circle(self, center: Tuple[float, float, float],
                    radius: float, layer: str = None, color: int = None,
                    lineweight=None) -> Optional[str]:
        """원 그리기"""
        if not self.is_running():
            return None

        try:
            center = self._normalize(center)

            circle = self.doc.ModelSpace.AddCircle(self._pt(center), float(radius))

            self._apply_style(circle, layer, color, lineweight)

            # 뷰 갱신
            self.refresh_view()

            logger.debug(f"원 그리기 완료: 중심{center}, 반지름{radius}, 레이어{layer if layer else '기본값'}, 색상{color if color is not None else '기본값'}")
            return self._handle_of(circle)

        except Exception as e:
            logger.error(f"원 그리기 중 오류 발생: {str(e)}")
            return None

    def draw_arc(self, center: Tuple[float, float, float],
                 radius: float, start_angle: float, end_angle: float,
                 layer: str = None, color: int = None, lineweight=None) -> Optional[str]:
        """호 그리기"""
        if not self.is_running():
            return None

        try:
            center = self._normalize(center)

            # 각도를 라디안으로 변환
            start_rad = math.radians(start_angle)
            end_rad = math.radians(end_angle)

            arc = self.doc.ModelSpace.AddArc(
                self._pt(center), float(radius), start_rad, end_rad)

            self._apply_style(arc, layer, color, lineweight)

            # 뷰 갱신
            self.refresh_view()

            logger.debug(f"호 그리기 완료: 중심{center}, 반지름{radius}, 시작각도{start_angle}, 끝각도{end_angle}, 레이어{layer if layer else '기본값'}, 색상{color if color is not None else '기본값'}")
            return self._handle_of(arc)
        except Exception as e:
            logger.error(f"호 그리기 실패: {str(e)}")
            return None

    def draw_ellipse(self, center: Tuple[float, float, float],
                     major_axis: float, minor_axis: float, rotation: float = 0,
                     layer: str = None, color: int = None, lineweight=None) -> Optional[str]:
        """타원 그리기"""
        if not self.is_running():
            return None

        try:
            center = self._normalize(center)

            if rotation is None:
                rotation = 0

            # 회전각을 라디안으로 변환
            rotation_rad = math.radians(rotation)

            # 타원 장축 벡터 계산
            major_x = major_axis * math.cos(rotation_rad)
            major_y = major_axis * math.sin(rotation_rad)

            ellipse = self.doc.ModelSpace.AddEllipse(
                self._pt(center),
                self._vec(major_x, major_y, 0),
                minor_axis / major_axis)

            self._apply_style(ellipse, layer, color, lineweight)

            # 뷰 갱신
            self.refresh_view()

            logger.debug(f"타원 그리기 완료: 중심{center}, 장축{major_axis}, 단축{minor_axis}, 회전각{rotation}, 레이어{layer if layer else '기본값'}, 색상{color if color is not None else '기본값'}")
            return self._handle_of(ellipse)
        except Exception as e:
            logger.error(f"타원 그리기 실패: {str(e)}")
            return None

    def _add_polyline(self, normalized: List[Tuple[float, float, float]]) -> Any:
        """폴리라인 생성. CAD별로 메서드 이름과 인자 형태가 달라 순차 시도"""
        ms = self.doc.ModelSpace
        attempts = []

        # 1순위: 표준 AddPolyline (CADian은 Points 컬렉션, 그 외는 평탄화 배열)
        try:
            return ms.AddPolyline(self._pts(normalized))
        except Exception as e:
            attempts.append(f"AddPolyline: {e}")

        # 2순위: 경량 폴리라인 (2D 좌표만 사용)
        try:
            if self._is_dispatch_point_cad():
                arg = self._pts(normalized)
            else:
                flat2d = [c for p in normalized for c in (p[0], p[1])]
                arg = self._variant_doubles(flat2d)
            return ms.AddLightWeightPolyline(arg)
        except Exception as e:
            attempts.append(f"AddLightWeightPolyline: {e}")

        # 3순위: 3D 폴리라인
        try:
            return ms.Add3DPoly(self._pts(normalized))
        except Exception as e:
            attempts.append(f"Add3DPoly: {e}")

        logger.error("폴리라인 생성 실패. 시도 내역: " + " | ".join(attempts))
        return None

    def _make_polyline(self, points: List[Tuple[float, float, float]],
                       closed: bool = False, layer: str = None,
                       color: int = None, lineweight=None) -> Any:
        """폴리라인을 그리고 COM 엔티티 객체를 그대로 반환한다 (내부 전용).

        draw_hatch가 경계선을 만든 뒤 그 COM 객체를 AppendOuterLoop에 직접
        넘겨야 하므로 핸들 문자열로는 부족하다. 같은 작업 스레드 안에서
        끝나는 호출이라 엔티티를 그대로 다뤄도 안전하다. 작업 스레드 밖으로
        나가는 공개 API는 draw_polyline처럼 반드시 핸들만 반환해야 한다.
        """
        if not self.is_running():
            return None

        try:
            # 모든 점을 3차원으로 정규화
            processed_points = [self._normalize(p) for p in points]

            polyline = self._add_polyline(processed_points)
            if polyline is None:
                return None

            # 닫힘 처리가 필요한 경우
            if closed and len(processed_points) > 2:
                try:
                    polyline.Closed = True
                except Exception as e:
                    logger.warning(f"폴리라인 닫기 실패: {e}")

            self._apply_style(polyline, layer, color, lineweight)

            # 뷰 갱신
            self.refresh_view()

            logger.debug(f"폴리라인 그리기 완료: {len(points)}개 점, {'닫힘' if closed else '열림'}, 레이어{layer if layer else '기본값'}, 색상{color if color is not None else '기본값'}")
            return polyline
        except Exception as e:
            logger.error(f"폴리라인 그리기 중 오류 발생: {str(e)}")
            return None

    def draw_polyline(self, points: List[Tuple[float, float, float]],
                      closed: bool = False, layer: str = None,
                      color: int = None, lineweight=None) -> Optional[str]:
        """폴리라인 그리기"""
        entity = self._make_polyline(points, closed, layer, color, lineweight)
        return self._handle_of(entity)

    def draw_rectangle(self, corner1: Tuple[float, float, float],
                       corner2: Tuple[float, float, float], layer: str = None,
                       color: int = None, lineweight=None) -> Optional[str]:
        """직사각형 그리기"""
        if not self.is_running():
            return None

        try:
            corner1 = self._normalize(corner1)
            corner2 = self._normalize(corner2)

            # 직사각형의 네 꼭짓점 계산
            x1, y1, z1 = corner1
            x2, y2, z2 = corner2

            # 직사각형의 네 점 생성
            points = [
                (x1, y1, z1),
                (x2, y1, z1),
                (x2, y2, z1),
                (x1, y2, z1),
                (x1, y1, z1)  # 직사각형 닫기
            ]

            # 폴리라인으로 직사각형 그리기
            return self.draw_polyline(points, True, layer, color, lineweight)
        except Exception as e:
            logger.error(f"직사각형 그리기 중 오류 발생: {str(e)}")
            return None

    def draw_text(self, position: Tuple[float, float, float],
                  text: str, height: float = 2.5, rotation: float = 0,
                  layer: str = None, color: int = None) -> Optional[str]:
        """텍스트 추가"""
        if not self.is_running():
            return None

        try:
            position = self._normalize(position)

            # AddText(TextString, InsertionPoint, Height)
            text_obj = self.doc.ModelSpace.AddText(
                text, self._pt(position), float(height))

            # 회전각 설정
            if rotation:
                try:
                    text_obj.Rotation = math.radians(rotation)
                except Exception as e:
                    logger.warning(f"텍스트 회전 설정 실패: {e}")

            self._apply_style(text_obj, layer, color, None)

            # 뷰 갱신
            self.refresh_view()

            logger.debug(f"텍스트 추가 완료: '{text}', 위치{position}, 높이{height}, 회전{rotation}도, 레이어{layer if layer else '기본값'}, 색상{color if color is not None else '기본값'}")
            return self._handle_of(text_obj)
        except Exception as e:
            logger.error(f"텍스트 추가 중 오류 발생: {str(e)}")
            return None

    def _try_delete(self, entity: Any) -> None:
        """실패한 중간 산출물 제거 (지원 메서드가 CAD별로 달라 순차 시도)"""
        if entity is None:
            return
        for name in ("Delete", "Erase"):
            try:
                getattr(entity, name)()
                return
            except Exception:
                continue

    def _create_hatch(self, pattern_name: str) -> Any:
        """해치 객체 생성

        IIcadModelSpace.AddHatch(PatternType, PatternName, Associativity, HatchObjectType)
          - PatternType: 0=사용자정의, 1=미리정의, 2=커스텀
            "SOLID" 같은 표준 패턴은 1이어야 한다.
          - HatchObjectType: 기본값이 있는 VARIANT 인자지만 생략하면
            E_INVALIDARG로 거부하는 구현이 있어 명시 전달도 시도한다.
        성공한 조합은 캐시한다.
        """
        ms = self.doc.ModelSpace

        if self._hatch_args is not None:
            pattern_type, extra = self._hatch_args
            if extra is None:
                return ms.AddHatch(pattern_type, pattern_name, True)
            return ms.AddHatch(pattern_type, pattern_name, True, extra)

        combos = [
            (1, 0),      # 미리정의 + HatchObjectType 명시
            (1, None),   # 미리정의 + 3인자
            (0, 0),      # 사용자정의 + 명시
            (0, None),   # 원본 동작
            (2, 0),      # 커스텀
        ]

        errors = []
        for pattern_type, extra in combos:
            try:
                if extra is None:
                    hatch = ms.AddHatch(pattern_type, pattern_name, True)
                else:
                    hatch = ms.AddHatch(pattern_type, pattern_name, True, extra)
                self._hatch_args = (pattern_type, extra)
                logger.info(f"AddHatch 인자 조합 확정: PatternType={pattern_type}, "
                            f"HatchObjectType={'생략' if extra is None else extra}")
                return hatch
            except Exception as e:
                errors.append(f"({pattern_type}, {extra}): {str(e)[:60]}")

        logger.error("AddHatch 실패. 시도 내역: " + " | ".join(errors))
        logger.error(f"ModelSpace 해치 관련 메서드: "
                     f"{[n for n in dir(ms) if 'atch' in n]}")
        return None

    def _append_outer_loop(self, hatch: Any, boundary: Any) -> bool:
        """해치 외부 경계 추가

        메서드 이름과 인자 형태가 CAD별로 다르므로 실제 객체에서
        후보를 찾아 순서대로 시도한다.
        """
        available = [n for n in dir(hatch) if 'oop' in n]
        logger.debug(f"해치 루프 메서드 후보: {available}")

        ordered = [n for n in ("AppendOuterLoop", "AppendLoop", "InsertLoop")
                   if n in available]
        ordered += [n for n in available
                    if n not in ordered and n.lower().startswith('append')
                    and 'inner' not in n.lower()]
        if not ordered:
            logger.error(f"해치 경계 추가 메서드를 찾을 수 없습니다. dir: {available}")
            return False

        arg_forms = [
            ("VT_ARRAY|VT_DISPATCH",
             win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, [boundary])),
            ("VT_ARRAY|VT_VARIANT",
             win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_VARIANT, [boundary])),
            ("단일 객체", boundary),
        ]

        errors = []
        for method_name in ordered:
            fn = getattr(hatch, method_name, None)
            if fn is None:
                continue
            for describe, arg in arg_forms:
                try:
                    fn(arg)
                    logger.info(f"해치 경계 추가 성공: {method_name}({describe})")
                    return True
                except Exception as e:
                    errors.append(f"{method_name}/{describe}: {str(e)[:60]}")

        logger.error("해치 경계 추가 실패: " + " | ".join(errors))
        return False

    def draw_hatch(self, points: List[Tuple[float, float, float]],
                   pattern_name: str = "SOLID", scale: float = 1.0,
                   layer: str = None, color: int = None) -> Optional[str]:
        """해치(채우기 패턴) 그리기

        Args:
            points: 해치 경계를 이루는 점 목록. 각 점은 2차원 또는 3차원 좌표 튜플
            pattern_name: 해치 패턴 이름, 기본값은 "SOLID"(단색 채우기)
            scale: 해치 패턴 비율, 기본값은 1.0
            layer: 레이어 이름, None이면 현재 레이어 사용
            color: 색상 인덱스, None이면 기본 색상 사용

        Returns:
            성공 시 해치 객체의 핸들, 실패 시 None
        """
        if not self.is_running():
            return None

        try:
            # 모든 점이 유효한지 확인
            if not points or len(points) < 3:
                logger.error("해치 생성 실패: 경계를 정의하려면 최소 3개의 점이 필요합니다")
                return None

            # 경계로 사용할 닫힌 폴리라인 생성 (경계 COM 객체 자체가 필요하므로 _make_polyline 사용)
            closed_polyline = self._make_polyline(points, closed=True, layer=layer)
            if not closed_polyline:
                logger.error("해치 생성 실패: 경계 폴리라인을 만들 수 없습니다")
                return None

            # 해치 객체 생성 (경계와 연결)
            hatch = self._create_hatch(pattern_name)
            if hatch is None:
                self._try_delete(closed_polyline)
                return None

            # 외부 경계 루프 추가
            if not self._append_outer_loop(hatch, closed_polyline):
                self._try_delete(hatch)
                return None

            # 해치 패턴 비율 설정
            try:
                hatch.PatternScale = scale
            except Exception as e:
                logger.warning(f"해치 스케일 설정 실패: {e}")

            self._apply_style(hatch, layer, color, None)

            # 해치 갱신 (채우기 영역 계산)
            # AutoCAD는 Evaluate, IntelliCAD 계열은 Update를 쓴다
            refreshed = False
            for name in ("Evaluate", "Update", "Regen"):
                fn = getattr(hatch, name, None)
                if fn is None:
                    continue
                try:
                    fn()
                    logger.debug(f"해치 갱신: {name}()")
                    refreshed = True
                    break
                except Exception:
                    continue
            if not refreshed:
                logger.debug("해치 갱신 메서드 없음 (자동 계산되는 구현으로 판단)")

            # 뷰 갱신
            self.refresh_view()

            logger.debug(f"해치 생성 완료: 패턴 {pattern_name}, 비율 {scale}, 레이어{layer if layer else '기본값'}, 색상{color if color is not None else '기본값'}")
            return self._handle_of(hatch)
        except Exception as e:
            logger.error(f"해치 생성 중 오류 발생: {str(e)}")
            return None

    def zoom_extents(self) -> bool:
        """모든 객체가 보이도록 뷰 확대/축소"""
        if not self.is_running():
            return False

        # CAD별로 진입점이 달라 순차 시도
        try:
            self.doc.ActiveViewport.ZoomExtents()
            logger.info("모든 객체가 보이도록 뷰를 확대/축소했습니다")
            return True
        except Exception as e1:
            try:
                self.app.ZoomExtents()
                logger.info("모든 객체가 보이도록 뷰를 확대/축소했습니다 (Application.ZoomExtents)")
                return True
            except Exception as e2:
                if self._send_command("_ZOOM\n_E\n"):
                    logger.info("모든 객체가 보이도록 뷰를 확대/축소했습니다 (SendCommand)")
                    return True
                logger.error(f"뷰 확대/축소 중 오류 발생: {e1} / {e2}")
                return False

    def close(self) -> None:
        """CAD 컨트롤러 종료"""
        try:
            # COM 리소스 해제
            if self.app is not None:
                del self.app
            pythoncom.CoUninitialize()
        except:
            pass

    def create_layer(self, layer_name: str) -> bool:    # , color: Union[int, Tuple[int, int, int]] = 7
        """새 레이어 생성

        Args:
            layer_name: 레이어 이름

        Returns:
            작업 성공 여부
        """
        if not self.is_running():
            return False

        try:
            # 이름으로 직접 조회 (인덱스 기준이 CAD별로 달라 이 방식이 안전)
            try:
                existing = self.doc.Layers.Item(layer_name)
                if existing is not None:
                    self.doc.ActiveLayer = existing
                    return True
            except Exception:
                pass

            # 이름 조회가 안 되는 구현이면 컬렉션을 순회
            try:
                for layer in self.doc.Layers:
                    if layer.Name == layer_name:
                        self.doc.ActiveLayer = layer
                        return True
            except Exception:
                # 열거자를 지원하지 않으면 인덱스로 (0-base, 1-base 모두 시도)
                try:
                    count = self.doc.Layers.Count
                    for base in (0, 1):
                        for i in range(base, count + base):
                            try:
                                layer = self.doc.Layers.Item(i)
                            except Exception:
                                break
                            if layer.Name == layer_name:
                                self.doc.ActiveLayer = layer
                                return True
                except Exception:
                    pass

            # 새 레이어 생성
            new_layer = self.doc.Layers.Add(layer_name)

            # 현재 레이어로 설정
            self.doc.ActiveLayer = new_layer
            logger.info(f"새 레이어 생성 완료: {layer_name}")
            return True
        except Exception as e:
            logger.error(f"레이어 생성 중 오류 발생: {str(e)}")
            return False

    def add_dimension(self, start_point: Tuple[float, float, float],
                      end_point: Tuple[float, float, float],
                      text_position: Tuple[float, float, float] = None,
                      textheight: float = 5, layer: str = None,
                      color: int = None) -> Optional[str]:
        """선형 치수 추가"""
        if not self.is_running():
            return None

        try:
            start_point = self._normalize(start_point)
            end_point = self._normalize(end_point)

            # 텍스트 위치가 제공되지 않으면 자동으로 계산
            if text_position is None:
                # 시작점과 끝점 사이 중점 위쪽
                mid_x = (start_point[0] + end_point[0]) / 2
                mid_y = (start_point[1] + end_point[1]) / 2
                text_position = (mid_x, mid_y + 5, 0)
            text_position = self._normalize(text_position)

            # 정렬 치수 추가
            dimension = self.doc.ModelSpace.AddDimAligned(
                self._pt(start_point), self._pt(end_point), self._pt(text_position))

            # 문자 높이 설정
            if textheight is not None:
                try:
                    dimension.TextHeight = textheight
                except Exception as e:
                    logger.warning(f"치수 문자 높이 설정 실패: {e}")

            self._apply_style(dimension, layer, color, None)

            # 뷰 갱신
            self.refresh_view()

            logger.info(f"치수 추가 완료: {start_point}에서 {end_point}까지, 레이어{layer if layer else '기본값'}")
            return self._handle_of(dimension)
        except Exception as e:
            logger.error(f"치수 추가 중 오류 발생: {str(e)}")
            return None

    # ==================================================================
    # 편집 도구
    #
    # 확정된 시그니처 (IIcadLine 등 모든 엔티티가 공유):
    #   Move(Point1, Point2)                          -> None
    #   Rotate(BasePoint, RotationAngle)              -> None   (라디안)
    #   ScaleEntity(BasePoint, ScaleFactor)           -> None
    #   Copy()                                        -> 새 엔티티
    #   Offset(Distance)                              -> SelectionSet
    #   Mirror(Point1, Point2, MakeCopy=True)         -> 엔티티
    #   ArrayRectangular(행, 열, 층, 행간, 열간, 층간) -> SelectionSet
    #   ArrayPolar(개수, 포함각, 중심, PointOnAxis, 회전) -> SelectionSet
    #   Delete()                                      -> 삭제 (Erase는 표시만 숨김)
    # ==================================================================

    def get_entity_by_handle(self, handle: str) -> Any:
        """핸들로 엔티티 찾기"""
        if not self.is_running():
            return None
        try:
            return self.doc.HandleToObject(str(handle))
        except Exception as e:
            logger.error(f"핸들 {handle} 조회 실패: {str(e)}")
            return None

    def _entity_or_log(self, handle: str) -> Any:
        entity = self.get_entity_by_handle(handle)
        if entity is None:
            logger.error(f"핸들 {handle}에 해당하는 엔티티가 없습니다")
        return entity

    def move_entity(self, handle: str, offset: Tuple[float, float]) -> bool:
        """엔티티를 상대 이동. offset은 (dx, dy) 또는 (dx, dy, dz)"""
        entity = self._entity_or_log(handle)
        if entity is None:
            return False
        try:
            dx, dy, dz = self._normalize(offset)
            entity.Move(self._pt((0, 0, 0)), self._pt((dx, dy, dz)))
            self.refresh_view()
            logger.info(f"핸들 {handle} 이동: ({dx}, {dy}, {dz})")
            return True
        except Exception as e:
            logger.error(f"이동 실패({handle}): {str(e)}")
            return False

    def rotate_entity(self, handle: str, base_point: Tuple[float, float],
                      angle: float) -> bool:
        """엔티티 회전. angle은 도(degree)"""
        entity = self._entity_or_log(handle)
        if entity is None:
            return False
        try:
            entity.Rotate(self._pt(base_point), math.radians(float(angle)))
            self.refresh_view()
            logger.info(f"핸들 {handle} 회전: 기준 {base_point}, {angle}도")
            return True
        except Exception as e:
            logger.error(f"회전 실패({handle}): {str(e)}")
            return False

    def scale_entity(self, handle: str, base_point: Tuple[float, float],
                     factor: float) -> bool:
        """엔티티 축척 변경"""
        entity = self._entity_or_log(handle)
        if entity is None:
            return False
        try:
            entity.ScaleEntity(self._pt(base_point), float(factor))
            self.refresh_view()
            logger.info(f"핸들 {handle} 축척: 기준 {base_point}, {factor}배")
            return True
        except Exception as e:
            logger.error(f"축척 실패({handle}): {str(e)}")
            return False

    def copy_entity(self, handle: str,
                    offset: Tuple[float, float] = None) -> Optional[str]:
        """엔티티 복사. 새 엔티티의 핸들 반환"""
        entity = self._entity_or_log(handle)
        if entity is None:
            return None
        try:
            copy = entity.Copy()
            if offset:
                dx, dy, dz = self._normalize(offset)
                copy.Move(self._pt((0, 0, 0)), self._pt((dx, dy, dz)))
            self.refresh_view()
            new_handle = None
            try:
                new_handle = copy.Handle
            except Exception:
                pass
            logger.info(f"핸들 {handle} 복사 -> {new_handle}")
            return new_handle
        except Exception as e:
            logger.error(f"복사 실패({handle}): {str(e)}")
            return None

    def mirror_entity(self, handle: str, point1: Tuple[float, float],
                      point2: Tuple[float, float],
                      make_copy: bool = True) -> bool:
        """두 점을 지나는 축에 대해 대칭"""
        entity = self._entity_or_log(handle)
        if entity is None:
            return False
        try:
            entity.Mirror(self._pt(point1), self._pt(point2), bool(make_copy))
            self.refresh_view()
            logger.info(f"핸들 {handle} 대칭: {point1}-{point2}, 복사={make_copy}")
            return True
        except Exception as e:
            logger.error(f"대칭 실패({handle}): {str(e)}")
            return False

    def offset_entity(self, handle: str, distance: float) -> bool:
        """평행 복사. 거리의 부호가 방향을 결정한다"""
        entity = self._entity_or_log(handle)
        if entity is None:
            return False
        try:
            entity.Offset(float(distance))
            self.refresh_view()
            logger.info(f"핸들 {handle} 평행복사: {distance}")
            return True
        except Exception as e:
            logger.error(f"평행복사 실패({handle}): {str(e)}")
            return False

    def array_polar(self, handle: str, count: int,
                    center: Tuple[float, float],
                    included_angle: float = 360.0,
                    rotate_objects: bool = True) -> bool:
        """원형 배열 복제

        PointOnAxis 기본값이 VT_DISPATCH 자리에 정수 0을 넣어 실패하므로
        중심점을 명시적으로 한 번 더 넘긴다 (같은 점이면 2D 배열).
        """
        entity = self._entity_or_log(handle)
        if entity is None:
            return False

        center_pt = self._pt(center)
        angle_rad = math.radians(float(included_angle))
        attempts = [
            ("중심점 중복 전달",
             lambda: entity.ArrayPolar(int(count), angle_rad, center_pt,
                                       center_pt, bool(rotate_objects))),
            ("VT_DISPATCH null",
             lambda: entity.ArrayPolar(
                 int(count), angle_rad, center_pt,
                 win32com.client.VARIANT(pythoncom.VT_DISPATCH, None),
                 bool(rotate_objects))),
            ("3인자",
             lambda: entity.ArrayPolar(int(count), angle_rad, center_pt)),
        ]
        errors = []
        for label, call in attempts:
            try:
                call()
                self.refresh_view()
                logger.info(f"핸들 {handle} 원형배열 {count}개 ({label})")
                return True
            except Exception as e:
                errors.append(f"{label}: {str(e)[:50]}")
        logger.error("원형배열 실패: " + " | ".join(errors))
        return False

    def array_rectangular(self, handle: str, rows: int, columns: int,
                          row_spacing: float, column_spacing: float,
                          levels: int = 1,
                          level_spacing: float = 0.0) -> bool:
        """격자 배열 복제"""
        entity = self._entity_or_log(handle)
        if entity is None:
            return False
        try:
            entity.ArrayRectangular(int(rows), int(columns), int(levels),
                                    float(row_spacing), float(column_spacing),
                                    float(level_spacing))
            self.refresh_view()
            logger.info(f"핸들 {handle} 격자배열 {rows}x{columns}")
            return True
        except Exception as e:
            logger.error(f"격자배열 실패({handle}): {str(e)}")
            return False

    def delete_entity(self, handle: str) -> bool:
        """엔티티 삭제

        Erase()는 화면 표시만 숨기고 DB에는 남으므로 Delete()를 쓴다.
        """
        entity = self._entity_or_log(handle)
        if entity is None:
            return False
        try:
            entity.Delete()
            self.refresh_view()
            logger.info(f"핸들 {handle} 삭제")
            return True
        except Exception as e:
            logger.error(f"삭제 실패({handle}): {str(e)}")
            return False

    def delete_by_layer(self, layer: str) -> int:
        """지정한 레이어의 모든 엔티티를 삭제한다. 삭제된 개수를 반환

        자료가 바뀌어 도면을 다시 그려야 할 때, 이전에 그린 것을 레이어
        단위로 한 번에 지우기 위한 용도.
        """
        if not self.is_running():
            return 0
        try:
            # 삭제하면서 순회하면 라이브 COM 컬렉션이 흔들릴 수 있으므로
            # 먼저 스냅샷 리스트로 받아둔다.
            snapshot = list(self.doc.ModelSpace)
            deleted = 0
            for entity in snapshot:
                try:
                    if entity.Layer == layer:
                        entity.Delete()
                        deleted += 1
                except Exception as e:
                    logger.warning(f"레이어 삭제 중 엔티티 하나 실패: {e}")
            self.refresh_view()
            logger.info(f"레이어 '{layer}' 삭제: {deleted}개")
            return deleted
        except Exception as e:
            logger.error(f"레이어 삭제 실패({layer}): {str(e)}")
            return 0

    @staticmethod
    def _entity_bbox(info: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:
        """cad_reader.read_entity()가 만든 dict에서 2D 바운딩 박스를 구한다.

        cad_reader.bounds_of()의 점 수집 로직과 동일하다. 엔티티 하나 단위로
        판정해야 하는 delete_by_window 전용으로 뽑아 놓았다.
        """
        xs: List[float] = []
        ys: List[float] = []

        def add(xyz):
            if xyz:
                xs.append(xyz[0])
                ys.append(xyz[1])

        add(info.get('start'))
        add(info.get('end'))
        add(info.get('position'))
        add(info.get('ext1'))
        add(info.get('ext2'))
        add(info.get('text_position'))
        for p in info.get('points', []) or []:
            add(p)
        for loop in info.get('loops', []) or []:
            for p in loop:
                add(p)
        center = info.get('center')
        if center:
            r = info.get('radius') or info.get('major') or 0
            xs.extend([center[0] - r, center[0] + r])
            ys.extend([center[1] - r, center[1] + r])

        if not xs or not ys:
            return None
        return (min(xs), min(ys), max(xs), max(ys))

    def delete_by_window(self, x1: float, y1: float, x2: float, y2: float,
                         mode: str = "crossing") -> int:
        """사각형 범위에 걸리는 엔티티를 삭제한다. 삭제된 개수를 반환

        mode="crossing"(기본): 범위와 조금이라도 겹치면 삭제.
        mode="inside": 범위 안에 완전히 들어온 것만 삭제.

        CADian의 GetBoundingBox / SelectionSets.Select는 COM 인자 요구사항이
        불안정해(필수 매개변수 오류) 못 믿는다. 대신 이미 검증된
        cad_reader.read_entity() 좌표 추출 로직으로 바운딩 박스를 파이썬에서
        직접 계산해 판정한다.
        """
        if not self.is_running():
            return 0

        xmin, xmax = min(x1, x2), max(x1, x2)
        ymin, ymax = min(y1, y2), max(y1, y2)
        mode = str(mode).lower()

        try:
            import cad_reader
            lib = self.app.Library if self._is_dispatch_point_cad() else None
            snapshot = list(self.doc.ModelSpace)

            deleted = 0
            for entity in snapshot:
                try:
                    info = cad_reader.read_entity(entity, lib)
                except Exception:
                    continue
                bbox = self._entity_bbox(info)
                if bbox is None:
                    continue
                ex0, ey0, ex1, ey1 = bbox
                if mode == "inside":
                    hit = ex0 >= xmin and ex1 <= xmax and ey0 >= ymin and ey1 <= ymax
                else:
                    hit = not (ex1 < xmin or ex0 > xmax or ey1 < ymin or ey0 > ymax)
                if not hit:
                    continue
                try:
                    entity.Delete()
                    deleted += 1
                except Exception as e:
                    logger.warning(f"윈도우 삭제 중 엔티티 하나 실패: {e}")

            self.refresh_view()
            logger.info(f"윈도우 삭제 ({x1},{y1})-({x2},{y2}) {mode}: {deleted}개")
            return deleted
        except Exception as e:
            logger.error(f"윈도우 삭제 실패: {str(e)}")
            return 0

    # ==================================================================
    # 블록
    #
    #   doc.Blocks.Add(InsertionPoint, Name)                -> Block
    #   Block.AddLine / AddCircle / AddText / AddPolyline ... (ModelSpace 동일)
    #   Block.AddAttributeDef(Height, Mode, Prompt, Point, Tag, Value)
    #   ModelSpace.InsertBlock(Point, Name, Xs, Ys, Zs, Rot) -> BlockInsert
    #   BlockInsert.GetAttributes() -> [ .TagString, .TextString ]
    # ==================================================================

    # AddAttributeDef의 Mode 값
    ATTR_MODE_NORMAL = 0
    ATTR_MODE_INVISIBLE = 1
    ATTR_MODE_CONSTANT = 2
    ATTR_MODE_PRESET = 8

    def _draw_into(self, space: Any, spec: Dict[str, Any]) -> Any:
        """임의의 공간(ModelSpace 또는 Block)에 도형 하나 생성

        spec 예:
          {"type": "line", "start": [0,0], "end": [40,0]}
          {"type": "circle", "center": [20,10], "radius": 5}
          {"type": "arc", "center": [0,0], "radius": 10,
           "start_angle": 0, "end_angle": 90}
          {"type": "rectangle", "corner1": [0,0], "corner2": [40,20]}
          {"type": "polyline", "points": [[0,0],[10,10]], "closed": true}
          {"type": "text", "position": [2,14], "text": "고정", "height": 4}
        모든 spec에 "color", "layer" 를 넣을 수 있다.
        """
        kind = str(spec.get("type", "")).lower()
        entity = None

        if kind == "line":
            entity = space.AddLine(self._pt(spec["start"]), self._pt(spec["end"]))

        elif kind == "circle":
            entity = space.AddCircle(self._pt(spec["center"]),
                                     float(spec["radius"]))

        elif kind == "arc":
            entity = space.AddArc(self._pt(spec["center"]), float(spec["radius"]),
                                  math.radians(float(spec.get("start_angle", 0))),
                                  math.radians(float(spec.get("end_angle", 90))))

        elif kind == "ellipse":
            major = float(spec.get("major_axis", 1.0))
            minor = float(spec.get("minor_axis", major / 2))
            rot = math.radians(float(spec.get("rotation", 0)))
            entity = space.AddEllipse(
                self._pt(spec["center"]),
                self._vec(major * math.cos(rot), major * math.sin(rot), 0),
                minor / major)

        elif kind in ("rectangle", "rect"):
            x1, y1, _ = self._normalize(spec["corner1"])
            x2, y2, _ = self._normalize(spec["corner2"])
            pts = [(x1, y1, 0), (x2, y1, 0), (x2, y2, 0), (x1, y2, 0), (x1, y1, 0)]
            entity = space.AddPolyline(self._pts([self._normalize(p) for p in pts]))
            try:
                entity.Closed = True
            except Exception:
                pass

        elif kind == "polyline":
            points = [self._normalize(p) for p in spec["points"]]
            entity = space.AddPolyline(self._pts(points))
            if spec.get("closed"):
                try:
                    entity.Closed = True
                except Exception:
                    pass

        elif kind == "text":
            entity = space.AddText(str(spec.get("text", "")),
                                   self._pt(spec["position"]),
                                   float(spec.get("height", 2.5)))
            if spec.get("rotation"):
                try:
                    entity.Rotation = math.radians(float(spec["rotation"]))
                except Exception:
                    pass

        else:
            logger.warning(f"알 수 없는 도형 종류: {kind}")
            return None

        self._apply_style(entity, spec.get("layer"), spec.get("color"),
                          spec.get("lineweight"))
        return entity

    def draw_entities(self, entities: List[Dict[str, Any]]) -> List[Optional[str]]:
        """여러 도형을 한 번에 그린다. 각 항목은 _draw_into가 받는 spec과 동일
        (create_block의 entities 인자와 같은 형식).

        도형 하나마다 왕복하던 것을 한 번의 호출로 줄이기 위한 배치 도구.
        실패한 항목은 목록에서 None으로 남아 순서와 인덱스가 유지된다.
        도중에 하나가 실패해도 나머지는 계속 그린다.
        """
        if not self.is_running():
            return [None] * len(entities or [])

        ms = self.doc.ModelSpace
        handles: List[Optional[str]] = []
        for i, spec in enumerate(entities or []):
            try:
                entity = self._draw_into(ms, spec)
                handles.append(self._handle_of(entity))
            except Exception as e:
                logger.error(f"배치 작도 {i}번째 실패({spec.get('type')}): {str(e)}")
                handles.append(None)

        self.refresh_view()
        ok = sum(1 for h in handles if h)
        logger.info(f"배치 작도 완료: {len(handles)}개 중 {ok}개 성공")
        return handles

    def list_blocks(self) -> List[Dict[str, Any]]:
        """정의된 블록 목록. '*'로 시작하는 내부 블록은 제외"""
        if not self.is_running():
            return []
        result = []
        try:
            for block in self.doc.Blocks:
                try:
                    name = block.Name
                except Exception:
                    continue
                if name.startswith('*'):
                    continue
                info: Dict[str, Any] = {"name": name}
                try:
                    info["entity_count"] = block.Count
                except Exception:
                    pass
                tags = []
                try:
                    for item in block:
                        if getattr(item, 'ObjectName', '') == 'AcDbAttributeDefinition':
                            tags.append(item.TagString)
                except Exception:
                    pass
                if tags:
                    info["attributes"] = tags
                result.append(info)
        except Exception as e:
            logger.error(f"블록 목록 조회 실패: {str(e)}")
        return result

    def create_block(self, name: str, entities: List[Dict[str, Any]],
                     attributes: List[Dict[str, Any]] = None,
                     base_point: Tuple[float, float] = (0, 0)) -> bool:
        """블록 정의 생성

        entities: _draw_into가 받는 spec 목록
        attributes: [{"tag": "NAME", "prompt": "장비명", "default": "미지정",
                      "position": [2,6], "height": 5, "invisible": false}]
        """
        if not self.is_running():
            return False

        try:
            existing = None
            try:
                existing = self.doc.Blocks.Item(name)
            except Exception:
                pass
            if existing is not None:
                logger.warning(f"블록 '{name}'이 이미 존재합니다. 기존 정의를 사용하세요")
                return False

            block = self.doc.Blocks.Add(self._pt(base_point), str(name))
            logger.info(f"블록 '{name}' 생성 (기준점 {base_point})")

            made = 0
            for spec in entities or []:
                try:
                    if self._draw_into(block, spec) is not None:
                        made += 1
                except Exception as e:
                    logger.error(f"블록 도형 생성 실패({spec.get('type')}): {str(e)}")

            for attr in attributes or []:
                try:
                    mode = (self.ATTR_MODE_INVISIBLE if attr.get("invisible")
                            else self.ATTR_MODE_NORMAL)
                    block.AddAttributeDef(
                        float(attr.get("height", 2.5)),
                        mode,
                        str(attr.get("prompt", attr.get("tag", ""))),
                        self._pt(attr.get("position", (0, 0))),
                        str(attr["tag"]).upper(),
                        str(attr.get("default", "")))
                    logger.info(f"  속성 '{attr['tag']}' 정의")
                except Exception as e:
                    logger.error(f"속성 정의 실패({attr.get('tag')}): {str(e)}")

            logger.info(f"블록 '{name}' 완료: 도형 {made}개")
            return True
        except Exception as e:
            logger.error(f"블록 생성 실패({name}): {str(e)}")
            return False

    def insert_block(self, name: str, position: Tuple[float, float],
                     scale: float = 1.0, rotation: float = 0.0,
                     attributes: Dict[str, str] = None,
                     layer: str = None) -> Optional[str]:
        """블록 삽입. 삽입된 인스턴스의 핸들 반환

        attributes: {"NAME": "라우터"} 형태. 태그는 대소문자 구분 없이 매칭
        """
        if not self.is_running():
            return None
        try:
            scale = float(scale)
            ref = self.doc.ModelSpace.InsertBlock(
                self._pt(position), str(name),
                scale, scale, scale, math.radians(float(rotation)))

            if layer:
                try:
                    self.create_layer(layer)
                    ref.Layer = layer
                except Exception as e:
                    logger.warning(f"블록 레이어 설정 실패: {e}")

            if attributes:
                self._set_attributes(ref, attributes)

            self.refresh_view()
            handle = None
            try:
                handle = ref.Handle
            except Exception:
                pass
            logger.info(f"블록 '{name}' 삽입 at {position} -> 핸들 {handle}")
            return handle
        except Exception as e:
            logger.error(f"블록 삽입 실패({name}): {str(e)}")
            return None

    def _set_attributes(self, ref: Any, attributes: Dict[str, str]) -> int:
        """블록 인스턴스의 속성값 설정. 변경된 개수 반환"""
        wanted = {str(k).upper(): str(v) for k, v in attributes.items()}
        changed = 0
        try:
            if not getattr(ref, 'HasAttributes', False):
                logger.warning("이 블록 인스턴스에는 속성이 없습니다")
                return 0
            for attr in ref.GetAttributes():
                try:
                    tag = str(attr.TagString).upper()
                except Exception:
                    continue
                if tag in wanted:
                    try:
                        attr.TextString = wanted[tag]
                        changed += 1
                    except Exception as e:
                        logger.warning(f"속성 '{tag}' 설정 실패: {e}")
            missing = set(wanted) - {str(a.TagString).upper()
                                     for a in ref.GetAttributes()}
            if missing:
                logger.warning(f"블록에 없는 속성 태그: {sorted(missing)}")
        except Exception as e:
            logger.error(f"속성 설정 실패: {str(e)}")
        return changed

    def set_block_attributes(self, handle: str,
                             attributes: Dict[str, str]) -> bool:
        """이미 삽입된 블록의 속성값 변경"""
        ref = self._entity_or_log(handle)
        if ref is None:
            return False
        changed = self._set_attributes(ref, attributes)
        self.refresh_view()
        logger.info(f"핸들 {handle} 속성 {changed}개 변경")
        return changed > 0
