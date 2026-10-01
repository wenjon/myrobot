# -*- coding: utf-8 -*-
"""相机采集：双目（左右并排帧 / 两个相机）与单目，移植自 VisionDev 的 camera.py。

三种模式：
  single —— 一个 USB 双目相机，输出左右并排的一帧（宽 = 2×高），这里切成 left/right
  dual   —— 两个独立 USB 相机（left_index / right_index）
  mono   —— 只有一个普通相机：只有 left，没有 right，因此**没有视差、没有深度**
  auto   —— 依次尝试 single → mono → dual

注意 `mono` 是移植时新增的：VisionDev 原版在 single 失败后会直接报错，
而本机实测只有单目相机，所以必须能优雅退到 mono 而不是整个子系统挂掉。
"""
from __future__ import annotations

import cv2


class CameraError(Exception):
    """相机不可用。由上层捕获后决定是降级还是关闭视觉子系统。"""


class StereoCamera:
    def __init__(self, cfg):
        self.cfg = cfg
        self.mode = None
        self.cap_left = None
        self.cap_right = None

    def open(self) -> bool:
        cam_cfg = self.cfg["camera"]
        width = cam_cfg.get("width", 640)
        height = cam_cfg.get("height", 480)
        mode = cam_cfg.get("mode", "auto")
        backend = cam_cfg.get("backend", "default")
        cap_backend = 0 if backend == "default" else getattr(cv2, "CAP_" + str(backend).upper(), 0)

        def try_open(index):
            cap = cv2.VideoCapture(index, cap_backend)
            if not cap.isOpened():
                cap.release()
                return None
            return cap

        # ---- 双目：单个相机 + 左右并排画面 ----
        if mode in ("auto", "single"):
            cap = try_open(cam_cfg.get("single_index", 0))
            if cap is not None:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, width * 2)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
                ok, frame = cap.read()
                # 判据与 VisionDev 一致：帧宽至少是高的 2 倍才算「左右并排」
                if ok and frame is not None and frame.shape[1] >= frame.shape[0] * 2:
                    self.mode = "single"
                    self.cap_left = cap
                    return True
                # 不是并排帧 → 当成单目用（原版这里会直接失败）
                self.mode = "mono"
                self.cap_left = cap
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
                return True

        # ---- 双目：两个相机 ----
        if mode in ("auto", "dual"):
            left = try_open(cam_cfg.get("left_index", 0))
            right = try_open(cam_cfg.get("right_index", 1))
            if left is not None and right is not None:
                for cap in (left, right):
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
                self.mode = "dual"
                self.cap_left, self.cap_right = left, right
                return True
            for cap in (left, right):
                if cap is not None:
                    cap.release()
        return False

    def read(self):
        """返回 (left, right)；right 为 None 表示当前是单目（无深度）。"""
        if self.mode == "single":
            ok, frame = self.cap_left.read()
            if not ok or frame is None:
                return None, None
            half = frame.shape[1] // 2
            return frame[:, :half], frame[:, half:]
        if self.mode == "dual":
            ok_l, left = self.cap_left.read()
            ok_r, right = self.cap_right.read()
            if not (ok_l and ok_r) or left is None or right is None:
                return None, None
            return left, right
        if self.mode == "mono":
            ok, left = self.cap_left.read()
            if not ok or left is None:
                return None, None
            return left, None
        return None, None

    def release(self):
        for cap in (self.cap_left, self.cap_right):
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass