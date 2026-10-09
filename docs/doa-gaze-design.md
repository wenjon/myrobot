# 声源定位 → 数字人注视转向 设计（麦克风阵列 DOA → gaze）

> 状态：**A 方案已实现并跑通离线闭环**（纯 Python GCC-PHAT，零新核心依赖）
> 配套：设计规格第 19 章（摄像头感知与注视跟随，本设计为**声源维度**的补充）、第 5 章（WS 消息）、第 13 章（配置）
> 目标：**照本文可以复现整套逻辑** —— 包含选型理由、每个模块的职责与关键算法、
>        所有踩过的坑及其根因。看代码前先看这。

---

## 0. 一句话概览

用 **ReSpeaker 4 Mic Array（UAC1.0）** 这 4 个麦克风，在 PC 端做 **GCC-PHAT 声源测向**，
估算出说话人的**方位角（azimuth）**；把方位角映射成屏幕前方平面上的水平坐标 `x ∈ [-1,1]`，
通过 WebSocket 推给前端，驱动 3D 数字人的**眼珠 + 颈/头分层转向**，看向说话人。

整块是**尽力而为**的（与视觉子系统同款哲学）：任何一环坏掉（没插阵列、采集失败、前端没
检测到声源）都自动降级，绝不牵连对话链路。

```
                    ┌────────── backend/doa/ ──────────┐
 ReSpeaker 4-Mic    │                                  │
  ├ ch0..3 ─────────▶ source.py  (可插拔采集源) ───────┐ │
  │   (UAC1.0)        File回放 / sounddevice / pyusb    │ ├─▶ gccphat.py ─▶ estimator.py ─┐
  └────────────────────────────────────────────────────┘ │      (帧缓冲+   (方位估计     │
                                                          │       GCC-PHAT)  +去抖/暂停)  │
                                                          │                              │ ──▶ WS doa_azimuth ──▶ 前端 gaze
                                                          └──────────────────────────────┘         lookAt / turnTo / setPose
```

## 1. 为什么是「本地算方向」而不是「设备给我方向」

关键事实（实测环境确认）：ReSpeaker 4 Mic Array (UAC1.0)（`VID_2886&PID_0018`）在 Windows
上只是一个 **USB 音频类（UAC）四声道声卡**。它**不做板载 DOA**，只上报 4 路原始 PCM。
所以「声源在哪个方位」必须由我们自己在 PC 上从 4 路的**到达时间差**算出来。

- 社区成熟做法是 **ODAS**（Open embeddeD Audition System）：`GCC-PHAT → 三角测向 → JSON 流`。
- 本项目 A 方案选择**纯 Python 手写 GCC-PHAT**，不引入 ODAS 二进制。理由见 § 3 取舍表。

## 2. 数据链路总览

| 环节 | 模块 | 输入 → 输出 | 说明 |
|---|---|---|---|
| 采集 | `source.py` | 4 路 PCM → 帧(4×N，16k，约100ms) | **可插拔源接口**：File / sounddevice / pyusb |
| 预处理 | `gccphat.py` | 帧 → **帧级时延差集合** | 去直流 / Windowing / 可选高通 / 能量 VAD |
| 估计 | `estimator.py` | 时延差 → **azimuth / elevation / 活动度** | 四方阵几何反解 + 时间平滑去抖 |
| 暂停/抑制 | `estimator.py` | 背景任务 → 冻结 / 静默 | 回声抑制（自己 TTS 播放时暂停） |
| 下发 | `server.py` | 方位 → **WS `doa_azimuth`** | 低频节流（约 4–8 Hz） |
| 前端 | `vision.js` / `main.js` | 方位 → **屏幕 x,y** | 映射 + 交给 head3d gaze 系统 |
| 呈现 | `head3d.js` | x,y → **眼珠 + 颈/头旋转** | 已存在：lookAt / turnTo / setPose |

## 3. 选型理由：为什么 A 方案用「纯 Python GCC-PHAT」

三方候选对比（本专题只讨论测向，不讨论 ASR）：

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| **A. 纯 Python GCC-PHAT** | 零二进制、可控、易看懂易调参、跨平台 | 精度略逊于专用算法；需自己写帧/平滑 | **选定 A（本轮）** |
| **B. ODAS + 透传** | 精度高、含活动度 / 回声稳健估计 | Windows 编译困难、黑盒、需透传/降维 | 后续可选升级 |
| C. ReSpeaker pyusb/官方工具 | 与硬件贴合 | 多为 Linux 示例代码，Windows 兼容需自己适配 | 作为 source 的一种 |

**选 A 的核心理由**：本需求本质是**「把说话人写到屏幕前方的 x 坐标」**（demo 级 ±10–15° 足矣），
不是高精度声学工程。GCC-PHAT 在四阵间距（约 5–7cm）下算出的是「哪几只麦克风先听到」的
几何反解角度。这个精度覆盖「前方 ±60° 内分左右」的场景（数字人转头），足够 demo。

而 A 方案的可插拔 Source + 后端/前端接线（WS、gaze 转向）对 B 也一样适用——以后想换 ODAS
只换 estimator 一层，主链路不动。

## 4. 核心算法：GCC-PHAT 测向

### 4.1 为什么做 GCC-PHAT 而不是直接互相关

两路麦克风时延差直接用互相关（Cross-Correlation）在**混响**下会找错峰。PHAT 加权把互谱
归一成「纯相位」，锐化峰值、对多径更稳健。对语音 + 桌面距离这是阵列测向的标准选择
（代价是对环境噪声略敏感）。

### 4.2 算法步骤（每帧，参考 mic = 能量最大的一只）

1. 取当前帧两路 `x1, x2`（每路约 100ms≈1600 samples @16k，叠 50% hop）。
2. 去均值（DC）+ 可选高通（80Hz，消低频气流/电源哼）。
3. 逐帧短窗（Hamming），零填充到 2×N 做 FFT，得 `X1=FFT(x1)`、`X2=FFT(x2)`。
4. 互谱 `G=X1 * conj(X2)`；PHAT 加权 `G/|G|`（`|G|≈0` 处 clamp 到 0 防爆）。
5. 逆 FFT 得到时延域 `cc`；取 `tau = argmax(cc) - N/2`（单位 sample）。
6. 时延差 `d_samples` → 时间差 `tau_s = d / sample_rate` → 距离差 `delta = tau_s * c`。
7. **几何反解方位角**：四方阵即 4 个麦克风在平面上的位置，两两距离差满足
   `delta_ij = |u - p_i| - |u - p_j|`。取两对正交的基线（front-back 与 left-right），
   得到的两个 delta 分别代表前后（纵深）与左右（横向）的相位斜率：
   `azimuth = atan2(delta_leftright, delta_frontback)`。
   （严格做法是解超定方程组最小二乘；demo 用两对主正交分量近似，精度足够 ±10–15°。）
   elevation 可选的：只用水平基线时把抬头仰角并入 y 分量（见 § 6 映射）。
8. **活动度/有效性**：当前帧能量与长期底噪比（平滑 SNR）低于阈值 → `active=False`，
   不推进角度（避免把静噪声当方向一甩一甩）。

### 4.3 为什么配 50% hop + 时域平滑

- 50% hop ⇒ 相邻帧重叠，方位角经过指数平滑后不跳变，数字人转向才自然。
- 数字人转头天然有惯性（前端 `lookAt`/`setPose` 自带平滑），后端再加一层慢速去抖，双保险。

### 4.4 精度与边界（诚实说明）

- 阵间距约 5–7cm + 16kHz → 时差分辨率 ±1 采样 = ±2.1cm，正前方角度分辨率约 ±10–15°。
- 阵列放桌上，左右与前后两基线正交 → 前方 ±90° 内 azimuth 单调、**前方 180° 内唯一可解**；
  背面存在前后对称模糊（180° 翻转）——数字人只转头不看背，demo 接受此模糊。
- 多说话人同时说话会测到合成中心，不处理（后续可做能量门限加权）。

## 5. 采集源（Source）可插拔设计

为什么可插拔：**采集是「设备相关」的部分，测向是「通用」的部分**。分开后：

- 没插阵列 / 阵列采集失败 → 可换 File 回放源跑通整条链路（demo 先这样验证）；
- 换第二块阵列 / 未来上 ODAS → 只换 `source.py` 一个实现。

```python
class AudioSource(ABC):
    sample_rate: int = 16000
    num_channels: int = 4
    @abstractmethod
    def read_frames(self) -> Iterator[np.ndarray]:
        """逐帧产出 (4, N) float32，N ≈ 采样率*0.1（100ms）；StopIteration = 结束"""
    @abstractmethod
    def close(self): ...
```

- **FileSource**：读一个多声道 WAV / 测试信号（扫描声源），用于**离片验证算法**（本阶段先做）。
- **SounddeviceSource**：`sounddevice` / `sd.InputStream(device=阵列, channels=4, samplerate=16k)`。
  sounddevice 是**可选**依赖：没装则找不到真实源，退回 File。
  （UAC1.0 在 Windows 上 `sounddevice`/PortAudio 实测多通道可能受默认格式限制，
  若 4 声道采不上再降级到 `pyusb` bulk 读，均列入 § 9 风险。）
- `pyusb`（后续）：直接 bulk 读 UAC ISO IN 端点，避开系统混音器。

**为什么采样率取 16k**：语音带；4 麦 USB 常见 16k/48k。16k 时差分辨率已够本精度目标，
且宿主 CPU 轻。配置可改。

## 6. 方位角 → 屏幕前方平面 → gaze 映射

数字人住在**屏幕坐标系**（屏幕 = 世界前方，相机朝 z）。说话人在**现实空间**的水平方位角
映射过去：

- 定义：`az` 为相对屏幕正前方的水平角（逆时针为正，单位度），阵列中心在屏幕下部。
- **横向**：`x = clamp(az / (FOV_x/2), -1, 1)`，`FOV_x`（如 60°，配置）。把左右分得清晰。
- **纵向**：可选的 elevation `ele` → `y = clamp(ele / (FOV_y/2), -1, 1)`（FOV_y 如 30°）。
  demo 若阵列与脸部接近，可把 y 直接映射过去；无 elevation 时 y 走 0（对正前方）。
- **前端驱动**：`main.js` 收到 `doa_azimuth{az_deg, active, ts}` 后，计算 `x,y` →
  调用 `head.lookAt(x, y, holdMs≈1200)`（眼珠）+ `head.turnTo(x)` /
  `head.setPose(yaw=x*MAX_YAW, pitch=y*MAX_PITCH)`（颈/头分层）。
  `holdMs` 期间暂停随机扫视，避免「刚才明明在看说话人，又被随机扫视抢走 0.3s」。

前端已有机制（`head3d.js` 导出 `lookAt/turnTo/setPose`），无需重写，只需接到新事件上。

## 7. 回声抑制 / 自激：最重要的一环

扬声器就在阵列旁边，DOA 会把「人声」和「扬声器放出的内容」都当成声源，导致数字人**表演式
乱转**。抑制分三层（按代价递增）：

1. **TTS 播放状态强制冻结**（最廉价，先做）：后端维护一个 `tts_active` 标志，
   在对话 worker 的“正在合成 / 推给前端播放”期间置位。DOA 线程每帧查询：`tts_active == True`
   即**本轮不改角度**（沿用上次角度，或缓慢回正前方）。该标志与现有「回声自激」修复
   同一处维护，避免数字人被自己说的话带跑。
2. **能量门限 + 近场**：人声能量高于扬声器残余的阈值才下单；低估远端弱音也不误转。
3. **AEC 接入**（后续 / 依赖硬件路径）：不推荐在测向链路里混入系统 AEC 的估计信号——
   那会破坏 4 路原始相位差。主抑制就是第 1 条：**自己播放时不转**。

设计目标：**自己 TTS 播放 → 不转（或回正）**；发言间隙 → 转；人未说话 → 保上次方向。

## 8. WS 协议与前端接入（零侵入）

新增一条**服务端→前端**单向消息，频率低（约 5 Hz），不要求前端 ACK：

```json
{"type": "doa_azimuth", "az_deg": 35.0, "active": true, "ts": 1750000000000}
```

- `az_deg`：相对屏幕前方的水平方位角（度，逆时针为正，-90..+90 认为有效范围）。
- `active`：本帧是否检测到有效声源；false 时前端可保留上次方向或缓慢回正。
- `ts`：发送端时间戳（毫秒），前端据此丢弃明显迟到 / 乱序帧。

前端处理：放在 **`vision.js`**（已有「左下浮层」的习惯）或单独 `doa.js`。**不占用**对话 WS
队列，与 `interrupt/ping/profile_*` 一样为**旁路消息**，即使数字人正在说话也能实时响应
（§ 回声抑制除外）。

## 9. 已知风险与降级路线（写代码前先想清）

| 风险 | 表现 | 对策 |
|---|---|---|
| UAC1.0 Windows 4 声道采不上 | 无帧源 | FileSource 先跑通算法；再 sounddevice → pyusb 顺序试 |
| 声源在背面 | azimuth 前后对称模糊 | 数字人只转前方，demo 接受；文档注明 |
| 自己 TTS 引起误转 | 数字人乱摆动 | §7 三层抑制，先做 tts_active 冻结 |
| 混响 / 环境噪声 | 峰值抖动 | PHAT + 时域平滑 + SNR 门限加大 |
| 多说话人 | 指向合成中心 | 能量门限加权（后续） |
| 声卡采样格式限制 | 只有 48k / 单声道 | 采样率可配；必要时 48k 采集再 16k 抽取 |

## 10. 配置项（拟新增到 config.py，与 VISION_ 平级命名 DOA_）

```python
DOA_ENABLED       = os.getenv("DOA_ENABLED", "1") == "1"      # 总开关，默认开
DOA_SOURCE        = os.getenv("DOA_SOURCE", "file")           # file|sounddevice|pyusb
DOA_WAV_PATH      = os.getenv("DOA_WAV_PATH", "")             # file 源：4 声道 wav / 测试信号
DOA_FS            = int(os.getenv("DOA_FS", "16000"))
DOA_FRAME_MS      = int(os.getenv("DOA_FRAME_MS", "100"))
DOA_HOP_MS        = int(os.getenv("DOA_HOP_MS", "50"))
DOA_SNR_DB        = float(os.getenv("DOA_SNR_DB", "6.0"))    # 活动度阈值
DOA_FOV_X         = float(os.getenv("DOA_FOV_X", "60"))      # 屏幕水平视野（度）
DOA_FOV_Y         = float(os.getenv("DOA_FOV_Y", "30"))
DOA_PUSH_HZ       = float(os.getenv("DOA_PUSH_HZ", "5"))     # WS 下发频率
DOA_SMOOTH        = float(os.getenv("DOA_SMOOTH", "0.4"))    # 时域平滑系数 (0.1..0.9)
DOA_TTS_FREEZE    = os.getenv("DOA_TTS_FREEZE", "1") == "1"  # 自己说话时不转
```

（命名对齐现有 `VISION_` 风格；`DOA_SOURCE` 可实现不同源，回放即可离片验证。）

## 11. 验收标准（demo 级）

- [x] 离片：`AzimuthSweepSource` 合成各方位角信号，`estimator` 反解正确（符号对、误差≤12°，实测 max 7.5°）。
- [x] 离片：TTS 播放标志置 1 时，`estimator` 不更新角度（冻结生效，见 `check_doa.py`）。
- [ ] 真机：插上 ReSpeaker 4-Mic，`DOA_SOURCE=sounddevice/pyusb` 采集 4 路帧成功。
- [ ] 真机：对着阵列左侧 / 右侧说话，前端数字人眼珠 + 头转向对应方向。
- [ ] 降级：拔阵列 / Source 报错 → 子系统标记 error，对话链路不受影响。
- [x] `scripts/check_doa.py`（离片断言）已进 CI（`python-syntax` job）。

## 12. 本期范围（先做最小闭环）

- [x] 本文档（已提交）
- [x] `backend/doa/`：`model.py` + `source.py`(sweep/sounddevice) + `gccphat.py` + `estimator.py` + `service.py`（纯 numpy 测向闭环）
- [x] `server.py` 加旁路 WS `doa_azimuth` 下发 + `/api/doa/snapshot`（lifespan 启停）
- [x] `config.py` 补 DOA_* 配置（13 项，见 §10）
- [x] `frontend` 接 `doa_azimuth` → head gaze 转向（新增 `frontend/src/doa.js`，`main.js` 分发）
- [x] `scripts/check_doa.py`（离片断言）+ CI 步骤
- [ ] 真机 sounddevice 源 + TTS 冻结验证（待插阵列实测）

（后续：ODAS 升级 / pyusb 源 / AEC / 多说话人，均在 § 9 之外。）
