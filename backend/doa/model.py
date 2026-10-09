
# -*- coding: utf-8 -*-
"""麦克风阵列几何模型：麦克风位置 + 来波方向 → 时延 → 方位角。

坐标约定（与渲染相机一致）：
    x = +右，y = +上，z = +屏幕向外（朝向说话人）。
四个麦放在同一竖直平面、与 z 轴垂直：
    ch0 (+a, 0, 0)  右
    ch1 (-a, 0, 0)  左
    ch2 (0, +a, 0)  上
    ch3 (0, -a, 0)  下
真实环形阵列相位差可标定（换阵列只改 mic_positions 的坐标即可）。

来波方向（水平面，俯瞰）：
    az=0 → 正前方(z 方向)；az>0 → 声源在右。=> unit_wave = (-sin(az), 0, -cos(az))
横向基线 `右-左` 的路径差 = unit_wave·(右-左) = -sin(az)·(2a)
    → az = asin( d_lr / (-2a) )    （声源偏右 ⇒ 右麦更早到 ⇒ d_lr<0 ⇒ az>0）
"""
from __future__ import annotations

import math

SOUND_SPEED = 343.0


def mic_positions(a: float = 0.035):
    """4 个麦克风的 (x, y, z) 坐标（米）。"""
    return [
        (+a, 0.0, 0.0),   # 0 右
        (-a, 0.0, 0.0),   # 1 左
        (0.0, +a, 0.0),   # 2 上
        (0.0, -a, 0.0),   # 3 下
    ]


def unit_wave(az_deg: float, el_deg: float = 0.0):
    """来波单位向量（水平俯瞰；el 照顾抬头）。"""
    az = math.radians(az_deg)
    el = math.radians(el_deg)
    ce = math.cos(el)
    return (-math.sin(az) * ce, -math.sin(el), -math.cos(az) * ce)


def azimuth_from_deltas(d_leftright: float, a: float = 0.035) -> float:
    """由右-左路径差（米）估方位角 az（度，逆时针为正，-90..+90）。"""
    d = max(-1.0, min(1.0, d_leftright / (-2.0 * a)))
    return math.degrees(math.asin(d))


def sample_azimuth_signal(az_deg: float, el_deg: float = 0.0, n_samples: int = 1600,
                          fs: int = 16000, carrier_hz: float = 300.0,
                          a: float = 0.035, noise: float = 0.03, seed: int = 0):
    """离片验证用：按 (az, el) 合成 4 路测试信号。

    每个麦相对中心的路径增减 = unit_wave · pos_i（米）；
    该麦相对中心的到达时间 = -（unit_wave · pos_i）/ c。
    相位 exp(+j 2π f τ) 即对该麦做「数字延迟」：τ>0 表示更晚到。
    """
    import numpy as np
    c = SOUND_SPEED
    rng = np.random.default_rng(seed)
    t = np.arange(n_samples) / fs
    tone = np.sin(2 * np.pi * carrier_hz * t) + np.sin(2 * np.pi * carrier_hz * 2.0 * t) * 0.3
    mix = tone + float(noise) * rng.standard_normal(n_samples)

    out = np.zeros((4, n_samples), dtype=np.float32)
    fftn = 2 * n_samples
    V = np.fft.rfft(mix, fftn)
    freqs = np.fft.rfftfreq(fftn, 1.0 / fs)
    u = unit_wave(az_deg, el_deg)
    for ci, p in enumerate(mic_positions(a)):
        d0 = u[0] * p[0] + u[1] * p[1] + u[2] * p[2]   # 沿来波方向的增减（米）
        tau = -d0 / c                                   # 秒；更近的麦 ⇒ 延迟更负（更早）
        phase = np.exp(1j * 2 * np.pi * freqs * tau)
        out[ci] = np.fft.irfft(V * phase, fftn)[:n_samples].astype(np.float32)
    return out
