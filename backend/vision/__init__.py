# -*- coding: utf-8 -*-
"""视觉子系统（可选）：双目/单目相机 → 目标检测 → 深度/方位 → 前端左下角浮层。

从 D:\\agentos\\VisionDev\\vision_app 移植并精简而来，改动点：

1. **可选依赖**：cv2 / ultralytics 缺失时整个子系统优雅关闭（VISION_ENABLED 置 False），
   myrobot 的对话链路完全不受影响——视觉只是锦上添花，不该成为启动前提。
2. **画面走 MJPEG 流**：VisionDev 原本只在 PyQt GUI 里显示标注画面，没有对外接口；
   这里把标注帧编码成 JPEG 由 /api/vision/stream 推给浏览器。
3. **单目降级**：只有一个普通 USB 相机时，出「识别 + 方位角度」，深度栏显示未知；
   插上双目相机（一个相机输出左右并排画面，或两个相机）后自动改走视差测距，无需改代码。

线程模型：Pipeline 是后台守护线程，用 Lock 保护 latest_frame / latest_event，
HTTP 处理器只读快照，彼此不阻塞。
"""
from .pipeline import VisionPipeline, vision_available

__all__ = ["VisionPipeline", "vision_available"]