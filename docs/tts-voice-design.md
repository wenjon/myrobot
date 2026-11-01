# 自定义音色 TTS 设计（voice cloning / provider 抽象 / 流式）

> 状态：**P0 已落地（provider 抽象 + 本地音色注册表），P1/P2 仅设计**
> 配套：设计规格第 18 章（TTS 双后端）、第 5 章（WS 消息）、第 7 章（数字人表现）、
>       第 11 章（打断与收尾）、第 13 章（配置项）、第 21 章（可观测性）
> 目标：把「只能从微软音色库里挑一个」升级为「可注册任意音色、可克隆、可流式」，
>       且**对话主链路与前端契约不变**。

---

## 0. 一句话概览

现在 TTS 是 `edge-tts` 硬绑定：音色来自微软的固定清单，合成是「整句 mp3 + 词边界时间轴」。
本设计把合成能力抽成 **provider 接口**，音色抽成 **本地注册表**（`voices/<id>/`），
先接 Edge 保持现状，再接本地克隆模型（CosyVoice 2）与云端声音复刻，
对外仍是 `/api/tts` 同一份返回结构；流式和真口型对齐作为独立阶段叠加。

```
                    ┌──────────── backend/tts/ ────────────┐
  voices/<id>/      │                                      │
   ref.wav ────────▶│ voices.py  注册表/校验/试听          │
   meta.json        │        │                             │
                    │        ▼                             │
  /api/tts ────────▶│ registry.py  按 TTS_PROVIDER_ORDER   │
                    │        │       选 provider + 降级链   │
                    │        ├── edge.py       （现状迁移）  │──▶ {audio, marks, duration_ms}
                    │        ├── cosyvoice.py  （本地克隆）  │──▶ WS sentence / 音频流
                    │        └── cloud.py      （云复刻）    │
                    └──────────────────────────────────────┘
```

---

## 1. 「选定音色」到底指什么

同一个词在三种场景下需要三套实现，先分清，避免选错方案：

| 场景 | 描述 | 实现手段 | 工期 |
|---|---|---|---|
| A. 换预设音色 | 从已有音色库里挑一个更好听的 | 只改 `TTS_VOICE`，或前端调试面板切换 | 已完成 |
| B. 克隆指定音色 | 给 10~20 秒参考音频，合成同一个人 | 零样本克隆模型 / 云端声音复刻 | P1 |
| C. 训练专属音色 | 给 1 分钟以上数据，追求最高相似度 | GPT-SoVITS 等微调 | P2 |

本设计同时覆盖 B 与 C，但**默认走 B**：demo 阶段用 10~20 秒样本换音色，成本最低、可逆。

---

## 2. 现状诊断（基于代码事实）

### 2.1 现在的链路

| 位置 | 事实 |
|---|---|
| `backend/config.py:69-85` | `TTS_ENGINE` 只有 `edge` / `web` 两值；音色是 `TTS_VOICE=zh-CN-XiaoyiNeural` |
| `backend/pipeline/tts_edge.py` | `synthesize()` 返回 `{audio: base64 mp3, marks, duration_ms, segments, style}` |
| 同上 | 多段韵律靠 `prosody.plan()` 拆句 + `mp3_utils` 拼接，段间用静音帧补停顿 |
| 同上 | `list_voices()` 直接透传 `edge_tts.list_voices()`，**只能列微软音色** |
| `backend/server.py:305-345` | `/api/tts/config`、`/api/tts/voices`、`/api/tts` 三个端点是唯一出口 |
| `frontend/src/tts.js` | `speak()` 入队即预取；`speakEdge()` 用 `marks` + `requestAnimationFrame` 驱动口型 |
| `frontend/src/tts.js` | 失败自动降级 `web`（Web Speech），保证数字人不哑 |

### 2.2 为什么不能直接改 `tts_edge.py`

根因有三条，任一条都要求引入抽象层：

1. **依赖不同**：CosyVoice 需要 `torch` + 数 GB 权重，直接 import 进 FastAPI 进程会让启动慢一个量级，
   且模型 OOM 会连带整个对话服务崩溃。
2. **输出格式不同**：本地模型吐 PCM/WAV（16k/24k/48k），Edge 吐 mp3；前端 `<audio>` 两者都能放，
   但 `marks` 的来源不同（见 §7）。
3. **失败模式不同**：Edge 失败是网络/限流，本地模型失败是显存/进程；降级策略必须可配置，而不是写死 `try/except`。

结论：**provider 抽象 + 独立进程**，而不是在一个文件里堆 `if engine == ...`。

---

## 3. 选型对比

### 3.1 本地模型

| 方案 | 参考音频 | 中文自然度 | 相似度 | 流式 | 许可 | 结论 |
|---|---|---|---|---|---|---|
| **CosyVoice 2（0.5B）** | 3~10s 零样本 | 高 | 高 | 支持 | Apache-2.0（以仓库为准） | **P1 首选** |
| GPT-SoVITS v2/v4 | 1 分钟微调 | 高 | 最高 | 弱（按句） | MIT（以仓库为准） | P2，追求极致相似度 |
| F5-TTS | 10~15s 零样本 | 高 | 中高 | 一般 | 代码 MIT / 权重需复核 | 备选 |
| IndexTTS 2 | 5~15s | 高 | 高 | 支持 | 需复核 | 备选 |

选择 CosyVoice 2 的理由：中文韵律最稳、原生支持零样本克隆与 instruct 情感控制、
官方有流式推理接口，和本项目「分句流式 + 表情联动」的形态最匹配。

### 3.2 云端声音复刻

| 供应商 | 样本要求 | 接入方式 | 适用场景 |
|---|---|---|---|
| 火山引擎（豆包）声音复刻 | 10~20s | HTTP/WS，返回 PCM/MP3 | 不想碰显卡，追求稳定低延迟 |
| 阿里云 CosyVoice 复刻 | 10~20s | DashScope HTTP/WS | 与现有百炼 MaaS 账号可复用 |
| MiniMax / ElevenLabs | 10s 级 | HTTP 流式 | 多语种、音色库丰富 |

云端的价值不是「更便宜」，而是**把 AMD/Windows 的部署风险外包出去**，作为降级链的一环。

### 3.3 为什么不在浏览器端做

Web Speech 只有 SAPI 音色；浏览器端跑克隆模型需要下载数百 MB 权重且无法复用后端音色库，
与「一个后端服务、多端访问（手机/桌面/远程隧道）」的现状冲突。TTS 继续留在服务端。

---

## 4. 目标架构

### 4.1 模块划分

```
backend/tts/
  __init__.py
  base.py         # TTSProvider 抽象、TTSResult/TTSMark 数据结构、错误类型
  registry.py     # 按配置装配 provider，维护降级链，统一入口 synthesize()
  voices.py       # 本地音色注册表：扫描 voices/*/meta.json，校验、合并、试听
  edge.py         # 原 pipeline/tts_edge.py 迁移（行为不变）
  cosyvoice.py    # 本地 CosyVoice HTTP 客户端（模型跑在独立进程/WSL）
  cloud.py        # 云端声音复刻适配器（火山/阿里/MiniMax 各一个子类）
  protocols.py    # 帧协议：PCM 头、marks 对齐、采样率协商（P2）

backend/pipeline/tts_edge.py   # 保留为薄包装，避免外部引用断裂
backend/server.py              # 三个端点改为调用 registry
frontend/src/tts.js            # 增加 provider/voice 元数据，播放路径不变
voices/                        # 音色注册表（git 忽略 ref.wav，见 §6.3）
scripts/import_voice.py        # 从一段录音生成标准音色目录（P1）
scripts/check_tts.py           # 离片自检，接入 CI（P0）
```

### 4.2 为什么模型要独立进程

| 方案 | 启动耦合 | 故障隔离 | AMD/Windows 兼容 | 结论 |
|---|---|---|---|---|
| 直接 import 模型 | 高（+10~30s） | 无（OOM 拖垮服务） | 差 | 否 |
| 同机独立 HTTP 服务 | 低 | 好 | 好（可跑在 WSL 里） | **选定** |
| 子进程按需拉起 | 中 | 好 | 中 | 备选（冷启动慢） |

CosyVoice 服务对外只需两个接口：`POST /tts`（整句）、`POST /tts/stream`（P2 流式）。
主服务只依赖 `httpx`，没装模型也能正常启动并降级到 Edge。

---

## 5. Provider 契约（代码级）

### 5.1 数据结构

```python
@dataclass
class TTSMark:
    char_index: int       # 相对整句的字符下标（与现状一致）
    text: str
    offset_ms: int
    duration_ms: int

@dataclass
class TTSResult:
    audio: bytes          # mp3 / wav / pcm，由 mime + format 声明
    mime: str
    marks: list[TTSMark]
    duration_ms: float
    sample_rate: int      # PCM 时必填；mp3 可为 0
    channels: int
    provider: str
    voice: str
    segments: int
    marks_source: str     # "provider" | "estimated"
    timings: dict         # 首包/总耗时，供 /api/health 与日志使用
```

`TTSResult` 是现有返回字典的超集：P0 阶段 `registry.synthesize()` 仍返回旧字典
（`audio` 转 base64），保证 `frontend/src/tts.js` **零改动**。

### 5.2 抽象接口

```python
class TTSProvider(Protocol):
    name: str
    supports_stream: bool
    supports_clone: bool
    emotions: set[str]

    async def health(self) -> bool: ...
    async def list_voices(self) -> list[dict]: ...
    async def synthesize(self, req: TTSRequest) -> TTSResult: ...
    async def cancel(self, seq: str) -> None: ...   # 软打断时取消在途合成
```

- `health()` 用于启动时探活，不通过就跳过该 provider（不阻塞启动）。
- `cancel(seq)` 是打断链路的一部分：第 11 章的软打断要求「已发出的合成请求能撤回」，
  否则用户插话后旧音频仍会晚到并抢占播放队列。

### 5.3 装配与降级链

```python
# registry.py 伪代码
for name in TTS_PROVIDER_ORDER:          # 例：cosyvoice,cloud,edge
    provider = build(name)
    if await provider.health():
        return provider
return web_fallback                         # 前端 Web Speech
```

- 默认顺序 `cosyvoice,cloud,edge`；只配了 Edge 的机器行为与今天完全一致。
- 单次合成失败 → 记录 `provider_error` → 尝试链上下一个 → 全失败返回 `ok=False`
  （前端已有降级路径，不需要新逻辑）。

---

## 6. 音色注册表

### 6.1 目录结构

```
voices/
  xiaoyou/
    meta.json
    ref.wav          # 16k/24k、单声道、10~20s、无背景音乐
    ref.txt          # 可选：参考音频逐字文本，提升克隆稳定性
    preview.mp3      # 可选：试听片段
```

### 6.2 meta.json

```json
{
  "id": "xiaoyou",
  "display_name": "小柚",
  "locale": "zh-CN",
  "gender": "female",
  "source": "clone",
  "provider": "cosyvoice",
  "ref_audio": "ref.wav",
  "ref_text": "你好，我是小柚，很高兴见到你。",
  "tags": ["活泼", "少女"],
  "consent": {"type": "self", "note": "本人录制"},
  "license": "internal-demo",
  "created_at": "2026-11-02"
}
```

字段约束：

- `source`: `edge`（预设）/ `clone`（零样本）/ `trained`（微调）/ `cloud`（云端复刻）。
- `consent.type`: `self` / `authorized` / `public-figure`（后者默认拒绝加载）。
- `provider` 缺省时按 `source` 推断，允许单个音色覆盖全局 provider。

### 6.3 仓库策略

- `meta.json` / `ref.txt` / `preview.mp3` 入库。
- `ref.wav` **不入库**（体积 + 生物特征），`.gitignore` 忽略，用 `scripts/import_voice.py` 本地生成。
- 缺失 `ref.wav` 时该音色仍可列在 UI，但标记 `unavailable`，选择后自动回退到默认音色。

### 6.4 参考音频采集规范

| 项 | 要求 | 原因 |
|---|---|---|
| 时长 | 10~20s | 太短音色不稳，太长收益递减 |
| 采样率 | 24k 或 16k 单声道 | 与模型输入一致，避免二次重采样 |
| 内容 | 覆盖陈述句、疑问句、数字、儿化 | 音素覆盖不足会让克隆「只会念一种调」 |
| 环境 | 安静、无混响、无背景音乐 | 噪声会被当成音色特征学进去 |
| 音量 | 峰值 -6dBFS，不削波 | 削波会引入不可逆失真 |

### 6.5 音色选择接口

`GET /api/tts/voices` 合并三路来源：

1. `edge_tts.list_voices()`（`source=edge`）
2. `voices/*/meta.json`（`source=clone/trained/cloud`）
3. 云端返回的音色清单（`source=cloud`，带 `provider` 标签）

返回结构保持 `{ok, voices[]}`，每条新增 `id`、`source`、`available` 字段；
前端下拉框按 `source` 分组，旧字段（`name`/`locale`/`gender`）保持不变。

---

## 7. 流式与口型对齐

### 7.1 三个层次，不要一步跨到最难

| 层级 | 行为 | 首包延迟 | 改动面 | 阶段 |
|---|---|---|---|---|
| L0（现状） | 整句合成 → 前端排队播放 | 0.5~1.7s | 无 | 已完成 |
| L1 | LLM 每出一句就并发合成（已有预取） | 首句 0.5~1.7s，句间≈0 | 小 | 已完成 |
| L2 | provider 边合成边吐 PCM，前端流式播放 | 150~300ms（GPU） | 中 | P2 |

关键判断：**不需要为了「流畅」立刻上 L2**。现在句级流式已经把句间空档压到接近 0，
用户感知的卡顿主要来自首句和音色机械感。P1 换音色的收益大于 P2 上真流式。

### 7.2 L2 契约（P2）

```
POST /api/tts/stream
  body: {text, voice, emotion, style, seq}
  resp: application/octet-stream
        L16 PCM, 单声道, 采样率见 /api/tts/config 的 sample_rate
```

- 音频纯裸流，不混 JSON；`marks` 通过 WS 单独下发 `tts_marks`（带 `seq` 关联）。
- 采样率默认 24000（CosyVoice 原生），前端用 `AudioContext` 按 `sampleRate` 建缓冲，
  避免浏览器隐式重采样导致的音调偏移。
- 首个 chunk 必须 ≤ 300ms 产出，否则该 provider 自动降级到 L1。

### 7.3 口型对齐的两条路

| 来源 | 精度 | 适用 |
|---|---|---|
| `marks_source=provider` | 高（真实边界） | Edge、部分云端 |
| `marks_source=estimated` | 中 | 本地克隆模型不返回边界时 |

`estimated` 的做法：按音频能量包络切分音节（10ms 帧、自适应阈值），
再把音节映射到 `backend/pipeline/text_router.py` 的 viseme 表。
这比现在「按文本长度均分」准，也比引入强制对齐模型轻。

无论哪条路，前端接口不变：仍是 `onBoundary({char, time, elapsedSinceLast})`。

---

## 8. 情感与表情联动

现有链路：LLM 输出 `[表情:开心]` → 前端切表情 → 同一句的 TTS 请求带 `emotion=开心`
（`frontend/src/tts.js` 在入队时取当前情绪，服务端 `prosody.plan()` 转成 rate/pitch 偏置）。

换 provider 后保留这条链路，新增**情感能力矩阵**：

| 表情 | CosyVoice instruct | 云端 | Edge（现有） |
|---|---|---|---|
| 开心 | `用开心的语气说` | `happy` | rate +, pitch + |
| 悲伤 | `用悲伤的语气说` | `sad` | rate -, pitch - |
| 生气 | `用生气的语气说` | `angry` | rate +, pitch + |
| 惊讶 | `惊讶地` | `surprised` | rate +, pitch + |
| 疑惑 | `疑惑地` | `confused` | rate -, pitch + |
| 平静 | 空 instruct | `neutral` | 无偏置 |

provider 不支持某情绪时**静默降级为平静**，不报错、不打断播放。

---

## 9. 部署：AMD 显卡与 Windows

### 9.1 现实约束

- 官方 PyTorch ROCm 轮子只提供 Linux；Windows 原生不支持。
- `torch-directml` 可用，但 CosyVoice 等仓库的自定义算子兼容性差，不建议作为主路线。
- 因此本地克隆模型有三条可用路线：

| 路线 | 做法 | 优点 | 缺点 | 建议 |
|---|---|---|---|---|
| A. CPU 推理 | 直接 Windows + CPU | 零配置、稳定 | RTF 约 0.5~1.2，长句偏慢 | **先跑通** |
| B. WSL2 + ROCm | WSL 内跑 CosyVoice 服务，Windows 侧 `http://127.0.0.1:9880` 访问 | 能用上 AMD 显卡 | 需要 WSL 显存直通、驱动版本匹配 | P1 性能优化 |
| C. 云端复刻 | 不本地跑模型 | 延迟稳定、免显卡 | 按量计费、依赖网络 | 降级链兜底 |

### 9.2 进程拓扑

```
Windows
  ├─ backend/server.py        (FastAPI, 端口 8000)
  ├─ CosyVoice 服务           (可选；CPU 直跑或 WSL 内)
  └─ 浏览器 / 手机 / 隧道
```

主服务启动时探活 `TTS_COSYVOICE_URL/health`，不通就跳过，不影响对话。

### 9.3 性能预算（待实测校准）

| 环节 | 目标 | 实测方式 |
|---|---|---|
| 首包（CosyVoice GPU） | ≤ 300ms | `scripts/check_tts.py --bench` |
| 首包（CosyVoice CPU） | ≤ 1200ms | 同上 |
| 单句 20 字总耗时 | ≤ 2.5s | 同上 |
| 显存占用 | ≤ 4GB（0.5B 模型） | `nvidia-smi` / `rocm-smi` |

数值为设计目标，P1 落地后回填实测值（保留本节表格，不删）。

---

## 10. 配置项（拟新增，并入第 13 章）

| 变量 | 默认 | 说明 |
|---|---|---|
| `TTS_ENGINE` | `edge` | 兼容旧值；新增 `cosyvoice` / `cloud` |
| `TTS_PROVIDER_ORDER` | 空 | 降级链顺序；空时由 `TTS_ENGINE` 推导（`edge`→`edge`，`web`→空，其它→`<引擎>,edge`） |
| `TTS_VOICE_DIR` | `voices` | 音色注册表根目录 |
| `TTS_DEFAULT_VOICE` | 空 | 逻辑音色 ID 或 Edge 名称；空时沿用 `TTS_VOICE` |
| `TTS_COSYVOICE_URL` | `http://127.0.0.1:9880` | 本地模型服务地址 |
| `TTS_COSYVOICE_TIMEOUT_MS` | `8000` | 合成超时，超时即降级 |
| `TTS_CLOUD_VENDOR` | `` | `volcano` / `aliyun` / `minimax` |
| `TTS_CLOUD_KEY` | `` | 从环境变量读，禁止入库（第 14 章） |
| `TTS_CLOUD_VOICE_ID` | `` | 云端复刻后的音色 ID |
| `TTS_STREAM` | `0` | 1 = 启用 L2 流式 |
| `TTS_SAMPLE_RATE` | `24000` | L2 输出采样率 |
| `TTS_MARKS_SOURCE` | `auto` | `provider` / `estimated` / `auto` |
| `TTS_MAX_CHARS` | `200` | 单次合成上限，防止长文撑爆延迟 |

---

## 11. 降级与故障处理

### 11.1 降级阶梯

```
cosyvoice ──失败──▶ cloud ──失败──▶ edge ──失败──▶ 前端 Web Speech
   │                  │                │
   └─ 超时/显存不足    └─ 网络/配额      └─ 未安装/限流
```

### 11.2 错误分类

| 错误码 | 含义 | 处理 |
|---|---|---|
| `provider_unavailable` | 探活失败 | 跳过，不重试 |
| `synthesis_timeout` | 超过 `TTS_*_TIMEOUT_MS` | 取消请求，降级 |
| `empty_audio` | 返回空音频 | 视为失败 |
| `voice_not_found` | 音色 ID 不存在 | 回退默认音色并提示 |
| `consent_denied` | 音色未授权 | 拒绝加载，记日志 |

### 11.3 与软打断的配合

第 11 章的软打断要求「用户插话时立刻降低音量并收尾」。换 provider 后需补两件事：

- `cancel(seq)`：取消在途合成，避免旧音频晚到后重新入队。
- 播放侧渐弱不变（前端已有），provider 侧只需保证 **不再产出新 chunk**。

---

## 12. 安全与合规

- 克隆他人声音必须取得授权，`meta.json` 的 `consent` 字段为强制项。
- 公众人物音色默认拒绝加载（`consent.type=public-figure`）。
- 生成内容在 UI 上保留「AI 合成」标识（国内深度合成规定）。
- 云端 API Key 只从环境变量读取，沿用第 14 章的密钥管理约定。
- `ref.wav` 属于生物特征数据，不入库、不上传、不写入日志。

---

## 13. 验收与自检

### 13.1 离线自检 `scripts/check_tts.py`（已扩展）

| 用例 | 断言 |
|---|---|
| provider 装配 | 只配 edge 时，选中的是 edge，行为与现状一致 |
| 降级链 | cosyvoice 探活失败 → 选中 cloud；全失败 → `ok=False` |
| 音色注册表 | 合法 `meta.json` 可加载；缺 `ref.wav` 标记 `available=false` |
| 授权校验 | `public-figure` 被拒绝 |
| marks 单调性 | `offset_ms` 递增、`char_index` 覆盖全文 |
| 空文本 / 超长文本 | 分别抛 `TTSUnavailable` / 截断到 `TTS_MAX_CHARS` |
| 超时 | 模拟慢 provider，确认触发降级 |

接入 `.github/workflows/checks.yml` 的 `python-syntax` job，与 `check_doa.py` 并列
（现有 `check_tts.py` 只覆盖 mp3 帧解析与韵律，本设计新增的用例追加到同一文件）。

P0 已实际覆盖：帧解析、韵律规划、情绪表一致性、音色注册表（缺参考音频 /
公众人物 / 坏 JSON / `edge_voice` 映射）、provider 顺序推导、降级链、
本地与引擎音色合并、`TTSResult.to_legacy()` 返回契约。在线 Edge 合成
仍采用「失败只警告」策略，断网时不会让 CI 变红。

> 2026-11-02 实测：`edge-tts 7.2.8` 的 WSS 握手返回 403（凭证版本过期，
> 与业务代码无关）；升级到 `7.3.1` 后在线合成恢复。`requirements.txt`
> 的最低版本已同步抬高，排障时先看 `pip show edge-tts`。

### 13.2 端到端

1. 起 `backend/server.py`，`GET /api/tts/config` 返回新字段且不破坏旧字段。
2. `GET /api/tts/voices` 同时列出 Edge 与本地音色。
3. `POST /api/tts` 用本地音色合成，返回非空音频 + marks。
4. 前端播放，口型与音频同步，打断后 300ms 内静音。
5. 拔掉 CosyVoice 服务，重跑 3，确认自动降级到 Edge。

### 13.3 音色质量评估（P1）

- 主观：MOS ≥ 3.8（5 分制，3 人各 10 句）。
- 客观：说话人相似度（ECAPA-TDNN 余弦）≥ 0.75。
- 稳定性：同一文本合成 5 次，音高/语速方差不超过 8%。

---

## 14. 分阶段实施

### P0：抽象与注册表（不改音色，只换骨架）

- 新增 `backend/tts/{base,registry,voices,edge}.py`。
- `pipeline/tts_edge.py` 改为薄包装，`server.py` 三个端点改调 `registry`。
- `/api/tts/voices` 合并本地音色；返回结构向后兼容。
- 扩展 `scripts/check_tts.py`（provider / 注册表 / 降级链）+ CI。
- **验收：现有 Edge 行为、前端零改动、全部自检通过。**

### P1：本地克隆

- `cosyvoice.py` HTTP 适配器 + `/health` 探活。
- `scripts/import_voice.py`：录音 → 重采样/裁静音/校验 → 生成 `voices/<id>/`。
- 音频能量口型（`marks_source=estimated`）。
- 文档回填 §9.3 实测数据。
- **验收：10~20s 样本可注册并在对话中使用；拔掉服务自动降级。**

### P2：流式与云端

- `POST /api/tts/stream` + `tts_marks` WS 消息。
- `cloud.py` 首个供应商适配（火山或阿里）。
- 情感 instruct 映射落地（§8）。
- **验收：首包 ≤ 300ms（GPU/云），口型与音频偏差 ≤ 80ms。**

---

## 15. 风险与未决

| 风险 | 影响 | 缓解 |
|---|---|---|
| AMD 显卡在 Windows 上无法直接加速 | 本地克隆偏慢 | 先 CPU；性能不足再上 WSL2+ROCm 或云端 |
| 克隆相似度不达预期 | 用户体验落差 | 提供相似度评估脚本；P2 上 GPT-SoVITS 微调 |
| 模型许可证限制商用 | 法律风险 | 落地前逐仓库复核 LICENSE，文档记录版本号 |
| 流式 PCM 与口型不同步 | 嘴型错位 | L2 阶段先做 200ms 缓冲；必要时回退 L1 |
| 音色文件泄露 | 隐私风险 | `ref.wav` 不入库、不上传、日志脱敏 |

未决问题（等有结论再更新本节）：

- 云端供应商最终选哪家（取决于价格与中文自然度实测）。
- 是否需要「多音色切换」UI，还是固定一个默认音色。
- 是否引入说话人分离（多人场景下按人切换音色）。

---

## 16. 演进记录

- 2026-11-02：初版设计。确定 provider 抽象 + 本地音色注册表 + 独立模型进程；
  本地首选 CosyVoice 2，AMD 先走 CPU，云端作降级；流式定在 P2。
- 2026-11-02：P0 落地。新增 `backend/tts/{base,registry,voices,edge}.py`，
  `pipeline/tts_edge.py` 降为兼容薄包装；`/api/tts/config` 与 `/api/tts/voices`
  增加 provider / 音色字段且保持旧字段；`check_tts.py` 新增 5 组离线用例并接入 CI。
  同时把 `edge-tts` 最低版本抬到 7.3.1，修复旧凭证导致的 403。
