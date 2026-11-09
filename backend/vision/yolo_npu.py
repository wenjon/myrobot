# -*- coding: utf-8 -*-
"""YOLOv8 的 ONNX Runtime 后端（优先 AMD NPU / VitisAI）。

为什么单独做一层，而不是给 ultralytics 换个 device：
- Ultralytics 的 `device=` 走 PyTorch；AMD NPU 在 Windows 上要吃
  ONNX Runtime + VitisAIExecutionProvider，二者不是同一条路。
- VitisAI 只接管**量化过（含 Q/DQ）的子图**。直接喂 float32 ONNX，
  EP 能注册成功但一个算子都不会接（日志会打 `NO DPU Subgraph Create!`），
  单帧延迟仍是 CPU 水平。本机实测：float 约 45ms/帧、NPU 引擎占用 0%；
  XINT8 量化后约 20ms/帧（含前后处理），NPU `engtype_compute` 占用 95%+。
- 首次加载要编译 DPU 子图，本机约 20 秒；编译产物不落盘，冷启动成本要计入。
  把它和 CPU 路径分开，失败时可以无痛回退。

这一层只负责「图像进 → 检测框出」，接口与 ultralytics 后端保持一致：
    detect(frame) -> [{bbox, type, type_en, confidence}]
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path

import numpy as np

from .labels import COCO_NAMES, to_zh

logger = logging.getLogger(__name__)


def _letterbox(frame, size: int):
    """等比缩放并补边到 size×size，返回 (图, 缩放比, 左补边, 上补边)。

    YOLO 训练时就是 letterbox，推理也必须一致；直接拉伸会把宽高比压扁，
    框的位置和置信度都会偏。
    """
    import cv2

    h, w = frame.shape[:2]
    scale = min(size / float(w), size / float(h))
    new_w, new_h = int(round(w * scale)), int(round(h * scale))
    resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

    pad_x = (size - new_w) // 2
    pad_y = (size - new_h) // 2
    canvas = np.full((size, size, 3), 114, dtype=np.uint8)  # YOLO 的灰边
    canvas[pad_y:pad_y + new_h, pad_x:pad_x + new_w] = resized
    return canvas, scale, pad_x, pad_y


def _nms(boxes: np.ndarray, scores: np.ndarray, iou_thr: float) -> list[int]:
    """单类别 NMS。输入 boxes 是 xyxy，返回保留下标（按分数降序）。"""
    if boxes.size == 0:
        return []
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size:
        i = int(order[0])
        keep.append(i)
        rest = order[1:]
        xx1 = np.maximum(x1[i], x1[rest])
        yy1 = np.maximum(y1[i], y1[rest])
        xx2 = np.minimum(x2[i], x2[rest])
        yy2 = np.minimum(y2[i], y2[rest])
        inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        union = areas[i] + areas[rest] - inter
        iou = np.divide(inter, union, out=np.zeros_like(inter), where=union > 0)
        order = rest[iou <= iou_thr]
    return keep


class OnnxYoloDetector:
    """加载 ONNX 模型并用指定 ExecutionProvider 推理。"""

    def __init__(self, cfg: dict):
        import onnxruntime as ort

        yolo_cfg = cfg.get("yolo", {}) or {}
        self.input_size = int(yolo_cfg.get("input_size", 640))
        self.conf = float(yolo_cfg.get("conf_threshold", 0.4))
        self.nms_iou = float(yolo_cfg.get("nms_iou", 0.45))
        self.max_det = int(yolo_cfg.get("max_det", 300))
        self.model_path = self._resolve_model(yolo_cfg.get("onnx_model", "auto"))
        self.provider = yolo_cfg.get("provider", "VitisAIExecutionProvider")
        self.warmup_ms = None
        # 下面三个字段专门回答一个问题：“到底有没有算子在 NPU 上跑？”
        # 只看到 provider 名字不算数，必须看 profile 里的真实分区事件。
        self.model_quantized = self._has_qdq(self.model_path)
        self.npu_subgraphs = None
        self.npu_active = None

        # Ryzen AI 的 EP 需要知道运行时安装位置；用户显式设了就不覆盖。
        ryzen_dir = (yolo_cfg.get("ryzen_ai_path") or "").strip()
        if ryzen_dir and not os.getenv("RYZEN_AI_INSTALLATION_PATH"):
            os.environ["RYZEN_AI_INSTALLATION_PATH"] = ryzen_dir

        available = ort.get_available_providers()
        if self.provider not in available:
            raise RuntimeError(
                f"ONNX Runtime 没有 {self.provider}；可用 providers={available}。"
                "请用带 Ryzen AI 运行时的 Python 环境（如 .venv-ryzenai）启动服务。"
            )

        # 只请求首选 provider；ORT 会自动追加 CPU 作为不支持算子的兜底。
        # 这并不意味着所有算子都在 NPU，日志/describe() 会给出实际 providers。
        provider_options: list[dict] = [{}]
        if self.provider == "VitisAIExecutionProvider":
            cache_dir = str(Path((yolo_cfg.get("cache_dir") or ".cache/ryzenai")).resolve())
            Path(cache_dir).mkdir(parents=True, exist_ok=True)
            provider_options = [{
                "cache_dir": cache_dir,
                # 按模型文件名派生，避免 float 与量化模型复用同一份编译产物
                "cache_key": str(
                    yolo_cfg.get("cache_key")
                    or f"{Path(self.model_path).stem}_{self.input_size}"
                ),
                "log_level": str(yolo_cfg.get("log_level") or "warning"),
            }]

        severity = {"verbose": 0, "info": 1, "warning": 2, "error": 3, "fatal": 4}
        so = ort.SessionOptions()
        so.log_severity_level = severity.get(str(yolo_cfg.get("log_level", "warning")).lower(), 2)
        # NPU 只接管一部分算子，其余落回 CPU。若不限制线程，ORT 会用满所有核，
        # 还可能让线程忙等（spin），把 LLM/TTS 的 CPU 抢走。默认 2 线程 + 关闭自旋，
        # 对 640×640 的小模型足够，而且更符合“视觉只占一小块算力”的定位。
        so.intra_op_num_threads = max(1, int(yolo_cfg.get("intra_threads", 2)))
        so.inter_op_num_threads = max(1, int(yolo_cfg.get("inter_threads", 1)))
        if not bool(yolo_cfg.get("allow_spinning", False)):
            so.add_session_config_entry("session.intra_op.allow_spinning", "0")
            so.add_session_config_entry("session.inter_op.allow_spinning", "0")

        # 只在 warmup 那一帧开 profile：拿到 VitisAI 子图事件数后立刻关闭并删文件。
        # 长期开 profiling 会持续写盘，也会给每帧加采样开销。
        probe_npu = (
            self.provider == "VitisAIExecutionProvider"
            and bool(yolo_cfg.get("probe_npu", True))
            and self.model_quantized
        )
        self._profile_dir = None
        if probe_npu:
            self._profile_dir = tempfile.mkdtemp(prefix="ort-prof-")
            so.enable_profiling = True
            so.profile_file_prefix = str(Path(self._profile_dir) / "probe")

        t0 = time.perf_counter()
        self.session = ort.InferenceSession(
            self.model_path,
            sess_options=so,
            providers=[self.provider],
            provider_options=provider_options,
        )
        self.load_ms = round((time.perf_counter() - t0) * 1000, 1)
        self.providers = self.session.get_providers()

        self.input_name = self.session.get_inputs()[0].name
        # 导出时固定 1×3×640×640；这里记录一下，方便启动时核对。
        self.input_shape = self.session.get_inputs()[0].shape

        if bool(yolo_cfg.get("warmup", True)) or probe_npu:
            dummy = np.zeros((1, 3, self.input_size, self.input_size), dtype=np.float32)
            t0 = time.perf_counter()
            self.session.run(None, {self.input_name: dummy})
            if bool(yolo_cfg.get("warmup", True)):
                self.warmup_ms = round((time.perf_counter() - t0) * 1000, 1)

        if probe_npu:
            self._collect_npu_evidence()

        logger.info(
            "[YOLO-NPU] 模型=%s provider=%s providers=%s 量化=%s npu_active=%s "
            "子图=%s 编译=%.1fms warmup=%sms",
            Path(self.model_path).name, self.provider, self.providers,
            self.model_quantized, self.npu_active, self.npu_subgraphs,
            self.load_ms, self.warmup_ms,
        )

    @staticmethod
    def _has_qdq(model_path: str) -> bool:
        """静态检查模型是否含 QuantizeLinear（即 XINT8/INT8 量化模型）。"""
        try:
            import onnx
            return any(
                node.op_type == "QuantizeLinear"
                for node in onnx.load(model_path).graph.node
            )
        except Exception:  # onnx 缺失或模型损坏时不要阻断启动
            return False

    def _collect_npu_evidence(self) -> None:
        """解析 warmup 的 ORT profile，统计真正的 VitisAI 子图执行事件。

        ORT 的 Python API 不提供「算子 → EP」映射；profile 里
        `VitisAIExecutionProvider_*` 事件是唯一可靠的证据。
        没有这类事件就说明：EP 只是注册了，算子全在 CPU。
        """
        path = None
        try:
            path = self.session.end_profiling()
        except Exception:  # 没开 profiling 或已结束时保持未知
            path = None
        try:
            if path and Path(path).is_file():
                events = json.loads(Path(path).read_text(encoding="utf-8"))
                self.npu_subgraphs = sum(
                    1 for ev in events
                    if ev.get("cat") == "Node"
                    and str(ev.get("name", "")).startswith("VitisAIExecutionProvider_")
                )
                self.npu_active = self.npu_subgraphs > 0
        except Exception as exc:  # noqa: BLE001
            logger.warning("[YOLO-NPU] profile 解析失败：%s", exc)
        finally:
            if self._profile_dir:
                shutil.rmtree(self._profile_dir, ignore_errors=True)
                self._profile_dir = None

    @staticmethod
    def _resolve_model(name: str) -> str:
        """把配置里的 ONNX 文件名解析为本地路径（与 .pt 的搜索顺序一致）。

        name="auto" 时优先量化模型：它是 NPU 真正能接管的那一份；
        缺失才退回 float32（此时仍能检测，但只跑 CPU）。
        """
        if str(name).strip().lower() == "auto":
            for candidate in ("yolov8n_xint8.onnx", "yolov8n.onnx"):
                try:
                    return OnnxYoloDetector._resolve_model(candidate)
                except FileNotFoundError:
                    continue
            raise FileNotFoundError(
                "找不到可用 ONNX 模型：请把 yolov8n_xint8.onnx（量化，NPU 用）或 "
                "yolov8n.onnx（float，CPU 用）放到 backend/vision/weights/，"
                "或用 VISION_YOLO_ONNX_MODEL 指向绝对路径。"
            )
        p = Path(name)
        if p.is_absolute() and p.is_file():
            return str(p)
        candidates = [
            Path(__file__).resolve().parent / "weights" / name,
            Path(__file__).resolve().parent / "weights" / Path(name).name,
        ]
        env_dir = os.getenv("VISION_MODEL_DIR", "").strip()
        if env_dir:
            candidates.append(Path(env_dir) / Path(name).name)
        for c in candidates:
            if c.is_file():
                return str(c)
        raise FileNotFoundError(
            f"找不到 ONNX 模型 {name}；请把 yolov8n.onnx 放到 backend/vision/weights/，"
            "或用 VISION_YOLO_ONNX_MODEL 指向绝对路径。"
        )

    def describe(self) -> dict:
        return {
            "backend": "onnx",
            "provider": self.provider,
            "providers": self.providers,
            # provider 只是“注册成功”；npu_active 才是“算子真的跑在 NPU 上”。
            "provider_registered": self.provider in self.providers,
            "npu_active": bool(self.npu_active),
            "npu_subgraphs": self.npu_subgraphs,
            "model_quantized": self.model_quantized,
            "model": Path(self.model_path).name,
            "input_size": self.input_size,
            "load_ms": self.load_ms,
            "warmup_ms": self.warmup_ms,
        }

    def detect(self, frame) -> list[dict]:
        import cv2

        h, w = frame.shape[:2]
        canvas, scale, pad_x, pad_y = _letterbox(frame, self.input_size)
        # BGR → RGB、HWC → CHW、uint8 → float32[0,1]
        blob = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)
        blob = blob.transpose(2, 0, 1)[None].astype(np.float32) / 255.0

        out = self.session.run(None, {self.input_name: blob})[0]
        return self._decode(out, w, h, scale, pad_x, pad_y)

    def _decode(self, out: np.ndarray, frame_w: int, frame_h: int,
                scale: float, pad_x: int, pad_y: int) -> list[dict]:
        """把 Ultralytics 导出格式 (1, 4+80, N) 解成检测框。

        输出前 4 行是 cx/cy/w/h（输入图上的绝对像素），后面 80 行是类别分数。
        这里不做任何“模型内部已 NMS”的假设，统一自己做阈值 + 分类别 NMS。
        """
        pred = np.asarray(out)
        if pred.ndim == 3:
            pred = pred[0]
        # 兼容 (84, N) 和 (N, 84) 两种导出姿态。
        if pred.shape[0] == 4 + len(COCO_NAMES):
            pred = pred.T
        if pred.shape[1] != 4 + len(COCO_NAMES):
            raise RuntimeError(f"Unexpected YOLO output shape: {pred.shape}")

        boxes_cxcywh = pred[:, :4]
        class_scores = pred[:, 4:]
        class_ids = class_scores.argmax(axis=1)
        scores = class_scores[np.arange(class_scores.shape[0]), class_ids]

        keep_mask = scores >= self.conf
        if not np.any(keep_mask):
            return []

        boxes_cxcywh = boxes_cxcywh[keep_mask]
        scores = scores[keep_mask]
        class_ids = class_ids[keep_mask]

        cx, cy, bw, bh = (boxes_cxcywh[:, i] for i in range(4))
        boxes = np.stack([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], axis=1)

        # 分类别 NMS：不同类别即使在画面上重叠也不该互相压制。
        selected: list[int] = []
        for cls in np.unique(class_ids):
            idx = np.where(class_ids == cls)[0]
            selected.extend(idx[_nms(boxes[idx], scores[idx], self.nms_iou)])
        selected.sort(key=lambda i: float(scores[i]), reverse=True)
        selected = selected[:self.max_det]

        detections: list[dict] = []
        for i in selected:
            x1, y1, x2, y2 = boxes[i]
            # 从 letterbox 输入坐标还原到原始画面坐标，并裁掉补边产生的越界。
            x1 = (x1 - pad_x) / scale
            y1 = (y1 - pad_y) / scale
            x2 = (x2 - pad_x) / scale
            y2 = (y2 - pad_y) / scale
            x1, x2 = max(0, min(frame_w - 1, x1)), max(0, min(frame_w - 1, x2))
            y1, y2 = max(0, min(frame_h - 1, y1)), max(0, min(frame_h - 1, y2))
            if x2 <= x1 or y2 <= y1:
                continue
            en = COCO_NAMES[int(class_ids[i])]
            detections.append({
                "bbox": [int(x1), int(y1), int(x2), int(y2)],
                "type": to_zh(en),
                "type_en": en,
                "confidence": round(float(scores[i]), 3),
            })
        return detections
