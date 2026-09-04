"""패치된 cad_controller.py를 CADian에서 실제로 검증하는 스크립트.

src/ 폴더에 두고 실행:
    python src\verify_cadian.py

config.json의 "cad": {"type": "CADian"} 상태여야 한다.
"""
import logging
import os
import sys

logging.basicConfig(
    level=logging.DEBUG,
    format='%(levelname)-7s %(name)s: %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)])

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cad_controller import CADController  # noqa: E402

c = CADController()
print(f"\n=== cad_type = {c.cad_type} / dispatch-point = {c._is_dispatch_point_cad()} ===\n")

if not c.start_cad():
    print("!! start_cad 실패. 위 로그 확인")
    sys.exit(1)

print(f"연결됨: {c.app.Name} {c.app.Version} / 문서: {c.doc.Name}\n")

results = []


def check(name, fn):
    try:
        r = fn()
        ok = r is not None and r is not False
        results.append((name, "OK" if ok else "FAIL(None)"))
    except Exception as e:
        results.append((name, f"EXC {type(e).__name__}: {str(e)[:60]}"))


check("draw_line",      lambda: c.draw_line((0, 0), (100, 0), color=1))
check("draw_circle",    lambda: c.draw_circle((50, 50), 25, color=3))
check("draw_arc",       lambda: c.draw_arc((150, 50), 25, 0, 90, color=4))
check("draw_ellipse",   lambda: c.draw_ellipse((250, 50), 40, 20, 0, color=5))
check("draw_polyline",  lambda: c.draw_polyline([(0, 100), (50, 150), (100, 100)], color=6))
check("draw_rectangle", lambda: c.draw_rectangle((150, 100), (250, 160), color=2))
check("draw_text",      lambda: c.draw_text((0, 200), "CADian MCP", 10, 0, color=7))
check("create_layer",   lambda: c.create_layer("MCP_TEST"))
check("draw_hatch",     lambda: c.draw_hatch([(300, 100), (380, 100), (340, 170)],
                                             "SOLID", 1.0, color=1))
check("add_dimension",  lambda: c.add_dimension((0, 0), (100, 0), textheight=8))
check("zoom_extents",   lambda: c.zoom_extents())

print("\n================ 결과 ================")
width = max(len(n) for n, _ in results)
for name, status in results:
    mark = "PASS" if status == "OK" else "----"
    print(f"[{mark}] {name.ljust(width)}  {status}")
print("======================================")

if c._pts_strategy:
    print(f"\n다중 점 전략: {c._pts_strategy}")

out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   '..', 'output', 'verify_cadian.dwg')
print(f"\n저장 시도: {os.path.abspath(out)}")
print("save_drawing:", c.save_drawing(os.path.abspath(out)))
