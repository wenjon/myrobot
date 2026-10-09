
# -*- coding: utf-8 -*-
"""DOA 后台服务：source → estimator → 最新方位快照（供 server 广播）。

运行方式：
  - 离线（demo/CI）：DOA_SOURCE=sweep，用 AzimuthSweepSource 合成「声源在做圆周扫」
    直接驱动，无需真机。
  - 真机（后续）：DOA_SOURCE=sounddevice，接 ReSpeaker 4-Mic，见 docs/doa-gaze-design.md §5。

线程模型：DoaService 是一个后台守护线程，生产/更新快照放进一个受锁保护的 dict；
server 的 WS 协程只读（atomically），绝不在 IO loop 里跑 GCC-PHAT（会卡住连接）。
"""
from __future__ import annotations

import threading
import time
from typing import Optional

import numpy as np

from config import (
    DOA_ENABLED, DOA_SOURCE, DOA_FS, DOA_FRAME_MS, DOA_HOP_MS,
    DOA_ACTIVE_DB, DOA_SMOOTH, DOA_TTS_FREEZE, DOA_PUSH_HZ,
)
from .estimator import DoaEstimator
from .source import AzimuthSweepSource


class DoaService:
    """把采集源 + 估计器包成一个节流的后台线程，暴露只读快照。"""

    def __init__(self, enabled: bool = DOA_ENABLED, source: str = DOA_SOURCE,
                 fs: int = DOA_FS, frame_ms: int = DOA_FRAME_MS, hop_ms: int = DOA_HOP_MS,
                 active_db: float = DOA_ACTIVE_DB, smooth: float = DOA_SMOOTH,
                 tts_freeze: bool = DOA_TTS_FREEZE, push_hz: float = DOA_PUSH_HZ,
                 log_fn=None):
        self.enabled = bool(enabled)
        self.source_name = source
        self.fs = int(fs)
        self.frame_n = int(self.fs * frame_ms / 1000)
        self.hop_n = max(1, int(self.fs * hop_ms / 1000))
        self.estimator = DoaEstimator(fs=self.fs, smooth=smooth,
                                      active_db=active_db, tts_freeze=tts_freeze)
        self.push_interval = 1.0 / max(0.5, float(push_hz))
        self._tts_active_flag = False
        self.log = log_fn or (lambda *_a, **_k: None)

        # 最新快照（跨线程只读）
        self._lock = threading.Lock()
        self._snap = {"active": False, "az": 0.0, "ts": 0, "seq": 0,
                      "status": "init", "enabled": self.enabled}
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def set_logger(self, log_fn):
        """接入 server 的统一日志器（server.py 在 lifespan 里调用）。"""
        self.log = log_fn or (lambda *_a, **_k: None)

    # ---------------- 快照 ----------------
    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._snap)

    def _set(self, **kw):
        with self._lock:
            self._snap.update(kw)

    # ---------------- 生命周期 ----------------
    def start(self):
        if not self.enabled:
            self._set(status="disabled")
            self.log("[DOA] 已禁用（DOA_ENABLED=0），不启动。")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="doa-service", daemon=True)
        self._thread.start()
        self.log(f"[DOA] 后台线程启动（source={self.source_name}, fs={self.fs}, "
                 f"frame={self.frame_n} sp）")

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._set(status="stopped", active=False)
        self.log("[DOA] 后台线程已停止。")

    # ---------------- 主循环 ----------------
    def _run(self):
        try:
            src = self._make_source()
            next_push = 0.0
            hop_sec = self.hop_n / self.fs
            seq = 0
            for frame in src.read_frames():
                if self._stop.is_set():
                    break
                now = time.time()
                # TTS 冻结标志：由 server 每轮对话写入（见 server.py）
                with self._lock:
                    tts_active = self._tts_active_flag
                self.estimator.tts_active = tts_active
                self.estimator.update(frame, now)
                seq += 1
                self._set(active=self.estimator.active,
                          az=float(self.estimator.azimuth_deg),
                          ts=int(now * 1000), seq=seq,
                          status="running")
                # 节流推送：server 侧以 push_hz 为准，但快照本身每帧都在推进
                if now >= next_push:
                    next_push = now + self.push_interval
            self._set(status="eof", active=False)
        except Exception as e:  # noqa: BLE001
            self._set(status="error", error=str(e), active=False)
            self.log(f"[DOA] 后台线程异常结束: {type(e).__name__}: {e}")

    def _make_source(self):
        from .source import (
            AzimuthSweepSource,
            SounddeviceSource,
        )
        if self.source_name == "sounddevice":
            return SounddeviceSource(fs=self.fs, frame_ms=int(self.frame_n * 1000 / self.fs))
        return AzimuthSweepSource(fs=self.fs, frame_n=self.frame_n)

    def note_tts_active(self, active: bool):
        """由 server 每轮对话调用：记录「自己正在播报」，供 estimator 冻结转向。"""
        with self._lock:
            self._tts_active_flag = bool(active)


# 全局单例（与 vision 同款）：server.py 直接 import 使用
doa_service = DoaService(log_fn=None)
