r"""CADian 도면 읽기 + 미리보기 이미지 생성.

CAD 측 Export/화면캡처가 모두 막혀 있으므로, COM으로 엔티티를 읽어
파이썬이 직접 이미지를 그린다. 항상 동작하고 좌표 주석도 넣을 수 있다.

단독 실행 시 현재 도면을 읽어 output 폴더에 미리보기를 만든다:
    python src\cad_reader.py
"""
import logging
import math
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger('cad_reader')

# ACI 색상 인덱스 -> RGB (미리보기용 근사값)
ACI_RGB = {
    1: (255, 0, 0), 2: (255, 255, 0), 3: (0, 255, 0), 4: (0, 255, 255),
    5: (0, 0, 255), 6: (255, 0, 255), 7: (0, 0, 0), 8: (128, 128, 128),
    9: (192, 192, 192), 30: (255, 127, 0), 200: (127, 0, 255),
    221: (255, 127, 191), 250: (51, 51, 51), 251: (128, 64, 0),
    256: (0, 0, 0),   # BYLAYER
    0: (0, 0, 0),     # BYBLOCK
}


def aci_to_rgb(index) -> Tuple[int, int, int]:
    try:
        return ACI_RGB.get(int(index), (0, 0, 0))
    except (TypeError, ValueError):
        return (0, 0, 0)


# ======================================================================
# 읽기
# ======================================================================

def pt_xyz(point) -> Optional[Tuple[float, float, float]]:
    """IIcadPoint -> (x, y, z)

    캐디안의 IIcadPoint는 소문자 x/y/z 속성을 쓴다.
    """
    if point is None:
        return None
    try:
        return (float(point.x), float(point.y), float(point.z))
    except Exception:
        pass
    # 예비 경로
    try:
        return tuple(float(v) for v in point.Application.Library
                     .ConvertPointToArray(point, True))
    except Exception:
        return None


def points_list(collection, lib=None) -> List[Tuple[float, float, float]]:
    """IIcadPoints -> [(x, y, z), ...]"""
    result = []
    if collection is None:
        return result
    # 한 번에 배열로 받는 쪽이 빠르다
    if lib is not None:
        try:
            flat = lib.ConvertPointsToArray(collection, True)
            return [(float(flat[i]), float(flat[i + 1]), float(flat[i + 2]))
                    for i in range(0, len(flat) - 2, 3)]
        except Exception:
            pass
    try:
        for i in range(collection.Count):
            xyz = pt_xyz(collection.Item(i))
            if xyz:
                result.append(xyz)
    except Exception as e:
        logger.debug(f"Points 순회 실패: {e}")
    return result


def vec_xyz(vector) -> Optional[Tuple[float, float, float]]:
    """IIcadVector -> (x, y, z). Point와 마찬가지로 소문자 x/y/z를 쓴다."""
    if vector is None:
        return None
    try:
        return (float(vector.x), float(vector.y), float(vector.z))
    except Exception:
        return None


def hatch_loops(entity, lib=None) -> Tuple[List[List[Tuple[float, float, float]]], List[str]]:
    """해치 경계 루프의 좌표를 뽑는다.

    GetLoopAt(i)의 반환 형태가 문서화돼 있지 않아 여러 해석을 시도하고,
    실제로 무엇이 왔는지 두 번째 반환값에 기록한다.
    """
    loops: List[List[Tuple[float, float, float]]] = []
    kinds: List[str] = []

    try:
        count = int(entity.NumberOfLoops)
    except Exception:
        return loops, kinds

    for i in range(count):
        loop = None
        for args in ((i,), (i + 1,)):
            try:
                loop = entity.GetLoopAt(*args)
                break
            except Exception:
                continue
        if loop is None:
            kinds.append(f"loop{i}:호출실패")
            continue

        tname = type(loop).__name__
        kinds.append(f"loop{i}:{tname}")

        # 1) Points 컬렉션으로 온 경우
        pts = points_list(loop, lib)
        if pts:
            loops.append(pts)
            continue

        # 2) 엔티티들의 배열/컬렉션으로 온 경우
        members = []
        try:
            members = list(loop)
        except Exception:
            members = [loop]

        collected: List[Tuple[float, float, float]] = []
        for member in members:
            sub = points_list(getattr(member, 'Coordinates', None), lib)
            if sub:
                collected.extend(sub)
                continue
            start = pt_xyz(getattr(member, 'StartPoint', None))
            end = pt_xyz(getattr(member, 'EndPoint', None))
            if start:
                collected.append(start)
            if end:
                collected.append(end)
        if collected:
            loops.append(collected)

    return loops, kinds


TEXT_JUSTIFY_NAMES = {
    0: "left", 1: "center", 2: "right", 3: "aligned", 4: "middle", 5: "fit",
    6: "top_left", 7: "top_center", 8: "top_right",
    9: "middle_left", 10: "middle_center", 11: "middle_right",
    12: "bottom_left", 13: "bottom_center", 14: "bottom_right",
}
MTEXT_ATTACH_NAMES = {
    1: "top_left", 2: "top_center", 3: "top_right",
    4: "middle_left", 5: "middle_center", 6: "middle_right",
    7: "bottom_left", 8: "bottom_center", 9: "bottom_right",
}


def com_bbox(entity, lib) -> Optional[Tuple[float, float, float, float]]:
    """GetBoundingBox로 실제 외곽. 캐디안은 in/out 점 객체 두 개를 넘겨야 한다"""
    if lib is None:
        return None
    try:
        mn, mx = entity.GetBoundingBox(lib.CreatePoint(0, 0, 0), lib.CreatePoint(0, 0, 0))
        return (float(mn.x), float(mn.y), float(mx.x), float(mx.y))
    except Exception:
        return None


def round_floats(value, ndigits: int = 1):
    """출력용 반올림 (dict/list 재귀). 좌표 34847.62719249372 같은 긴 숫자로
    토큰을 낭비하지 않게 한다 (개선 요청 4)"""
    if isinstance(value, float):
        r = round(value, ndigits)
        return int(r) if ndigits <= 0 or r == int(r) else r
    if isinstance(value, dict):
        return {k: round_floats(v, ndigits) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [round_floats(v, ndigits) for v in value]
    return value


def read_entity(entity, lib=None, with_bbox: bool = True) -> Dict[str, Any]:
    """엔티티 하나를 dict로

    텍스트는 삽입점만으로는 실제 위치를 알 수 없다(정렬/부착점에 따라 글자가
    삽입점 왼쪽·가운데·오른쪽에 놓인다). 그래서 Text/MText에는 정렬 이름과
    GetBoundingBox로 구한 실제 외곽(bbox)을 함께 넣는다.
    """
    info: Dict[str, Any] = {}

    for prop in ('ObjectName', 'EntityName', 'Handle', 'Layer'):
        try:
            info[prop] = getattr(entity, prop)
        except Exception:
            pass
    try:
        info['color'] = entity.color
    except Exception:
        pass

    kind = info.get('ObjectName', '')

    def grab_point(name, key):
        xyz = pt_xyz(getattr(entity, name, None))
        if xyz:
            info[key] = xyz

    if kind == 'AcDbLine':
        grab_point('StartPoint', 'start')
        grab_point('EndPoint', 'end')
        for prop, key in (('Length', 'length'),):
            try:
                info[key] = float(getattr(entity, prop))
            except Exception:
                pass

    elif kind in ('AcDbCircle', 'AcDbArc'):
        grab_point('Center', 'center')
        for prop, key in (('Radius', 'radius'), ('StartAngle', 'start_angle'),
                          ('EndAngle', 'end_angle'), ('Area', 'area')):
            try:
                info[key] = float(getattr(entity, prop))
            except Exception:
                pass

    elif kind == 'AcDbEllipse':
        grab_point('Center', 'center')
        for prop, key in (('MajorRadius', 'major'), ('MinorRadius', 'minor'),
                          ('RadiusRatio', 'ratio')):
            try:
                info[key] = float(getattr(entity, prop))
            except Exception:
                pass
        # MajorAxis 벡터에서 회전각을 계산 (축 정렬이 아닌 타원 대응)
        axis = vec_xyz(getattr(entity, 'MajorAxis', None))
        if axis:
            info['rotation'] = math.degrees(math.atan2(axis[1], axis[0]))

    elif kind == 'AcDbHatch':
        for prop, key in (('PatternName', 'pattern_name'),
                          ('PatternType', 'pattern_type')):
            try:
                info[key] = getattr(entity, prop)
            except Exception:
                pass
        for prop, key in (('PatternScale', 'pattern_scale'),
                          ('PatternAngle', 'pattern_angle'),
                          ('Area', 'area')):
            try:
                info[key] = float(getattr(entity, prop))
            except Exception:
                pass
        try:
            info['loop_count'] = int(entity.NumberOfLoops)
        except Exception:
            pass
        loops, kinds = hatch_loops(entity, lib)
        if loops:
            info['loops'] = loops
        if kinds:
            info['loop_kinds'] = kinds

    elif 'Dimension' in kind:
        grab_point('ExtLine1Point', 'ext1')
        grab_point('ExtLine2Point', 'ext2')
        grab_point('TextPosition', 'text_position')
        for prop, key in (('Measurement', 'measurement'),
                          ('TextHeight', 'text_height'),
                          ('Rotation', 'rotation')):
            try:
                info[key] = float(getattr(entity, prop))
            except Exception:
                pass
        for prop, key in (('TextOverride', 'text_override'),
                          ('StyleName', 'style')):
            try:
                value = getattr(entity, prop)
                if value:
                    info[key] = value
            except Exception:
                pass

    elif 'Block' in kind or 'Insert' in kind or kind == 'AcDbBlockReference':
        for prop, key in (('Name', 'block_name'),
                          ('EffectiveName', 'block_effective_name')):
            try:
                value = getattr(entity, prop)
                if value:
                    info[key] = value
            except Exception:
                pass
        grab_point('InsertionPoint', 'position')
        for prop, key in (('XScaleFactor', 'scale_x'),
                          ('YScaleFactor', 'scale_y'),
                          ('Rotation', 'rotation_rad')):
            try:
                info[key] = float(getattr(entity, prop))
            except Exception:
                pass
        if 'rotation_rad' in info:
            info['rotation'] = math.degrees(info['rotation_rad'])
        # 속성값 (이름만 바꿔 재사용하는 용도의 핵심)
        try:
            if getattr(entity, 'HasAttributes', False):
                attrs = {}
                for attr in entity.GetAttributes():
                    try:
                        attrs[str(attr.TagString)] = str(attr.TextString)
                    except Exception:
                        continue
                if attrs:
                    info['attributes'] = attrs
        except Exception:
            pass

    elif kind == 'AcDbAttributeDefinition':
        grab_point('InsertionPoint', 'position')
        for prop, key in (('TagString', 'tag'), ('PromptString', 'prompt'),
                          ('TextString', 'default')):
            try:
                info[key] = getattr(entity, prop)
            except Exception:
                pass
        try:
            info['height'] = float(entity.Height)
        except Exception:
            pass

    elif kind in ('AcDbText', 'AcDbMText'):
        grab_point('InsertionPoint', 'position')
        for prop, key in (('TextString', 'text'),):
            try:
                info[key] = getattr(entity, prop)
            except Exception:
                pass
        for prop, key in (('Height', 'height'), ('Rotation', 'rotation')):
            try:
                info[key] = float(getattr(entity, prop))
            except Exception:
                pass
        if 'rotation' in info:
            info['rotation'] = math.degrees(info['rotation'])
        if kind == 'AcDbText':
            try:
                code = int(entity.Alignment)
                info['justify'] = TEXT_JUSTIFY_NAMES.get(code, code)
                if code != 0:
                    grab_point('TextAlignmentPoint', 'align_point')
            except Exception:
                pass
        else:
            try:
                info['attachment'] = MTEXT_ATTACH_NAMES.get(int(entity.AttachmentPoint),
                                                            entity.AttachmentPoint)
            except Exception:
                pass
            try:
                info['width'] = float(entity.Width)
            except Exception:
                pass
        if with_bbox:
            bb = com_bbox(entity, lib)
            if bb:
                info['bbox'] = bb

    elif kind in ('AcDb2dPolyline', 'AcDbPolyline', 'AcDb3dPolyline'):
        info['points'] = points_list(getattr(entity, 'Coordinates', None), lib)
        for prop, key in (('Closed', 'closed'),):
            try:
                info[key] = bool(getattr(entity, prop))
            except Exception:
                pass
        for prop, key in (('Area', 'area'), ('Length', 'length')):
            try:
                info[key] = float(getattr(entity, prop))
            except Exception:
                pass

    return info


def read_block_definition(doc, name: str, lib=None,
                          cache: Dict[str, Any] = None) -> Dict[str, Any]:
    """블록 정의의 도형과 기준점을 읽는다"""
    if cache is not None and name in cache:
        return cache[name]

    result: Dict[str, Any] = {'origin': (0.0, 0.0, 0.0), 'entities': []}
    try:
        block = doc.Blocks.Item(name)
        origin = pt_xyz(getattr(block, 'Origin', None))
        if origin:
            result['origin'] = origin
        try:
            members = list(block)
        except Exception:
            members = []
            try:
                for i in range(block.Count):
                    members.append(block.Item(i))
            except Exception:
                pass
        result['entities'] = [read_entity(m, lib) for m in members]
    except Exception as e:
        logger.debug(f"블록 '{name}' 정의 읽기 실패: {e}")

    if cache is not None:
        cache[name] = result
    return result


def _transform(xyz, insert, scale_x, scale_y, rot_rad, origin):
    """블록 정의 좌표를 삽입 좌표계로 변환"""
    px = (xyz[0] - origin[0]) * scale_x
    py = (xyz[1] - origin[1]) * scale_y
    cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)
    return (insert[0] + px * cos_r - py * sin_r,
            insert[1] + px * sin_r + py * cos_r,
            0.0)


def expand_blocks(entities: List[Dict[str, Any]], doc,
                  lib=None) -> List[Dict[str, Any]]:
    """블록 인스턴스를 실제 도형으로 펼친 목록을 만든다 (렌더링 전용)

    list_entities에는 쓰지 않는다. 원본 목록은 그대로 두고 새 목록을 반환.
    """
    cache: Dict[str, Any] = {}
    result: List[Dict[str, Any]] = []

    for e in entities:
        name = e.get('block_name') or e.get('block_effective_name')
        insert = e.get('position')
        if not name or not insert:
            result.append(e)
            continue

        result.append(e)  # 라벨용으로 원본도 남긴다
        definition = read_block_definition(doc, name, lib, cache)
        origin = definition.get('origin', (0.0, 0.0, 0.0))
        sx = e.get('scale_x', 1.0) or 1.0
        sy = e.get('scale_y', sx) or sx
        rot = e.get('rotation_rad', 0.0) or 0.0
        attrs = e.get('attributes') or {}

        for sub in definition['entities']:
            clone = dict(sub)
            clone['from_block'] = name
            clone.pop('bbox', None)   # 블록 정의 좌표계 기준이라 그대로 쓸 수 없다
            # 블록 정의의 색상이 BYLAYER면 인스턴스 색을 물려준다
            if clone.get('color', 256) in (0, 256):
                clone['color'] = e.get('color', 256)

            for key in ('start', 'end', 'center', 'position',
                        'ext1', 'ext2', 'text_position'):
                if clone.get(key):
                    clone[key] = _transform(clone[key], insert, sx, sy, rot, origin)
            if clone.get('points'):
                clone['points'] = [_transform(p, insert, sx, sy, rot, origin)
                                   for p in clone['points']]
            if clone.get('loops'):
                clone['loops'] = [[_transform(p, insert, sx, sy, rot, origin)
                                   for p in loop] for loop in clone['loops']]
            for key in ('radius', 'major', 'minor', 'height', 'text_height'):
                if clone.get(key):
                    clone[key] = clone[key] * sx

            # 속성 정의는 실제 값으로 치환해 문자로 그린다
            if clone.get('ObjectName') == 'AcDbAttributeDefinition':
                tag = str(clone.get('tag', '')).upper()
                value = None
                for k, v in attrs.items():
                    if str(k).upper() == tag:
                        value = v
                        break
                clone['ObjectName'] = 'AcDbText'
                clone['text'] = value if value not in (None, '') else clone.get('default', tag)

            result.append(clone)

    return result


def compact_entity(e: Dict[str, Any]) -> Dict[str, Any]:
    """좌표 배열(points/loops)을 뺀 요약 dict.

    list_entities 기본 응답용. 폴리라인/해치는 점이 많으면 엔티티 하나가
    수백 자를 먹어서, 엔티티 수가 조금만 늘어도 응답이 통째로 토큰 한도를
    넘긴다(217개에서 56,570자로 실제 초과 확인, 2026-09-04). 좌표 자체가
    필요 없는 개수/종류/레이어 확인에는 이 요약이면 충분하고, 좌표가
    필요하면 list_entities의 include_geometry=True로 원본을 받는다.
    """
    out = {k: v for k, v in e.items() if k not in ('points', 'loops')}
    pts = e.get('points')
    if pts:
        out['point_count'] = len(pts)
    return out


def read_entities(doc, lib=None, limit: int = 1500) -> List[Dict[str, Any]]:
    """모델 공간의 모든 엔티티를 읽는다"""
    result = []
    try:
        for entity in doc.ModelSpace:
            result.append(read_entity(entity, lib))
            if len(result) >= limit:
                logger.warning(f"엔티티 {limit}개에서 중단")
                break
    except Exception as e:
        logger.error(f"엔티티 순회 실패: {e}")
    return result


def drawing_info(doc, lib=None) -> Dict[str, Any]:
    """도면 전역 정보"""
    info: Dict[str, Any] = {}
    for prop in ('Name', 'FullName'):
        try:
            info[prop.lower()] = getattr(doc, prop)
        except Exception:
            pass
    try:
        info['entity_count'] = doc.ModelSpace.Count
    except Exception:
        pass
    for var, key in (('EXTMIN', 'extmin'), ('EXTMAX', 'extmax')):
        try:
            info[key] = pt_xyz(doc.GetVariable(var))
        except Exception:
            pass
    for var, key in (('CLAYER', 'current_layer'), ('CECOLOR', 'current_color'),
                     ('TEXTSIZE', 'text_size'), ('INSUNITS', 'units')):
        try:
            info[key] = doc.GetVariable(var)
        except Exception:
            pass
    try:
        info['layers'] = [layer.Name for layer in doc.Layers]
    except Exception:
        pass
    return info


# ======================================================================
# 렌더링
# ======================================================================

_FONT_CACHE: Dict[int, Any] = {}
_MAX_FONT_SIZE = 200  # 이보다 큰 폰트는 안 쓴다. 이유는 아래 참고.


def _korean_font(size: int):
    """크기별 폰트 캐시. 문자 엔티티마다 디스크를 읽지 않도록 한다.

    size를 _MAX_FONT_SIZE로 clamp한다. zoom_to로 좁은 영역만 확대하면
    scale(픽셀/도면단위)이 커지는데, 그 상태에서 실제 도면의 큰
    text_height(치수 문자 등, 수백 단위)를 곱하면 폰트 크기가 수천pt로
    치솟는다. 그 크기로 draw.text를 호출하면 Pillow가 글리프 래스터화
    중 큰 비트맵을 할당하려다 DecompressionBombError로 죽는다(2026-09-04,
    zoom_to로 25x30 단위 영역만 확대했을 때 실제로 재현: "Image size
    1,297,835,816 pixels exceeds limit").
    """
    size = max(1, min(int(size), _MAX_FONT_SIZE))
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    from PIL import ImageFont
    font = None
    for name in ('malgun.ttf', 'gulim.ttc', 'batang.ttc', 'arial.ttf'):
        try:
            font = ImageFont.truetype(name, size)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()
    _FONT_CACHE[size] = font
    return font


def _collect_xy(e: Dict[str, Any], xs: List[float], ys: List[float]) -> None:
    def add(xyz):
        if xyz:
            xs.append(xyz[0])
            ys.append(xyz[1])

    bbox = e.get('bbox')
    if bbox:
        xs.extend([bbox[0], bbox[2]])
        ys.extend([bbox[1], bbox[3]])
    else:
        add(e.get('position'))
    add(e.get('start'))
    add(e.get('end'))
    add(e.get('ext1'))
    add(e.get('ext2'))
    add(e.get('text_position'))
    for p in e.get('points', []) or []:
        add(p)
    for loop in e.get('loops', []) or []:
        for p in loop:
            add(p)
    center = e.get('center')
    if center:
        r = e.get('radius') or e.get('major') or 0
        xs.extend([center[0] - r, center[0] + r])
        ys.extend([center[1] - r, center[1] + r])


def entity_bbox(info: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:
    """read_entity() 결과 하나의 2D 외곽 (텍스트는 bbox가 있으면 그것을 쓴다)"""
    xs: List[float] = []
    ys: List[float] = []
    _collect_xy(info, xs, ys)
    if not xs or not ys:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def bounds_of(entities: List[Dict[str, Any]]) -> Tuple[float, float, float, float]:
    xs: List[float] = []
    ys: List[float] = []
    for e in entities:
        _collect_xy(e, xs, ys)

    if not xs or not ys:
        return (0.0, 0.0, 100.0, 100.0)

    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    # 가장자리 도형(치수선 등)이 잘리지 않도록 3% 여유
    pad_x = max((x1 - x0) * 0.03, 1e-6)
    pad_y = max((y1 - y0) * 0.03, 1e-6)
    return (x0 - pad_x, y0 - pad_y, x1 + pad_x, y1 + pad_y)


def render_png(entities: List[Dict[str, Any]], width: int = 1200,
               height: int = 900, margin: int = 40,
               show_labels: bool = False,
               bounds: Optional[Tuple[float, float, float, float]] = None
               ) -> bytes:
    """엔티티 목록을 PNG 바이트로 렌더링"""
    from PIL import Image, ImageDraw

    img = Image.new('RGB', (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    x0, y0, x1, y1 = bounds or bounds_of(entities)
    span_x = max(x1 - x0, 1e-9)
    span_y = max(y1 - y0, 1e-9)
    scale = min((width - 2 * margin) / span_x, (height - 2 * margin) / span_y)
    off_x = (width - span_x * scale) / 2
    off_y = (height - span_y * scale) / 2

    def to_px(x, y):
        # CAD는 Y가 위로, 이미지는 아래로 증가하므로 뒤집는다
        return (off_x + (x - x0) * scale, height - (off_y + (y - y0) * scale))

    font = _korean_font(12)

    def curve_points(cx, cy, ra, rb, a0, a1, rot=0.0, steps=96):
        """원/원호/타원을 매개변수 방정식으로 점 목록 생성

        PIL의 arc/ellipse는 회전을 지원하지 않으므로 직접 계산한다.
        각도는 라디안, rot도 라디안.
        """
        cos_r, sin_r = math.cos(rot), math.sin(rot)
        result = []
        for i in range(steps + 1):
            t = a0 + (a1 - a0) * i / steps
            lx, ly = ra * math.cos(t), rb * math.sin(t)
            result.append(to_px(cx + lx * cos_r - ly * sin_r,
                                cy + lx * sin_r + ly * cos_r))
        return result

    def lighten(rgb, factor=0.75):
        return tuple(int(v + (255 - v) * factor) for v in rgb)

    for e in sorted(entities,
                    key=lambda x: 0 if x.get('ObjectName') == 'AcDbHatch' else 1):
        rgb = aci_to_rgb(e.get('color', 256))
        kind = e.get('ObjectName', '')

        if kind == 'AcDbLine' and e.get('start') and e.get('end'):
            draw.line([to_px(*e['start'][:2]), to_px(*e['end'][:2])],
                      fill=rgb, width=2)

        elif kind == 'AcDbCircle' and e.get('center') and e.get('radius'):
            cx, cy = e['center'][:2]
            r = e['radius']
            draw.line(curve_points(cx, cy, r, r, 0, 2 * math.pi),
                      fill=rgb, width=2)

        elif kind == 'AcDbArc' and e.get('center') and e.get('radius'):
            cx, cy = e['center'][:2]
            r = e['radius']
            a0 = e.get('start_angle', 0.0)
            a1 = e.get('end_angle', math.pi / 2)
            if a1 <= a0:
                a1 += 2 * math.pi
            draw.line(curve_points(cx, cy, r, r, a0, a1), fill=rgb, width=2)

        elif kind == 'AcDbEllipse' and e.get('center'):
            cx, cy = e['center'][:2]
            ra = e.get('major') or 1.0
            rb = e.get('minor') or ra * 0.5
            rot = math.radians(e.get('rotation') or 0.0)
            draw.line(curve_points(cx, cy, ra, rb, 0, 2 * math.pi, rot),
                      fill=rgb, width=2)

        elif kind == 'AcDbHatch':
            # 경계 루프를 옅은 색으로 채운다 (경계선 자체는 별도 폴리라인이 그린다)
            for loop in e.get('loops', []) or []:
                if len(loop) >= 3:
                    draw.polygon([to_px(p[0], p[1]) for p in loop],
                                 fill=lighten(rgb), outline=rgb)

        elif 'Dimension' in kind and e.get('ext1') and e.get('ext2'):
            p1, p2 = e['ext1'][:2], e['ext2'][:2]
            tp = (e.get('text_position') or ((p1[0] + p2[0]) / 2,
                                             (p1[1] + p2[1]) / 2))[:2]
            mid = ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2)
            off = (tp[0] - mid[0], tp[1] - mid[1])
            d1 = (p1[0] + off[0], p1[1] + off[1])
            d2 = (p2[0] + off[0], p2[1] + off[1])

            draw.line([to_px(*d1), to_px(*d2)], fill=rgb, width=1)
            draw.line([to_px(*p1), to_px(*d1)], fill=rgb, width=1)
            draw.line([to_px(*p2), to_px(*d2)], fill=rgb, width=1)

            label = e.get('text_override') or e.get('measurement')
            if label is not None:
                if isinstance(label, float):
                    label = f"{label:g}"
                size = max(int((e.get('text_height') or 2.5) * scale), 9)
                px, py = to_px(*tp)
                draw.text((px, py - size), str(label), fill=rgb,
                          font=_korean_font(size))

        elif ('Block' in kind or 'Insert' in kind) and e.get('position'):
            # 정의를 펼치지 못한 경우의 대체 표시
            px, py = to_px(*e['position'][:2])
            draw.line([(px - 6, py), (px + 6, py)], fill=rgb, width=2)
            draw.line([(px, py - 6), (px, py + 6)], fill=rgb, width=2)
            label = e.get('block_name', '?')
            attrs = e.get('attributes') or {}
            if attrs:
                label += " [" + ", ".join(f"{k}={v}" for k, v in attrs.items()) + "]"
            draw.text((px + 8, py - 6), label, fill=rgb, font=font)

        elif kind in ('AcDbText', 'AcDbMText') and (e.get('bbox') or e.get('position')):
            size = max(int((e.get('height') or 2.5) * scale), 8)
            fnt = _korean_font(size)
            text = str(e.get('text', '')).replace('\\P', '\n')
            bbox = e.get('bbox')
            if bbox:
                # 정렬/부착점에 따라 글자는 삽입점 왼쪽에도, 가운데에도 놓인다.
                # 예전에는 늘 삽입점에서 왼쪽 정렬로 그려서, 미러 후 부착점이
                # 오른쪽으로 바뀐 MText가 글자 폭만큼 밀려 보였다.
                bx0, by1 = to_px(bbox[0], bbox[3])
                bx1, _ = to_px(bbox[2], bbox[1])
                try:
                    tw = draw.multiline_textbbox((0, 0), text, font=fnt)[2]
                except Exception:
                    tw = 0
                attach = str(e.get('attachment') or '')
                if attach.endswith('right'):
                    x = bx1 - tw
                elif attach.endswith('center'):
                    x = (bx0 + bx1 - tw) / 2
                else:
                    x = bx0
                draw.multiline_text((x, by1), text, fill=rgb, font=fnt)
            else:
                px, py = to_px(*e['position'][:2])
                draw.multiline_text((px, py - size), text, fill=rgb, font=fnt)

        elif e.get('points'):
            pts = [to_px(p[0], p[1]) for p in e['points']]
            if e.get('closed') and len(pts) > 2:
                pts = pts + [pts[0]]
            if len(pts) >= 2:
                draw.line(pts, fill=rgb, width=2)

        if show_labels and e.get('Handle'):
            anchor = (e.get('start') or e.get('center') or e.get('position')
                      or e.get('ext1') or (e.get('points') or [None])[0])
            if anchor:
                px, py = to_px(anchor[0], anchor[1])
                draw.text((px + 4, py + 4), str(e['Handle']),
                          fill=(160, 160, 160), font=font)

    # 범위 표시
    draw.text((8, 8), f"X {x0:.1f} ~ {x1:.1f}   Y {y0:.1f} ~ {y1:.1f}   "
                      f"엔티티 {len(entities)}개",
              fill=(90, 90, 90), font=font)

    import io
    buffer = io.BytesIO()
    img.save(buffer, 'PNG', optimize=True)
    return buffer.getvalue()


def summarize(entities: List[Dict[str, Any]]) -> str:
    """엔티티 목록을 사람이 읽는 요약으로"""
    if not entities:
        return "도면이 비어 있습니다."
    counts: Dict[str, int] = {}
    layers: Dict[str, int] = {}
    for e in entities:
        counts[e.get('ObjectName', '?')] = counts.get(e.get('ObjectName', '?'), 0) + 1
        layers[e.get('Layer', '?')] = layers.get(e.get('Layer', '?'), 0) + 1
    x0, y0, x1, y1 = bounds_of(entities)
    lines = [
        f"엔티티 {len(entities)}개",
        "종류: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())),
        "레이어: " + ", ".join(f"{k}={v}" for k, v in sorted(layers.items())),
        f"범위: ({x0:.2f}, {y0:.2f}) ~ ({x1:.2f}, {y1:.2f})",
    ]
    return "\n".join(lines)


# ======================================================================
if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.DEBUG,
                        format='%(levelname)-7s %(name)s: %(message)s')

    import win32com.client as w

    app = w.gencache.EnsureDispatch('ICAD.Application')
    app.Visible = True
    if app.Documents.Count == 0:
        app.Documents.Add()
    doc = app.ActiveDocument
    lib = app.Library

    print("\n=== 도면 정보 ===")
    print(json.dumps(drawing_info(doc, lib), ensure_ascii=False, indent=2))

    print("\n=== 엔티티 ===")
    ents = read_entities(doc, lib)
    for e in ents:
        print("  ", json.dumps(e, ensure_ascii=False))

    print("\n=== 요약 ===")
    print(summarize(ents))

    out_dir = os.path.abspath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..', 'output'))
    os.makedirs(out_dir, exist_ok=True)

    for labels, name in ((False, 'preview.png'), (True, 'preview_labeled.png')):
        try:
            data = render_png(ents, show_labels=labels)
            path = os.path.join(out_dir, name)
            with open(path, 'wb') as f:
                f.write(data)
            print(f"\n미리보기 저장: {path} ({len(data)} bytes)")
        except Exception as ex:
            print(f"\n렌더링 실패({name}): {ex}")
