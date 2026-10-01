# -*- coding: utf-8 -*-
"""立体测距与方位几何，移植自 VisionDev 的 stereo.py（几乎原样保留）。

距离公式：distance = focal_length * baseline / disparity
  focal_length 与 baseline 需按实际相机标定，写在 config 的 stereo 段里。
  单目时没有 disparity，distance 恒为 None，前端显示「—」，但方位仍可算。
"""
from __future__ import annotations

import math

import cv2
import numpy as np


class StereoDepth:
    def __init__(self, cfg):
        st = cfg["stereo"]
        self.focal = float(st.get("focal_length", 700.0))
        self.baseline = float(st.get("baseline", 0.06))
        self.min_d = float(st.get("min_distance", 0.2))
        self.max_d = float(st.get("max_distance", 20.0))
        self.matcher = cv2.StereoSGBM_create(
            minDisparity=0, numDisparities=96, blockSize=7,
            P1=8 * 3 * 7 * 7, P2=32 * 3 * 7 * 7,
            disp12MaxDiff=1, uniquenessRatio=10,
            speckleWindowSize=100, speckleRange=32,
        )

    def disparity_map(self, left, right):
        """视差图；无效像素置 NaN（而不是 0），避免被当成「无穷远」。"""
        g_l = cv2.cvtColor(left, cv2.COLOR_BGR2GRAY)
        g_r = cv2.cvtColor(right, cv2.COLOR_BGR2GRAY)
        disp = self.matcher.compute(g_l, g_r).astype(np.float32) / 16.0
        disp[disp <= 0] = np.nan
        return disp

    def distance_for_box(self, disp, bbox):
        """取框中心 40% 区域视差的中位数估距；取中位数是为了抗少量错误匹配。"""
        x1, y1, x2, y2 = bbox
        w, h = x2 - x1, y2 - y1
        cx1, cx2 = int(x1 + w * 0.3), int(x1 + w * 0.7)
        cy1, cy2 = int(y1 + h * 0.3), int(y1 + h * 0.7)
        region = disp[cy1:cy2, cx1:cx2]
        if region.size == 0:
            return None
        valid = region[~np.isnan(region)]
        if valid.size == 0:
            return None
        d = float(np.median(valid))
        if d <= 0:
            return None
        distance = self.focal * self.baseline / d
        if not (self.min_d <= distance <= self.max_d):
            return None
        return round(distance, 2)

    def bearing(self, bbox, frame_width):
        """水平方位角（度）：负=左，正=右。单目也有意义（只依赖焦距与像素位置）。"""
        cx = (bbox[0] + bbox[2]) / 2
        return math.degrees(math.atan((cx - frame_width / 2) / self.focal))


def direction_label(angle_deg):
    if angle_deg <= -15:
        return "left"
    if angle_deg >= 15:
        return "right"
    return "center"


def position(distance, angle_deg):
    """相机坐标系位置：x 横向、z 前向，单位米。"""
    rad = math.radians(angle_deg)
    return {
        "x": round(distance * math.sin(rad), 2),
        "y": 0.0,
        "z": round(distance * math.cos(rad), 2),
    }