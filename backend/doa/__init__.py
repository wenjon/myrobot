# -*- coding: utf-8 -*-
"""麦克风阵列声源测向（DOA）→ 数字人注视转向。

设计文档：docs/doa-gaze-design.md（先读它，再读代码）。
包布局：
  source.py    可插拔采集源（AzimuthSweepSource 离片回放 / sounddevice 真机）
  gccphat.py   GCC-PHAT 帧级时延差估计（核心算法）
  estimator.py 方位角估计 + 时域去抖 + 活动度门限 + TTS 冻结
  model.py     四方阵几何模型（含时延→方位角的正/反解）
"""
