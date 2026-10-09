
# -*- coding: utf-8 -*-
"""DOA 声源测向自检（docs/doa-gaze-design.md）。

纯离线：不请求 LLM、不需要阵列、不需 Key。用 model.sample_azimuth_signal 合成
已知方位角的 4 路测试信号，喂给 estimator 验证三件事：
  1) 方位角反解正确（符号对、误差在允许范围）；
  2) 安静帧被判为不活动（能量门限）；
  3) TTS 冻结生效（自己播报时不转向）——防止“回声自激”回归。

在仓库根目录运行：python scripts/check_doa.py
"""
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from doa.estimator import DoaEstimator        # noqa: E402
from doa.model import sample_azimuth_signal    # noqa: E402

FS = 48000          # 采样率：48k 下 70mm 基线全幅可达；16k 只到约 ±63°
N = 1600            # 帧长（样本）
SCAN = [-90, -60, -30, 0, 30, 60, 90]
MAX_ERR = 12.0      # 「期望 az -> 期望估计」单帧最大允许偏差（度）


def est_one(az, seed=5):
    f = sample_azimuth_signal(az, n_samples=N, fs=FS, seed=seed)
    e = DoaEstimator(fs=FS, smooth=1.0, active_db=-40.0)
    e.update(f)
    return e


def main():
    ok = True
    results = []

    # 1) 方位角反解：符号 + 误差
    errors = []
    for az in SCAN:
        e = est_one(az)
        err = abs(az - e.azimuth_deg)
        errors.append(err)
        tag = "PASS" if err <= MAX_ERR else "FAIL"
        if err > MAX_ERR:
            ok = False
        results.append(f"  [{tag}] az={az:+4d} est={e.azimuth_deg:+7.1f} "
                       f"(err={err:.1f}) active={e.active}")
    # 符号校验：+az 应估计为正、-az 为负（至少不反号）
    neg = est_one(-30)
    pos = est_one(30)
    if not (pos.azimuth_deg > 0 and neg.azimuth_deg < 0):
        ok = False
        results.append("  [FAIL] 方位角符号方向错误（+az 应正向）")

    # 2) 安静帧 -> inactive
    quiet = np.random.default_rng(0).standard_normal((4, N)).astype("float32") * 0.003
    e = DoaEstimator(fs=FS, smooth=0.9, active_db=-30.0)
    e.update(quiet)
    results.append(f"  [{'PASS' if not e.active else 'FAIL'}] 安静帧被判 inactive={not e.active}")

    # 3) TTS 冻结
    f = sample_azimuth_signal(30, n_samples=N, fs=FS, seed=7)
    e = DoaEstimator(fs=FS, smooth=1.0, active_db=-40.0, tts_freeze=True)
    e.tts_active = True
    e.update(f)
    frozen = (not e.active) and abs(e.azimuth_deg) <= 0.5
    results.append(f"  [{'PASS' if frozen else 'FAIL'}] TTS 冻结：自己播报时不转 (est={e.azimuth_deg:+.1f})")

    print("DOA 自检（离片，无阵列/无密钥）:")
    for r in results:
        print(r)
    max_abs_err = max(errors)
    print(f"  最大单帧误差: {max_abs_err:.1f}°（上限 {MAX_ERR}°）")
    print("OK：DOA 测向自检通过" if ok else "FAIL：DOA 测向自检失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
