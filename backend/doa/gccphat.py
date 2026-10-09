
# -*- coding: utf-8 -*-
"""GCC-PHAT：两路麦克风之间的时延差估计（核心算法，纯 numpy）。

为什么 PHAT：直接互相关（CC）在混响/多径下会找错峰；把互谱归一成纯相位
（G = X1·conj(X2) / |..|）能锐化峰值，对语音 + 桌面距离更稳健。
设计文档 § 4。本模块不关心「麦克风在哪」——只回答「这两路的时延差是多少秒」。

为什么做抛物线插值到亚样本：Windows 上 0.7ms 及 16k 采样下，70mm 基线只有约
3 个样本的路径差；用「整数 argmax」只有 3~4 档，数字人会僵在几个角度。
对互相关峰值做三点抛物线拟合，可把时延细分到零点几样本，转向才连续。
"""
from __future__ import annotations

import numpy as np


def _parabolic_peak(cc: np.ndarray, center: int, half: int = 2):
    """围绕 center 的三点抛物线拟合，返回亚样本偏移（相对于 center）。"""
    n = cc.shape[0]
    # 取以 center 为中心的 5 点做抛物线最小二乘（更稳于只用 3 点）
    idx = [(center + k) % n for k in range(-half, half + 1)]
    xs = np.arange(-half, half + 1, dtype=float)
    ys = np.array([cc[i].real for i in idx], dtype=float)
    if np.ptp(ys) == 0:
        return 0.0
    # 拟合 y = a x^2 + b x + c，顶点 x* = -b/(2a)
    A = np.vstack([xs ** 2, xs, np.ones_like(xs)]).T
    try:
        coef, *_ = np.linalg.lstsq(A, ys, rcond=None)
    except np.linalg.LinAlgError:
        return 0.0
    if coef[0] == 0:
        return 0.0
    return -coef[1] / (2.0 * coef[0])


def gcc_phat(x1: np.ndarray, x2: np.ndarray, fs: int = 16000) -> float:
    """返回两路之间的时延差 tau（秒）。正数表示 x1 比 x2 晚到。"""
    n = len(x1)
    if n == 0:
        return 0.0
    x1 = x1 - np.mean(x1)
    x2 = x2 - np.mean(x2)
    w = np.hamming(n)
    x1 = x1 * w
    x2 = x2 * w

    N = 2 * n
    X1 = np.fft.rfft(x1, N)
    X2 = np.fft.rfft(x2, N)
    G = X1 * np.conj(X2)
    G /= np.maximum(np.abs(G), 1e-12)

    cc = np.fft.irfft(G, N)
    # 用带符号的循环偏移表示时延
    center = int(np.argmax(np.abs(cc)))
    offset = _parabolic_peak(cc, center)
    delay = float(center) + offset
    if delay > N // 2:
        delay -= N
    # clamp 到物理合理范围（几 ms 以内）
    max_s = 0.005 * fs  # ±5ms
    if abs(delay) > max_s:
        delay = delay % N
    return delay / float(fs)
