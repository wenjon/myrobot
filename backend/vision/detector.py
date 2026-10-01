# -*- coding: utf-8 -*-
"""YOLO 目标检测封装，移植自 VisionDev 的 detector.py。

两个刻意的设计：
1. **中文标签**：YOLO 的类名是英文（person/car/...），前端浮层给中文用户看，
   这里直接映射成中文，映射表只覆盖 COCO 常见类，未知类回落到英文原名。
2. **CPU 默认**：本机是 AMD 显卡，torch 走 CPU。detect_every_n_frames 让检测
   每 N 帧跑一次（中间帧沿用 tracker 的位置），否则 CPU 上跟不上相机帧率。
"""
from __future__ import annotations

# COCO 常见类的中文名（只列会用到的；未列出的保留英文原名，不做臆造）
_ZH = {
    "person": "人", "bicycle": "自行车", "car": "汽车", "motorcycle": "摩托车",
    "airplane": "飞机", "bus": "公交车", "train": "火车", "truck": "卡车",
    "boat": "船", "traffic light": "红绿灯", "fire hydrant": "消防栓",
    "stop sign": "停止标志", "bench": "长椅", "bird": "鸟", "cat": "猫",
    "dog": "狗", "horse": "马", "sheep": "羊", "cow": "牛", "elephant": "大象",
    "bear": "熊", "backpack": "背包", "umbrella": "雨伞", "handbag": "手提包",
    "tie": "领带", "suitcase": "行李箱", "bottle": "瓶子", "wine glass": "酒杯",
    "cup": "杯子", "fork": "叉子", "knife": "刀", "spoon": "勺子", "bowl": "碗",
    "banana": "香蕉", "apple": "苹果", "sandwich": "三明治", "orange": "橙子",
    "broccoli": "西兰花", "carrot": "胡萝卜", "pizza": "披萨", "donut": "甜甜圈",
    "cake": "蛋糕", "chair": "椅子", "couch": "沙发", "potted plant": "盆栽",
    "bed": "床", "dining table": "餐桌", "toilet": "马桶", "tv": "电视",
    "laptop": "笔记本电脑", "mouse": "鼠标", "remote": "遥控器", "keyboard": "键盘",
    "cell phone": "手机", "microwave": "微波炉", "oven": "烤箱", "toaster": "烤面包机",
    "sink": "水槽", "refrigerator": "冰箱", "book": "书", "clock": "时钟",
    "vase": "花瓶", "scissors": "剪刀", "teddy bear": "玩具熊",
    "hair drier": "吹风机", "toothbrush": "牙刷",
}


def to_zh(name: str) -> str:
    return _ZH.get(name, name)


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
    from pathlib import Path

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


class Detector:
    def __init__(self, cfg):
        from ultralytics import YOLO  # 延迟 import：缺依赖时由上层捕获

        yolo_cfg = cfg.get("yolo", {})
        self.model = YOLO(resolve_weights(yolo_cfg.get("model", "yolov8n.pt")))
        self.conf = float(yolo_cfg.get("conf_threshold", 0.4))
        self.device = yolo_cfg.get("device", "cpu")

    def detect(self, frame):
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