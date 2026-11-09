# 视觉子系统设计（相机 → 识别 → 深度）

> 状态：**已实现并实测通过**（2026-10-02；2026-11-09 增加 AMD NPU 后端，XINT8 量化后真正卸载到 NPU）。
> 代码：`backend/vision/`（889 行）+ `frontend/src/vision.js`。
> 配套：设计规格第 2 章（选型）、第 13.1c 节（配置）、第 15 章（CI）。
>
> 目标：**照本文可以复现整套逻辑**——包含选型理由、每个模块的职责与关键算法、
> 所有踩过的坑及其根因。看代码前先看这。

---

## 0. 一句话概览

把一台相机的画面做成「左下角浮层」：实时画面 + YOLO 物体识别 + **真实距离与方位**。
深度由结构光相机提供（不是视差）。整块是**尽力而为**的：任何一环坏掉都自动降级，
绝不牵连对话链路。

推理侧优先走 **AMD Ryzen AI NPU**（ONNX Runtime + VitisAIExecutionProvider）。
注意：**float32 ONNX 不会真的上 NPU**，必须使用含 Q/DQ 的 XINT8 量化模型，
否则 provider 只是注册成功、算子全在 CPU。失败时自动回退 PyTorch/CPU；
实际选中的后端会写进 `/api/vision/status.detector`，前端浮层标题也会显示
`NPU / GPU / CPU`，不做“静默回退”。

```
                     ┌──────────── backend/vision/ ────────────┐
  Astra Pro          │                                          │
  ├ PID_0403 ─结构光─▶ astra.py    ─┐                          │
  │  (厂商 C API)     │             ├─▶ pipeline.py ─┬─▶ JPEG ─▶ /api/vision/stream (MJPEG)
  └ PID_0501 ─RGB────▶ camera.py ──┘   (后台线程)     │
     (UVC/OpenCV)     │      │        │               └─▶ JSON ─▶ /api/vision/objects
                      │      └▶ detector.py (门面)     │
                      │         ├ yolo_npu.py (ONNX/NPU)│
                      │         └ ultralytics (CPU 兜底)│
                      │         tracker.py  (稳定 id)  │
                      │         stereo.py   (视差备选) │
                      └──────────────────────────────────┘
                                                       frontend/src/vision.js
                                                       └▶ 左下角浮层
```

---

## 1. 硬件事实（一切设计的前提）

**Orbbec Astra Pro**，实测枚举出**两个** USB 设备，它**不是立体双目相机**：

| USB ID | 名称 | Class | 提供什么 | 谁能访问 |
|---|---|---|---|---|
| `VID_2BC5&PID_0403&MI_00` | ORBBEC Depth Sensor | Orbbec | **结构光深度** | 仅厂商私有接口 |
| `VID_2BC5&PID_0501&MI_00` | Astra Pro HD Camera | Camera | RGB 彩色 | UVC → OpenCV |

**由此推出三条硬约束**：

1. **OpenCV 永远拿不到这台相机的深度**——它只暴露 RGB 那一半。
   `cv2.VideoCapture` 无论请求什么分辨率，实测只给 `640x480` 或 `1280x720`
   （请求 `2560x720` 也只回 `1280x720`），因为深度不在 UVC 通道上。
2. **深度必须走厂商 SDK**。已实测排除的路线：
   - OpenNI2（含 SDK 自带的 `orbbec.dll` 驱动）→ `oniGetDeviceList` **返回 0 个设备**
   - `primesense`（OpenNI2 的 Python 封装）→ Python 3.13 上 `import` 直接失败
     （`CEnum` 元类报 `TypeError: abstract class`）
   - 厂商自带的 `DepthReaderPoll.exe` → **能正常出深度帧**，证明硬件/驱动没问题
   → 所以最终选 **Astra SDK 的 C API**。
3. 换一台真正的双目相机时，走的是另一条路（视差），代码已保留，会自动切过去。

### 1.1 实测硬件能力（可复现）

```
深度:   640 x 480, uint16, 毫米, 0=无效
        有效像素占比 0.6~0.8（随场景浮动，见 §8 说明）
        量程实测 443mm ~ 4578mm
        hfov = 58.59°  (SDK 官方值，比用焦距反推更准)
深度帧率: ~30fps（我们只需 10fps 级，够用）
彩色:   640x480 / 1280x720（UVC）
```

---

## 2. 深度来源：三级优先级（自动选，不手工切）

`pipeline.py` 启动时按顺序尝试，命中即停：

| 优先级 | 来源 | 判据 | `distance_m` |
|---|---|---|---|
| 1 | **结构光** `astra` | `VISION_ASTRA_SDK_DIR` 配了且 SDK 打开成功 | 真实米数 |
| 2 | **双目视差** `stereo` | 相机模式是 `single`（左右并排帧）或 `dual`（两个相机） | `focal*baseline/disparity` |
| 3 | 无 `none` | 以上都不行（普通单目） | `null`，前端显示 `—` |

**为什么不做成配置项手工切**：可用性是硬件事实，让代码自己去探测比让人记住去改配置可靠。
实测本机走到的是 1（结构光）。

方位角**三级都有**（它只依赖像素位置与视场角）：

```python
# 有 hfov（结构光路径）→ 用官方视场角线性换算，最准
angle = (cx - w/2) * (hfov_deg / w)
# 否则退回焦距换算（视差路径）
angle = degrees(atan((cx - w/2) / focal))
```

---

## 3. 模块逐个说

### 3.1 `astra.py` — 结构光深度（ctypes 调厂商 C API，243 行）

**符号分散在两个 DLL**（实测出来的，不是照文档写的）：

| DLL | 提供 |
|---|---|
| `astra_core.dll` | `astra_initialize` / `astra_terminate` / `astra_update` / `astra_streamset_open` / `astra_reader_create` / `astra_reader_open_frame` / `astra_stream_start` |
| `astra.dll` | `astra_reader_get_depthstream` / `astra_frame_get_depthframe` / `astra_depthframe_get_data_ptr` / `astra_depthstream_get_hfov` |

**两个未导出符号的坑**：
`astra_depthframe_get_width` / `get_height` **根本没导出**。
帧尺寸只能由数据字节数反推（16bit 单通道 → `像素数 = byteLength / 2`）：

```python
_KNOWN_SHAPES = {640*480: (480,640), 320*240: (240,320), 1280*960: (960,1280)}
# 未知分辨率时按 4:3 反推，至少不崩
```

调用顺序（照抄厂商 `samples/c-api/DepthReaderPoll/main.c`）：

```
astra_initialize
astra_streamset_open("device/default")   ← 连接串就是这五个字
astra_reader_create
astra_reader_get_depthstream
astra_depthstream_get_hfov              ← 拿方位换算要用的视场角
astra_stream_start
── 循环 ──
  astra_update
  astra_reader_open_frame(reader, 0, &frame)    ← 0 = 非阻塞
  astra_frame_get_depthframe(frame, &depthFrame)
  astra_depthframe_get_data_ptr(...)            ← 得到 int16* + byteLength
  ... 读数据 ...
  astra_reader_close_frame(&frame)
── 收尾 ──
astra_stream_stop → astra_reader_destroy → astra_streamset_close → astra_terminate
```

**三个必须注意的实现细节**：

1. **数据必须立刻 copy**。`get_data_ptr` 给的是 SDK 内部缓冲，
   `close_frame` 之后即失效。`astra.py:read()` 里 `.copy()` 是必需的，不是保险。
2. **无阻塞读会返回 `None`**。`open_frame(reader, 0, ...)` 超时 0ms，
   两次深度帧之间去读必然失败。**照实透传会让距离一帧有一帧无地闪**（实测踩过，
   见 §5.2）。处理办法在 pipeline 层（见 §3.6）。
3. **取深度用邻域中位数，不是中心点，也不是均值**。

```python
def depth_at(depth, x, y, window=10):
    roi = depth[y1:y2, x1:x2]
    valid = roi[roi > 0]            # 0 = 无回波，必须剔除
    if valid.size == 0: return None # 老实返回未知，不假装有距离
    return round(float(np.median(valid)) / 1000.0, 2)
```

- 不用**中心单点**：实测框中心常常正好落在无效像素上（原始值 0mm），
  单点取值会大面积返回 None。
- 不用**均值**：结构光在物体边缘与反光面会有少量离谱野值，均值会被带偏。
  自检里有一条专门的用例锁住这个性质（往有效区塞一个 9000mm 野值，
  结果必须仍是 1.5m）。

### 3.2 `camera.py` — RGB 采集（105 行）

三种模式，`auto` 会依次尝试：

| 模式 | 场景 | 产出 |
|---|---|---|
| `single` | 一个相机输出左右并排帧（宽 ≥ 2×高） | left, right |
| `dual` | 两个独立相机 | left, right |
| `mono` | **普通单目（本机就是这种）** | left, **right=None** |

**相对 VisionDev 的改动**：原版在 `single` 失败后会直接报错，
本机实测只有单目，所以新增 `mono` 兜底——否则整个视觉子系统起不来。
判据沿用原版：`frame.shape[1] >= frame.shape[0] * 2` 才算并排帧。

### 3.3 `detector.py` — YOLO 后端门面

`detector.py` 现在不再直接绑死 Ultralytics，而是负责**选后端 + 统一接口**：

```
Detector.detect(frame) ─┬─ OnnxYoloDetector（yolo_npu.py，NPU/GPU）
                        └─ UltralyticsDetector（PyTorch/CPU 兜底）
```

- **对外接口不变**：`detect(frame) -> [{bbox, type, type_en, confidence}]`。
  `pipeline.py` 不需要知道下面跑的是哪条推理路径。
- **`describe()`**：返回实际后端、provider、模型、编译/warmup 耗时、回退原因；
  由 `/api/vision/status.detector` 暴露。
- **类名中文化**：模型给 `person`，界面要给「人」。映射表抽到
  `labels.py`，与纯 ONNX 后端共用；未列出的**回落英文原名**（不臆造）。
- **权重路径解析 `resolve_weights()`** —— 这个函数存在的唯一理由是踩过坑：

  ```python
  优先级: 配置给的绝对路径 → backend/vision/weights/<name> → VISION_MODEL_DIR → 交给 ultralytics
  ```

  **为什么必须有**：`YOLO("yolov8n.pt")` 在本地没有权重时会去 GitHub 下载，
  而本机**连不上 GitHub**（connect timeout + 重试），结果是模型加载卡死、
  整个视觉管线永久停在 `init`、一帧都不出。所以必须先找一个本地已有的权重。

### 3.4 `yolo_npu.py` — ONNX Runtime + AMD NPU（量化后真正生效）

**为什么不能直接给 Ultralytics 换 `device`**：Ultralytics 的 `device=` 走 PyTorch；
AMD NPU 在 Windows 上要吃 ONNX Runtime + `VitisAIExecutionProvider`，是两条不同的路。

**处理流程**：

```
BGR frame
  → letterbox 到 640×640（灰边 114，保持宽高比）
  → BGR→RGB、HWC→CHW、uint8→float32 /255
  → ONNX Session（VitisAIExecutionProvider）
  → 输出 (1, 84, 8400)：前 4 行 cx/cy/w/h，后 80 行类别分数
  → 置信度阈值 → 分类别 NMS → 反 letterbox → 原始画面坐标
```

关键点：

- ONNX 不含 Ultralytics 的 `names` 元数据，类别名按 `labels.py::COCO_NAMES`
  的固定下标还原；顺序错了会把 bus 认成 person，而且不会报错。
- **VitisAI 只接管含 Q/DQ 的子图**。直接喂 float32 ONNX 时 EP 能注册成功，
  但日志会打印 `NO DPU Subgraph Create!`，一个算子都不接；此时
  `get_providers()` 仍包含 VitisAI，很容易误判成“NPU 已生效”。
- 必须用 Quark 把模型量化成 XINT8（UINT8 激活 + INT8 权重）。本机 profile 显示：
  float32 时 VitisAI 执行事件 **0 个**；XINT8 后出现 **3~4 个** DPU 子图。
- **输出头不能跟着量化**：`/model.22/Concat_3` 把回归框（0~640）和类别概率（0~1）
  合并后共用一个 scale（实测 4~8），概率会被四舍五入成 0，模型“能跑但永远检测不到”。
  量化时把该节点放进 `nodes_to_exclude`，输出保持 float32，其余计算仍在 NPU。
- NPU 不是“整网加速”：无法编译的检测头算子仍会回退 CPU。
- CPU 兜底线程必须收窄：默认 `intra_threads=2`、`inter_threads=1`、
  `allow_spinning=0`。不限制时 ORT 会吃满核心并忙等，直接抢走 LLM/TTS 的算力。
- 首次加载要编译 DPU 子图，本机实测 **约 20 秒**；VitisAI 编译缓存目前不落盘，
  每次冷启动都要重新编译。`warmup_ms` 只代表 warmup 那一帧，`load_ms` 才是总耗时。
- **判断 NPU 是否真生效必须看 profile**：加载时只在 warmup 那一帧临时开 ORT
  profiling，统计 `VitisAIExecutionProvider_*` 执行事件，然后立即关闭并删除临时文件。
  `describe()` 中的 `npu_active` / `npu_subgraphs` 就是这次探测的结果。
**本机实测**（`bus.jpg`，640 letterbox）：

| 后端 | 模型 | 初始化 | 端到端/帧 | NPU 占用 | 结果 |
|---|---|---:|---:|---:|---|
| Ultralytics / CPU | `yolov8n.pt` | ~2.4s | 28.3ms | 0% | 3 人 + 1 公交 |
| ONNX / CPU（VitisAI 未接管） | `yolov8n.onnx` float32 | ~2s | **45.6ms** | **0%** | 3 人 + 1 公交 |
| ONNX / NPU | `yolov8n_xint8.onnx` | **约 20s**（首次编译） | **20.7ms** | **95.6%** | person 0.914 / bus 0.664，框位置一致 |

纯推理连续运行实测 **68.5~71.5 FPS（约 14ms/帧）**。端到端 20.7ms 含
letterbox、前后处理与 NMS。结论：量化后 **墙钟延迟也明显下降**，不再只是“省 CPU”。
每 3 帧检测一次（`VISION_DETECT_EVERY_N=3`）仍足够覆盖 30fps 画面。

### 3.5 `tracker.py` — 质心追踪（43 行，原样移植）

给每帧的检测框一个**稳定 id**，否则界面上的编号每帧都在跳。
同 id 只在**同类型**之间匹配；连续 `max_misses+1` 帧没出现就回收。

**踩过的坑**：tracker 原本只存 `type`，导致中间帧重建检测时
`type_en` 被写成中文（`"人"`），标注画面上就出现 `#3 人` 而不是 `#3 person`。
现在建 track 时一并存 `type_en`。自检里有断言锁住。

### 3.6 `pipeline.py` — 编排（313 行，后台守护线程）

主循环（`run()`）：

```
while running:
    left, right = cam.read()                    ← 无帧则 sleep 50ms
    ┌ 检测：每 _detect_every 帧真跑一次 YOLO
    │  中间帧不跑模型，用 tracker 的位置兜住     ← CPU 上保证画面流畅的关键
    └  （tracked=True 的框置信度记 0.0，前端据此不显示置信度）
    depth_map = astra.read() 或复用最近一帧      ← 见下方「深度帧复用」
    for 每个物体:
        距离 = 结构光中位数 / 视差 / None
        方位 = hfov 换算 / 焦距换算
        方向 = left(≤-15°) / right(≥15°) / center
        三维 = position(距离, 方位)  → x 横向, z 前向
    标注 → JPEG 编码 → 更新三份快照（Lock 保护）
```

**深度帧复用（关键修正）**：`astra.read()` 是非阻塞的，相邻两次读取之间会返回 `None`。
照实透传会让距离**一帧有一帧无地闪**。深度 30fps 远快于检测，所以复用最近一帧：

```python
if depth_map is not None:
    self._last_depth, self._last_depth_ts = depth_map, time.time()
elif 最近一帧还在 _DEPTH_STALE_S(0.3s) 内:
    depth_map = self._last_depth      # 复用
# 超过 0.3s 就丢掉——否则相机动了还拿着旧距离不放
```

**线程模型**：管线跑在守护线程，三份快照（`latest_frame` / `latest_jpeg` / `latest_event`）
用一把 `Lock` 保护；HTTP 处理器只做 read-under-lock，不与检测抢时间。

**异常策略**：整个 `run()` 包在 try 里，任何异常只置 `status="error"` 并记 `error`，
**绝不冒泡到 myrobot 主服务**——对话链路必须照常。

### 3.7 `_depth_stats()` — 诊断字段（值得单独说）

事件里带一个 `depth_stats`，因为它把三种「距离是 None」的情况**区分开**了：

| `depth_stats` | 含义 |
|---|---|
| `{source: "astra", valid_ratio: 0.78, min_mm, max_mm}` | 深度图好着呢，None 是取点问题 |
| `{source: "astra", valid_ratio: 0.0}` | 深度出帧了但整帧全 0（镜头被挡/超量程） |
| `{source: "none"}` | 这一帧根本没拿到深度图 |

没有它，排障时只能看到「全是 None」，无法判断是哪一环。
**这个字段就是实际排障时加上的**（见 §5.2）。

---

## 4. 前后端接口

### 4.1 服务端（`backend/server.py`）

| 接口 | 返回 | 说明 |
|---|---|---|
| `GET /api/vision/status` | `{available, status, mode, depth_source, depth, has_frame, detector, error}` | 前端据此决定渲染什么；`detector` 说明实际推理后端 |
| `GET /api/vision/objects` | `{ts, frame_id, mode, depth_source, depth_available, depth_stats, objects[]}` | 前端 500ms 轮询 |
| `GET /api/vision/stream` | `multipart/x-mixed-replace; boundary=frame` | MJPEG，直接给 `<img src>` |

`objects[]` 里每一项：

```json
{
  "id": 12, "type": "人", "type_en": "person", "confidence": 0.87,
  "bbox": [210, 156, 390, 336],
  "distance_m": 1.89,                     // null = 无深度，前端显示「—」
  "angle_deg": -25.4,                     // 负=左，正=右
  "direction": "left",                    // left / center / right
  "position": {"x": -0.8, "y": 0.0, "z": 1.7}   // 相机坐标系，米
}
```

`status.detector` 的实际形态（NPU 真正生效时）：

```json
{
  "backend": "onnx",
  "provider": "VitisAIExecutionProvider",
  "providers": ["VitisAIExecutionProvider", "CPUExecutionProvider"],
  "provider_registered": true,
  "npu_active": true,
  "npu_subgraphs": 3,
  "model_quantized": true,
  "model": "yolov8n_xint8.onnx",
  "input_size": 640,
  "load_ms": 20400.0,
  "warmup_ms": 42.8,
  "requested": "auto",
  "fallback_reason": null
}
```

`providers` 里同时出现 CPU 是预期结果：NPU 只接管可编译子图，其余算子回退 CPU。
**只有 `npu_active === true` 且 `npu_subgraphs > 0` 才算 NPU 真正接管**；
`provider` 或 `providers` 里出现 VitisAI 本身不算。`fallback_reason` 非空则说明
当前实际跑的是 Ultralytics/CPU，不能只看 `available`。

**为什么画面和物体分两个接口**：画面要流畅（每帧都推），物体列表一秒刷两次就够看。
两者频率解耦，也省得为了列表去解析视频帧。

**MJPEG 为什么不用 WebSocket**：`<img src>` 直接就能显示，前端零解码代码；
而且它只要「一直显示最新帧」，不需要消息语义。
流里**只在 `frame_id` 变化时才推**，避免同一张图反复发。

**启动/关闭**挂在 FastAPI 的 lifespan 上，与工具框架共用同一套生命周期。

### 4.2 前端（`frontend/src/vision.js`）

- 浮层在 `#wrap` 内（数字人画布容器），`position:absolute; left:12px; bottom:12px`，
  所以「左下角」是相对数字人舞台，不是整个页面。
- 点标题栏可折叠，状态存 `localStorage`。
- 状态点四态：**绿**=在出帧 / **黄**=可用但没画面 / **红**=不可用 / **灰**=未连接。
- 深度来源中文名映射：`astra→结构光测距`、`stereo→双目测距`、`none→单目（无深度）`。

---

## 5. 排障记录：踩过的坑与根因

这一节是本文最有价值的部分——每个坑都曾让人误判方向。

### 5.1 权重下载卡死 → 整个视觉停摆

- **现象**：`/api/vision/status` 永远 `status:"init"`、`frame_id:0`、无画面。
- **误判**：以为是相机问题。
- **根因**：`YOLO("yolov8n.pt")` 本地没有权重 → 去 GitHub 下载 → 本机连不上 →
  connect timeout + 重试 3 次，模型加载一直不返回。
- **修**：`resolve_weights()` 优先用本地权重；已把 `yolov8n.pt` 放进
  `backend/vision/weights/`（该目录 `.gitignore` 忽略，二进制不入库）。

### 5.2 距离一帧有一帧无地闪

- **现象**：有时物体有距离、有时全是 `None`，随机跳。
- **根因**：Astra 的 `read()` 是非阻塞的，两次深度帧之间返回 `None`，
  照实透传 → 那一帧所有距离都是 None。
- **修**：复用最近一帧深度（0.3s 内），见 §3.6。
- **顺带**：这次才加上 `depth_stats`，否则根本分不清是
  「没出帧」「整帧 0」还是「取点无效」。

### 5.3 中心点取深度经常失败

- **现象**：`depth_stats` 显示深度图 78% 有效，但物体距离还是 None。
- **根因**：取了 bbox 中心**单点**，而该点常常正好是无效像素（原始值 0）。
- **修**：改成取邻域（半径 `max(4, dw/40)`）**有效值的中位数**。

### 5.4 OpenNI2 白折腾

- **现象**：SDK 自带 `orbbec.dll` 驱动，但 `oniGetDeviceList` 返回 0 个设备。
- **根因**：Astra Pro 的深度走厂商私有接口，OpenNI2 驱动没把 PID_0403 认成 OpenNI 设备。
- **验证**：厂商的 `DepthReaderPoll.exe` 能出深度帧 → 硬件/驱动没问题 → 换 C API。
- **另**：VisionDev 的 `astra_depth.py` 里硬编码了 `PID_0403` 与一个 E 盘路径，
  但用的是 OpenNI2 路线，对这台机器无效。

### 5.5 `primesense` 与 Python 3.13 不兼容

- `import primesense` → `TypeError: abstract class`（`CEnum` 元类在 3.13 上失效）。
- 这台机器是 Python 3.13，所以**任何**依赖 `primesense` 的方案都不可用。

### 5.6 RGB 与深度的坐标系不是同一个

- RGB 640×480、深度 640×480，但**是两个不同传感器**：视场角不同
  （RGB 更窄），所以像素**不能直接对应**。
- 当前做法：按各自尺寸做归一化坐标映射（`dx = cx * dw / fw`），**够 demo 用**。
- 要更准需做深度-彩色对齐标定，SDK 提供
  `astra_depthstream_set_registration` / `astra_depthstream_get_d2c_resolution`。
  **这是一个已知的精度上限，不是 bug。**

---

## 6. 配置项

| 变量 | 默认 | 说明 |
|---|---|---|
| `VISION_ENABLED` | `1` | 总开关；`0` 则完全不启动 |
| `VISION_ASTRA_SDK_DIR` | （空） | Astra SDK 的 `bin` 目录；留空则不用结构光，退回单目方位 |
| `VISION_USE_ASTRA` | `1` | 是否允许使用结构光 |
| `VISION_CAMERA_MODE` | `auto` | `auto`/`single`/`dual`/`mono` |
| `VISION_CAMERA_INDEX` | `0` | single/mono 的相机索引 |
| `VISION_LEFT_INDEX` / `VISION_RIGHT_INDEX` | `0` / `1` | dual 模式 |
| `VISION_WIDTH` / `VISION_HEIGHT` | `640` / `480` | RGB 采集分辨率 |
| `VISION_FOCAL_LENGTH` / `VISION_BASELINE` | `700.0` / `0.06` | 仅视差路径用；需按相机标定 |
| `VISION_YOLO_MODEL` | `yolov8n.pt` | PyTorch/CPU 兜底用的权重名 |
| `VISION_YOLO_CONF` | `0.4` | 置信度阈值（ONNX 与 Ultralytics 共用） |
| `VISION_YOLO_DEVICE` | `cpu` | **仅 Ultralytics 后端**的 `device=`；ONNX 后端忽略 |
| `VISION_YOLO_BACKEND` | `auto` | `auto` / `npuvitis` / `dml` / `onnx` / `ultralytics`，见下表 |
| `VISION_YOLO_ONNX_MODEL` | `auto` | `auto` 优先 `yolov8n_xint8.onnx`，缺失才退回 `yolov8n.onnx` |
| `VISION_ONNX_PROVIDER` | `VitisAIExecutionProvider` | ONNX Runtime provider；`dml` 模式会自动改成 DirectML |
| `VISION_ONNX_CACHE_DIR` | `.cache/ryzenai` | VitisAI 编译缓存目录（首次启动会创建） |
| `VISION_ONNX_CACHE_KEY` | （空） | 留空时按「模型文件名 + 输入尺寸」派生；手动指定后换模型/尺寸要记得改 |
| `VISION_ONNX_LOG_LEVEL` | `warning` | `verbose/info/warning/error/fatal`；`info` 会刷算子分区日志 |
| `VISION_ONNX_WARMUP` | `1` | 加载后先跑一张空图，把首帧抖动挪到启动阶段 |
| `VISION_ONNX_INTRA_THREADS` | `2` | CPU 兜底算子线程数（**不要放开**，会抢 LLM/TTS） |
| `VISION_ONNX_INTER_THREADS` | `1` | 并行图数；视觉只有一路，保持 1 |
| `VISION_ONNX_ALLOW_SPINNING` | `0` | 禁止 ORT 线程忙等；放开会显著抬高 CPU |
| `VISION_YOLO_INPUT_SIZE` | `640` | 必须与 ONNX 导出时的 `imgsz` 一致 |
| `VISION_YOLO_NMS_IOU` | `0.45` | 分类别 NMS 的 IoU 阈值 |
| `VISION_YOLO_MAX_DET` | `300` | 单帧最多保留的检测框 |
| `RYZEN_AI_INSTALLATION_PATH` | `C:\Program Files\RyzenAI\1.7.0` | Ryzen AI 运行时根目录，VitisAI EP 依赖 |
| `VISION_DETECT_EVERY_N` | `3` | 每 N 帧检测一次；中间帧用 tracker 兜住 |
| `VISION_MODEL_DIR` | （空） | 追加的权重搜索目录（`.pt` 与 `.onnx` 都查） |
| `VISION_JPEG_QUALITY` | `70` | MJPEG 画质 |

`VISION_YOLO_BACKEND` 的选择语义：

| 值 | 行为 |
|---|---|
| `auto` | 优先量化 ONNX；provider 可用就试，但只有 `npu_active=true` 才算 NPU 生效；任何失败都回退 Ultralytics |
| `npuvitis` | 强制 `VitisAIExecutionProvider`；失败仍回退，但原因写进 `fallback_reason` |
| `dml` | 强制 `DmlExecutionProvider`（AMD GPU / DirectML） |
| `onnx` | 按 `VISION_ONNX_PROVIDER` 指定的 provider 走 ONNX |
| `ultralytics` | 完全不走 ONNX，直接 PyTorch/CPU |

> 后端是否真的落到 NPU，不看配置，看 `/api/vision/status.detector` 的
> `provider` / `providers` / `fallback_reason`。普通 Python 环境通常没有
> VitisAI EP，`auto` 会显式回退到 CPU。

---

## 7. 依赖与降级

### 7.1 YOLO 权重（必须自己准备，不入库）

权重是二进制模型，**不进仓库**，也不该指望运行时代下载（本机连不上 GitHub，
见 §5.1）。请按下面的信息自己放一份。

| 项 | 值 |
|---|---|
| 文件名 | `yolov8n.pt` |
| 模型 | YOLOv8n（COCO 80 类，nanosmall 最小档） |
| 大小 | 6,549,796 字节（约 6.25 MiB） |
| SHA256 | `f59b3d833e2ff32e194b5bb8e08d211dc7c5bdf144b90d2c8412c47ccfc83b36` |
| 适配版本 | ultralytics **8.4.170**（本机实测；8.x 各版本基本通用） |
| 官方下载 | `https://github.com/ultralytics/assets/releases/download/v8.4.0/yolov8n.pt` |
| 放置位置 | `backend/vision/weights/yolov8n.pt`（该目录已被 `.gitignore` 忽略） |

**下载方式（三选一，按可行性排序）**：

```bash
# a) 用 ultralytics 自动拉（有外网时最简单，会下到当前目录再手动移过去）
python -c "from ultralytics import YOLO; YOLO('yolov8n.pt')"
#    然后 mv yolov8n.pt backend/vision/weights/

# b) 直接从上面的官方地址下载（浏览器/下载工具都行）

# c) 复用别处已下好的权重，不必拷贝，指个目录即可
#    在 .env 里设 VISION_MODEL_DIR=<那个目录>
```

**校验下载是否完整**（比对 SHA256；对不上说明没下完或下错了）：

```bash
python -c "import hashlib;print(hashlib.sha256(open('backend/vision/weights/yolov8n.pt','rb').read()).hexdigest())"
```

**换更大模型**：把 `VISION_YOLO_MODEL` 改成 `yolov8s.pt` / `yolov8m.pt` 等，
并按同样方式准备 `.pt`（CPU 兜底）与 `.onnx`（NPU）两份权重。
NPU 路径的单帧墙钟延迟未必比 CPU 低，但 CPU 时间占用明显更小；换大模型后
仍应调大 `VISION_DETECT_EVERY_N` 并实测。类别集合不变（都是 COCO 80 类），
所以 `labels.py` 的中文映射表不用改。

**搜索顺序**（`detector.py::resolve_weights`）：
配置里的绝对路径 → `backend/vision/weights/<名>` → `VISION_MODEL_DIR/<名>` → 交给 ultralytics 自行下载。

### 7.1b YOLOv8n float32 ONNX 权重（量化输入，不入库）

| 项 | 值 |
|---|---|
| 文件名 | `yolov8n.onnx`（float32，本身**不会**上 NPU） |
| 模型 | YOLOv8n，COCO 80 类，opset **12**，静态输入 |
| 大小 | 12,824,061 字节（约 12.2 MiB） |
| SHA256 | `64B00AF280E86F13158EEAD49D5AE89FCA88D31C0E4BFA78CC264746D5A82968` |
| 输入 | `1 × 3 × 640 × 640 float32`（RGB、0~1、NCHW） |
| 输出 | `1 × 84 × 8400`（前 4 行 cx/cy/w/h，后 80 行类别分数） |
| 放置位置 | `backend/vision/weights/yolov8n.onnx`（同样被 `.gitignore` 忽略） |

**导出命令**（在已安装 ultralytics 的环境执行，`opset=12` 是 VitisAI 兼容性关键）：

```bash
python -c "from ultralytics import YOLO; YOLO('yolov8n.pt').export(format='onnx', opset=12, imgsz=640, dynamic=False, simplify=False)"
# 生成 yolov8n.onnx 后移到 backend/vision/weights/
```

**校验**：

```bash
python -c "import hashlib;print(hashlib.sha256(open('backend/vision/weights/yolov8n.onnx','rb').read()).hexdigest())"
```

导出的 `imgsz` 必须与 `VISION_YOLO_INPUT_SIZE` 一致；它是 §7.1c 量化脚本的输入。
`VISION_ONNX_CACHE_KEY` 留空时会自动按模型名和输入尺寸派生，避免 float/量化模型
共用同一份编译产物。

**搜索顺序**（`yolo_npu.py::_resolve_model`）：
绝对路径 → `backend/vision/weights/<名>` → `VISION_MODEL_DIR/<名>`。
找不到会**显式抛错**，再由 `Detector` 回退 Ultralytics，不会静默去外网下载。

### 7.1c XINT8 量化权重（NPU 真正使用，不入库）

| 项 | 值 |
|---|---|
| 文件名 | `yolov8n_xint8.onnx` |
| 量化格式 | Quark **XINT8**（UINT8 激活 + INT8 权重，MinMSE 校准） |
| 大小 | 3,544,884 字节（约 3.4 MiB） |
| SHA256 | `3705DD493105E812D59F24563D867D62059ECED842CE3F8C801155557A49AD9F` |
| 输入 | `1 × 3 × 640 × 640 float32`（线上预处理不变） |
| 输出 | `1 × 84 × 8400`；最终 `/model.22/Concat_3` 保持 float32 |
| 放置位置 | `backend/vision/weights/yolov8n_xint8.onnx`（被 `.gitignore` 忽略） |

**为什么这一份才是 NPU 模型**：VitisAI 只为带 `QuantizeLinear` / `DequantizeLinear`
的子图创建 DPU 子图。float32 模型即使 `get_providers()` 里有 VitisAI，profile 中
NPU 执行事件仍是 0。判断标准见 §8。

**生成方式**（Quark 环境 + 校准图，约 4 分钟）：

```powershell
# 清掉 PATH 里的 Python312，否则 Quark 里的 torch 会误判 ROCm
$env:PATH = ($env:PATH -split ';' | Where-Object { $_ -notlike '*Python312*' }) -join ';'
$env:RYZEN_AI_INSTALLATION_PATH = 'C:\Program Files\RyzenAI\1.7.0'

.\.venv-ryzenai\Scripts\python.exe scripts\quantize_yolo.py `
  --calib-dir .scratch\calib_src `
  --out backend\vision\weights\yolov8n_xint8.onnx
```

校准集要求与坑：

- 放 **20~60 张真实相机画面**（含明暗、翻转、裁剪变体最好），必须覆盖实际视场和光照；
  用无关图片会让量化区间偏掉，框会漂移。
- 默认排除 `/model.22/Concat_3`。不要删掉这个例外：它同时承载 0~640 的框坐标
  和 0~1 的类别概率，共用 scale 会把概率压成 0。
- `QConfig.get_default_config("XINT8")` 使用 MinMSE；`CalibMethod.MinMax`
  在 `enable_npu_cnn` 下不支持，改了会直接校准失败。
- 脚本最后会用 CPU EP 跑一张图并检查 `score_max`；**`score_max <= 0.01` 视为量化失败**
  （退出码 2），不要把这个模型拿去上线。
- `custom_ops.dll` 偶发 GBK 解码警告不影响标准 Conv/QDQ 量化。

### 7.2 Astra SDK（结构光深度，必须自己准备）

SDK 的 DLL **不进仓库**（体积大 + 许可限制）。本机使用的版本与路径：

| 项 | 值 |
|---|---|
| 产品 | Orbbec Astra Pro（Astra Pro HD Camera + ORBBEC Depth Sensor） |
| SDK | **AstraSDK v2.1.3**，build `94bca0f52e`（20210608，VS2015 win64） |
| 需要的关键文件 | `bin/astra_core.dll`、`bin/astra.dll`、`bin/orbbec.dll`、`bin/OpenNI2/Drivers/*` |
| 配置变量 | `VISION_ASTRA_SDK_DIR` = 该 `bin` 目录的绝对路径 |
| 本机路径 | `E:\BaiduNetdiskDownload\Astra Pro深度相机\Window_SDK\AstraSDK-v2.1.3-...-vs2015-win64\bin` |

驱动另有单独安装包：`Windows驱动/SensorDriver_V4.3.0.17.exe`（装好后设备管理器里
能看到 `ORBBEC Depth Sensor`）。**驱动不装，SDK 也读不到深度。**

验证 SDK 是否可用（最纯粹的一步，不经过本项目）：直接跑 SDK 自带的
`bin/DepthReaderPoll.exe`，能持续打印 `depth frameIndex N value ...` 就说明硬件链路通。

**Python 依赖**：

| 路径 | 必需依赖 | 说明 |
|---|---|---|
| 公共 | `opencv-python`、`numpy` | 采集、letterbox、绘制、JPEG |
| NPU / ONNX 路径 | `onnxruntime-vitisai`（自带 `VitisAIExecutionProvider`）、`onnx` | 只在 `.venv-ryzenai` 中安装；普通 Python 没有这个 EP |
| CPU 兜底路径 | `ultralytics`（含 `torch`） | ONNX 失败时仍能靠它出检测框 |

另两个必须手工准备的资源（`.pt` / `.onnx` 权重、Astra SDK）见 §7.1 / §7.1b / §7.2。

**降级矩阵**（每一格都实测过）：

| 情况 | 行为 |
|---|---|
| `VISION_ENABLED=0` | 完全不启动，日志一条 |
| 缺 `cv2`/`numpy` 且无任何检测后端 | `vision_available()` 返回明确原因，浮层显示「视觉不可用（缺依赖）」 |
| 只有 `onnxruntime`，没有 `ultralytics` | 可走 ONNX；ONNX 失败后无 CPU 兜底 → `status: "error"` |
| 只有 `ultralytics`，没有 ONNX Runtime / VitisAI EP | `auto` 回退 PyTorch/CPU，`fallback_reason` 写明缺失的 provider |
| 有 ONNX Runtime 但 provider 不在 `ort.get_available_providers()` | ONNX 初始化抛错 → 回退 Ultralytics；启动日志和 `describe()` 都有原因 |
| `.onnx` 缺失或路径不对 | `_resolve_model()` 抛 `FileNotFoundError` → 回退 Ultralytics，不联网下载 |
| ONNX 初始化成功但推理抛异常 | 该帧检测失败；`pipeline` 置 error 态，主对话链路不受影响（不会自动切换后端） |
| `RYZEN_AI_INSTALLATION_PATH` 不存在 | VitisAI EP 加载失败 → 回退 CPU；先修环境变量或安装 Ryzen AI 运行时 |
| 相机被占用 | 启动失败 → `status: "start-failed"`，对话不受影响 |
| `.pt` 权重缺失且无网 | Ultralytics 加载失败 → `status: "error"`（这正是 §5.1 的现象，现已靠本地权重避免） |
| 没配 `VISION_ASTRA_SDK_DIR` | 深度退回单目方位，`depth_source: "none"` |
| 相机被拔掉 | 循环里异常 → 置 error 态，主服务照常 |

---

## 8. 验证方法（照着做即可复现）

```bash
# 1) 纯逻辑自检（不需要相机，CI 也跑这个）
python scripts/check_vision.py
#   覆盖：tracker id 稳定性与 type_en 保留 / 类名中文化 /
#         权重路径解析（.pt 与 .onnx）/ COCO 类别顺序 /
#         结构光方位换算 / 取深度中位数与抗野值

# 2) 接口自检（服务跑起来后）
curl -s http://127.0.0.1:8765/api/vision/status
curl -s http://127.0.0.1:8765/api/vision/objects
#   status.detector 应给出实际 backend / provider / fallback_reason，
#   不能只看 "available": true 就以为 NPU 生效

# 3) 画面流（MJPEG，人眼看）
#    浏览器打开 http://127.0.0.1:8765/app/index.html  → 左下角浮层

# 4) 结构光深度单独验证（不经 myrobot，最纯粹的硬件自检）
#    跑 SDK 自带示例：DepthReaderPoll.exe
```

**NPU 启动方式**（必须用带 VitisAI EP 的环境）：

```powershell
.\scripts\run_server_npu.ps1          # 默认监听 8000，参数会透传给 server.py
curl -s http://127.0.0.1:8000/api/vision/status
# 期望：detector.npu_active=true 且 npu_subgraphs>0
#       （只看到 provider=VitisAIExecutionProvider 不算数）
```

**如何确认 NPU 真的在算**（三层证据，缺一不可）：

1. `/api/vision/status.detector`：`provider_registered === true`、
   `npu_active === true`、`npu_subgraphs > 0`、`model_quantized === true`；
2. 启动日志：`[YOLO-NPU] ... 量化=True npu_active=True 子图=3~4`；
3. 连续推理时用 PID 采样任务管理器/性能计数器：`engtype_compute` 持续 90%+
   （本机实测 95.6%）。只看 `get_providers()` 会把“注册但全跑 CPU”误判成 NPU。

```powershell
# 采样 NPU 引擎占用（把 <PID> 换成后端进程号）
(Get-Counter "\GPU Engine(*)\Utilization Percentage" -SampleInterval 1 -MaxSamples 5).CounterSamples |
  Where-Object { $_.InstanceName -like "*engtype_compute*" } |
  Select-Object InstanceName, CookedValue
```

首次启动会有约 **20 秒**的 DPU 子图编译；`warmup_ms` 只代表 warmup 这一次，
`load_ms` 才是编译总耗时。普通 `python backend\server.py` 通常没有 VitisAI EP，
`auto` 会回退到 `ultralytics`，这是预期行为，不是 NPU 生效。

**期望值**（本机实测）：`depth_source: "astra"`，物体带真实 `distance_m` 与 `angle_deg`，
`min_mm ≈ 440~550`，`max_mm ≈ 4500`。

`valid_ratio` **随场景变化**，不要当成硬指标——它表示这一帧有多少比例的像素拿到了有效回波，
室内实测在 **0.6 ~ 0.8** 之间浮动（正对墙面时更高；对着窗户/深色吸光物/超量程处会更低）。
判断深度是否正常，看 `valid_ratio > 0` 且 `min_mm/max_mm` 落在量程内即可。

---

## 9. 已知限制

1. **深度-彩色未做对齐标定**：距离取的是「RGB 框中心映射到深度图」的邻域，
   两个传感器视场角不同，边缘物体的距离会有偏差。要更准需上 SDK 的配准接口（§5.6）。
2. **NPU 只部分卸载，CPU 仍参与**：VitisAI 编译不了的检测头算子会回到 CPU；
   `VISION_DETECT_EVERY_N=3` 是流畅度与实时性的折中，快速移动物体会看到框
   「追」上去的滞后感。CPU 兜底线程必须保持 `intra=2/inter=1/spinning=0`，
   否则会明显抢占 LLM/TTS 的算力。
3. **追踪器是质心的**，不是 ReID：物体交叉换位可能串 id。
4. **`position.y` 恒为 0**：只用了水平方位角，没有垂直方向的仰角定位。
5. **MJPEG 逐帧 JPEG 编码**，带宽比 H.264 高；局域网够用，外网隧道下会吃流量。
6. **仅本机验证过 Astra Pro**。换别的结构光相机需重写 `astra.py`（各厂商 API 不同）；
   换真双目相机则改走 `stereo.py` 视差路径，需标定 `focal_length` / `baseline`。
7. **NPU 生效依赖量化模型**：float32 ONNX 会静默落回 CPU，必须先生成
   `yolov8n_xint8.onnx`。最终输出 Concat 为保精度留在 CPU/float32，属于有意妥协。
8. **冷启动约 20 秒**：DPU 子图编译发生在视觉管线启动阶段；VitisAI 编译缓存
   目前不落盘，`cache_dir` / `cache_key` 已配置但未观察到可复用产物，
   因此每次冷启动都按首次编译预估。
9. **ONNX 后端不会热切换**：初始化失败才回退 Ultralytics；运行中某帧推理异常
   只置 error 态，不会在帧级自动换后端。要看实际状态，查
   `/api/vision/status.detector`。
