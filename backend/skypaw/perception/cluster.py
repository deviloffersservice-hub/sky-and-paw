"""Merge world-frame hits into candidates: hits within ``radius`` (1.0 m) are one candidate.

A :class:`Hit` is one projected detection (RGB person box, RGB non-person object such as the
decoy marker, or a synthetic thermal hotspot). A :class:`Cluster` keeps every hit, its
position is the mean of the visual hits (thermal hotspots only when there is no visual hit),
its RGB confidence is the best person confidence, and it remembers the frames it came from.
Clustering is greedy and order-independent enough for a few dozen hits: each hit joins the
nearest cluster within ``radius`` (horizontal distance), then clusters closer than
``radius`` are merged until none are.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

MERGE_RADIUS_M = 1.0


@dataclass(frozen=True)
class Hit:
    """One detection projected to the world frame."""

    position: tuple[float, float, float]
    kind: str  # "person" | "object" | "thermal"
    conf: float | None = None
    detector: str = ""  # "yolo" | "marker" | "synthetic"
    frame_id: str | None = None
    label: str = ""


@dataclass
class Cluster:
    hits: list[Hit] = field(default_factory=list)

    @property
    def visual(self) -> list[Hit]:
        return [h for h in self.hits if h.kind in ("person", "object")]

    @property
    def position(self) -> tuple[float, float, float]:
        src = self.visual or self.hits
        n = float(len(src))
        return (
            sum(h.position[0] for h in src) / n,
            sum(h.position[1] for h in src) / n,
            sum(h.position[2] for h in src) / n,
        )

    @property
    def best_person(self) -> Hit | None:
        people = [h for h in self.hits if h.kind == "person" and h.conf is not None]
        return max(people, key=lambda h: h.conf or 0.0, default=None)

    @property
    def rgb_conf(self) -> float | None:
        best = self.best_person
        return best.conf if best else None

    @property
    def frames(self) -> list[str]:
        return list(dict.fromkeys(h.frame_id for h in self.hits if h.frame_id))

    def dist_xy(self, p: tuple[float, float, float] | list[float]) -> float:
        q = self.position
        return math.hypot(q[0] - p[0], q[1] - p[1])


def add_hits(
    clusters: list[Cluster], hits: list[Hit], radius: float = MERGE_RADIUS_M
) -> list[Cluster]:
    """Add ``hits`` to ``clusters`` (in place) and merge clusters within ``radius``."""
    for h in hits:
        near = min(clusters, key=lambda c: c.dist_xy(h.position), default=None)
        if near is not None and near.dist_xy(h.position) <= radius:
            near.hits.append(h)
        else:
            clusters.append(Cluster([h]))
    merged = True
    while merged:
        merged = False
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                if clusters[i].dist_xy(clusters[j].position) <= radius:
                    clusters[i].hits.extend(clusters[j].hits)
                    del clusters[j]
                    merged = True
                    break
            if merged:
                break
    return clusters


def cluster_hits(hits: list[Hit], radius: float = MERGE_RADIUS_M) -> list[Cluster]:
    """Clusters of a fresh list of hits."""
    return add_hits([], hits, radius)


__all__ = ["MERGE_RADIUS_M", "Cluster", "Hit", "add_hits", "cluster_hits"]
