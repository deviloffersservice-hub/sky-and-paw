#!/usr/bin/env python3
# ruff: noqa: E501  # long CSS/HTML string literals are intentional
"""Build the public demo page for GitHub Pages.

The approved interactive mockup (docs/mockups/ops-v2.html) is authored in the
Claude artifact format: it starts with <title>/<meta>/<link>/<style> and then the
body markup, without <html>/<head>/<body>. This script wraps it into a complete
standalone document and, when real simulation frames exist under
tests/evidence/M1, injects an evidence strip showing them.

Usage: build_demo_page.py <dist_dir>
  - writes <dist_dir>/index.html   (the demo)
  - moves an existing <dist_dir>/index.html (the backend console shell) to app.html
"""

from __future__ import annotations

import html
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
MOCKUP = ROOT / "docs" / "mockups" / "ops-v2.html"
EVIDENCE_DIRS = [
    ROOT / "tests" / "evidence" / "M2" / "dev",
    ROOT / "tests" / "evidence" / "M1" / "dev",
    ROOT / "tests" / "evidence" / "M1",
]
# key substring in the filename -> caption; the first match per key across EVIDENCE_DIRS wins
FRAME_LABELS = {
    "overhead_bottom": "Skydio X2 at 6 m · downward camera over the rubble",
    "go2_front_moved": "Unitree Go2 · front camera, approaching the rubble",
    "overhead_payload": "Skydio X2 at 6 m · forward camera",
    "drone_payload": "Skydio X2 · forward camera",
    "go2_front": "Unitree Go2 · front camera",
    "drone_bottom": "Skydio X2 · downward camera (ground)",
}
MAX_FRAMES = 4

STRIP_CSS = """
.evidence{position:absolute;right:10px;bottom:10px;z-index:7;background:rgba(17,24,32,.95);border:1px solid #3ddc84;border-radius:6px;padding:8px 10px;display:grid;gap:6px;max-width:min(560px,calc(100% - 20px))}
.evidence h3{margin:0;font:600 11px/1 "Barlow Condensed",sans-serif;letter-spacing:.12em;text-transform:uppercase;color:#3ddc84}
.evidence p{margin:0;font-size:11.5px;color:#9fb0c0}
.evidence .row{display:flex;gap:6px;flex-wrap:wrap}
.evidence figure{margin:0;width:160px}
.evidence img{display:block;width:160px;aspect-ratio:16/9;object-fit:cover;border:1px solid #243040;border-radius:3px;background:#000}
.evidence figcaption{font-size:10.5px;color:#9fb0c0;margin-top:3px}
.evidence a{color:#5ab4ff}
@media (max-width:900px){.evidence{position:static;margin:8px}}
"""


def find_frames() -> list[tuple[str, pathlib.Path]]:
    found: list[tuple[str, pathlib.Path]] = []
    used: set[str] = set()
    for key, label in FRAME_LABELS.items():
        for d in EVIDENCE_DIRS:
            if not d.is_dir():
                continue
            hits = [
                p
                for p in sorted(d.glob(f"*{key}*.jpg")) + sorted(d.glob(f"*{key}*.png"))
                if p.name not in used
            ]
            if hits:
                found.append((label, hits[0]))
                used.add(hits[0].name)
                break
        if len(found) >= MAX_FRAMES:
            break
    return found


def build(dist: pathlib.Path) -> None:
    src = MOCKUP.read_text(encoding="utf-8")
    cut = src.index("</style>") + len("</style>")
    head, body = src[:cut], src[cut:]

    frames = find_frames()
    strip = ""
    if frames:
        frames_dir = dist / "evidence"
        frames_dir.mkdir(parents=True, exist_ok=True)
        figs = []
        for label, p in frames:
            dest = frames_dir / p.name
            shutil.copyfile(p, dest)
            figs.append(
                f'<figure><a href="evidence/{html.escape(p.name)}" target="_blank" rel="noopener">'
                f'<img src="evidence/{html.escape(p.name)}" alt="{html.escape(label)}"></a>'
                f"<figcaption>{html.escape(label)}</figcaption></figure>"
            )
        strip = (
            '<div class="evidence"><h3>Real Cyberwave MuJoCo frames · captured today</h3>'
            "<p>Rendered by the live simulation from our scene script (Go2 + Skydio X2 + rubble GLB). "
            "Everything else on this page is the approved interactive mockup running a scripted mission.</p>"
            f'<div class="row">{"".join(figs)}</div></div>'
        )
        head = head.replace("</style>", STRIP_CSS + "</style>", 1)
        body = body.replace(
            '<main class="stage" id="stage">',
            '<main class="stage" id="stage">' + strip,
            1,
        )

    doc = (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        '<meta name="description" content="Sky & Paw — map-first operator console for drone + Go2 '
        'disaster response, interactive demo.">\n'
        f"{head}\n</head>\n<body>\n{body}\n</body>\n</html>\n"
    )

    dist.mkdir(parents=True, exist_ok=True)
    shell = dist / "index.html"
    if (
        shell.exists()
        and "Sky &amp; Paw Ops" in shell.read_text(encoding="utf-8", errors="replace")
        and "SP.Map2D" not in shell.read_text(encoding="utf-8", errors="replace")
    ):
        shutil.move(str(shell), str(dist / "app.html"))
    (dist / "index.html").write_text(doc, encoding="utf-8")
    print(
        f"demo page written to {dist / 'index.html'} ({len(doc)} bytes); frames: {len(frames)}"
    )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    build(pathlib.Path(sys.argv[1]))
