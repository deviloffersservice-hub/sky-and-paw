"""Is the stall explained by the convex hull of the single rubble collision mesh?"""

import json
import math
import sys

import numpy as np

obj, ev = sys.argv[1], sys.argv[2]
v = np.array([[float(t) for t in ln.split()[1:4]] for ln in open(obj) if ln.startswith("v ")])
print("vertices", len(v), "bbox", v.min(0).round(2), v.max(0).round(2))
def monotone_hull(pts):
    pts = sorted(set(map(tuple, pts)))
    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lo, up = [], []
    for q in pts:
        while len(lo) >= 2 and cross(lo[-2], lo[-1], q) <= 0:
            lo.pop()
        lo.append(q)
    for q in reversed(pts):
        while len(up) >= 2 and cross(up[-2], up[-1], q) <= 0:
            up.pop()
        up.append(q)
    return np.array(lo[:-1] + up[:-1])  # counter-clockwise


poly = monotone_hull(v[:, :2])
edges = list(zip(poly, np.roll(poly, -1, 0), strict=True))
# outward normals for a CCW polygon: (dy, -dx)
equations = []
for a, b in edges:
    n = np.array([b[1] - a[1], -(b[0] - a[0])])
    n = n / np.linalg.norm(n)
    equations.append(np.array([n[0], n[1], -float(n @ a)]))
print("2D hull vertices (world xy):", [tuple(p.round(2)) for p in poly])


def signed_dist(p):
    """Positive outside the hull, negative inside (2D)."""
    best_out = -1e9
    for eq in equations:  # n.x + d <= 0 inside
        best_out = max(best_out, float(eq[:2] @ p + eq[2]))
    if best_out > 0:
        d = min(
            math.dist(p, closest)
            for closest in (
                a + np.clip(np.dot(p - a, b - a) / np.dot(b - a, b - a), 0, 1) * (b - a)
                for a, b in zip(poly, np.roll(poly, -1, 0), strict=True)
            )
        )
        return d
    return best_out


for run in ("run1", "run2"):
    tr = json.load(open(f"{ev}/{run}/pose_trace.json"))["controller"]
    for r in tr:
        front = np.array([r["x"] + 0.35 * math.cos(r["yaw"]), r["y"] + 0.35 * math.sin(r["yaw"])])
        r["front_hull_d"] = signed_dist(front)
    first = next((r for r in tr if r["front_hull_d"] < 0.15), None)
    last = tr[-1]
    print(run, "first front within 0.15 m of hull:", first and (first["t"], first["x"], first["y"],
          round(first["front_hull_d"], 3)), "| stall front dist:", round(last["front_hull_d"], 3),
          "| min over run:", round(min(r["front_hull_d"] for r in tr), 3))

for name, xy in {"V-01": (3.5, 2.0), "V-02": (-4.0, -3.0), "V-03": (7.0, -4.8),
                 "staging": (-12.5, -7.5)}.items():
    print(name, "hull signed dist", round(signed_dist(np.array(xy)), 2))
