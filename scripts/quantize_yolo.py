# -*- coding: utf-8 -*-
r"""把 YOLOv8n 的 float32 ONNX 量化成 VitisAI 能接管的 XINT8 模型。

为什么必须量化：AMD VitisAIExecutionProvider 只会给**带 Q/DQ 节点**的子图
分 DPU 资源。直接喂 float32 ONNX 时 provider 能注册成功、日志也不报错，
但实际一个算子都不接（日志里是 `NO DPU Subgraph Create!`），NPU 占用恒为 0。

本脚本的关键一步是**排除最终 Concat 的量化**：
YOLOv8 的输出是 (1,84,8400)，前 4 行是回归框（0~640 像素），后 80 行是
类别概率（0~1）。Quark 默认会给这个 Concat 输出一个统一 scale（实测 4~8），
概率 0~1 被四舍五入后全部变成 0，模型“能跑但永远检测不到东西”。
把该节点放进 nodes_to_exclude，输出头保持 float32，精度立刻恢复；
其余 99% 的计算仍在 NPU 上。

用法（必须用 .venv-ryzenai 的 Python，且先清掉 PATH 里的 Python312）：

    $env:PATH = ($env:PATH -split ';' | Where-Object { $_ -notlike '*Python312*' }) -join ';'
    $env:RYZEN_AI_INSTALLATION_PATH = 'C:\Program Files\RyzenAI\1.7.0'
    .\.venv-ryzenai\Scripts\python.exe scripts\quantize_yolo.py `
        --calib-dir .scratch\calib_src `
        --out backend\vision\weights\yolov8n_xint8.onnx

校准集建议 20~60 张真实场景图（含明暗/翻转/裁剪变体），覆盖实际相机的
视场和光照；用不相关图片会让量化区间偏掉，框会明显漂移。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from vision.yolo_npu import _letterbox  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="YOLOv8n -> Quark XINT8 (Ryzen AI)")
    parser.add_argument(
        "--model",
        default=str(ROOT / "backend" / "vision" / "weights" / "yolov8n.onnx"),
        help="输入 float32 ONNX",
    )
    parser.add_argument(
        "--out",
        default=str(ROOT / "backend" / "vision" / "weights" / "yolov8n_xint8.onnx"),
        help="输出量化 ONNX",
    )
    parser.add_argument(
        "--calib-dir",
        default=str(ROOT / ".scratch" / "calib_src"),
        help="校准图片目录（jpg/png，会再派生翻转/明暗/裁剪变体）",
    )
    parser.add_argument("--input-size", type=int, default=640)
    parser.add_argument(
        "--exclude-output-concat",
        default="/model.22/Concat_3",
        help="保持 float 的最终 Concat 节点名；留空则整网量化（通常不可用）",
    )
    return parser.parse_args()


def load_images(calib_dir: Path) -> list[np.ndarray]:
    images = []
    for pattern in ("*.jpg", "*.jpeg", "*.png", "*.bmp"):
        for path in sorted(calib_dir.glob(pattern)):
            image = cv2.imread(str(path))
            if image is not None:
                images.append(image)
    return images


def variants(image: np.ndarray):
    """同一张图派生几种分布，让校准覆盖更宽的取值区间。"""
    yield image
    yield cv2.flip(image, 1)
    yield cv2.convertScaleAbs(image, alpha=1.15, beta=20)   # 提亮
    yield cv2.convertScaleAbs(image, alpha=0.85, beta=-20)  # 压暗
    h, w = image.shape[:2]
    yield image[int(h * 0.08):int(h * 0.92), int(w * 0.08):int(w * 0.92)]


def build_inputs(images: list[np.ndarray], input_size: int) -> list[np.ndarray]:
    """构造与线上推理完全一致的输入：letterbox + RGB + NCHW + 0~1。"""
    inputs = []
    for image in images:
        for variant in variants(image):
            boxed, _, _, _ = _letterbox(variant, input_size)
            rgb = cv2.cvtColor(boxed, cv2.COLOR_BGR2RGB)
            inputs.append((rgb.astype(np.float32).transpose(2, 0, 1) / 255.0)[None])
    return inputs


class ImageCalibReader:
    """Quark 校准读取器：每次 get_next 返回一条样本，用完后 rewind。"""

    def __init__(self, input_name: str, inputs: list[np.ndarray]):
        self.input_name = input_name
        self.inputs = inputs
        self.index = 0

    def get_next(self):
        if self.index >= len(self.inputs):
            return None
        item = {self.input_name: self.inputs[self.index]}
        self.index += 1
        return item

    def rewind(self) -> None:
        self.index = 0


def sanity_check(model_path: Path, sample: np.ndarray) -> bool:
    """量化后用 CPU EP 跑一张图，确认类别分数没有被压成 0。

    这一步专门防“模型能跑、但永远检测不到东西”的静默失败。
    """
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.log_severity_level = 3
    session = ort.InferenceSession(
        str(model_path), sess_options=options, providers=["CPUExecutionProvider"]
    )
    out = session.run(None, {"images": sample})[0][0].T
    scores = out[:, 4:]
    best = float(scores.max(axis=1).max())
    print(f"[check] score_max={best:.4f} box_max={float(out[:, :4].max()):.1f}")
    if best <= 0.01:
        print("[check] 失败：类别分数接近 0，量化配置把输出头压坏了。", file=sys.stderr)
        return False
    return True


def main() -> int:
    args = parse_args()
    from quark.onnx import ModelQuantizer, QConfig

    model_path = Path(args.model)
    out_path = Path(args.out)
    if not model_path.is_file():
        print(f"找不到输入模型：{model_path}", file=sys.stderr)
        return 1

    images = load_images(Path(args.calib_dir))
    if not images:
        print(f"校准集为空：{args.calib_dir}（至少放 1 张真实场景图）", file=sys.stderr)
        return 1
    inputs = build_inputs(images, args.input_size)
    print(f"[quantize] {len(images)} 张图 -> {len(inputs)} 条校准样本")

    # XINT8 是 Ryzen AI 的官方推荐档：UINT8 激活 + INT8 权重 + MinMSE 校准。
    # 注意 MinMax 在 enable_npu_cnn 下不被支持，改了会直接校准失败。
    config = QConfig.get_default_config("XINT8")
    if args.exclude_output_concat:
        config.global_quant_config.nodes_to_exclude = [args.exclude_output_concat]
        print(f"[quantize] 保持 float 的节点：{args.exclude_output_concat}")

    reader = ImageCalibReader("images", inputs)
    ModelQuantizer(config).quantize_model(str(model_path), str(out_path), reader)
    print(f"[quantize] 输出：{out_path} ({out_path.stat().st_size} bytes)")

    return 0 if sanity_check(out_path, inputs[0]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
