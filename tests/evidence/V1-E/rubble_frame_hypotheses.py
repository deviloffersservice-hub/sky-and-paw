"""Which placement of the rubble explains both Go2 stalls with a clear approach path?

Run: cd backend && ../.venv/bin/python ../tests/evidence/V1-E/rubble_frame_hypotheses.py
"""

import json
import math

from skypaw.mission.grid import box_footprint_world, polygons_overlap
from skypaw.scene.site import load_site

site = load_site()
boxes = site.rubble
EV = str(__import__("pathlib").Path(__file__).resolve().parent)  # tests/evidence/V1-E

vol = [b.sx * b.sy * b.sz for b in boxes]
com = (
    sum(v * b.x for v, b in zip(vol, boxes, strict=True)) / sum(vol),
    sum(v * -b.z for v, b in zip(vol, boxes, strict=True)) / sum(vol),
)
print("volume COM (world xy)", [round(c, 2) for c in com])


def robot_poly(x, y, yaw, length=0.75, width=0.45):
    c, s = math.cos(yaw), math.sin(yaw)
    return [
        (x + c * dx - s * dy, y + s * dx + c * dy)
        for dx, dy in ((-length / 2, -width / 2), (length / 2, -width / 2),
                       (length / 2, width / 2), (-length / 2, width / 2))
    ]


def transform(poly, kind):
    out = []
    for x, y in poly:
        if kind == "as_modelled":
            out.append((x, y))
        elif kind == "mirror_y":
            out.append((x, -y))
        elif kind == "mirror_x":
            out.append((-x, y))
        elif kind == "rot180":
            out.append((-x, -y))
        elif kind == "com_shift":
            out.append((x - com[0], y - com[1]))
        elif kind == "mirror_y_com_shift":
            out.append((x - com[0], -y + com[1]))
    return out


traces = []
for run in ("run1", "run2"):
    t = json.load(open(f"{EV}/{run}/pose_trace.json"))["controller"]
    traces.append((run, t))

for kind in ("as_modelled", "mirror_y", "mirror_x", "rot180", "com_shift", "mirror_y_com_shift"):
    polys = [(b, transform(box_footprint_world(b), kind)) for b in boxes]
    report = []
    for run, t in traces:
        first_hit = None
        for r in t:
            rp = robot_poly(r["x"], r["y"], r["yaw"])
            hit = [(b.x, b.z, b.sy) for b, p in polys if polygons_overlap(rp, p)]
            if hit:
                first_hit = (r["t"], round(r["x"], 2), round(r["y"], 2), hit)
                break
        stall = t[-1]
        rp = robot_poly(stall["x"], stall["y"], stall["yaw"], 0.95, 0.6)  # inflated
        near = [(b.x, b.z, b.sy) for b, p in polys if polygons_overlap(rp, p)]
        report.append((run, "first_overlap", first_hit, "stall_touch(inflated)", near))
    print(kind)
    for r in report:
        print("   ", r)
