# -*- coding: utf-8 -*-
"""视觉管线：采集 → 检测 → 深度/方位 → 标注 → 发布。

深度来源有两条路，**按可用性自动选**（不需要人工切换）：

  1. **结构光（首选）**：Orbbec Astra Pro 的深度传感器，走厂商 Astra SDK。
     真实物理测距，精度高、不受纹理影响。实测本机可用（640x480，75% 有效像素）。
  2. **双目视差（备选）**：一个相机输出左右并排画面 / 两个相机时的
     `distance = focal * baseline / disparity`。
  3. **都没有**：单目降级——只出方位角（靠 hfov 或焦距换算），距离为 None。

原版（VisionDev）只有第 2 条；本机这台 Astra Pro 由两个 USB 设备组成，
OpenCV 只能看到它的 RGB 那一半，所以补了第 1 条才拿到真实深度。

线程安全：本类跑在后台守护线程里，用 Lock 保护三份快照；
HTTP 处理器只做 read-under-lock，不与检测抢时间。
"""
from __future__ import annotations

import threading
import time

# Astra 深度帧常见分辨率（按面积反推，因为 SDK 没导出取宽高的符号）
_DEPTH_SHAPES = {640 * 480: (480, 640), 320 * 240: (240, 320), 1280 * 960: (960, 1280)}

# 深度帧可复用的最长时间（秒）。超过就认为相机已移动，宁可显示未知也不要给旧距离。
_DEPTH_STALE_S = 0.3


def vision_available() -> tuple[bool, str]:
    """检查视觉依赖是否齐全。返回 (可用, 原因)。

    只检查包在不在，不在这里加载 YOLO 权重（那会触发下载，太重）。
    """
    import importlib.util as u

    for mod in ("cv2", "numpy"):
        if u.find_spec(mod) is None:
            return False, f"缺少依赖 {mod}（pip install opencv-python numpy）"
    if u.find_spec("ultralytics") is None:
        return False, "缺少依赖 ultralytics（pip install ultralytics）"
    return True, "ok"


class VisionPipeline(threading.Thread):
    def __init__(self, cfg):
        super().__init__(daemon=True, name="vision-pipeline")
        self.cfg = cfg
        self.running = threading.Event()
        self.lock = threading.Lock()
        self.latest_frame = None
        self.latest_jpeg = None
        self.latest_event = {"ts": 0, "frame_id": 0, "objects": []}
        self.status = "init"
        self.mode = None            # mono / single / dual
        self.depth_source = None    # astra / stereo / none
        self.frame_id = 0
        self.error = ""

        yolo_cfg = cfg.get("yolo", {})
        self._detect_every = max(1, int(yolo_cfg.get("detect_every_n_frames", 3)))
        self._detect_counter = 0
        self._jpeg_quality = int(cfg.get("server", {}).get("jpeg_quality", 70))
        self._hfov_deg = None
        self._last_depth = None       # 最近一帧深度图（无阻塞读拿不到时应复用）
        self._last_depth_ts = 0.0

    # ---------- 快照读取（HTTP 处理器用） ----------
    def get_latest_event(self):
        with self.lock:
            return dict(self.latest_event)

    def get_latest_jpeg(self):
        with self.lock:
            return self.latest_jpeg

    def snapshot(self) -> dict:
        with self.lock:
            has_frame = self.latest_jpeg is not None
        return {
            "status": self.status,
            "mode": self.mode,
            "depth_source": self.depth_source,          # astra / stereo / none
            "depth": self.depth_source in ("astra", "stereo"),
            "frame_id": self.frame_id,
            "has_frame": has_frame,
            "error": self.error,
        }

    # ---------- 深度源装配 ----------
    def _open_astra(self):
        """尝试打开结构光深度。失败返回 (None, 原因)，绝不抛出。"""
        from .astra import AstraDepth, AstraUnavailable

        sdk_dir = (self.cfg.get("astra") or {}).get("sdk_dir", "")
        if not sdk_dir:
            return None, "未配置 VISION_ASTRA_SDK_DIR"
        try:
            a = AstraDepth(sdk_dir)
            a.open()
            return a, ""
        except AstraUnavailable as e:
            return None, str(e)
        except Exception as e:  # noqa: BLE001
            return None, f"{type(e).__name__}: {e}"

    # ---------- 主循环 ----------
    def run(self):
        from .camera import StereoCamera
        from .detector import Detector
        from .stereo import StereoDepth, direction_label, position
        from .tracker import CentroidTracker

        try:
            cam = StereoCamera(self.cfg)
            if not cam.open():
                self.status = "no-camera"
                self.error = "未找到可用相机"
                return
            self.mode = cam.mode

            detector = Detector(self.cfg)
            stereo = StereoDepth(self.cfg)
            tracker = CentroidTracker()

            # 深度优先级：结构光 > 双目视差 > 无
            astra, why = self._open_astra()
            if astra is not None:
                self.depth_source = "astra"
                self._hfov_deg = astra.hfov_deg
            elif cam.mode in ("single", "dual"):
                self.depth_source = "stereo"
            else:
                self.depth_source = "none"
                if why:
                    self.error = f"结构光深度不可用（{why}），退回单目"
            self.status = f"running:{cam.mode}"
            self.running.set()

            prev_detections = []
            while self.running.is_set():
                left, right = cam.read()
                if left is None:
                    time.sleep(0.05)
                    continue
                h, w = left.shape[:2]

                # ---- 检测：每 N 帧跑一次，中间帧用 tracker 位置兜住 ----
                self._detect_counter += 1
                if self._detect_counter % self._detect_every == 0:
                    detections = detector.detect(left)
                    tracker.update(detections)
                    prev_detections = [dict(d) for d in detections]
                else:
                    detections = []
                    for tid, tr in tracker.tracks.items():
                        cx, cy = tr["center"]
                        sz = 40
                        for d in prev_detections:
                            if d.get("id") == tid:
                                x1, y1, x2, y2 = d["bbox"]
                                sz = max(x2 - x1, y2 - y1) // 2
                                break
                        detections.append({
                            "bbox": [int(cx - sz), int(cy - sz), int(cx + sz), int(cy + sz)],
                            "type": tr["type"],
                            "type_en": tr.get("type_en", tr["type"]),
                            "confidence": 0.0,
                            "id": tid,
                            "tracked": True,
                        })
                    prev_detections = detections

                # ---- 取这一帧的深度图（结构光）----
                # Astra 是无阻塞读：两次深度帧之间去读会拿到 None。若照实传 None，
                # 距离就会一帧有一帧无地闪（实测踩过）。深度 30fps，比检测快得多，
                # 复用最近一帧既正确又平滑——但只要超过 _DEPTH_STALE_S 就必须丢掉，
                # 否则相机动了还会拿着旧距离不放。
                depth_map = astra.read() if astra is not None else None
                if depth_map is not None:
                    self._last_depth = depth_map
                    self._last_depth_ts = time.time()
                elif (self._last_depth is not None
                      and time.time() - self._last_depth_ts < _DEPTH_STALE_S):
                    depth_map = self._last_depth
                disp = None
                if depth_map is None and right is not None:
                    disp = stereo.disparity_map(left, right)

                for det in detections:
                    x1, y1, x2, y2 = det["bbox"]
                    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2

                    if depth_map is not None:
                        # 结构光：框中心附近有效深度的中位数
                        det["distance_m"] = _depth_at(depth_map, cx, cy, w, h)
                    elif disp is not None:
                        det["distance_m"] = stereo.distance_for_box(disp, det["bbox"])
                    else:
                        det["distance_m"] = None

                    # 方位角：有 hfov 就用官方的（更准），否则用焦距换算
                    if self._hfov_deg:
                        angle = (cx - w / 2.0) * (self._hfov_deg / w)
                    else:
                        angle = stereo.bearing(det["bbox"], w)
                    det["angle_deg"] = round(angle, 1)
                    det["direction"] = direction_label(angle)
                    det["position"] = (position(det["distance_m"], angle)
                                       if det["distance_m"] is not None else None)

                self.frame_id += 1
                annotated = self._annotate(left, detections)
                jpeg = self._encode(annotated)
                event = {"ts": time.time(), "frame_id": self.frame_id,
                         "mode": self.mode,
                         "depth_source": self.depth_source,
                         "depth_available": self.depth_source != "none",
                         "depth_stats": self._depth_stats(depth_map, disp, left.shape),
                         "objects": detections}
                with self.lock:
                    self.latest_frame = annotated
                    self.latest_jpeg = jpeg
                    self.latest_event = event

            if astra is not None:
                astra.close()
            cam.release()
            self.status = "stopped"
        except Exception as e:  # noqa: BLE001
            # 相机被拔、模型加载失败等：置错误态让前端浮层显示出来，
            # 绝不让异常冒到 myrobot 主服务（对话链路必须照常）。
            self.status = "error"
            self.error = f"{type(e).__name__}: {e}"

    @staticmethod
    def _depth_stats(depth_map, disp, frame_shape):
        """诊断用：这一帧深度图到底有没有有效值、范围多少。

        没有它就只能看到「距离全是 None」，分不清是「深度没出帧」「整帧都是 0」
        还是「取深度那个点恰好无效」——排障时这三点差别很大。
        """
        import numpy as np

        if depth_map is not None:
            valid = depth_map[depth_map > 0]
            if valid.size == 0:
                return {"source": "astra", "shape": list(depth_map.shape), "valid_ratio": 0.0}
            return {"source": "astra", "shape": list(depth_map.shape),
                    "valid_ratio": round(float((depth_map > 0).mean()), 3),
                    "min_mm": int(valid.min()), "max_mm": int(valid.max()),
                    "median_mm": int(np.median(valid))}
        if disp is not None:
            valid = disp[~np.isnan(disp)]
            return {"source": "stereo", "valid_ratio": round(float(valid.size / disp.size), 3)}
        return {"source": "none", "frame": list(frame_shape[:2])}

    def _encode(self, frame):
        import cv2

        ok, buf = cv2.imencode(".jpg", frame,
                               [int(cv2.IMWRITE_JPEG_QUALITY), self._jpeg_quality])
        return buf.tobytes() if ok else None

    @staticmethod
    def _annotate(frame, detections):
        import cv2

        out = frame.copy()
        for det in detections:
            x1, y1, x2, y2 = det["bbox"]
            tracked = det.get("tracked", False)
            color = (0, 180, 0) if tracked else (0, 220, 0)
            cv2.rectangle(out, (x1, y1), (x2, y2), color, 1 if tracked else 2)
            dist = det.get("distance_m")
            label = f"#{det['id']} {det.get('type_en') or det['type']}"
            if det.get("confidence", 0) > 0:
                label += f" {det['confidence']:.2f}"
            if dist is not None:
                label += f" {dist:.1f}m {det['angle_deg']:+.0f}deg"
            elif det.get("angle_deg") is not None:
                label += f" {det['angle_deg']:+.0f}deg"
            cv2.putText(out, label, (x1, max(y1 - 8, 16)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)
        return out

    def stop(self):
        self.running.clear()


def _depth_at(depth, cx, cy, frame_w, frame_h):
    """在深度图上取框中心邻域的有效深度中位数（米）。

    RGB 与深度是**两个不同传感器**、视场角和分辨率都不同，不能按像素直接对应。
    这里先按各自尺寸做归一化坐标映射（够 demo 用）；要更准需要做深度-彩色对齐标定
    （Astra SDK 提供 astra_depthstream_set_registration，后续可接）。
    """
    import numpy as np

    if depth is None:
        return None
    dh, dw = depth.shape[:2]
    dx = int(cx * dw / frame_w) if frame_w else 0
    dy = int(cy * dh / frame_h) if frame_h else 0
    r = max(4, dw // 40)
    x1, x2 = max(0, dx - r), min(dw, dx + r + 1)
    y1, y2 = max(0, dy - r), min(dh, dy + r + 1)
    roi = depth[y1:y2, x1:x2]
    if roi.size == 0:
        return None
    valid = roi[roi > 0]
    if valid.size == 0:
        return None
    return round(float(np.median(valid)) / 1000.0, 2)