# -*- coding: utf-8 -*-
"""YOLO 目标检测门面：优先 AMD NPU，失败时回退 PyTorch/CPU。

两条后端共用同一个对外接口：
    detect(frame) -> [{bbox,type,type_en,confidence}]

这样 vision/pipeline.py 不需要知道下面跑的是 ONNX+VitisAI 还是
Ultralytics+PyTorch。后端选择、回退原因都通过 describe() 暴露到
/api/vision/status，避免“看起来在跑，其实静默用了 CPU”。
"""
from __future__ import annotations

import logging
from pathlib import Path

from .labels import to_zh

logger = logging.getLogger(__name__)


def resolve_weights(model_name: str) -> str:
    """把模型名解析成一个**本地已存在**的权重路径。

    为什么需要：`YOLO("yolov8n.pt")` 若本地没有会去 GitHub 下载，而本机访问
    GitHub 超时，结果是模型加载卡住重试、整个视觉管线停摆（实测踩过）。
    这里按优先级找一个现成的权重文件，找不到才把原名交回 ultralytics
    （让它去下——至少那时的失败是显式的、能报出来的）。

    优先级：
      1) 配置给的就是存在的路径
      2) 仓库内 backend/vision/weights/<name>
      3) 环境变量 VISION_MODEL_DIR 指向的目录（如上一份 VisionDev 的下载）
    """
    import os

    if os.path.isabs(model_name) and Path(model_name).is_file():
        return model_name

    candidates = [Path(__file__).resolve().parent / "weights" / model_name]
    env_dir = os.getenv("VISION_MODEL_DIR", "").strip()
    if env_dir:
        candidates.append(Path(env_dir) / model_name)
    for p in candidates:
        if p.is_file():
            return str(p)
    return model_name


class UltralyticsDetector:
    """原有 PyTorch 路径：CPU（或 torch 支持的设备），作为最终兜底。"""

    def __init__(self, cfg: dict):
        from ultralytics import YOLO  # 延迟 import：缺依赖时由上层捕获

        yolo_cfg = cfg.get("yolo", {}) or {}
        self.model_name = resolve_weights(yolo_cfg.get("model", "yolov8n.pt"))
        self.model = YOLO(self.model_name)
        self.conf = float(yolo_cfg.get("conf_threshold", 0.4))
        self.device = yolo_cfg.get("device", "cpu")

    def describe(self) -> dict:
        return {
            "backend": "ultralytics",
            "provider": None,
            "model": Path(self.model_name).name,
            "device": self.device,
        }

    def detect(self, frame) -> list[dict]:
        """返回 [{bbox:[x1,y1,x2,y2], type:中文名, type_en, confidence}]。"""
        results = self.model.predict(frame, conf=self.conf,
                                     device=self.device, verbose=False)[0]
        detections = []
        names = results.names
        for box in results.boxes:
            cls_id = int(box.cls[0])
            en = names[cls_id]
            detections.append({
                "bbox": [int(v) for v in box.xyxy[0].tolist()],
                "type": to_zh(en),
                "type_en": en,
                "confidence": round(float(box.conf[0]), 3),
            })
        return detections


class Detector:
    """后端门面：按配置选择 ONNX/NPU 或 Ultralytics，并记录回退原因。

    配置 VISION_YOLO_BACKEND：
      auto       → 有 ONNX 且 provider 可用就试 ONNX，失败回退 ultralytics
      npuvitis   → 强制 VitisAI；失败仍回退，但 describe() 会写明原因
      dml        → 强制 DirectML（AMD GPU）
      onnx       → 按 provider 配置走 ONNX
      ultralytics→ 始终走原 PyTorch 路径
    """

    def __init__(self, cfg: dict):
        yolo_cfg = cfg.get("yolo", {}) or {}
        self.requested = str(yolo_cfg.get("backend", "auto")).lower()
        self.fallback_reason = ""
        self.impl = None

        if self.requested != "ultralytics":
            backend_cfg = dict(yolo_cfg)
            if self.requested == "dml":
                backend_cfg["provider"] = "DmlExecutionProvider"
            elif self.requested in ("npuvitis", "vitisai"):
                backend_cfg["provider"] = "VitisAIExecutionProvider"
            try:
                from .yolo_npu import OnnxYoloDetector
                self.impl = OnnxYoloDetector({"yolo": backend_cfg})
                logger.info("[YOLO] 使用 ONNX 后端：%s", self.impl.describe())
            except Exception as e:  # noqa: BLE001
                self.fallback_reason = f"{type(e).__name__}: {e}"
                logger.warning("[YOLO] ONNX 后端不可用，回退 ultralytics：%s", self.fallback_reason)

        if self.impl is None:
            self.impl = UltralyticsDetector(cfg)
            logger.info("[YOLO] 使用 PyTorch 后端：%s", self.impl.describe())

    def describe(self) -> dict:
        info = dict(self.impl.describe())
        info.update({
            "requested": self.requested,
            "fallback_reason": self.fallback_reason or None,
        })
        return info

    def detect(self, frame) -> list[dict]:
        return self.impl.detect(frame)
