
# -*- coding: utf-8 -*-
"""方位角估计：帧缓冲 → 横向基线时延差 → 方位角 + 去抖 + 活动度 + TTS 冻结。

把「GCC-PHAT（每帧纯函数）」与「状态化平滑」分开：这里负责把连续帧拼出
稳定的 az，并在自己 TTS 播放时冻结（防自激）。
设计文档 § 4.3 / § 7。算法基于 ReSpeaker 环形阵列经典做法——横向 `右-左`
一条基线 + asin 反解（见 model.py）。
"""
from __future__ import annotations

import math
import time

import numpy as np

from .gccphat import gcc_phat
from .model import SOUND_SPEED, azimuth_from_deltas


class DoaEstimator:
    """DOA 估计器：右-左（ch0-ch1）横向基线估计方位角。"""

    def __init__(self, fs: int = 16000, smooth: float = 0.4,
                 active_db: float = -30.0, tts_freeze: bool = True):
        self.fs = fs
        self.smooth = max(0.0, min(0.95, float(smooth)))
        self.active_db = float(active_db)
        self.tts_freeze = bool(tts_freeze)

        self.azimuth_deg = 0.0
        self.active = False
        self.last_ts = 0.0
        self.tts_active = False   # 外部每帧写入：自己是否正在播报（回声抑制）

    @staticmethod
    def rms_db(frame: np.ndarray) -> float:
        """整帧 RMS 电平（dBFS）。约 -50 安静、-20 正常说话、0 削顶。"""
        level = max(float(np.sqrt(np.mean(frame ** 2))), 1e-12)
        return 20.0 * math.log10(level)

    def update(self, frame: np.ndarray, now: float | None = None):
        now = now or time.time()
        if frame.shape[0] < 4:
            self.active = False
            return

        # (a) 活动度：能量门限
        if self.rms_db(frame) < self.active_db:
            self.active = False
            return

        # (b) TTS 冻结：自己说话时不改角度
        if self.tts_freeze and self.tts_active:
            self.active = False
            return

        # (c) 横向基线时延差 → 方位角
        tau_lr = gcc_phat(frame[0], frame[1], self.fs)   # 秒，正=右麦更晚到
        d_lr = tau_lr * SOUND_SPEED                       # 米
        az = azimuth_from_deltas(d_lr)

        # (d) 时域平滑去抖
        self.azimuth_deg += (az - self.azimuth_deg) * self.smooth
        self.active = True
        self.last_ts = now
