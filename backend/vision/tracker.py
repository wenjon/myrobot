# -*- coding: utf-8 -*-
"""极简质心追踪器，移植自 VisionDev 的 tracker.py（原样保留）。

作用：给每帧的检测框一个**稳定的 id**，避免同一物体在列表里跳来跳去、
或界面上编号每帧都在变。同 id 只会在同类型（type）之间匹配。
"""


class CentroidTracker:
    def __init__(self, max_distance=80, max_misses=15):
        self.max_distance = max_distance
        self.max_misses = max_misses
        self.next_id = 1
        self.tracks = {}  # id -> {center, misses, type}

    def update(self, detections):
        for det in detections:
            x1, y1, x2, y2 = det["bbox"]
            center = ((x1 + x2) / 2, (y1 + y2) / 2)
            best_id, best_dist = None, self.max_distance
            for tid, tr in self.tracks.items():
                dx = center[0] - tr["center"][0]
                dy = center[1] - tr["center"][1]
                dist = (dx * dx + dy * dy) ** 0.5
                if dist < best_dist and tr["type"] == det["type"]:
                    best_id, best_dist = tid, dist
            if best_id is None:
                best_id = self.next_id
                self.next_id += 1
                self.tracks[best_id] = {"center": center, "misses": 0,
                                        "type": det["type"],
                                        "type_en": det.get("type_en", det["type"])}
            else:
                self.tracks[best_id]["center"] = center
                self.tracks[best_id]["misses"] = 0
            det["id"] = best_id

        used = {d["id"] for d in detections}
        for tid in list(self.tracks):
            if tid not in used:
                self.tracks[tid]["misses"] += 1
                if self.tracks[tid]["misses"] > self.max_misses:
                    del self.tracks[tid]
        return detections