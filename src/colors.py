"""색상 인자 해석.

도구마다 기본 색이 달라 사고가 났다(2026-10-12 개선 요청 2-4): 단일 작도 도구는
색을 안 주면 7(흰색)로 강제됐고, draw_entities는 캐디안의 현재 색(CECOLOR)을
따라가서, CECOLOR가 빨강으로 남아 있던 날 엔티티 70개가 전부 빨강으로 그려졌다.

규칙: 색을 지정하지 않으면 None을 돌려주고, 호출하는 쪽(CADController)이
명시적으로 BYLAYER(256)를 넣는다. 현재 색상 설정에 영향을 받지 않는다.
"""
from typing import Any, Optional

BYLAYER = 256
BYBLOCK = 0

# ACI(AutoCAD Color Index) 표준 색
_NAMES = {
    # 한국어
    "빨강": 1, "빨간색": 1, "적색": 1, "노랑": 2, "노란색": 2, "황색": 2,
    "초록": 3, "초록색": 3, "녹색": 3, "청록": 4, "청록색": 4, "하늘색": 4,
    "파랑": 5, "파란색": 5, "청색": 5, "자홍": 6, "자홍색": 6, "보라": 6,
    "흰색": 7, "하양": 7, "흰": 7, "검정": 7, "검은색": 7, "회색": 8, "밝은회색": 9,
    "주황": 30, "주황색": 30, "갈색": 251,
    # 영어
    "red": 1, "yellow": 2, "green": 3, "cyan": 4, "blue": 5, "magenta": 6,
    "white": 7, "black": 7, "gray": 8, "grey": 8, "lightgray": 9, "light gray": 9,
    "orange": 30, "brown": 251,
    # 중국어 (원본 프로젝트 호환)
    "红色": 1, "黄色": 2, "绿色": 3, "青色": 4, "蓝色": 5, "洋红色": 6,
    "白色": 7, "灰色": 8, "浅灰色": 9, "橙色": 30, "棕色": 251,
    # 특수값
    "bylayer": BYLAYER, "레이어": BYLAYER, "byblock": BYBLOCK, "블록": BYBLOCK,
}


def parse_color(value: Any) -> Optional[int]:
    """색상 인자를 ACI 정수로. 지정 안 함(None, 빈 문자열)이면 None

    정수/숫자 문자열은 0~256 범위 그대로, 이름은 위 표로 찾는다. 모르는 이름은
    조용히 다른 색으로 바꾸지 않고 ValueError를 낸다.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"색상 값이 올바르지 않습니다: {value!r}")
    if isinstance(value, (int, float)):
        n = int(value)
    else:
        text = str(value).strip()
        if not text:
            return None
        key = text.lower()
        if key in _NAMES:
            return _NAMES[key]
        if key.lstrip('-').isdigit():
            n = int(key)
        else:
            raise ValueError(
                f"알 수 없는 색상 이름 '{value}'. ACI 번호(1~255), BYLAYER, BYBLOCK "
                "또는 red/빨강 같은 이름을 쓰세요.")
    if not 0 <= n <= 256:
        raise ValueError(f"색상 번호는 0(BYBLOCK)~256(BYLAYER) 범위여야 합니다: {n}")
    return n


def color_sysvar(value: Any) -> str:
    """CECOLOR 시스템 변수에 넣을 문자열 ("BYLAYER", "BYBLOCK", "1" ...)"""
    n = parse_color(value)
    if n is None or n == BYLAYER:
        return "BYLAYER"
    if n == BYBLOCK:
        return "BYBLOCK"
    return str(n)
