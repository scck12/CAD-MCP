r"""CAD COM 호출 계층.

이 클래스의 모든 메서드는 cad_worker의 작업 스레드 안에서만 실행된다.
공개 메서드가 반환하는 값은 핸들 문자열이나 plain Python 값(dict/list/str/
int/float/bool)뿐이어야 한다 — 살아 있는 COM 객체가 작업 스레드 밖으로 나가면
RPC_E_WRONG_THREAD가 난다 (spec.md "절대 규칙").

실패는 None/False로 숨기지 않고 CADError(또는 COM 예외)로 올린다. 서버가
원인을 HRESULT 이름과 함께 그대로 보고한다.
"""
import json
import logging
import math
import os
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import win32com.client
    # pythoncom은 pywin32의 일부이므로 별도 설치가 필요 없다
    import pythoncom
    import pywintypes
except ImportError:
    logging.error("win32com.client 또는 pythoncom을 임포트할 수 없습니다. "
                  "pywin32 라이브러리가 설치되어 있는지 확인하세요")
    raise

import cad_reader
from colors import BYLAYER, color_sysvar, parse_color

# config.json 파일을 직접 읽는다
config_path = os.path.join(os.path.dirname(__file__), 'config.json')
with open(config_path, 'r', encoding='utf-8') as f:
    config = json.load(f)

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

# AcAlignment (Text.Alignment). 캐디안 12.1에서 실측 확인 (2026-10-12)
TEXT_JUSTIFY = {
    "left": 0, "center": 1, "right": 2, "middle": 4,
    "top_left": 6, "top_center": 7, "top_right": 8,
    "middle_left": 9, "middle_center": 10, "middle_right": 11,
    "bottom_left": 12, "bottom_center": 13, "bottom_right": 14,
}
_JUSTIFY_ALIASES = {"tl": "top_left", "tc": "top_center", "tr": "top_right",
                    "ml": "middle_left", "mc": "middle_center", "mr": "middle_right",
                    "bl": "bottom_left", "bc": "bottom_center", "br": "bottom_right",
                    "centre": "center", "l": "left", "c": "center", "r": "right"}
TEXT_JUSTIFY_NAMES = {v: k for k, v in TEXT_JUSTIFY.items()}

# AcAttachmentPoint (MText.AttachmentPoint)
MTEXT_ATTACH = {
    "top_left": 1, "top_center": 2, "top_right": 3,
    "middle_left": 4, "middle_center": 5, "middle_right": 6,
    "bottom_left": 7, "bottom_center": 8, "bottom_right": 9,
}
MTEXT_ATTACH_NAMES = {v: k for k, v in MTEXT_ATTACH.items()}

# 자주 보는 HRESULT 이름 (에러 메시지에 같이 찍어 원인 파악을 빠르게)
HRESULT_NAMES = {
    0x8001010E: "RPC_E_WRONG_THREAD",
    0x80010001: "RPC_E_CALL_REJECTED",
    0x8001010A: "RPC_E_SERVERCALL_RETRYLATER",
    0x80010108: "RPC_E_DISCONNECTED",
    0x800706BA: "RPC_S_SERVER_UNAVAILABLE",
    0x800706BE: "RPC_S_CALL_FAILED",
    0x80020009: "DISP_E_EXCEPTION",
    0x80020004: "DISP_E_PARAMNOTFOUND",
    0x80020005: "DISP_E_TYPEMISMATCH",
    0x80070057: "E_INVALIDARG",
    0x80004005: "E_FAIL",
    0x80004001: "E_NOTIMPL",
    0x800401F0: "CO_E_NOTINITIALIZED",
    0x800401E3: "MK_E_UNAVAILABLE",
    0x80040154: "REGDB_E_CLASSNOTREG",
}


class CADError(RuntimeError):
    """CAD 작업 실패 (원인을 사람이 읽을 수 있게 담는다)"""


def describe_error(e: BaseException) -> str:
    """예외를 한 줄 설명으로. COM 예외는 HRESULT 이름을 붙인다"""
    if isinstance(e, pywintypes.com_error):
        hr = e.args[0] & 0xFFFFFFFF if e.args else 0
        msg = e.args[1] if len(e.args) > 1 else ""
        text = f"COM 오류 {HRESULT_NAMES.get(hr, '')}(0x{hr:08X}) {msg}".strip()
        exc = e.args[2] if len(e.args) > 2 else None
        if exc and len(exc) >= 6:
            if exc[2]:
                text += f" - {exc[2]}"
            scode = (exc[5] or 0) & 0xFFFFFFFF
            if scode:
                text += f" [내부 {HRESULT_NAMES.get(scode, '')}(0x{scode:08X})]"
        return text
    if isinstance(e, CADError):
        return str(e)
    return f"{type(e).__name__}: {e}"


def _unit(dx: float, dy: float) -> Tuple[float, float]:
    length = math.hypot(dx, dy)
    if length < 1e-9:
        raise CADError("두 점이 같아 방향을 정할 수 없습니다")
    return dx / length, dy / length


class CADController:
    """CAD 컨트롤러. 작업 스레드 안에서만 쓴다"""

    # 엔티티 속성 이름이 CAD별로 다르다.
    #   AutoCAD 계열 : Color      / LineWeight
    #   CADian       : color(소문자) / Lineweight(소문자 w)
    # 성공한 이름을 캐시해 매 도형마다 예외가 발생하지 않게 한다.
    COLOR_PROPS = ("Color", "color", "ColorIndex")
    LINEWEIGHT_PROPS = ("LineWeight", "Lineweight")

    # AddAttributeDef의 Mode 값
    ATTR_MODE_NORMAL = 0
    ATTR_MODE_INVISIBLE = 1
    ATTR_MODE_CONSTANT = 2
    ATTR_MODE_PRESET = 8

    def __init__(self):
        self.app = None
        self.doc = None
        self._lib = None
        self.doc_name: Optional[str] = None
        self._doc_key: Optional[str] = None
        self._known_layers: set = set()
        cad_cfg = config.get("cad", {})
        draw_cfg = config.get("drawing", {})
        self.startup_wait_time = cad_cfg.get("startup_wait_time", 20)
        self.command_delay = cad_cfg.get("command_delay", 0.5)
        self.cad_type = cad_cfg.get("type", "AutoCAD")
        self.default_layer = draw_cfg.get("default_layer", "0")
        self.door_layer = draw_cfg.get("door_layer", "문")
        self.window_layer = draw_cfg.get("window_layer", "창호")
        # 테스트 전용: 사용자가 쓰는 캐디안을 건드리지 않도록 새 인스턴스를 띄운다
        self._isolated = os.environ.get("CAD_MCP_ISOLATED") == "1"
        self.valid_lineweights = [-3, -2, -1, 0, 5, 9, 13, 15, 18, 20, 25, 30, 35,
                                  40, 50, 53, 60, 70, 80, 90, 100, 106, 120, 140,
                                  158, 200, 211]
        # 다중 점 인자 생성 전략 캐시 (CADian 전용, 최초 1회만 탐색)
        self._pts_strategy = None
        self._color_prop = None
        self._lineweight_prop = None
        # AddHatch 인자 조합 캐시 (PatternType, HatchObjectType)
        self._hatch_args = None
        logger.info(f"CAD 컨트롤러 초기화 완료 (type={self.cad_type}"
                    f"{', 격리 모드' if self._isolated else ''})")

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
    # ==================================================================

    def _is_dispatch_point_cad(self) -> bool:
        """좌표를 COM 객체로 넘겨야 하는 CAD인지"""
        return self.cad_type.lower() in DISPATCH_POINT_TYPES

    @staticmethod
    def _normalize(point) -> Tuple[float, float, float]:
        """2D/3D 좌표를 (x, y, z) float 튜플로 정규화"""
        if point is None or len(point) < 2:
            raise CADError(f"좌표가 올바르지 않습니다: {point!r}")
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

    @property
    def lib(self):
        if self._lib is None:
            self._lib = self.app.Library
        return self._lib

    def _pt(self, point) -> Any:
        """단일 점 인자 생성 (AddLine / AddCircle / AddText / AddDimAligned 등)"""
        x, y, z = self._normalize(point)
        if self._is_dispatch_point_cad():
            return self.lib.CreatePoint(x, y, z)
        return self._variant_doubles((x, y, z))

    def _vec(self, x: float, y: float, z: float = 0.0) -> Any:
        """벡터 인자 생성 (AddEllipse의 장축 벡터 등)"""
        if self._is_dispatch_point_cad():
            return self.lib.CreateVector(float(x), float(y), float(z))
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

        if self._pts_strategy == "convert":
            return self.lib.ConvertArrayToPoints(self._variant_doubles(flat), True)
        if self._pts_strategy == "collection":
            return self._pts_collection(normalized)

        # 최초 1회: 어떤 방식이 통하는지 탐색하고 캐시
        # IIcadLibrary.ConvertArrayToPoints(ArrayOfDoubles, Is3DPoint) - 인자 2개 필수
        try:
            result = self.lib.ConvertArrayToPoints(self._variant_doubles(flat), True)
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
        pts = self.lib.CreatePoints()
        for x, y, z in normalized:
            pts.Add(x, y, z)
        return pts

    @staticmethod
    def _handle_of(entity: Any) -> Optional[str]:
        """엔티티에서 핸들(문자열)만 추출한다.

        이 값만이 작업 스레드 경계를 안전하게 넘을 수 있다. COM 엔티티
        객체를 그대로 반환하면, 호출자가 다른 스레드에서 그 값을 만지는 순간
        (str(), 속성 접근, GC의 Release()까지) RPC_E_WRONG_THREAD가 난다.
        """
        if entity is None:
            return None
        try:
            return str(entity.Handle)
        except Exception:
            return None

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

    # ==================================================================
    # 연결 / 문서
    # ==================================================================

    def is_running(self) -> bool:
        return self.app is not None and self.doc is not None

    def release(self) -> None:
        """COM 참조를 모두 놓는다 (작업 스레드 종료 직전에 호출)"""
        self._lib = None
        self.doc = None
        self.app = None
        self.doc_name = None
        self._doc_key = None
        self._known_layers = set()

    def _bind_document(self, doc: Any) -> None:
        self.doc = doc
        try:
            name = doc.Name
        except Exception:
            name = "?"
        try:
            full = doc.FullName or ""
        except Exception:
            full = ""
        self.doc_name = name
        self._doc_key = f"{full}|{name}"
        self._known_layers = set()

    def connect(self) -> None:
        """캐디안에 연결. 실패하면 CADError

        사용자 문서를 닫거나 버리는 일은 절대 하지 않는다. (예전 코드는 활성
        문서 읽기가 실패하면 열린 문서를 저장 없이 모두 닫았다.)
        """
        self.release()
        app_id, app_name = self._resolve_app_id()

        if self._isolated:
            logger.info(f"격리 모드: 새 {app_name} 인스턴스를 띄웁니다")
            self.app = self._wrap_early_bound(win32com.client.DispatchEx(app_id))
            self.app.Visible = True
            time.sleep(2)
            self._bind_document(self.app.Documents.Add())
        else:
            try:
                logger.info(f"기존 {app_name} 인스턴스에 연결 시도... (ProgID={app_id})")
                self.app = self._wrap_early_bound(win32com.client.GetActiveObject(app_id))
                logger.info(f"실행 중인 {app_name} 인스턴스에 연결 성공")
            except Exception as e:
                logger.info(f"실행 중인 {app_name}를 찾지 못해 새로 시작합니다: {e}")
                try:
                    self.app = win32com.client.gencache.EnsureDispatch(app_id)
                except Exception as gen_ex:
                    logger.warning(f"EnsureDispatch 실패, Dispatch로 재시도: {gen_ex}")
                    self.app = win32com.client.Dispatch(app_id)
                self.app.Visible = True
                time.sleep(self.startup_wait_time)
            if self.app.Documents.Count == 0:
                logger.info("열린 문서가 없어 새 문서를 만듭니다")
                self._bind_document(self.app.Documents.Add())
            else:
                self._bind_document(self.app.ActiveDocument)

        if self._is_dispatch_point_cad():
            try:
                self._lib = self.app.Library
            except Exception as e:
                self.release()
                raise CADError(f"CADian Library 객체를 가져올 수 없습니다: {describe_error(e)}")
        logger.info(f"CAD 연결 완료: 문서 {self.doc_name}")

    def start_cad(self) -> bool:
        """호환용: connect()의 bool 버전"""
        try:
            self.connect()
            return True
        except Exception as e:
            logger.error(f"CAD 시작 실패: {describe_error(e)}")
            return False

    def sync_active_document(self) -> Optional[str]:
        """캐디안의 활성 문서와 작업 대상을 맞춘다. 바뀌었으면 알림 문장 반환

        예전에는 연결 시점의 문서를 계속 붙잡고 있어서, 사용자가 캐디안에서
        탭을 바꿔도 이전 문서에 그렸다 (개선 요청 2-3).
        """
        try:
            active = self.app.ActiveDocument
            key = f"{active.FullName or ''}|{active.Name}"
        except Exception as e:
            logger.warning(f"활성 문서를 읽을 수 없어 다시 연결합니다: {describe_error(e)}")
            self.connect()
            return f"캐디안 연결이 끊겨 다시 연결했습니다 (대상 문서: {self.doc_name})."
        if key != self._doc_key:
            old = self.doc_name
            self._bind_document(active)
            logger.info(f"작업 대상 문서 변경: {old} -> {self.doc_name}")
            if old:
                return (f"작업 대상 문서가 '{old}' → '{self.doc_name}'(으)로 바뀌었습니다 "
                        "(캐디안 활성 탭 기준).")
        return None

    def prepare(self) -> Optional[str]:
        """작업 직전 호출: 연결 보장 + 활성 문서 동기화"""
        if not self.is_running():
            self.connect()
            return None
        return self.sync_active_document()

    def documents(self, action: str = "list", name: str = None,
                  path: str = None) -> Dict[str, Any]:
        """문서 목록/새로 만들기/열기/전환/저장"""
        action = (action or "list").lower()
        docs = self.app.Documents

        def listing():
            items = []
            active = self.app.ActiveDocument.Name
            for i in range(docs.Count):
                d = docs.Item(i)
                items.append({"name": d.Name, "path": d.FullName or None,
                              "active": d.Name == active})
            return items

        if action == "list":
            return {"documents": listing()}
        if action == "new":
            d = docs.Add()
            d.Activate()
            self._bind_document(d)
            return {"created": self.doc_name, "documents": listing()}
        if action == "open":
            if not path:
                raise CADError("open에는 path가 필요합니다")
            full = os.path.abspath(path)
            for i in range(docs.Count):
                d = docs.Item(i)
                if (d.FullName or "").lower() == full.lower():
                    d.Activate()
                    self._bind_document(d)
                    return {"activated": self.doc_name, "note": "이미 열려 있어 전환만 했습니다"}
            if not os.path.exists(full):
                raise CADError(f"파일이 없습니다: {full}")
            d = docs.Open(full)
            d.Activate()
            self._bind_document(d)
            return {"opened": self.doc_name, "path": full}
        if action == "activate":
            if not name:
                raise CADError("activate에는 name이 필요합니다")
            key = name.lower()
            for i in range(docs.Count):
                d = docs.Item(i)
                full = (d.FullName or "").lower()
                if d.Name.lower() in (key, key + ".dwg") or full == key or \
                        os.path.basename(full) == key:
                    d.Activate()
                    self._bind_document(d)
                    return {"activated": self.doc_name}
            raise CADError(f"열린 문서 중 '{name}'이(가) 없습니다: "
                           f"{[x['name'] for x in listing()]}")
        if action == "save":
            if not (self.doc.FullName or ""):
                raise CADError("아직 저장된 적 없는 문서입니다. save_drawing에 경로를 주세요")
            self.zoom_extents()
            self.doc.Save()
            return {"saved": self.doc.FullName}
        raise CADError(f"지원하지 않는 action: {action} (list/new/open/activate/save)")

    # ==================================================================
    # 스타일
    # ==================================================================

    def validate_lineweight(self, lineweight) -> Optional[int]:
        """유효한 선두께 값(1/100mm, -1=BYLAYER)이 아니면 CADError"""
        if lineweight is None:
            return None
        value = int(lineweight)
        if value not in self.valid_lineweights:
            raise CADError(f"선두께 {lineweight}는 쓸 수 없습니다. "
                           f"허용값: {self.valid_lineweights} (-1=BYLAYER)")
        return value

    def _set_entity_prop(self, entity: Any, candidates: Tuple[str, ...],
                         cache_attr: str, value: Any, label: str) -> None:
        """후보 속성명을 순서대로 시도하고 성공한 이름을 캐시"""
        cached = getattr(self, cache_attr)
        names = (cached,) + tuple(n for n in candidates if n != cached) if cached else candidates
        last = None
        for name in names:
            try:
                setattr(entity, name, value)
                if cached != name:
                    setattr(self, cache_attr, name)
                    logger.info(f"{label} 속성명 확정: '{name}'")
                return
            except Exception as e:
                last = e
        raise CADError(f"{label} 설정 실패({value}): {describe_error(last) if last else ''}")

    def _layer_obj(self, name: str) -> Any:
        try:
            return self.doc.Layers.Item(name)
        except Exception:
            for layer in self.doc.Layers:
                if layer.Name == name:
                    return layer
        return None

    def _ensure_layer(self, name: str) -> None:
        """레이어가 없으면 만든다. 현재 레이어(CLAYER)는 바꾸지 않는다

        예전 create_layer는 도형 하나 그릴 때마다 ActiveLayer를 그 레이어로
        바꿔서, 테스트 레이어가 현재 레이어로 남는 사고가 났다 (요청 2-4).
        """
        if name in self._known_layers:
            return
        if self._layer_obj(name) is None:
            self.doc.Layers.Add(name)
            logger.info(f"레이어 생성: {name}")
        self._known_layers.add(name)

    def _apply_style(self, entity: Any, layer: Optional[str] = None,
                     color: Any = None, lineweight=None, linetype: Optional[str] = None,
                     defaults: bool = True) -> None:
        """레이어/색상/선두께/선종류 적용

        defaults=True(새로 만든 엔티티)면 지정 안 한 레이어는 기본 레이어
        ("0"), 지정 안 한 색은 BYLAYER로 명시한다. 캐디안의 현재 레이어/현재
        색상 설정에 결과가 좌우되지 않게 하기 위해서다.
        """
        if layer is None and defaults:
            layer = self.default_layer
        if layer:
            self._ensure_layer(str(layer))
            entity.Layer = str(layer)

        code = parse_color(color)
        if code is None and defaults:
            code = BYLAYER
        if code is not None:
            self._set_entity_prop(entity, self.COLOR_PROPS, '_color_prop', code, "색상")

        lw = self.validate_lineweight(lineweight)
        if lw is not None:
            self._set_entity_prop(entity, self.LINEWEIGHT_PROPS, '_lineweight_prop', lw, "선두께")

        if linetype:
            entity.Linetype = str(linetype)

    def _style_of(self, entity: Any) -> Dict[str, Any]:
        style: Dict[str, Any] = {}
        for key, getter in (("layer", lambda: entity.Layer),
                            ("color", lambda: entity.color),
                            ("lineweight", lambda: entity.Lineweight)):
            try:
                style[key] = getter()
            except Exception:
                pass
        return style

    # ==================================================================
    # 엔티티 조회
    # ==================================================================

    def _get(self, handle: str) -> Any:
        """핸들로 엔티티 조회 (정확한 타입 래퍼로 돌아온다)"""
        try:
            return self.doc.HandleToObject(str(handle))
        except Exception:
            raise CADError(f"핸들 {handle}에 해당하는 엔티티가 없습니다 (이미 삭제됐을 수 있음)")

    def _retype(self, obj: Any) -> Any:
        """Mirror/Copy가 돌려준 범용 IIcadEntity를 실제 타입으로 다시 꺼낸다"""
        return self.doc.HandleToObject(obj.Handle)

    def _bbox(self, entity: Any) -> Optional[Tuple[float, float, float, float]]:
        """실제 외곽 상자. 캐디안은 in/out 점 객체 두 개를 넘겨야 한다

        (예전 probe가 인자 없이 호출해 DISP_E_PARAMNOTFOUND가 났던 것은 이
        시그니처 때문이었다. 2026-10-12 확인)
        """
        try:
            if self._is_dispatch_point_cad():
                mn, mx = entity.GetBoundingBox(self.lib.CreatePoint(0, 0, 0),
                                               self.lib.CreatePoint(0, 0, 0))
                return (mn.x, mn.y, mx.x, mx.y)
            mn, mx = entity.GetBoundingBox()
            return (mn[0], mn[1], mx[0], mx[1])
        except Exception:
            try:
                info = cad_reader.read_entity(entity, self.lib, with_bbox=False)
                return cad_reader.entity_bbox(info)
            except Exception:
                return None

    @staticmethod
    def _type_key(object_name: str) -> str:
        return str(object_name or "").lower().replace("acdb", "")

    # types 필터 이름 -> ObjectName(소문자, acdb 제거) 묶음.
    # 부분 문자열로 비교하면 "line"이 "2dpolyline"에도 걸려서 명시적으로 묶는다.
    TYPE_GROUPS = {
        "line": {"line"},
        "polyline": {"polyline", "2dpolyline", "3dpolyline"},
        "text": {"text"},
        "mtext": {"mtext"},
        "circle": {"circle"},
        "arc": {"arc"},
        "ellipse": {"ellipse"},
        "hatch": {"hatch"},
        "block": {"blockreference"},
        "insert": {"blockreference"},
    }

    @classmethod
    def _type_matches(cls, key: str, wanted: str) -> bool:
        wanted = cls._type_key(wanted)
        if wanted in ("dimension", "dim"):
            return "dimension" in key
        group = cls.TYPE_GROUPS.get(wanted)
        return key in group if group else key == wanted

    def select(self, selector: Dict[str, Any]) -> List[Tuple[str, Any]]:
        """조건에 맞는 모델 공간 엔티티 (핸들, 객체) 목록. 작업 스레드 내부용

        selector:
          handles: [..]                     — 핸들 직접 지정
          window: [x1,y1,x2,y2], mode: "crossing"(기본, 걸치면 포함) | "inside"
          layers: [..]                      — 레이어 이름
          types: ["line","polyline","text","mtext","dimension","hatch",...]
        조건은 모두 AND. window 판정은 GetBoundingBox의 실제 외곽(텍스트 포함)으로 한다.
        """
        selector = selector or {}
        handles = selector.get("handles")
        window = selector.get("window")
        mode = str(selector.get("mode", "crossing")).lower()
        layers = set(selector.get("layers") or [])
        types = [str(t).lower() for t in (selector.get("types") or [])]
        if not (handles or window or layers or types):
            raise CADError("선택 조건이 없습니다 (handles/window/layers/types 중 하나 이상)")

        if window:
            if len(window) != 4:
                raise CADError("window는 [x1, y1, x2, y2] 형식이어야 합니다")
            x1, y1, x2, y2 = [float(v) for v in window]
            xmin, xmax, ymin, ymax = min(x1, x2), max(x1, x2), min(y1, y2), max(y1, y2)

        if handles:
            source = []
            for h in handles:
                try:
                    source.append(self._get(h))
                except CADError:
                    logger.info(f"선택: 핸들 {h} 없음, 건너뜀")
        else:
            source = list(self.doc.ModelSpace)

        result = []
        for entity in source:
            try:
                if layers and entity.Layer not in layers:
                    continue
                if types:
                    key = self._type_key(entity.ObjectName)
                    if not any(self._type_matches(key, t) for t in types):
                        continue
                if window:
                    bb = self._bbox(entity)
                    if bb is None:
                        continue
                    ex0, ey0, ex1, ey1 = bb
                    if mode == "inside":
                        if not (ex0 >= xmin and ex1 <= xmax and ey0 >= ymin and ey1 <= ymax):
                            continue
                    elif ex1 < xmin or ex0 > xmax or ey1 < ymin or ey0 > ymax:
                        continue
                result.append((str(entity.Handle), entity))
            except Exception as e:
                logger.debug(f"선택 판정 중 엔티티 건너뜀: {e}")
        return result

    def get_bbox(self, handles: Sequence[str]) -> List[Dict[str, Any]]:
        out = []
        for h in handles:
            try:
                bb = self._bbox(self._get(h))
                out.append({"handle": h, "bbox": [round(v, 2) for v in bb] if bb else None})
            except Exception as e:
                out.append({"handle": h, "error": describe_error(e)})
        return out

    # ==================================================================
    # 생성
    # ==================================================================

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

    def _add_polyline(self, space: Any, normalized: List[Tuple[float, float, float]]) -> Any:
        """폴리라인 생성. CAD별로 메서드 이름과 인자 형태가 달라 순차 시도"""
        attempts = []
        try:
            return space.AddPolyline(self._pts(normalized))
        except Exception as e:
            attempts.append(f"AddPolyline: {describe_error(e)}")
        try:
            if self._is_dispatch_point_cad():
                arg = self._pts(normalized)
            else:
                arg = self._variant_doubles([c for p in normalized for c in (p[0], p[1])])
            return space.AddLightWeightPolyline(arg)
        except Exception as e:
            attempts.append(f"AddLightWeightPolyline: {describe_error(e)}")
        try:
            return space.Add3DPoly(self._pts(normalized))
        except Exception as e:
            attempts.append(f"Add3DPoly: {describe_error(e)}")
        raise CADError("폴리라인 생성 실패: " + " | ".join(attempts))

    def _new_polyline(self, space: Any, points: Iterable, closed: bool = False) -> Any:
        pts = [self._normalize(p) for p in points]
        if closed and len(pts) > 2 and pts[0] == pts[-1]:
            pts = pts[:-1]     # 닫힘 플래그와 중복 끝점을 같이 쓰면 길이 0 구간이 생긴다
        if len(pts) < 2:
            raise CADError("폴리라인에는 점이 2개 이상 필요합니다")
        poly = self._add_polyline(space, pts)
        if closed and len(pts) > 2:
            poly.Closed = True
        return poly

    def _set_text_justify(self, text_obj: Any, justify: Optional[str], position) -> None:
        if not justify:
            return
        key = str(justify).lower().replace("-", "_").replace(" ", "_")
        key = _JUSTIFY_ALIASES.get(key, key)
        if key not in TEXT_JUSTIFY:
            raise CADError(f"justify '{justify}'는 지원하지 않습니다: {sorted(TEXT_JUSTIFY)}")
        code = TEXT_JUSTIFY[key]
        if code == 0:
            text_obj.Alignment = 0
            text_obj.InsertionPoint = self._pt(position)
            return
        text_obj.Alignment = code
        text_obj.TextAlignmentPoint = self._pt(position)

    @staticmethod
    def _attach_code(attachment: Optional[str]) -> Optional[int]:
        if attachment is None:
            return None
        if isinstance(attachment, int):
            return attachment
        key = str(attachment).lower().replace("-", "_").replace(" ", "_")
        key = _JUSTIFY_ALIASES.get(key, key)
        if key not in MTEXT_ATTACH:
            raise CADError(f"attachment '{attachment}'는 지원하지 않습니다: {sorted(MTEXT_ATTACH)}")
        return MTEXT_ATTACH[key]

    def _make_dimension(self, space: Any, spec: Dict[str, Any]) -> Any:
        p1 = self._normalize(spec.get("start") or spec.get("start_point"))
        p2 = self._normalize(spec.get("end") or spec.get("end_point"))
        orient = str(spec.get("orientation", "aligned")).lower()
        th = spec.get("textheight", spec.get("text_height"))
        text_pos = spec.get("text_position")
        if text_pos:
            loc = self._normalize(text_pos)
        else:
            offset = spec.get("offset")
            if offset is None:
                offset = 3.0 * float(th or 2.5)
            offset = float(offset)
            mx, my = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2
            if orient == "horizontal":
                base = max(p1[1], p2[1]) if offset >= 0 else min(p1[1], p2[1])
                loc = (mx, base + offset, 0.0)
            elif orient == "vertical":
                base = max(p1[0], p2[0]) if offset >= 0 else min(p1[0], p2[0])
                loc = (base + offset, my, 0.0)
            else:
                ux, uy = _unit(p2[0] - p1[0], p2[1] - p1[1])
                loc = (mx - uy * offset, my + ux * offset, 0.0)
        if orient == "horizontal":
            dim = space.AddDimRotated(self._pt(p1), self._pt(p2), self._pt(loc), 0.0)
        elif orient == "vertical":
            dim = space.AddDimRotated(self._pt(p1), self._pt(p2), self._pt(loc), math.pi / 2)
        elif orient == "rotated":
            dim = space.AddDimRotated(self._pt(p1), self._pt(p2), self._pt(loc),
                                      math.radians(float(spec.get("rotation", 0))))
        else:
            dim = space.AddDimAligned(self._pt(p1), self._pt(p2), self._pt(loc))
        if th:
            dim.TextHeight = float(th)
        if spec.get("text_override"):
            dim.TextOverride = str(spec["text_override"])
        return dim

    def _build(self, space: Any, spec: Dict[str, Any], out: List[Any]) -> None:
        """spec 하나를 만들어 out에 추가 (도형이 여러 개일 수 있음: door 등)"""
        kind = str(spec.get("type", "")).lower()

        if kind == "line":
            out.append(space.AddLine(self._pt(spec["start"]), self._pt(spec["end"])))

        elif kind == "circle":
            out.append(space.AddCircle(self._pt(spec["center"]), float(spec["radius"])))

        elif kind == "arc":
            # 각도는 도(degree), 반시계 방향으로 start -> end. 270 -> 0 도 90도 호다
            out.append(space.AddArc(self._pt(spec["center"]), float(spec["radius"]),
                                    math.radians(float(spec.get("start_angle", 0))),
                                    math.radians(float(spec.get("end_angle", 90)))))

        elif kind == "ellipse":
            major = float(spec.get("major_axis", 1.0))
            minor = float(spec.get("minor_axis", major / 2))
            rot = math.radians(float(spec.get("rotation", 0)))
            out.append(space.AddEllipse(
                self._pt(spec["center"]),
                self._vec(major * math.cos(rot), major * math.sin(rot), 0),
                minor / major))

        elif kind in ("rectangle", "rect"):
            x1, y1, _ = self._normalize(spec["corner1"])
            x2, y2, _ = self._normalize(spec["corner2"])
            out.append(self._new_polyline(space, [(x1, y1), (x2, y1), (x2, y2), (x1, y2)],
                                          closed=True))

        elif kind == "polyline":
            out.append(self._new_polyline(space, spec["points"], bool(spec.get("closed"))))

        elif kind == "text":
            pos = spec["position"]
            t = space.AddText(str(spec.get("text", "")), self._pt(pos),
                              float(spec.get("height", 2.5)))
            out.append(t)
            if spec.get("rotation"):
                t.Rotation = math.radians(float(spec["rotation"]))
            self._set_text_justify(t, spec.get("justify"), pos)

        elif kind == "mtext":
            m = space.AddMText(self._pt(spec["position"]), float(spec.get("width", 0) or 0),
                               str(spec.get("text", "")))
            out.append(m)
            m.Height = float(spec.get("height", 2.5))
            code = self._attach_code(spec.get("attachment"))
            if code:
                m.AttachmentPoint = code
            if spec.get("rotation"):
                m.Rotation = math.radians(float(spec["rotation"]))

        elif kind == "dimension":
            out.append(self._make_dimension(space, spec))

        elif kind == "hatch":
            boundary = self._new_polyline(space, spec["points"], closed=True)
            out.append(boundary)
            hatch = self._create_hatch(space, str(spec.get("pattern_name", "SOLID")))
            out.append(hatch)
            self._append_outer_loop(hatch, boundary)
            try:
                hatch.PatternScale = float(spec.get("scale", 1.0))
            except Exception as e:
                logger.warning(f"해치 스케일 설정 실패: {e}")
            for name in ("Evaluate", "Update"):
                fn = getattr(hatch, name, None)
                if fn is not None:
                    try:
                        fn()
                        break
                    except Exception:
                        continue

        elif kind == "wall":
            out.append(self._new_polyline(space, self._wall_corners(spec), closed=True))

        elif kind == "door":
            out.extend(self._door_entities(space, spec))

        elif kind == "window":
            out.extend(self._window_entities(space, spec))

        else:
            raise CADError(f"알 수 없는 도형 종류 '{kind}'. 지원: line, circle, arc, ellipse, "
                           "rectangle, polyline, text, mtext, dimension, hatch, wall, door, window")

    def _create(self, space: Any, spec: Dict[str, Any]) -> List[str]:
        """spec 하나 생성 + 스타일 + 핸들 확인. 중간에 실패하면 만든 것을 지운다

        반환값과 실제 도면이 어긋나지 않게 하는 핵심 경로다 (개선 요청 2-1).
        """
        made: List[Any] = []
        try:
            self._build(space, spec, made)
            handles = []
            for entity in made:
                self._apply_style(entity, spec.get("layer"), spec.get("color"),
                                  spec.get("lineweight"), spec.get("linetype"))
                h = self._handle_of(entity)
                if not h:
                    raise CADError("생성한 엔티티의 핸들을 읽지 못했습니다")
                handles.append(h)
            return handles
        except Exception:
            for entity in made:
                self._try_delete(entity)
            raise

    def _create_one(self, spec: Dict[str, Any]) -> str:
        handles = self._create(self.doc.ModelSpace, spec)
        self.refresh_view()
        return handles[0]

    # 단일 작도 도구 (반환: 핸들)
    def draw_line(self, start_point, end_point, layer=None, color=None, lineweight=None):
        return self._create_one({"type": "line", "start": start_point, "end": end_point,
                                 "layer": layer, "color": color, "lineweight": lineweight})

    def draw_circle(self, center, radius, layer=None, color=None, lineweight=None):
        return self._create_one({"type": "circle", "center": center, "radius": radius,
                                 "layer": layer, "color": color, "lineweight": lineweight})

    def draw_arc(self, center, radius, start_angle, end_angle, layer=None, color=None,
                 lineweight=None):
        return self._create_one({"type": "arc", "center": center, "radius": radius,
                                 "start_angle": start_angle, "end_angle": end_angle,
                                 "layer": layer, "color": color, "lineweight": lineweight})

    def draw_ellipse(self, center, major_axis, minor_axis, rotation=0, layer=None,
                     color=None, lineweight=None):
        return self._create_one({"type": "ellipse", "center": center,
                                 "major_axis": major_axis, "minor_axis": minor_axis,
                                 "rotation": rotation or 0, "layer": layer, "color": color,
                                 "lineweight": lineweight})

    def draw_polyline(self, points, closed=False, layer=None, color=None, lineweight=None):
        return self._create_one({"type": "polyline", "points": points, "closed": closed,
                                 "layer": layer, "color": color, "lineweight": lineweight})

    def draw_rectangle(self, corner1, corner2, layer=None, color=None, lineweight=None):
        return self._create_one({"type": "rectangle", "corner1": corner1, "corner2": corner2,
                                 "layer": layer, "color": color, "lineweight": lineweight})

    def draw_text(self, position, text, height=2.5, rotation=0, layer=None, color=None,
                  justify=None):
        return self._create_one({"type": "text", "position": position, "text": text,
                                 "height": height, "rotation": rotation, "justify": justify,
                                 "layer": layer, "color": color})

    def draw_mtext(self, position, text, height=2.5, width=0, attachment=None,
                   rotation=0, layer=None, color=None):
        return self._create_one({"type": "mtext", "position": position, "text": text,
                                 "height": height, "width": width, "attachment": attachment,
                                 "rotation": rotation, "layer": layer, "color": color})

    def draw_hatch(self, points, pattern_name="SOLID", scale=1.0, layer=None, color=None):
        if not points or len(points) < 3:
            raise CADError("해치 경계에는 점이 3개 이상 필요합니다")
        handles = self._create(self.doc.ModelSpace, {
            "type": "hatch", "points": points, "pattern_name": pattern_name,
            "scale": scale, "layer": layer, "color": color})
        self.refresh_view()
        return {"boundary": handles[0], "hatch": handles[1]}

    def add_dimension(self, start_point, end_point, text_position=None, textheight=None,
                      layer=None, color=None, orientation="aligned", offset=None):
        return self._create_one({"type": "dimension", "start": start_point, "end": end_point,
                                 "text_position": text_position, "textheight": textheight,
                                 "orientation": orientation, "offset": offset,
                                 "layer": layer, "color": color})

    def draw_entities(self, entities: List[Dict[str, Any]], atomic: bool = False,
                      default_layer: Optional[str] = None,
                      default_color: Any = None) -> Dict[str, Any]:
        """여러 도형을 한 번에. 항목별 결과(핸들 또는 오류)를 순서대로 반환

        atomic=True면 하나라도 실패할 때 이번 호출에서 만든 것을 전부 지운다.
        """
        ms = self.doc.ModelSpace
        results: List[Dict[str, Any]] = []
        created: List[str] = []
        for i, raw in enumerate(entities or []):
            spec = dict(raw)
            if spec.get("layer") is None and default_layer:
                spec["layer"] = default_layer
            if spec.get("color") is None and default_color is not None:
                spec["color"] = default_color
            try:
                handles = self._create(ms, spec)
                created.extend(handles)
                results.append({"index": i, "handles": handles})
            except Exception as e:
                results.append({"index": i, "error": describe_error(e)})
                if atomic:
                    rolled = self._delete_handles(created)
                    self.refresh_view()
                    return {"results": results, "rolled_back": rolled, "atomic_failed_at": i}
        self.refresh_view()
        return {"results": results}

    def add_dimension_chain(self, points, orientation="horizontal", offset=None,
                            overall=True, overall_offset=None, textheight=None,
                            layer=None, color=None) -> Dict[str, Any]:
        """연속 치수 + 전체 치수를 한 번에

        points: 치수를 잴 점들(예: 그리드 교점). 방향에 맞춰 정렬한다.
        offset: 점들 중 가장 바깥 점에서 치수선까지 거리. 양수면 위(수평)/오른쪽
                (수직), 음수면 아래/왼쪽.
        overall_offset: 전체 치수선 거리. 생략하면 offset보다 한 단 더 바깥.
        """
        orient = str(orientation).lower()
        if orient not in ("horizontal", "vertical"):
            raise CADError("orientation은 horizontal 또는 vertical")
        pts = [self._normalize(p) for p in points]
        axis = 0 if orient == "horizontal" else 1
        pts.sort(key=lambda p: p[axis])
        dedup = []
        for p in pts:
            if not dedup or abs(p[axis] - dedup[-1][axis]) > 1e-6:
                dedup.append(p)
        if len(dedup) < 2:
            raise CADError("서로 다른 점이 2개 이상 필요합니다")
        th = float(textheight) if textheight else None
        if offset is None:
            offset = 4.0 * (th or 2.5)
        offset = float(offset)
        sign = 1.0 if offset >= 0 else -1.0
        if overall_offset is None:
            overall_offset = offset + sign * max(abs(offset) * 0.6, 3.0 * (th or 2.5))
        cross = 1 - axis
        base = (max if sign > 0 else min)(p[cross] for p in dedup)

        def spec(a, b, off):
            line = base + off
            s = {"type": "dimension", "start": a, "end": b, "orientation": orient,
                 "textheight": th, "layer": layer, "color": color}
            mid = (a[axis] + b[axis]) / 2
            s["text_position"] = (mid, line, 0.0) if axis == 0 else (line, mid, 0.0)
            return s

        ms = self.doc.ModelSpace
        chain, created = [], []
        try:
            for a, b in zip(dedup, dedup[1:]):
                h = self._create(ms, spec(a, b, offset))
                chain += h
                created += h
            total = None
            if overall and len(dedup) > 2:
                total = self._create(ms, spec(dedup[0], dedup[-1], float(overall_offset)))[0]
                created.append(total)
        except Exception:
            self._delete_handles(created)
            raise
        self.refresh_view()
        return {"chain": chain, "overall": total}

    # --- 평면도 도메인 형상 ---------------------------------------------------

    def _wall_corners(self, spec: Dict[str, Any]) -> List[Tuple[float, float]]:
        sx, sy, _ = self._normalize(spec["start"])
        ex, ey, _ = self._normalize(spec["end"])
        t = float(spec.get("thickness", 100))
        ux, uy = _unit(ex - sx, ey - sy)
        nx, ny = -uy, ux                      # 진행 방향의 왼쪽
        justify = str(spec.get("justify", "center")).lower()
        if justify == "left":
            a, b = 0.0, t
        elif justify == "right":
            a, b = -t, 0.0
        else:
            a, b = -t / 2, t / 2
        return [(sx + nx * a, sy + ny * a), (ex + nx * a, ey + ny * a),
                (ex + nx * b, ey + ny * b), (sx + nx * b, sy + ny * b)]

    def _door_geometry(self, hinge, width: float, closed_deg: float,
                       swing: str) -> Tuple[Tuple, Tuple, float, float]:
        """문짝 끝점과 호 각도 (도). closed_deg: 경첩에서 닫힌 문짝이 향하는 방향"""
        hx, hy, _ = self._normalize(hinge)
        open_deg = closed_deg + (90.0 if swing == "left" else -90.0)
        tip = (hx + width * math.cos(math.radians(open_deg)),
               hy + width * math.sin(math.radians(open_deg)))
        # 호는 반시계 방향이므로 작은 각에서 큰 각으로
        start, end = (closed_deg, open_deg) if swing == "left" else (open_deg, closed_deg)
        return (hx, hy), tip, start, end

    def _door_entities(self, space: Any, spec: Dict[str, Any]) -> List[Any]:
        """문 기호: 90도 열린 문짝 선 + 1/4 호

        hinge: 경첩 위치, width: 문 폭, angle: 경첩에서 닫힌 문짝 끝(반대쪽
        문설주)을 향하는 방향(도), swing: "left"(그 방향 기준 왼쪽으로 열림) |
        "right"
        """
        swing = str(spec.get("swing", "left")).lower()
        if swing not in ("left", "right"):
            raise CADError("swing은 left 또는 right")
        width = float(spec["width"])
        hinge, tip, a0, a1 = self._door_geometry(spec["hinge"], width,
                                                 float(spec.get("angle", 0)), swing)
        leaf = space.AddLine(self._pt(hinge), self._pt(tip))
        arc = space.AddArc(self._pt(hinge), width, math.radians(a0), math.radians(a1))
        return [leaf, arc]

    def _window_entities(self, space: Any, spec: Dict[str, Any]) -> List[Any]:
        """창 기호: 벽 축 방향 평행선 n개 (start-end는 벽 중심선 위의 창 구간)"""
        sx, sy, _ = self._normalize(spec["start"])
        ex, ey, _ = self._normalize(spec["end"])
        t = float(spec.get("thickness", 200))
        n = max(2, int(spec.get("lines", 3)))
        ux, uy = _unit(ex - sx, ey - sy)
        nx, ny = -uy, ux
        out = []
        for k in range(n):
            off = -t / 2 + t * k / (n - 1)
            out.append(space.AddLine(self._pt((sx + nx * off, sy + ny * off)),
                                     self._pt((ex + nx * off, ey + ny * off))))
        return out

    # --- 해치 (캐디안 인자 탐색) ------------------------------------------------

    def _create_hatch(self, space: Any, pattern_name: str) -> Any:
        """해치 객체 생성

        AddHatch(PatternType, PatternName, Associativity, HatchObjectType)
          - PatternType: 0=사용자정의, 1=미리정의, 2=커스텀
            "SOLID" 같은 표준 패턴은 1이어야 한다.
          - HatchObjectType: 생략하면 E_INVALIDARG로 거부하는 구현이 있어 명시
            전달도 시도한다. 성공한 조합은 캐시한다.
        """
        if self._hatch_args is not None:
            pattern_type, extra = self._hatch_args
            if extra is None:
                return space.AddHatch(pattern_type, pattern_name, True)
            return space.AddHatch(pattern_type, pattern_name, True, extra)

        errors = []
        for pattern_type, extra in ((1, 0), (1, None), (0, 0), (0, None), (2, 0)):
            try:
                if extra is None:
                    hatch = space.AddHatch(pattern_type, pattern_name, True)
                else:
                    hatch = space.AddHatch(pattern_type, pattern_name, True, extra)
                self._hatch_args = (pattern_type, extra)
                logger.info(f"AddHatch 인자 조합 확정: PatternType={pattern_type}, "
                            f"HatchObjectType={'생략' if extra is None else extra}")
                return hatch
            except Exception as e:
                errors.append(f"({pattern_type}, {extra}): {describe_error(e)[:60]}")
        raise CADError("AddHatch 실패: " + " | ".join(errors))

    def _append_outer_loop(self, hatch: Any, boundary: Any) -> None:
        """해치 외부 경계 추가 (메서드 이름/인자 형태가 CAD별로 달라 후보 순차 시도)"""
        available = [n for n in dir(hatch) if 'oop' in n]
        ordered = [n for n in ("AppendOuterLoop", "AppendLoop", "InsertLoop") if n in available]
        ordered += [n for n in available
                    if n not in ordered and n.lower().startswith('append')
                    and 'inner' not in n.lower()]
        if not ordered:
            raise CADError(f"해치 경계 추가 메서드를 찾을 수 없습니다: {available}")
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
                    return
                except Exception as e:
                    errors.append(f"{method_name}/{describe}: {describe_error(e)[:60]}")
        raise CADError("해치 경계 추가 실패: " + " | ".join(errors))

    # ==================================================================
    # 편집
    # ==================================================================

    EDIT_OPERATIONS = ("move", "rotate", "scale", "copy", "mirror", "offset",
                       "delete", "set_properties")

    def _new_handles(self, ret: Any) -> List[str]:
        """Offset 등이 돌려준 객체/배열/선택집합에서 핸들 추출"""
        if ret is None:
            return []
        found = []
        try:
            items = list(ret)
        except Exception:
            items = [ret]
        for item in items:
            h = self._handle_of(item)
            if h:
                found.append(h)
        return found

    def _edit(self, entity: Any, handle: str, op: str, p: Dict[str, Any]) -> Dict[str, Any]:
        op = str(op).lower()
        if op == "move":
            dx, dy, dz = self._normalize(p["offset"])
            entity.Move(self._pt((0, 0, 0)), self._pt((dx, dy, dz)))
            return {"handle": handle}
        if op == "rotate":
            entity.Rotate(self._pt(p.get("base_point", (0, 0))), math.radians(float(p["angle"])))
            return {"handle": handle}
        if op == "scale":
            factor = float(p["factor"])
            if factor <= 0:
                raise CADError("축척 배율은 0보다 커야 합니다")
            entity.ScaleEntity(self._pt(p.get("base_point", (0, 0))), factor)
            return {"handle": handle}
        if op == "copy":
            new = entity.Copy()
            if p.get("offset"):
                dx, dy, dz = self._normalize(p["offset"])
                new.Move(self._pt((0, 0, 0)), self._pt((dx, dy, dz)))
            return {"handle": handle, "new": self._handle_of(new)}
        if op == "mirror":
            make_copy = bool(p.get("make_copy", True))
            ret = entity.Mirror(self._pt(p["point1"]), self._pt(p["point2"]), make_copy)
            new = self._handle_of(ret)
            out = {"handle": handle}
            if make_copy and new and new != handle:
                out["new"] = new
            return out
        if op == "offset":
            ret = entity.Offset(float(p["distance"]))
            return {"handle": handle, "new": self._new_handles(ret)}
        if op == "delete":
            entity.Delete()
            return {"handle": handle, "deleted": True}
        if op == "set_properties":
            keys = ("layer", "color", "lineweight", "linetype")
            if not any(p.get(k) is not None for k in keys):
                raise CADError("set_properties에는 layer/color/lineweight/linetype 중 하나가 필요합니다")
            self._apply_style(entity, p.get("layer"), p.get("color"), p.get("lineweight"),
                              p.get("linetype"), defaults=False)
            return {"handle": handle}
        raise CADError(f"지원하지 않는 편집 작업: {op} ({', '.join(self.EDIT_OPERATIONS)})")

    def edit_entity(self, handle: str, operation: str, **params) -> Dict[str, Any]:
        result = self._edit(self._get(handle), str(handle), operation, params)
        self.refresh_view()
        return result

    def edit_entities(self, operation: str, handles: Sequence[str] = None,
                      selector: Dict[str, Any] = None, **params) -> Dict[str, Any]:
        """여러 엔티티에 같은 편집을 한 번에. handles 또는 selector로 대상 지정"""
        if handles and selector:
            raise CADError("handles와 selector 중 하나만 주세요")
        if not handles and not selector:
            raise CADError("handles 또는 selector가 필요합니다")
        results = []
        if handles:
            for h in handles:
                try:
                    results.append(self._edit(self._get(h), str(h), operation, params))
                except Exception as e:
                    results.append({"handle": str(h), "error": describe_error(e)})
            matched = len(handles)
        else:
            targets = self.select(selector)
            for handle, entity in targets:
                try:
                    results.append(self._edit(entity, handle, operation, params))
                except Exception as e:
                    results.append({"handle": handle, "error": describe_error(e)})
            matched = len(targets)
        if results:
            self.refresh_view()
        return {"matched": matched, "results": results}

    def _delete_handles(self, handles: Iterable[str]) -> int:
        n = 0
        for h in handles:
            try:
                self.doc.HandleToObject(str(h)).Delete()
                n += 1
            except Exception:
                pass
        return n

    def delete_by_layer(self, layer: str) -> int:
        targets = self.select({"layers": [layer]})
        for _, entity in targets:
            entity.Delete()
        self.refresh_view()
        return len(targets)

    def delete_by_window(self, x1, y1, x2, y2, mode="crossing",
                         layers=None, types=None) -> int:
        targets = self.select({"window": [x1, y1, x2, y2], "mode": mode,
                               "layers": layers, "types": types})
        for _, entity in targets:
            entity.Delete()
        self.refresh_view()
        return len(targets)

    def edit_text(self, handle: str, text: str = None, height: float = None,
                  position=None, justify: str = None, attachment: str = None,
                  width: float = None, rotation: float = None) -> Dict[str, Any]:
        """Text/MText 내용·높이·위치·정렬 변경"""
        entity = self._get(handle)
        kind = self._type_key(entity.ObjectName)
        if kind == "text":
            if text is not None:
                entity.TextString = str(text)
            if height is not None:
                entity.Height = float(height)
            if rotation is not None:
                entity.Rotation = math.radians(float(rotation))
            if justify is not None:
                anchor = position
                if anchor is None:
                    # 위치를 안 주면 지금 보이는 자리 기준으로 새 정렬점을 정한다
                    x0, y0, x1, y1 = self._bbox(entity)
                    key = _JUSTIFY_ALIASES.get(str(justify).lower(), str(justify).lower())
                    ax = x0 if "left" in key or key == "left" else \
                        x1 if "right" in key else (x0 + x1) / 2
                    if key.startswith("top"):
                        ay = y1
                    elif key.startswith("bottom"):
                        ay = y0
                    elif key.startswith("middle"):
                        ay = (y0 + y1) / 2
                    else:
                        ay = entity.InsertionPoint.y
                    anchor = (ax, ay)
                self._set_text_justify(entity, justify, anchor)
            elif position is not None:
                if entity.Alignment == 0:
                    entity.InsertionPoint = self._pt(position)
                else:
                    entity.TextAlignmentPoint = self._pt(position)
        elif kind == "mtext":
            if text is not None:
                entity.TextString = str(text)
            if height is not None:
                entity.Height = float(height)
            if width is not None:
                entity.Width = float(width)
            if rotation is not None:
                entity.Rotation = math.radians(float(rotation))
            code = self._attach_code(attachment or justify)
            if code:
                entity.AttachmentPoint = code
            if position is not None:
                entity.InsertionPoint = self._pt(position)
        else:
            raise CADError(f"핸들 {handle}은(는) 텍스트가 아닙니다 ({entity.ObjectName})")
        self.refresh_view()
        info = cad_reader.read_entity(self._get(handle), self.lib)
        return {k: info.get(k) for k in ("Handle", "ObjectName", "text", "height",
                                         "justify", "attachment", "width", "bbox") if k in info}

    # --- 개구부 / 끊기 ---------------------------------------------------------

    def _poly_points(self, entity: Any) -> Tuple[List[Tuple[float, float]], bool]:
        pts = [(p[0], p[1]) for p in cad_reader.points_list(entity.Coordinates, self.lib)]
        closed = False
        try:
            closed = bool(entity.Closed)
        except Exception:
            pass
        if len(pts) > 1 and math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < 1e-6:
            pts = pts[:-1]
            closed = True
        try:
            for i in range(len(pts)):
                if abs(entity.GetBulge(i)) > 1e-9:
                    raise CADError("호(bulge) 구간이 있는 폴리라인은 아직 지원하지 않습니다")
        except CADError:
            raise
        except Exception:
            pass
        return pts, closed

    def insert_opening(self, handle: str, center, width: float, kind: str = "none",
                       swing_toward=None, hinge_at=None, door_layer: str = None,
                       window_layer: str = None, window_lines: int = 3) -> Dict[str, Any]:
        """사각형 벽(꼭짓점 4개 닫힌 폴리라인)에 개구부를 낸다

        벽을 개구부 양옆 두 조각으로 다시 그리고 원래 벽은 지운다. kind가
        door/window면 그 자리에 문/창 기호도 그린다.
        """
        wall = self._get(handle)
        pts, closed = self._poly_points(wall)
        if len(pts) != 4 or not closed:
            raise CADError(f"벽은 꼭짓점 4개짜리 닫힌 폴리라인이어야 합니다 "
                           f"(현재 {len(pts)}개, 닫힘={closed})")
        edges = [(pts[i], pts[(i + 1) % 4]) for i in range(4)]
        a, b = max(edges, key=lambda e: math.hypot(e[1][0] - e[0][0], e[1][1] - e[0][1]))
        ux, uy = _unit(b[0] - a[0], b[1] - a[1])
        nx, ny = -uy, ux
        ss = [(p[0] - a[0]) * ux + (p[1] - a[1]) * uy for p in pts]
        tt = [(p[0] - a[0]) * nx + (p[1] - a[1]) * ny for p in pts]
        s_min, s_max, t_min, t_max = min(ss), max(ss), min(tt), max(tt)
        cx, cy, _ = self._normalize(center)
        sc = (cx - a[0]) * ux + (cy - a[1]) * uy
        w = float(width)
        s1, s2 = sc - w / 2, sc + w / 2
        eps = 1e-6
        if s1 < s_min - eps or s2 > s_max + eps:
            raise CADError(f"개구부({s1:.0f}~{s2:.0f})가 벽 길이({s_min:.0f}~{s_max:.0f})를 벗어납니다")

        def at(s, t):
            return (a[0] + ux * s + nx * t, a[1] + uy * s + ny * t)

        style = self._style_of(wall)
        ms = self.doc.ModelSpace
        made_handles: Dict[str, List[str]] = {"pieces": [], "door": [], "window": []}
        created: List[str] = []
        try:
            for lo, hi in ((s_min, s1), (s2, s_max)):
                if hi - lo > eps:
                    spec = {"type": "polyline", "closed": True,
                            "points": [at(lo, t_min), at(hi, t_min), at(hi, t_max), at(lo, t_max)],
                            **style}
                    h = self._create(ms, spec)
                    made_handles["pieces"] += h
                    created += h
            kind = str(kind or "none").lower()
            if kind == "door":
                if hinge_at is not None:
                    hx, hy, _ = self._normalize(hinge_at)
                    sh = (hx - a[0]) * ux + (hy - a[1]) * uy
                    s_hinge, s_other = (s1, s2) if abs(sh - s1) <= abs(sh - s2) else (s2, s1)
                else:
                    s_hinge, s_other = s1, s2
                t_mid = (t_min + t_max) / 2
                side = 1.0
                if swing_toward is not None:
                    px, py, _ = self._normalize(swing_toward)
                    side = 1.0 if (px - a[0]) * nx + (py - a[1]) * ny >= t_mid else -1.0
                t_face = t_max if side > 0 else t_min
                hinge = at(s_hinge, t_face)
                closed_dir = (ux * (1 if s_other > s_hinge else -1),
                              uy * (1 if s_other > s_hinge else -1))
                closed_deg = math.degrees(math.atan2(closed_dir[1], closed_dir[0]))
                # 열리는 쪽(면 바깥 방향)이 닫힌 방향의 왼쪽이면 left
                cross = closed_dir[0] * (ny * side) - closed_dir[1] * (nx * side)
                swing = "left" if cross > 0 else "right"
                h = self._create(ms, {"type": "door", "hinge": hinge, "width": w,
                                      "angle": closed_deg, "swing": swing,
                                      "layer": door_layer or self.door_layer})
                made_handles["door"] = h
                created += h
            elif kind == "window":
                mid_t = (t_min + t_max) / 2
                h = self._create(ms, {"type": "window", "start": at(s1, mid_t), "end": at(s2, mid_t),
                                      "thickness": t_max - t_min, "lines": window_lines,
                                      "layer": window_layer or self.window_layer})
                made_handles["window"] = h
                created += h
            elif kind != "none":
                raise CADError("kind는 none, door, window 중 하나")
            wall.Delete()
        except Exception:
            self._delete_handles(created)
            raise
        self.refresh_view()
        made_handles["removed"] = str(handle)
        return made_handles

    def break_entity(self, handle: str, p1, p2, remove: str = "short") -> Dict[str, Any]:
        """선/폴리라인을 두 점 사이에서 끊어 낸다 (BREAK)

        닫힌 폴리라인은 두 점 사이의 두 경로 중 remove="short"(기본)면 짧은 쪽을,
        "long"이면 긴 쪽을 지우고 나머지를 열린 폴리라인 하나로 남긴다.
        """
        entity = self._get(handle)
        kind = self._type_key(entity.ObjectName)
        style = self._style_of(entity)
        ms = self.doc.ModelSpace
        q1 = self._normalize(p1)[:2]
        q2 = self._normalize(p2)[:2]

        if kind == "line":
            s = entity.StartPoint
            e = entity.EndPoint
            sp, ep = (s.x, s.y), (e.x, e.y)
            dx, dy = ep[0] - sp[0], ep[1] - sp[1]
            length2 = dx * dx + dy * dy
            ts = sorted(max(0.0, min(1.0, ((q[0] - sp[0]) * dx + (q[1] - sp[1]) * dy) / length2))
                        for q in (q1, q2))
            pieces = []
            for t0, t1 in ((0.0, ts[0]), (ts[1], 1.0)):
                if t1 - t0 > 1e-9:
                    pieces.append([(sp[0] + dx * t0, sp[1] + dy * t0), (sp[0] + dx * t1, sp[1] + dy * t1)])
        elif "polyline" in kind:
            pts, closed = self._poly_points(entity)
            segs = list(zip(pts, pts[1:] + ([pts[0]] if closed else [])))
            cum = [0.0]
            for (x0, y0), (x1, y1) in segs:
                cum.append(cum[-1] + math.hypot(x1 - x0, y1 - y0))
            total = cum[-1]

            def project(q):
                best = None
                for i, ((x0, y0), (x1, y1)) in enumerate(segs):
                    dx, dy = x1 - x0, y1 - y0
                    l2 = dx * dx + dy * dy
                    t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((q[0] - x0) * dx + (q[1] - y0) * dy) / l2))
                    px, py = x0 + dx * t, y0 + dy * t
                    d = math.hypot(q[0] - px, q[1] - py)
                    if best is None or d < best[0]:
                        best = (d, cum[i] + t * math.sqrt(l2))
                return best[1]

            def point_at(s):
                s = max(0.0, min(total, s))
                for i, ((x0, y0), (x1, y1)) in enumerate(segs):
                    if cum[i + 1] >= s - 1e-12:
                        seg = cum[i + 1] - cum[i]
                        t = 0.0 if seg == 0 else (s - cum[i]) / seg
                        return (x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)
                return segs[-1][1]

            def path(s_from, s_to):
                """s_from -> s_to 구간의 점 열 (s_to < s_from이면 끝을 돌아 감는다)"""
                out = [point_at(s_from)]
                if s_to >= s_from:
                    out += [pts[i] for i in range(len(pts)) if s_from < cum[i] < s_to]
                else:
                    out += [pts[i] for i in range(len(pts)) if cum[i] > s_from]
                    if closed:
                        out += [pts[0]] if s_from < total else []
                    out += [pts[i] for i in range(len(pts)) if 0 < cum[i] < s_to]
                out.append(point_at(s_to))
                clean = [out[0]]
                for q in out[1:]:
                    if math.hypot(q[0] - clean[-1][0], q[1] - clean[-1][1]) > 1e-6:
                        clean.append(q)
                return clean

            a_s, b_s = sorted((project(q1), project(q2)))
            if b_s - a_s < 1e-6:
                raise CADError("두 점이 같은 위치로 투영돼 끊을 구간이 없습니다")
            if closed:
                inner = b_s - a_s
                outer = total - inner
                remove_inner = (inner <= outer) if remove == "short" else (inner > outer)
                pieces = [path(b_s, a_s) if remove_inner else path(a_s, b_s)]
            else:
                pieces = []
                if a_s > 1e-6:
                    pieces.append(path(0.0, a_s))
                if total - b_s > 1e-6:
                    pieces.append(path(b_s, total))
        else:
            raise CADError(f"끊기는 선과 폴리라인만 지원합니다 ({entity.ObjectName})")

        created = []
        try:
            for piece in pieces:
                if len(piece) >= 2:
                    created += self._create(ms, {"type": "polyline" if len(piece) > 2 else "line",
                                                 "points": piece, "start": piece[0], "end": piece[-1],
                                                 **style})
            entity.Delete()
        except Exception:
            self._delete_handles(created)
            raise
        self.refresh_view()
        return {"removed": str(handle), "pieces": created}

    # --- 배열 (기존 동작 유지) --------------------------------------------------

    def array_polar(self, handle: str, count: int, center, included_angle: float = 360.0,
                    rotate_objects: bool = True) -> bool:
        """원형 배열 복제

        PointOnAxis 기본값이 VT_DISPATCH 자리에 정수 0을 넣어 실패하므로
        중심점을 명시적으로 한 번 더 넘긴다 (같은 점이면 2D 배열).
        """
        entity = self._get(handle)
        center_pt = self._pt(center)
        angle_rad = math.radians(float(included_angle))
        attempts = [
            ("중심점 중복 전달",
             lambda: entity.ArrayPolar(int(count), angle_rad, center_pt, center_pt,
                                       bool(rotate_objects))),
            ("VT_DISPATCH null",
             lambda: entity.ArrayPolar(int(count), angle_rad, center_pt,
                                       win32com.client.VARIANT(pythoncom.VT_DISPATCH, None),
                                       bool(rotate_objects))),
            ("3인자", lambda: entity.ArrayPolar(int(count), angle_rad, center_pt)),
        ]
        errors = []
        for label, call in attempts:
            try:
                call()
                self.refresh_view()
                return True
            except Exception as e:
                errors.append(f"{label}: {describe_error(e)[:50]}")
        raise CADError("원형배열 실패: " + " | ".join(errors))

    def array_rectangular(self, handle: str, rows: int, columns: int,
                          row_spacing: float, column_spacing: float,
                          levels: int = 1, level_spacing: float = 0.0) -> bool:
        entity = self._get(handle)
        entity.ArrayRectangular(int(rows), int(columns), int(levels),
                                float(row_spacing), float(column_spacing),
                                float(level_spacing))
        self.refresh_view()
        return True

    # ==================================================================
    # 레이어 / 현재값 / 정리
    # ==================================================================

    def _layer_usage(self) -> Tuple[Dict[str, int], set]:
        """레이어별 엔티티 수(모델+페이퍼+블록 정의 전체)와 참조된 블록 이름"""
        counts: Dict[str, int] = {}
        referenced = set()

        def tally(container):
            for entity in container:
                try:
                    counts[entity.Layer] = counts.get(entity.Layer, 0) + 1
                    if self._type_key(entity.ObjectName) == "blockreference":
                        referenced.add(entity.Name)
                except Exception:
                    continue

        saw_model = False
        for block in self.doc.Blocks:
            try:
                if block.Name.lower() == "*model_space":
                    saw_model = True
            except Exception:
                pass
            tally(block)
        if not saw_model:   # 블록 컬렉션에 모델 공간이 안 잡히는 구현 대비
            tally(self.doc.ModelSpace)
        return counts, referenced

    def list_layers(self) -> List[Dict[str, Any]]:
        # entities는 모델 공간 개수. 치수·블록 내부까지 센 사용 여부(in_use)는
        # 따로 둔다 — 합쳐서 보이면 치수 20개짜리 레이어가 400개로 보인다.
        usage, _ = self._layer_usage()
        model: Dict[str, int] = {}
        for entity in self.doc.ModelSpace:
            try:
                model[entity.Layer] = model.get(entity.Layer, 0) + 1
            except Exception:
                continue
        current = self.doc.GetVariable("CLAYER")
        out = []
        for layer in self.doc.Layers:
            info = {"name": layer.Name, "color": layer.color, "on": bool(layer.LayerOn),
                    "frozen": bool(layer.Freeze), "locked": bool(layer.Lock),
                    "entities": model.get(layer.Name, 0),
                    "in_use": bool(usage.get(layer.Name))}
            if layer.Name == current:
                info["current"] = True
            out.append(info)
        return out

    def layer_action(self, action: str, name: str = None, new_name: str = None,
                     color: Any = None, on: bool = None, frozen: bool = None,
                     locked: bool = None, lineweight=None) -> Dict[str, Any]:
        action = str(action or "list").lower()
        if action == "list":
            return {"layers": self.list_layers()}
        if not name:
            raise CADError(f"{action}에는 name이 필요합니다")
        layer = self._layer_obj(name)
        if action == "create":
            if layer is None:
                layer = self.doc.Layers.Add(name)
                self._known_layers.add(name)
            action = "set"
        if layer is None:
            raise CADError(f"레이어 '{name}'이(가) 없습니다")
        if action == "delete":
            if name in ("0", "Defpoints"):
                raise CADError(f"'{name}' 레이어는 지울 수 없습니다")
            if self.doc.GetVariable("CLAYER") == name:
                raise CADError("현재 레이어는 지울 수 없습니다. set_current로 바꾼 뒤 지우세요")
            counts, _ = self._layer_usage()
            if counts.get(name):
                raise CADError(f"'{name}' 레이어에 엔티티 {counts[name]}개가 있어 지울 수 없습니다. "
                               "delete_by_layer로 비우거나 edit_entities set_properties로 옮기세요")
            layer.Delete()
            self._known_layers.discard(name)
            return {"deleted": name}
        if action == "rename":
            if not new_name:
                raise CADError("rename에는 new_name이 필요합니다")
            if name == "0":
                raise CADError("'0' 레이어는 이름을 바꿀 수 없습니다")
            layer.Name = str(new_name)
            self._known_layers.discard(name)
            return {"renamed": [name, new_name]}
        if action == "set":
            code = parse_color(color)
            if code is not None:
                if code in (0, BYLAYER):
                    raise CADError("레이어 색은 1~255 번호로 지정하세요")
                layer.color = code
            if on is not None:
                layer.LayerOn = bool(on)
            if frozen is not None:
                layer.Freeze = bool(frozen)
            if locked is not None:
                layer.Lock = bool(locked)
            if lineweight is not None:
                layer.Lineweight = self.validate_lineweight(lineweight)
            return {"layer": name, "color": layer.color, "on": bool(layer.LayerOn),
                    "frozen": bool(layer.Freeze), "locked": bool(layer.Lock)}
        raise CADError(f"지원하지 않는 action: {action} (list/create/delete/rename/set)")

    def set_current(self, layer: str = None, color: Any = None) -> Dict[str, Any]:
        """현재 레이어(CLAYER)·현재 색(CECOLOR) 설정"""
        if layer is None and color is None:
            raise CADError("layer 또는 color 중 하나는 주세요")
        if layer is not None:
            self._ensure_layer(layer)
            self.doc.ActiveLayer = self._layer_obj(layer)
        if color is not None:
            self.doc.SetVariable("CECOLOR", color_sysvar(color))
        return {"clayer": self.doc.GetVariable("CLAYER"),
                "cecolor": self.doc.GetVariable("CECOLOR")}

    def current_settings(self) -> Dict[str, Any]:
        return {"clayer": self.doc.GetVariable("CLAYER"),
                "cecolor": self.doc.GetVariable("CECOLOR")}

    def purge(self, kind: str = "all") -> Dict[str, Any]:
        """쓰지 않는 레이어/블록 정리. 엔티티가 있는 레이어는 절대 지우지 않는다"""
        kind = str(kind or "all").lower()

        def names(coll):
            return {x.Name for x in coll}

        before_layers = names(self.doc.Layers)
        before_blocks = {n for n in names(self.doc.Blocks) if not n.startswith('*')}
        if kind == "all":
            self.doc.PurgeAll()
            self.doc.PurgeAll()   # 블록 안의 블록까지 한 번 더
        elif kind in ("layers", "blocks"):
            counts, referenced = self._layer_usage()
            if kind == "layers":
                current = self.doc.GetVariable("CLAYER")
                for layer in list(self.doc.Layers):
                    n = layer.Name
                    if n in ("0", "Defpoints") or n == current or counts.get(n):
                        continue
                    try:
                        layer.Delete()
                    except Exception as e:
                        logger.warning(f"레이어 '{n}' 정리 실패: {describe_error(e)}")
            else:
                for block in list(self.doc.Blocks):
                    n = block.Name
                    if n.startswith('*') or n in referenced:
                        continue
                    try:
                        if getattr(block, "IsXRef", False):
                            continue
                        block.Delete()
                    except Exception as e:
                        logger.warning(f"블록 '{n}' 정리 실패: {describe_error(e)}")
        else:
            raise CADError("kind는 all, layers, blocks 중 하나")
        self._known_layers = set()
        return {"removed_layers": sorted(before_layers - names(self.doc.Layers)),
                "removed_blocks": sorted(before_blocks - names(self.doc.Blocks))}

    # ==================================================================
    # 뷰 / 명령 / 저장 / 실행 취소
    # ==================================================================

    def refresh_view(self) -> None:
        try:
            self.doc.Regen(1)  # acAllViewports = 1
        except Exception as e:
            logger.warning(f"뷰 갱신 실패: {describe_error(e)}")

    def zoom_extents(self) -> bool:
        """전체 보기. Regen만으로는 EXTMIN/EXTMAX가 갱신되지 않아 이게 필요하다"""
        errors = []
        for label, call in (("ActiveViewport.ZoomExtents", lambda: self.doc.ActiveViewport.ZoomExtents()),
                            ("Application.ZoomExtents", lambda: self.app.ZoomExtents())):
            try:
                call()
                return True
            except Exception as e:
                errors.append(f"{label}: {describe_error(e)}")
        try:
            self.send_command("_.ZOOM\n_E\n", wait=3.0)
            return True
        except Exception as e:
            errors.append(f"SendCommand: {describe_error(e)}")
        logger.error("전체 보기 실패: " + " | ".join(errors))
        return False

    def extents(self) -> Dict[str, Any]:
        self.zoom_extents()
        self.refresh_view()
        info = {}
        for var, key in (("EXTMIN", "extmin"), ("EXTMAX", "extmax")):
            xyz = cad_reader.pt_xyz(self.doc.GetVariable(var))
            info[key] = [round(v, 2) for v in xyz] if xyz else None
        return info

    def _wait_idle(self, limit: float) -> Optional[bool]:
        """명령이 끝날 때까지 대기 (CMDACTIVE=0). 확인할 수 없으면 None"""
        deadline = time.monotonic() + max(0.0, limit)
        while True:
            try:
                if int(self.doc.GetVariable("CMDACTIVE")) == 0:
                    return True
            except Exception:
                time.sleep(min(limit, 0.5))
                return None
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)

    def send_command(self, command: str, cancel_first: bool = True,
                     wait: float = 5.0) -> Dict[str, Any]:
        """명령줄에 그대로 보낸다. 끝에 개행이 없으면 붙인다

        입력을 기다리는 명령을 보내면 CAD가 그 상태로 멈춘다 — 끝난 뒤에도
        명령이 살아 있으면 ESC로 취소하고 그렇다고 알려준다.
        """
        if not command:
            raise CADError("command가 비어 있습니다")
        before = self.doc.ModelSpace.Count
        if cancel_first:
            self.doc.SendCommand("\x1b\x1b")
        cmd = command if command.endswith(("\n", " ")) else command + "\n"
        self.doc.SendCommand(cmd)
        idle = self._wait_idle(float(wait))
        result = {"entity_delta": self.doc.ModelSpace.Count - before,
                  "finished": idle}
        if idle is False:
            self.doc.SendCommand("\x1b\x1b")
            result["note"] = (f"{wait:.0f}초 안에 명령이 끝나지 않아 ESC로 취소했습니다. "
                              "입력이 더 필요한 명령인지 확인하세요")
        self.refresh_view()
        return result

    def begin_undo(self) -> None:
        try:
            self.doc.StartUndoMark()
        except Exception:
            pass

    def end_undo(self) -> None:
        try:
            self.doc.EndUndoMark()
        except Exception:
            pass

    def undo(self, steps: int = 1) -> Dict[str, Any]:
        """최근 작업 되돌리기. 이 서버의 작도/편집 도구 호출 1번이 1단계다"""
        steps = max(1, int(steps))
        result = self.send_command(f"_.UNDO\n{steps}\n", cancel_first=True, wait=10.0)
        result["steps"] = steps
        return result

    def save_drawing(self, file_path: str) -> str:
        directory = os.path.dirname(file_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        # Regen만으로는 EXTMIN/EXTMAX가 갱신되지 않는다. 저장 전에 재계산
        self.zoom_extents()
        self.doc.SaveAs(file_path)
        logger.info(f"도면 저장: {file_path}")
        return file_path

    # ==================================================================
    # 블록
    #
    #   doc.Blocks.Add(InsertionPoint, Name)                -> Block
    #   Block.AddLine / AddCircle / AddText / AddPolyline ... (ModelSpace 동일)
    #   Block.AddAttributeDef(Height, Mode, Prompt, Point, Tag, Value)
    #   ModelSpace.InsertBlock(Point, Name, Xs, Ys, Zs, Rot) -> BlockInsert
    #   BlockInsert.GetAttributes() -> [ .TagString, .TextString ]
    # ==================================================================

    def list_blocks(self) -> List[Dict[str, Any]]:
        """정의된 블록 목록. '*'로 시작하는 내부 블록은 제외"""
        result = []
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
        return result

    def create_block(self, name: str, entities: List[Dict[str, Any]],
                     attributes: List[Dict[str, Any]] = None,
                     base_point=(0, 0)) -> Dict[str, Any]:
        """블록 정의 생성. entities는 draw_entities와 같은 spec 목록"""
        existing = None
        try:
            existing = self.doc.Blocks.Item(name)
        except Exception:
            pass
        if existing is not None:
            raise CADError(f"블록 '{name}'이(가) 이미 있습니다")
        block = self.doc.Blocks.Add(self._pt(base_point), str(name))
        made, errors = 0, []
        for i, spec in enumerate(entities or []):
            try:
                made += len(self._create(block, spec))
            except Exception as e:
                errors.append(f"[{i}] {describe_error(e)}")
        for attr in attributes or []:
            try:
                mode = self.ATTR_MODE_INVISIBLE if attr.get("invisible") else self.ATTR_MODE_NORMAL
                block.AddAttributeDef(float(attr.get("height", 2.5)), mode,
                                      str(attr.get("prompt", attr.get("tag", ""))),
                                      self._pt(attr.get("position", (0, 0))),
                                      str(attr["tag"]).upper(), str(attr.get("default", "")))
            except Exception as e:
                errors.append(f"속성 {attr.get('tag')}: {describe_error(e)}")
        return {"block": name, "entities": made, "errors": errors}

    def insert_block(self, name: str, position, scale: float = 1.0, rotation: float = 0.0,
                     attributes: Dict[str, str] = None, layer: str = None) -> str:
        scale = float(scale)
        ref = self.doc.ModelSpace.InsertBlock(self._pt(position), str(name),
                                              scale, scale, scale,
                                              math.radians(float(rotation)))
        try:
            self._apply_style(ref, layer, None)
            if attributes:
                self._set_attributes(ref, attributes)
        except Exception:
            self._try_delete(ref)
            raise
        self.refresh_view()
        return str(ref.Handle)

    def _set_attributes(self, ref: Any, attributes: Dict[str, str]) -> int:
        """블록 인스턴스의 속성값 설정. 변경된 개수 반환"""
        wanted = {str(k).upper(): str(v) for k, v in attributes.items()}
        if not getattr(ref, 'HasAttributes', False):
            raise CADError("이 블록 인스턴스에는 속성이 없습니다")
        changed = 0
        present = set()
        for attr in ref.GetAttributes():
            tag = str(attr.TagString).upper()
            present.add(tag)
            if tag in wanted:
                attr.TextString = wanted[tag]
                changed += 1
        missing = set(wanted) - present
        if missing:
            raise CADError(f"블록에 없는 속성 태그: {sorted(missing)} (있는 태그: {sorted(present)})")
        return changed

    def set_block_attributes(self, handle: str, attributes: Dict[str, str]) -> int:
        changed = self._set_attributes(self._get(handle), attributes)
        self.refresh_view()
        return changed
