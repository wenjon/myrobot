
# -*- coding: utf-8 -*-
"""采集源（可插拔）：离片合成 / 真机 sounddevice（可选）。

设计文档 § 5：采集是「设备相关」的部分，测向是「通用」的部分。
DoaService 只依赖 AudioSource 接口，切换硬件只换这里的实现。
"""
from __future__ import annotations

import abc
from typing import Iterator

import numpy as np


class AudioSource(abc.ABC):
    sample_rate: int = 48000
    num_channels: int = 4

    @abc.abstractmethod
    def read_frames(self) -> Iterator[np.ndarray]:
        """逐帧产出 (channels, frame_n) float32；StopIteration 或无限循环。"""

    def close(self):
        """释放底层设备（sounddevice 流 / 文件句柄）。"""


class AzimuthSweepSource(AudioSource):
    """离片源：按固定方位序列循环扫描，用于验证算法与标定。

    每一帧代表「声源位于某个 az」的 4 路测试信号（见 model.sample_azimuth_signal），
    循环往返，离线 demo 里数字人就会像在追踪说话人一样来回转头。
    """

    # 扫描序列（度）：从 -120 到 +120 往返共 18 档（略超 ±90 边缘，观察夹取）
    SCAN = [-120, -90, -60, -30, 0, 30, 60, 90, 120,
            +120, +90, +60, +30, 0, -30, -60, -90, -120]

    def __init__(self, fs: int = 48000, frame_n: int = 4800, n_channels: int = 4):
        self.fs = fs
        self.sample_rate = fs
        self.num_channels = n_channels
        self.frame_n = int(frame_n)

    def read_frames(self) -> Iterator[np.ndarray]:
        from .model import sample_azimuth_signal
        i = 0
        while True:
            az = self.SCAN[i % len(self.SCAN)]
            i += 1
            yield sample_azimuth_signal(az, n_samples=self.frame_n, fs=self.fs, seed=i % 997)


class SounddeviceSource(AudioSource):
    """真机采集（可选依赖 sounddevice）。未装则启动时被 DoaService 捕获并禁用。

    真机验证阶段再完善声道顺序（ReSpeaker 4 麦 UAC1.0 在 Windows 常为 4ch，
    但具体 ch0..3 顺序需实测对齐 model.mic_positions：右/左/上/下）。
    """

    def __init__(self, fs: int = 48000, frame_ms: int = 100, device: str = ""):
        self.fs = fs
        self.sample_rate = fs
        self.frame_ms = int(frame_ms)
        parent = device and device or ""
        self._device = parent
        # 延迟 import：没装 sounddevice 就不至于 import 失败
        try:
            import sounddevice  # noqa: F401
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"真机 DOA 需要 sounddevice（pip install sounddevice）：{e}")

    def read_frames(self) -> Iterator[np.ndarray]:
        import sounddevice as sd
        blocksize = int(self.fs * self.frame_ms / 1000)
        with sd.InputStream(device=self._device or None, channels=4,
                            samplerate=self.fs, dtype="float32",
                            blocksize=blocksize) as st:
            while True:
                data, _ = st.read(blocksize)          # (blocksize, 4)
                data = np.asarray(data, dtype="float32")
                if data.ndim == 1:
                    data = data[:, None]
                yield data.T[:4]                      # (4, blocksize)
