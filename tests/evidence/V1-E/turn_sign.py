"""Per-burst yaw change after turn_left / turn_right in the live traces."""

import json
import math
import sys
from statistics import mean, median

ev = sys.argv[1]


def wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


for run in ("run1", "run2", "run3"):
    t = json.load(open(f"{ev}/{run}/pose_trace.json"))["controller"]
    stats = {"turn_left": [], "turn_right": []}
    for a, b in zip(t, t[1:], strict=False):
        if a["action"] in stats:
            dt = b["t"] - a["t"]
            stats[a["action"]].append(wrap(b["yaw"] - a["yaw"]) / dt)
    for k, v in stats.items():
        if v:
            pos = sum(1 for x in v if x > 0)
            print(run, k, "n", len(v), "mean rate", round(mean(v), 3), "median", round(median(v), 3),
                  "positive(CCW)", pos, "negative(CW)", len(v) - pos)
