# -*- coding: utf-8 -*-
"""视觉子系统自检（backend/vision/）。

视觉是从 D:\\agentos\\VisionDev 移植进来的，管线本身要相机 + cv2 + YOLO 权重才能跑，
没法进 CI。所以这里只测**不依赖 cv2 的纯逻辑**，也就是最容易悄悄坏掉的三块：

  1. tracker：id 稳定性 + type/type_en 的保留（曾因只存 type 导致 type_en 变成中文）；
  2. detector.to_zh：英文类名 → 中文名的映射与回落；
  3. detector.resolve_weights：权重路径解析（不解析就会去 GitHub 下载，
     而本机连不上，模型加载会一直重试、整个视觉管线停摆——实测踩过）。

用法：
    python scripts/check_vision.py
退出码 0 = 通过；1 = 有失败。
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

# 这两个模块顶层不 import cv2（detector 的 ultralytics 是延迟 import），所以能进 CI
from vision.detector import resolve_weights, to_zh  # noqa: E402
from vision.tracker import CentroidTracker  # noqa: E402

failures = []
skipped = []   # 因环境缺依赖而跳过的用例（如 CI 里没有 numpy）


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: 期望 {want!r}，实际 {got!r}")


# ---- 1) tracker：id 稳定 + type_en 保留 ----
tr = CentroidTracker(max_distance=80, max_misses=2)
d1 = [{"bbox": [100, 100, 200, 200], "type": "人", "type_en": "person", "confidence": 0.9}]
tr.update(d1)
id_first = d1[0]["id"]

# 下一帧物体轻微移动：应沿用同一个 id，而不是新开一个
d2 = [{"bbox": [105, 103, 205, 203], "type": "人", "type_en": "person", "confidence": 0.9}]
tr.update(d2)
check("tracker 同物体跨帧保持 id", d2[0]["id"], id_first)

# 不同类型不应抢同一个 id
d3 = [{"bbox": [105, 103, 205, 203], "type": "猫", "type_en": "cat", "confidence": 0.9}]
tr.update(d3)
if d3[0]["id"] == id_first:
    failures.append("tracker 不同类型的物体不该复用同一个 id")
check("tracker 记录 type_en", tr.tracks[id_first].get("type_en"), "person")

# 连续 max_misses+1 帧没出现 → 该 track 被清理
for _ in range(3):
    tr.update([])
if id_first in tr.tracks:
    failures.append("tracker 该清理久未出现的 track")

# ---- 2) to_zh：已知映射 + 未知回落 ----
check("to_zh person", to_zh("person"), "人")
check("to_zh car", to_zh("car"), "汽车")
check("to_zh 未知类回落原样", to_zh("unknown-thing"), "unknown-thing")

# ---- 3) resolve_weights：优先本地，找不到才交回原名 ----
check("resolve_weights 命中仓库内权重",
      resolve_weights("yolov8n.pt").replace("\\", "/").endswith("vision/weights/yolov8n.pt")
      or resolve_weights("yolov8n.pt") == "yolov8n.pt",  # CI 里权重不入库，允许回落原名
      True)
check("resolve_weights 找不到时回落原名", resolve_weights("no-such-model-xyz.pt"),
      "no-such-model-xyz.pt")

# VISION_MODEL_DIR 指向的目录应能被搜到
with tempfile.TemporaryDirectory() as td:
    fake = Path(td) / "custom.pt"
    fake.write_bytes(b"x")
    os.environ["VISION_MODEL_DIR"] = td
    check("resolve_weights 认 VISION_MODEL_DIR", resolve_weights("custom.pt"), str(fake))
    os.environ.pop("VISION_MODEL_DIR", None)

# 绝对路径且存在时直接返回
with tempfile.TemporaryDirectory() as td:
    fake = Path(td) / "abs.pt"
    fake.write_bytes(b"x")
    check("resolve_weights 认绝对路径", resolve_weights(str(fake)), str(fake))

# ---- 4) 结构光深度：方位换算 + 取深度中位数 ----
# astra.py 顶层只在 open() 里载入 DLL，import 本身是安全的（能进 CI）。
from vision.astra import AstraDepth  # noqa: E402

# 方位换算：hfov=58.59°、宽 640 → 中心 0°，最左最右 ±hfov/2，且线性
a = AstraDepth("")           # 不 open，只测纯函数
a.hfov_deg = 58.59
check("方位: 画面正中为 0°", a.bearing_deg(320, 640), 0.0)
left = a.bearing_deg(0, 640)
right = a.bearing_deg(640, 640)
if not (left < 0 < right):
    failures.append(f"方位符号不对：左应负、右应正，实际 left={left} right={right}")
if abs(abs(left) - abs(right)) > 0.01:
    failures.append("左右方位应对称")
check("方位: 左边缘约 -hfov/2", round(left, 2), round(-58.59 / 2, 2))
# 没有 hfov 信息时不能瞎猜，应老实返回 0
a2 = AstraDepth("")
check("方位: 无 hfov 时返回 0", a2.bearing_deg(0, 640), 0.0)

# 取深度：中位数 + 忽略 0（无回波）。
# 这一段要 numpy，而 CI 的静态检查是在 pip install **之前**跑的（那里没有 numpy），
# 所以有就测、没有就跳过并明说——不能因为缺 numpy 就让整个检查失败。
try:
    import numpy as np  # noqa: E402
except ImportError:
    np = None

if np is None:
    skipped.append("取深度用例（需要 numpy；CI 的静态检查环境不装 numpy）")
else:
    dmap = np.zeros((480, 640), dtype=np.int16)
    dmap[230:250, 310:330] = 1500          # 1.5m 的一片有效区域
    dmap[230:250, 310:330:6] = 0           # 掺几个无效点，验证会被忽略
    check("取深度: 1.5m 区域", AstraDepth.depth_at(dmap, 320, 240, window=10), 1.5)
    check("取深度: 全无效区域返回 None", AstraDepth.depth_at(dmap, 10, 10), None)
    check("取深度: None 输入返回 None", AstraDepth.depth_at(None, 10, 10), None)
    # 中位数而非均值：加一个野值，结果应仍是 1.5 而不是被拉偏
    dmap[240, 320] = 9000
    check("取深度: 抗野值(用中位数)", AstraDepth.depth_at(dmap, 320, 240, window=10), 1.5)

# ---- 报告 ----
if failures:
    print("视觉子系统自检失败 %d 项：" % len(failures))
    for f in failures:
        print("  [x] " + f)
    sys.exit(1)
print("OK：视觉子系统纯逻辑自检通过（追踪 id / 类名中文化 / 权重路径 / 结构光方位与深度取值）")
for s in skipped:
    print("  跳过: " + s)