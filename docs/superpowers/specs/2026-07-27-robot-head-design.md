# 机器人头部交互式对话 — 软件模拟 Demo 设计规格

- 状态: Draft (Demo)
- 日期: 2026-07-27
- 作者: Codex (基于用户提供的《机器人头部交互式对话全链路技术方案（流式低延迟架构正式版）》)
- 关联方法论: superpowers/brainstorming → writing-plans

## 1. 背景与目标

用户提供了一份完整的机器人头部对话技术方案（全链路流式低延迟架构）。当前阶段**没有物理零件**，只做**纯软件模拟**，用于验证链路与交互体验。

本 Demo 目标：
- 打通对话主链路：`(可选)语音输入 → ASR → LLM → 文本解析中央调度 → TTS → 口型/表情驱动 → 浏览器虚拟头输出`。
- 体现原方案的核心思想：**全链路流式并行**、**边收边推边出**、**TTS 时间戳作为口型对齐基准时钟**、**动作指令与播报文本分流**。
- 用**浏览器里的 3D 数字人头像**替代物理伺服头部；伺服/时序补偿层留出接口占位。

非目标（本阶段不做）：
- 物理电机 / 伺服驱动 / AEC-AGC 硬件级音频前处理。
- ~~3D 拟人头（先做 2D 简版嘴型+表情，接口预留可升级 3D）。~~ **已完成**：已升级为 Three.js 3D 数字人（`avatar.glb`，ARKit 52 blendshape + Oculus viseme），详见第 7 章。
- 生产级性能优化、模型量化、NPU 部署。

## 2. 关键技术选型（Demo）

| 环节 | 方案 | 说明 |
|------|------|------|
| LLM | **三选一**：`ark`（火山引擎，云端，默认）/ `llamacpp`（本地 llama.cpp server，OpenAI 兼容）/ `ollama`（本地 `/api/chat`） | 供应商可切换（`config.LLM_PROVIDER`）；详见 2.1 |
| TTS | **Edge 神经语音**（`edge-tts`，服务端合成）+ 浏览器 Web Speech 兜底 | 神经语音自然度远高于本机 SAPI；返回逐字时间轴用于口型；失败自动降级，详见第 18 章 |
| 口型对齐 | Edge WordBoundary 逐字时间轴（或 Web Speech `boundary` 事件）→ 拼音/音素 → viseme | 前端实时驱动，符合“时间戳=基准时钟” |
| 虚拟头 | **Three.js 3D 数字人**（`avatar.glb`，GLTFLoader + 透视相机） | ARKit 52 blendshape 表情 + Oculus viseme 口型，WebGL 渲染 |
| ASR | 浏览器 **Web Speech Recognition**（可选）+ 文本输入兜底 | 无麦克风也能跑；后续可替换 faster-whisper |
| 后端 | **Python FastAPI + WebSocket** | 承载 LLM 流式转发 + 文本解析中央调度 |
| 通信 | WebSocket 全双工流式 | 边收边推边出 |

设计决策说明：
- **~~为什么 TTS 放前端~~**：初版因「本机 edge-tts 取不到音频」而把 TTS 放前端。**该结论已推翻**——问题实为 edge-tts ≥7 默认 `boundary="SentenceBoundary"`，一句只回一个 mark 显得「拿不到时间戳」；显式传 `WordBoundary` 即可。现默认走服务端 Edge 神经语音，Web Speech 保留为降级兜底。详见第 18 章。
- **为什么 3D 而不是 Wav2Lip/SadTalker/MuseTalk**：后者都依赖 NVIDIA CUDA（本机是 AMD 显卡，ROCm 不支持 Windows），而且它们是「整段音频 → 出 mp4」的**离线批处理**，与本方案的低延迟/可打断目标相冲。Three.js 方案纯 WebGL 渲染，无需 GPU 推理，实时驱动口型与表情，集显也能跑。

### 2.1 三个 LLM 供应商的取舍

| provider | 端点 | 默认模型 | 密钥 | 适用场景 |
|---|---|---|---|---|
| `ark` | `https://ark.cn-beijing.volces.com/api/coding/v3/chat/completions` | `ark-code-latest` | 需 `ARK_API_KEY` | 本机无强 GPU，要求回答质量 |
| `llamacpp` | `http://127.0.0.1:8080/v1/chat/completions` | `Qwen3.6-35B-A3B-MTP` | 默认无需（可选 `LLAMACPP_API_KEY`） | 本地推理、离线可用、不计费 |
| `ollama` | `http://127.0.0.1:11434/api/chat` | `gemma4:12b` | 不需 | 已装 Ollama 的环境 |

三者在 `llm_client.py` 里各有 `_stream` / `_once` / `_with_tools` 三个实现，
对外统一成 `stream_chat` / `chat_once` / `chat_with_tools`，**上层不感知供应商**。

#### Qwen3 思考模式开关（对语音对话很关键）

Qwen3 系列默认会先输出一大段**深度思考**（`reasoning_content`）再给正式答案。
这对写代码有利，但对**语音对话是灾难**——数字人会张着嘴呆等几秒才出声。

处理方式分两层：

1. **请求层关掉思考**：`ENABLE_THINKING=0`（默认）时在 payload 里传
   `chat_template_kwargs: {"enable_thinking": false}`，让模型直接作答。
   实测**首句延迟从 ~4s 降到 ~0.7s**。
2. **解析层忽略思考**：流式解析只取 `delta.content`，**丢掉 `reasoning_content`**。
   即使模型仍输出思考（某些构建不支持该参数），也不会被当成台词念出来。

> 需要高质量推理（如复杂问题）时可临时 `ENABLE_THINKING=1`，代价是首句变慢。

## 3. 系统架构

```
浏览器 (frontend)                        Python 后端 (backend)                LLM 供应商
┌────────────────────────┐           ┌─────────────────────────┐      ┌──────────┐
│ 麦克风/文本输入            │  WS 文本  │ /ws 对话端点              │ HTTP │ Ark      │
│  ├ Web Speech Recognition ─┼─────────▶│  ├ 队列 + 单 worker        │ SSE  │ /chat/   │
│  └ 文本框                  │           │  ├ turn_policy 轮次策略   ─┼─────▶│ completio│
│                            │           │  ├ agent 工具编排（两阶段）│      │ ns       │
│ tts.js（Web Speech 合成）  │ 分句/指令 │  │   · 探测→执行工具→回喂  │      └──────────┘
│  └ boundary 字级时间戳     │ ◀─────────┼─ ├ text_router 中央调度    │      ┌──────────┐
│ viseme.js（字→口型映射）  │           │  │   · 流式清洗/智能分句   │ HTTP │ llama.cpp│
│ head3d.js（Three.js 3D）   │           │  │   · 播报文本 vs 动作分流 │◀──────│ /v1/chat │
│  ├ ARKit 52 blendshape 表情│           │  ├ conversation 上下文管理 │（备选） └──────────┘
│  └ Oculus viseme 口型      │           │  └ 发送: sentence/action/  │
│                            │           │     status/interrupted    │      ┌──────────┐
└────────────────────────┘           └─────────────────────────┘      │ Tavily   │
                                                  ▲ tools/    │ HTTP │ /search  │
                                                  └──────────────┼─────▶└──────────┘
```

> 右侧 LLM 供应商是**三选一**（`LLM_PROVIDER` 切换）：图中画了 `ark`（默认）与 `llamacpp`，
> 第三种 `ollama`（`http://127.0.0.1:11434/api/chat`）接入位置与 `llamacpp` 完全一致，为保持图面简洁未重复绘制；
> 详见 § 2.1。

### 数据流（流式并行）
1. 用户文本/语音 → 前端经 WS 发送 `user_message`，服务端**入队**。
2. `turn_policy.classify_incoming` 判定本句是「打断 / 附和 / 新提问」，决定是否中止当前轮（详见第 11 章）。
3. `conversation.build_messages` 组装上下文：system（+摘要）+ 滑动窗口历史 + 本轮 user（详见第 16 章）。
4. `agent.agent_stream` 两阶段推理：先带工具清单探测，需要则执行工具（如 `web_search`）并回喂，最后**流式**生成答案（详见第 10 章）。
5. `text_router.route` 增量清洗（去 emoji/markdown/思考内容），**智能分句**，识别行内标记 `[表情:...]`/`[动作:...]`。
6. 每就绪一个短句 → 立刻经 WS 推给前端（`sentence`）；表情/动作用 `action` **提前下发**，使表情先于语音到位。
7. 前端 `tts.js` 向 `/api/tts` 取 Edge 神经语音音频 + 逐字时间轴（失败降级 Web Speech），播放时按 `currentTime` 查表触发 → `viseme.js` 算出口型权重 → `head3d.js` 驱动 blendshape 逐帧渲染。
8. 本轮正常结束→`commit_turn` 入库 + 下发 `llm_done`；被打断→不入库并下发 `interrupted` 让前端做自然收尾。

## 4. 模块划分

后端 `backend/`
- `config.py` — 全局配置（供应商 / 模型 / system prompt / 分句参数 / 上下文管理 / 工具框架 / 轮次策略 / 日志 等），全部可环境变量覆盖。
- `server.py` — FastAPI 装配 + HTTP 路由 + `/ws` 端点（队列与 worker）。单轮对话主循环已迁出到 `server_app/dialog.py`。
- `server_app/` — 从 server.py 拆出的辅助模块（职责单一）：
  - `__init__.py`，包描述
  - `logging.py` — 控制台 + 可选文件日志器（`ContextLogger`）、`log_context` / `log_output`。
  - `peers.py` — WS 客户端地址格式化（`format_peer`，IPv4/IPv6/转发头）。
  - `dialog.py` — 单轮对话主循环（`run_dialog`：LLM → 中央调度 → WS 推送 → commit/rollback）。
  - `notify.py` — 服务端主动下发辅助消息（如 `interrupted`）。
- `pipeline/` — 业务管线：
  - `llm_client.py` — LLM 流式客户端（Ark 与 Ollama 两套，`stream_chat` / `chat_once` / `chat_with_tools`）。
  - `text_router.py` — 中央调度：清洗 / 智能分句 / 动作抽取。
  - `conversation.py` — 多轮上下文与记忆管理（P0 安全提交 + P1 滑动窗口 + 反思沉淀 + TTL 回收）；模块层持有全局单例 `conversations`。
  - `agent.py` — 工具调用编排（两阶段：探测/工具循环 → 流式最终答）。
  - `turn_policy.py` — smart 轮次策略分类（`classify_incoming`：interrupt / backchannel / question）。
- `tools/` — 插件式工具框架（详见第 10 章）：
  - `base.py` — `Tool` / `Permission` / `ToolContext` / `ToolResult` 抽象。
  - `registry.py` — 全局 `REGISTRY`（注册 / 权限阈门 / 超时 / 日志）与 `@tool` 装饰器。
  - `loader.py` — 启动时自动扫描子包（builtin/web/document/database/hardware）完成注册。
  - `context.py` — `RESOURCES`（跨工具复用的资源池，当前仅 httpx.AsyncClient）。
  - `builtin/basic.py` — `get_time` / `echo`（零依赖）。
  - `web/web_search.py` — `web_search`（Tavily）。
  - `schema.py` — 从函数签名 + docstring 自动推导 JSON Schema（见 10.7）。
- `memory/` — 记忆模块（详见第 16 章）：
  - `types.py` — `MemoryQuery` / `MemoryHit` / `MemoryRetriever` / `MemoryKind` 抽象。
  - `retriever.py` — `AllMemoryRetriever`（全量拼接，画像优先于摘要）。
  - `profile.py` — `UserProfile`（三字段画像）与 `ProfileConflict`（B 方案冲突）。
  - `store.py` — `FileStore`（原子写 + 坏文件降级）与全局单例 `STORE`。

前端 `frontend/`
- `index.html` — 页面（画布 + 输入区 + 状态）。
- `src/ws.js` — WebSocket 客户端。
- `src/tts.js` — 双后端语音合成：Edge 神经语音（`/api/tts`，逐字时间轴 + 预取）/ Web Speech 兜底，含 `softStop`（渐弱软停）。详见第 18 章。
- `src/viseme.js` — 字/拼音 → viseme 权重映射。
- `src/head3d.js` — Three.js 加载 GLB（ARKit 52 blendshape + Oculus viseme）：18 种表情 / 口型 / 注视与眨眼 / 颈骨分层转头 / 面部叠加动作 / 自发微动 / 「我在听」倾听表情。详见第 7 章。
- `src/main.js` — 装配与事件流（含 `enterListening`）。
- `src/profile_card.js` — 画像冲突确认卡 UI（旧值删除线 / 新值高亮 / 提炼原句 / 确认与拒绝）。

## 5. 接口契约（WebSocket JSON 消息）

以下是完整定义（共 6 种 C→S、10 种 S→C），部分是后期增量添加的。

客户端 → 服务端:
- `{"type":"hello","session":"<sid>"}` — 必须首发；`session` 为空则服务端分配新 sid。未发 hello 前的其他帧会被忽略（保证不会错误绑定到别人的会话）。
- `{"type":"user_message","text":"...","session":"<sid>"}` — 发送一句用户输入。
- `{"type":"interrupt"}` — 手动打断（点「打断」按钮）：立即中止当前轮 + 清空队列。不会触发「自然收尾」。
- `{"type":"clear"}` — 打断 + 清队 + 清空该 sid 的会话历史。
- `{"type":"ping"}` — 心跳；服务端不回复但会记日志。
- `{"type":"profile_resolve","conflict_id":"<cid>","action":"accept|reject"}` — 用户对画像变更拍板（第 16 章）。**不进对话队列**：它只改记忆、不触发 LLM，即使数字人正在说话也能立即处理。

服务端 → 客户端:
- `{"type":"session","session":"<sid>"}` — 对 hello 的回应，供前端写入 localStorage 以持久化 sid。
- `{"type":"sentence","text":"这是一句播报","seq":0}` — 中央调度分句后的一句播报文本。
- `{"type":"action","action":"表情|动作","value":"开心"}` — 提前下发的表情/动作指令，让前端表情与语音同步。
- `{"type":"status","text":"正在联网搜索…"}` — 工具执行状态（如 web_search），让前端状态栏显示。
- `{"type":"llm_done"}` — 本轮正常结束（已入库）。被打断不发。
- `{"type":"error","message":"..."}` — LLM/网络/工具等异常。
- `{"type":"cleared"}` — 对 clear 的确认。
- `{"type":"interrupted","reason":"question|interrupt|always"}` — 自动 barge-in 时下发（仅 `INTERRUPT_MODE=always/smart` 触发），前端做自然收尾（渐弱 + 「我在听」倾听表情）。详见第 11 章。
- `{"type":"profile_conflict","conflict_id":"<cid>","field":"name","field_label":"称呼","old_value":"小王","new_value":"老王","source_quote":"以后叫我老王"}` — 画像变更需人工确认（第 16 章 B 方案）。**在本轮 `llm_done` 之后才下发**，避免确认卡打断正在进行的播报。
- `{"type":"profile_resolved","conflict_id":"<cid>","action":"accept","ok":true}` — 对 `profile_resolve` 的确认。`ok=false` 表示 conflict_id 未知或已超时；**无论成败都会回这一帧**，让前端能销毁卡片而不致于永久悬着。

协议小结：
- 连接必须先 `hello` 后才能处理其他帧；
- 同一个 sid 可以被多个连接共享（不同设备接入）；
- 会话隔离在 sid 层，不在 ws 连接层；为了上下文不串用户，前端需每个会话独立生成 sid。

LLM 输出约定（system prompt 引导）:
- 短句口语化；可用行内标记表达动作，如 `[表情:开心]` / `[动作:点头]`。完整清单见 7.2 / 7.3。
- 中央调度（text_router.route）负责把标记剥离成 `action` 消息，纯文本作为 `sentence`。
- 涉及实时 / 不确定内容需优先调用 web_search 进行联网查证。

## 6. 分句算法（要点）
- 按标点（。！？；，、…）与长度阈值（默认 12–24 字）做均衡切割。
- 不足阈值且未遇强标点时缓冲，遇强标点或超长即 flush。
- 流结束时 flush 残留缓冲。

## 7. 口型/表情映射（3D blendshape）

### 7.1 口型（viseme）
- 链路：TTS 字级时间戳（Edge WordBoundary 展开逐字 / Web Speech `boundary`）→ `viseme.js` 求出目标 viseme → `head3d.js` 驱动 Oculus viseme blendshape。
- 中文按拼音韵母粗分口型类别：`a`(大张)、`o/u`(圆唇)、`e/i`(扁平)、`闭合`(m/b/p/静音)。
- 无拼音库时用字符哈希兜底伪口型，保证动画连续（demo 级，非真音素级）。
- 对外接口：`visemeForChar(ch)` / `visemeForText(text)` / `CLOSED`。

### 7.2 表情（ARKit blendshape 组合）
`head3d.js` 的 `EXPR` 表定义了 **18 种**表情，每种是一组 blendshape 权重的叠加。
表情是**持续状态**，一直保持到下一次 `[表情:x]` 切换。

| 表情 | 主要 blendshape |
|---|---|
| 平静 | （全零，基准态） |
| 开心 | `mouthSmile*` 0.8、`mouthDimple*` 0.4、`cheekSquint*` 0.4、`eyeSquint*` 0.2 |
| 悲伤 | `mouthFrown*` 0.7、`browInnerUp` 0.7、`mouthShrugLower` 0.3 |
| 生气 | `browDown*` 0.9、`noseSneer*` 0.5、`mouthPress*` 0.5、`jawForward` 0.2 |
| 惊讶 | `browInnerUp` 0.8、`browOuterUp*` 0.7、`eyeWide*` 0.8、`jawOpen` 0.35 |
| 疑惑 | `browInnerUp` 0.5、`browOuterUpLeft` 0.6、`mouthLeft` 0.4、`jawLeft` 0.1 |
| 害羞 | `mouthSmile*` 0.45、`cheekSquint*` 0.6、`eyeSquint*` 0.4、`mouthShrugUpper` 0.2 |
| 调皮 | `mouthSmileLeft` 0.75 / `Right` 0.3（不对称坏笑）、`tongueOut` 0.25 |
| 无语 | `mouthShrugUpper` 0.6、`mouthClose` 0.25、`mouthRight` 0.2、`jawRight` 0.1 |
| 思考 | `browOuterUpLeft` 0.55、`mouthRollLower` 0.45、`mouthLeft` 0.25 |
| 尴尬 | `mouthStretch*` 0.6、`browInnerUp` 0.55、`eyeSquint*` 0.45（苦笑） |
| 得意 | `mouthSmile*` 0.7、`jawForward` 0.3、`browDown*` 0.25 |
| 委屈 | `mouthFrown*` 0.6、`browInnerUp` 0.85、`mouthPucker` 0.4 |
| 惊恐 | `eyeWide*` 1.0、`browInnerUp` 0.95、`jawOpen` 0.6、`mouthStretch*` 0.4 |
| 厌恶 | `noseSneer*` 0.8、`mouthUpperUp*` 0.6/0.45、`eyeSquintLeft` 0.55 |
| 困倦 | `eyeBlink*` 0.55（半闭）、`jawOpen` 0.2、`mouthLowerDown*` 0.2 |
| 撒娇 | `cheekPuff` 0.6、`mouthPucker` 0.65、`browInnerUp` 0.5 |
| 期待 | `eyeWide*` 0.55、`mouthSmile*` 0.55、`browInnerUp` 0.45、`jawOpen` 0.12 |

- 由 `action` 消息（`{"type":"action","action":"表情","value":"开心"}`）驱动，**插值过渡**避免突变。
- 切表情时先把 `EXPR_KEYS` 全体归零再套新值，避免上一个表情残留（如「惊恐」切「平静」眼睛还睁着）。
- 额外的「我在听」倾听表情（`setListening`）不属于上表，是被打断时的自然收尾，详见 11.4。

### 7.3 动作系统（非 blendshape 表情的其余能力）
除表情外还有三类**一次性动作**，由 `[动作:x]` 触发，`main.js` 的 `ACTION_MAP` 表驱动分发：

| 类别 | 动作 | 实现 |
|---|---|---|
| 头颈 | 点头、摇头、歪头 | 正弦包络叠加到 `Neck`(60%) + `Head`(40%) 两根骨骼的欧拉角 |
| 注视 | 看左、看右、看上、看下、环视、对视 | 8 个 `eyeLook*` blendshape + `LeftEye`/`RightEye` 骨骼微转，头颈随后跟上 |
| 眨眼 | 眨眼、眨左眼、眨右眼 | `eyeBlinkLeft/Right`，单眼眨（wink）时只压一侧 |
| 面部叠加 | 挑眉、单挑眉、皱眉、鼓腮、撅嘴、吐舌、咬唇、努嘴 | `OVERLAYS` 表，在当前表情基准值之上做 `0 → peak → 0` 的正弦包络，结束后回落到基准 |

三个设计要点：

1. **分层而非覆盖**：注视系统独占 `eyeLook*`，眨眼独占 `eyeBlink*`，表情用其余 42 个，
   叠加动作则记录 `exprBase` 后做增量相加。任何两层同时生效都不会互相抹掉。
2. **转颈骨而不是转 root**：早期实现旋转整个 `root`，视觉上是「整个人歪了」；
   现在按 6:4 分给 `Neck`/`Head`，才是「人在转头」。所有旋转都基于 `rest` 静止姿态做增量，不会累积漂移。
3. **无指令时的自发微动**：随机扫视（saccade，1.2~3.8s 一次）+ 头部低频摆动 + `Spine1` 呼吸起伏（约 15 次/分）。
   这三项与语义无关，但去掉了「雕像感」，是性价比最高的「活着」感来源。

### 7.4 覆盖率与自定义边界
`avatar.glb` 共 **67 个 blendshape** = 15 viseme + 52 ARKit，另有 67 根骨骼、0 个预制动画
（Ready Player Me 标准骨架，动作全部程序化生成）。当前 **52/52 个 ARKit blendshape 已全部用上**。

新增一种表情/动作需**同步三处**，否则前端会把标记降级丢弃：

| 文件 | 位置 | 作用 |
|---|---|---|
| `frontend/src/head3d.js` | `EXPR` / `OVERLAYS` | blendshape 组合定义 |
| `frontend/src/main.js` | `EMOTION_SET` / `ACTION_MAP` | 白名单与分发（不在名单内降级为「平静」） |
| `backend/config.py` | `SYSTEM_PROMPT` | 告知 LLM 有哪些标记可用 |

`index.html` 的 `#exprTest` 面板会自动列出全部表情与动作按钮，改完可直接点按钮肉眼验证，无需让模型配合。

## 8. 验收标准（Demo 级）

### 8.1 基础对话链路
- 输入一句中文，数字人能**流式**逐句播报并做口型动画，首句可见延迟主观“较快”。
- 口型与语音大致同步（demo 级，不追求 ≤120ms 硬指标）。
- LLM 输出的 `[表情:x]` / `[动作:x]` 能驱动 18 种表情与头颈/注视/眨眼/面部叠加动作。
- TTS 默认用 Edge 神经语音（服务端合成）；断网或未装 `edge-tts` 时自动降级浏览器 Web Speech，**不会哑掉**。

### 8.2 工具调用（第 10 章）
- 问「现在几点」能调 `get_time` 并用口语回答。
- 问时事/实时信息能自动调 `web_search` 并基于搜索结果作答，期间下发 `status` 提示「正在联网搜索…」。
- 工具循环不超过 `TOOL_MAX_ROUNDS`（默认 3），不会无限搜索。
- 背诵长文类请求**不会误用工具**当「说话通道」（`echo` 已降为 DANGEROUS 不对 LLM 暴露）。

### 8.3 轮次策略与打断（第 11 章）
- 点「打断」按钮能立即硬停当前播报（不做收尾）。
- `INTERRUPT_MODE=smart` 下：说「嗯嗯」「对」等**附和词不会打断**；说「等等」或抛新问题**会中止当前轮**。
- 自动 barge-in 时前端做**自然收尾**：渐弱余音 + 「我在听」倾听表情，而不是硬切。
- 被打断的残句**不入库**，不污染后续上下文。

### 8.4 上下文与记忆（第 16 章）
- 连续对话能记住前文（如先说「我叫小王」，后问「我叫什么」能答对）。
- 刷新页面 / 重连后（同一 `session`）上下文不丢。
- 不同 `session` 之间上下文**不串**。
- 超出窗口的老历史会被摘要券（`summary`）而不是直接丢弃。
- 服务端控制台能打印每轮的**实际上下文**与模型输出，带会话/连接标识便于调试。
- **进程重启后**同一 `session` 的摘要与用户画像仍在（落盘到 `backend/data/memory/`）。
- 说「以后叫我老王」且旧值不同时，前端弹**确认卡**；未确认前一律**保旧**。
- 确认卡的下发**不会打断**本轮播报（在 `llm_done` 之后）；点确认/拒绍**不会触发 LLM**。
- `python scripts/check_memory.py` 全部断言通过（已接入 CI）。

### 8.5 工程质量
- `python -m compileall backend/` 无语法错；`scripts/check_no_hardcoded_secrets.py` 返回 0。
- 代码中**无硬编码密钥**，密钥均从 `.env` 读取（第 14 章）。
- GitHub Actions 三个 job 全绿（第 15 章）。

## 9. 后续可扩展（对齐原方案）

已完成（保留演进痕迹）：
- ~~2D 头升级 Three.js 3D + ARKit 52 BlendShape。~~ **已完成**，详见第 7 章。
- ~~工具调用（function calling）与联网搜索。~~ **已完成**，详见第 10 章。
- ~~多轮上下文管理与打断安全。~~ **已完成**，详见第 11 / 16 章。
- ~~记忆持久化与用户画像落地。~~ **已完成**，详见第 16 章（三层存储 + Profile 冲突人工确认 + 文件持久化）。

待做：
- ~~TTS 换 Piper/本地音素引擎~~ 已改为 Edge 神经语音（第 18 章）；若要完全离线 + 真音素级时间戳，仍可考虑 Piper（当前是拼音粗分 + 字级时间戳）。
- ASR 换 faster-whisper 流式，后端做 VAD/AEC（当前依赖浏览器 Web Speech Recognition）。
- 加入伺服时序补偿模块（当前为接口占位，无物理硬件）。
- 全局时钟对齐、智能降级策略。
- 双模型路由（简单问题走快模型 / 复杂推理走强模型），待设计。


## 10. 工具调用框架（tools / function calling）

让 LLM 具备「联网搜索、处理文档、读数据库、调用硬件 API」等能力。核心原理：LLM 不能直接执行动作，只能**输出结构化调用请求**，由后端真正执行并把结果回喂，LLM 再据此作答。

### 10.1 设计目标
- 插件化：新增工具 = 新建一个文件放进 `tools/<分类>/`，自动被发现，**不改核心链路**。
- 统一契约：不论底层是 HTTP / SQL / 串口，对 LLM 都是 name + JSON Schema。
- 分级权限：`read` / `write` / `dangerous`，执行前经安全闸门；默认只放开 `read`。
- 资源复用：DB 连接池 / HTTP client / 串口句柄由 `ResourceManager` 懒加载并复用，关闭时统一释放。
- 可观测：超时、异常隔离、调用日志（复用 `[#连接号]` 前缀）。

### 10.2 目录结构
```
backend/tools/
  base.py       # Tool 抽象基类 / ToolCategory / Permission / ToolResult / ToolContext
  registry.py   # ToolRegistry：注册、按分类/权限导出 schema、执行分发；@tool 装饰器
  schema.py     # 从函数签名 + docstring 自动推导 JSON Schema（见 10.7）
  loader.py     # 启动时扫描子包自动注册（插件发现）
  context.py    # ResourceManager：共享 HTTP client / 未来 DB/串口
  builtin/basic.py    # 零依赖工具：get_time / echo
  web/web_search.py   # 联网搜索（Tavily）
  # 未来：document/ database/ hardware/ 按同一 Tool 契约添加
```

### 10.3 编排（pipeline/agent.py，两阶段）
- 阶段一（工具循环，非流式）：`chat_with_tools` 带工具清单请求 LLM；
  若返回 `tool_calls` 则执行工具→结果作为 `role:tool` 回喂→再探测，最多 `TOOL_MAX_ROUNDS` 轮。
- 阶段二（最终答，流式）：基于工具结果调用 `stream_chat`，复用现有分句/表情/口型链路。
- 若 LLM 一开始就直接作答（无工具），直接产出该答案，省一次调用。
- `ENABLE_TOOLS=0` 时行为完全退回原纯流式。

### 10.4 新增 WS 消息
- 服务端 → 客户端：`{"type":"status","text":"正在联网搜索：xxx"}`（工具执行时提示，前端显示在状态栏）。

### 10.5 相关配置（config.py）

`backend/config.py` 中的环境变量，分组列出（第 13 章会给出「所有环境变量总览」，这里只列与工具相关的）：

- `ENABLE_TOOLS`（默认 1）：总开关。设为 0 则走原有流式链路（不调工具）。
- `TOOL_MAX_ROUNDS`（默认 3）：工具循环最多轮次，防止无限调用。
- `TOOL_MAX_PERMISSION`（默认 `read`）：可暴露给 LLM 的最高权限；`write`/`dangerous` 需显式提升。
- `TAVILY_API_KEY` / `TAVILY_URL`：联网搜索后端。

### 10.6 联网搜索检索参数（web_search / Tavily 调优）
位置：`backend/tools/web/web_search.py`。这些参数决定「回喂给 LLM 的搜索内容有多全」，
直接影响长文/要点类问答的质量，也影响模型是否会「嫌不完整而反复搜」。

- `search_depth`（当前 `advanced`）：
  - `basic`：只返回短摘要片段，快但内容少，长文（如古文全文）只能拿到开头。
  - `advanced`：返回更长、更相关的正文片段，慢约 1~2 秒，但完整度明显更高。
- `PER_ITEM_MAX`（当前 1500）：每条结果保留的正文字符上限。
  之前为 300，导致长文被硬截断成开头片段，模型误判「没拿全」→ 换词反复搜直至轮次耗尽。
- `TOTAL_MAX`（当前 6000）：所有结果合计字符上限，防止多条结果撑爆上下文预算
  （需与 `MAX_CONTEXT_CHARS` 协调，避免工具结果挤占对话历史）。
- `max_results`（由工具入参 `top_k` 控制，默认 5，最多 10）。

调优经验：
- 「反复搜同一主题」通常不是模型任性，而是回喂内容被截断/不足——优先调大 `PER_ITEM_MAX`
  或提高 `search_depth`，而非只加大 `TOOL_MAX_ROUNDS`。
- 长文全文（诗词/条款原文）本质不适合搜索工具，`advanced`+1500 也只能覆盖较长片段；
  若要精确全文，宜内置「经典文本库」类工具。
- 想更省 token/更快：回退 `basic` 并把 `PER_ITEM_MAX` 调到 500~800。
- 想更全（消耗更大）：可加 Tavily `include_raw_content=true` 取网页原始正文。

### 10.7 新增工具：两种写法

#### 写法 A：自动推导 schema（推荐，适用于绝大多数工具）

`@tool` 的 `name` / `description` / `parameters` 三项均可省略，
省略时由 `tools/schema.py` 从函数自身推导：

```python
@tool(category=ToolCategory.SYSTEM, permission=Permission.READ)
async def get_time(timezone: str = "") -> str:
    """获取当前的日期和时间。当用户询问现在几点、今天日期、星期几时使用。

    Args:
        timezone: 时区名（可选），如 Asia/Shanghai。默认使用服务器本地时间。
    """
```

推导规则：

| schema 字段 | 来源 |
|---|---|
| `name` | 函数名 `func.__name__` |
| `description` | docstring **首段**（到第一个空行为止；后续补充段不计入，避免长描述干扰选工具） |
| `properties[x].type` | 参数类型注解：`str`→string、`int`→integer、`float`→number、`bool`→boolean、`List[str]`→array+items、`dict`→object |
| `properties[x].description` | docstring 的 `Args:` 段里对应行（支持 `name (str): 说明` 与多行接续） |
| `required` | **没有默认值**的参数（有默认值则为可选） |

几个细节：
- `Optional[X]` / `Union[X, None]` 会剥掉 `None` 取 `X`；枚举类转为 `string` + `enum` 候选值。
- **`ctx` / `self` / `cls` 自动忽略** —— 它们是框架注入的，不是 LLM 可填参数；
  函数是否需要 `ctx` 在**注册时反射一次并缓存**（`_FunctionTool._wants_ctx`），
  不再每次调用都跑 `inspect.signature`。
- `*args` / `**kwargs` 无法表达为 schema，会被跳过。
- **忘写 docstring 会在启动时直接报 `ValueError`**：描述为空的工具 LLM 根本不知道何时该调，
  属于必须尽早暴露的错误，不能默默注册一个永远不会被调用的工具。

#### 写法 B：手写 schema 覆盖（复杂结构时）

嵌套对象、数组元素是对象、手写 `enum` 限定值域……这些推不出来，
显式传 `parameters=` 即可，与以前完全一样：

```python
@tool(name="db_query", description="...", parameters={
    "type": "object",
    "properties": {
        "table": {"type": "string", "enum": ["users", "orders"]},
        "filters": {"type": "object", "properties": {}},
    },
    "required": ["table"],
})
```

**两种写法长期共存**，不强要求统一。推导仅在对应项**未传**时生效，
显式传了就用你写的；部分覆盖也合法（比如只覆盖 `parameters`、让 `description` 走推导）。

#### 为什么要做推导

手写 schema 有两个真实代价：

1. **样板量大**：`get_time` 原本 20 行里有 14 行是 schema，真正的逻辑只 3 行。
2. **会写歪且发现得极晚**：schema 里叫 `query`、函数参数叫 `q`，注册时不报错，
   要等到**用户真的触发那个工具**才在 `self._func(**args)` 处炸 `TypeError`。

第 2 点尤其阴险，所以额外配了一个自检脚本（已接入 CI，见 § 15.2）：

```
python scripts/check_tool_schemas.py
```

它对注册表里的每个工具做 5 项校验：
1. schema 声明的参数在函数里真存在（除非函数收 `**kwargs`）；
2. 函数的必填参数（无默认值）都在 `required` 里；
3. `required` 里的参数都在 `properties` 里有定义；
4. `name` / `description` 非空且 `parameters.type == "object"`；
5. 每个参数都有 `description`（**仅警告**，不阻断）。

这个脚本对两种写法都生效 —— 推导的天然一致，手写的靠它卡住。

#### 实施时的四个步骤

1. 在 `tools/<分类>/` 新建 `.py`，用 `@tool(...)` 装饰函数或继承 `Tool` 类；
2. 写清 docstring 首段（何时用 / 不该用）与 `Args:` 段；复杂结构才手写 `parameters=`；
3. 有状态资源放进 `ResourceManager`，通过 `ctx.resources` 获取；
4. 若在新分类目录，在 `loader.py` 的 `_TOOL_PACKAGES` 登记一次。

各类工具的额外注意事项：
- 文档类：解析后先切块/检索，只回喂相关片段，避免撑爆上下文。
- 数据库类：建议只读 + 参数化/白名单，防注入。
- 硬件类：标 `dangerous`，默认被闸门拦截，需显式提升权限或加确认流程。

### 10.8 供应商兼容性
- Ark（OpenAI 兼容）：已验证支持 `tools`（`ark-code-latest` 实测可用）。
- Ollama：`/api/chat` 支持 `tools`，但仅部分模型（qwen2.5 / llama3.1 等）；gemma 系列较弱。
- 兜底：不支持原生 tools 的模型可改用 ReAct 提示词模式，后端正则解析动作。

## 11. 对话轮次策略与自然收尾（turn-taking / barge-in）

### 11.1 问题
数字人正在说话时，用户又发了一句，到底应该“继续说 / 立即停 / 其它”？
硬打断（来一句就戳断）会丢失未开口那轮的上下文；完全排队又不能及时响应“别说了”。

### 11.2 三种模式（config.INTERRUPT_MODE）
- `queue`：排队依次说，只有点“打断”按钮才中止（最保守）。
- `always`：硬打断，任何新消息都立即中止当前播报。
- `smart`（默认）：先分类再决定，兼顾反应速度与上下文完整。

### 11.3 smart 分类（pipeline/turn_policy.py）
`classify_incoming(text)` 返回三类，只用轻量规则（关键词 + 长度），零延迟：
- `interrupt`：命中 停/别说了/不对/换个话题 等意图词 → barge-in。
- `backchannel`：嗯/对/好的/哈哈 等应声词（整句即一个词且较短）→ 不打断、继续说。
- `question`：其它需要正经回答的内容 → barge-in。
接口稳定，以后可无缝替换为小模型分类器。

### 11.4 自然收尾（“被打断”体验）
区分两种中止：
- 手动打断（`interrupt` 按钮）：用户主动要求立即安静 → 前端 `cancel()` 硬停，不做渐弱。
- 自动 barge-in（always / smart 判定 interrupt|question）：服务端额外下发 `interrupted`，前端做人性化收尾：
  1) `tts.softStop()`：丢掉后续排队句，给当前句一个 ~220ms “说完余音”窗口再 cancel（模拟渐弱拖尾）；
  2) `head.setListening(true)`：切“我在听”倾听表情（眉根微抬 + 双眼微睁 + 头部轻微侧倾）；
  3) 新一轮 `sentence` 到达时自动 `setListening(false)`；兜底 1.5s 后自动退出倾听。
注：Web Speech API 无法中途改音量，“渐弱”实为短延迟 cancel 的近似实现。

### 11.5 上下文处理
被打断那轮已产出的文本仍写入会话历史（部分回答）；附和词不入队也不打断，仅日志记录。

### 11.6 新增 WS 消息
- 服务端 → 客户端：`{"type":"interrupted","reason":"question|interrupt|always"}`。


## 12. 服务装配与生命周期（lifespan / app boot）

### 12.1 启动顺序（按时间）
1. `python -c "import server"` → 加载所有模块（pipeline / tools / server_app），触发装饰器注册工具。
2. `config.LOG_CONTEXT` 为真 → 打开上下文日志文件。
3. `REGISTRY.set_logger()` + `load_all()` → 工具框架接入日志，启动时扫描子包自动注册所有工具。
4. 启动 `uvicorn` → 应用进入“接受请求”状态。

### 12.2 生命周期钩子（FastAPI lifespan）
代码在 `server.py` 顶部（`FastAPI(...)` 之前）：

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        yield                       # 应用运行期
    finally:
        await REGISTRY.teardown_all()  # 逐个调工具的 teardown()
        await RESOURCES.aclose()       # 关闭共享资源（httpx client 等）
```

为什么不用 `@app.on_event("shutdown")` 了？
- 该接口从 FastAPI 0.93 弱化，上游 Starlette 也公告将移除；
- `lifespan` 是推荐替代，一个函数同时表达 startup + shutdown，代码更集中。

扩展点：未来可在 `try: yield` 之前加任务（如预热连接池、定时任务、预加载模型等）。

### 12.3 关闭顺序（为什么是这个顺序）
服务器收到中止信号时（Ctrl+C / SIGTERM）：
1. uvicorn 停止接受新连接；
2. 现有 WS 连接走 finally —— 中止当前轮 + 上传“停止哨兵”给 worker；
3. 应用交出 lifespan；调用 `teardown_all()` → `RESOURCES.aclose()`；
4. uvicorn 退出。

各步都会 catch 异常并记日志，不会因某个工具的 teardown 报错而拖累其它。

### 12.4 调试提示
- 查看工具是否都加载了：`python -c "from tools import load_all, REGISTRY; load_all(); print([t.name for t in REGISTRY.all()])"`
- 查看当前对 LLM 暴露了哪些工具：同上，加 `REGISTRY.schemas(max_permission=Permission.<level>)`
- 查看服务是否在跳动：`netstat -ano | findstr :8000`（Windows）/`lsof -i :8000`（macOS/Linux）


## 13. 配置项总览（config.py 全部环境变量）

`backend/config.py` 里的所有环境变量。不设置时都使用默认值，多数场景不需要改。

### 13.1 LLM 供应商（LLM_PROVIDER 切换）
| 变量 | 默认值 | 说明 |
|---|---|---|
| `LLM_PROVIDER` | `ark` | `ark`·火山引擎 Ark（OpenAI 兼容）或 `ollama`·本圻 |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | 本场 Ollama 服务地址 |
| `OLLAMA_MODEL` | `gemma4:12b` | Ollama 上要拉起来的模型名 |
| `ARK_BASE_URL` | `https://ark.cn-beijing.volces.com/api/coding/v3` | Ark 接口地址（OpenAI 兼容 `/chat/completions`） |
| `ARK_API_KEY` | 硬编码在仓里 | 生成函数调用的凭证；生产可迁到 .env |
| `ARK_MODEL` | `ark-code-latest` | Ark 上要用的模型 |

**本地 llama.cpp server（`LLM_PROVIDER=llamacpp`）**

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LLAMACPP_URL` | `http://127.0.0.1:8080/v1` | OpenAI 兼容基址（注意带 `/v1`） |
| `LLAMACPP_MODEL` | `Qwen3.6-35B-A3B-MTP` | 模型名，需与 llama.cpp 启动时一致 |
| `LLAMACPP_API_KEY` | （空） | 留空则**不发** `Authorization` 头；反代/中转要鉴权时才填 |
| `ENABLE_THINKING` | `0` | `0`=关掉 Qwen3 深度思考（秒回，适配语音）；`1`=开启（慢但质量高） |
### 13.2 分句与语言
| 变量 | 默认值 | 说明 |
|---|---|---|
| `SENTENCE_MIN_LEN` | `8` | 达到该长度后遇到次级标点（中英 `,;、`）才能切 |
| `SENTENCE_MAX_LEN` | `24` | 不遇任何标点时超过该长度强切 |
| `SYSTEM_PROMPT` | 默认小枡提示词 | 全部可覆盖，调人设定个性 |

`STRONG_PUNCT` / `WEAK_PUNCT` 为内编码恒定集合，不发布为环境变量。

### 13.3 服务地址与反代
| 变量 | 默认值 | 说明 |
|---|---|---|
| `HOST` | `127.0.0.1` | uvicorn 监听地址；仅本机访问请改 `0.0.0.0` |
| `PORT` | `8000` | uvicorn 监听端口 |
| `SHOW_REAL_IP` | `1` | `1`=信任 cloudflared 等反代转发头显示真实 IP；`0`=恒显示 127.0.0.1 |

### 13.4 工具调用框架（tools）
| 变量 | 默认值 | 说明 |
|---|---|---|
| `ENABLE_TOOLS` | `1` | 总开关；`0` 则不调工具，走原有流式链路 |
| `TOOL_MAX_ROUNDS` | `3` | 工具循环最多轮次，超过则强制进入「最终答」阶段，防无限调用 |
| `TOOL_MAX_PERMISSION` | `read` | 可暴露给 LLM 的最高权限；可选 `read` / `write` / `dangerous`。`echo` 默认被 `dangerous` 阈门拦住，设为 `dangerous` 才会出现 |
| `TAVILY_API_KEY` | 仓里硬编码 | 联网搜索；生产请搬到 .env |
| `TAVILY_URL` | `https://api.tavily.com/search` | 搜索接口地址，一般不动 |

### 13.5 对话轮次策略（INTERRUPT_MODE）
| 变量 | 默认值 | 说明 |
|---|---|---|
| `INTERRUPT_MODE` | `smart` | `queue`·排队依次说；`always`·任何新消息立即打断；`smart`·附和词继续说、打断词/新提问才 barge-in（推荐） |

详见第 11 章。

### 13.6 上下文管理（P0 + P1）
| 变量 | 默认值 | 说明 |
|---|---|---|
| `MAX_TURNS` | `10` | 滑动窗口保留的轮数（1 轮 = user+assistant） |
| `MAX_CONTEXT_CHARS` | `4000` | 发给 LLM 的上下文总字符上限，超过则从头裁 |
| `ENABLE_SUMMARY` | `1` | 是否启用「被裁掉的老历史 → 长期记忆摘要」 |
| `SUMMARY_TRIGGER_CHARS` | `1200` | 被裁掉的那些历史累计超过该字数才触发压缩为摘要 |
| `SESSION_TTL` | `3600` | 会话空闲多少秒后回收（踢出内存前会先落盘） |

### 13.7 记忆模块（第 16 章）
| 变量 | 默认值 | 说明 |
|---|---|---|
| `MEMORY_DATA_DIR` | `backend/data/memory` | 记忆落盘目录（`sessions/*.json` 与 `long_term.json`） |
| `MEMORY_PERSIST` | `1` | `0` = 纯内存，重启就忘；跑测试时用 |
| `MEMORY_FLUSH_EVERY_N_TURNS` | `5` | 每 N 轮强制落盘一次，应对进程崩溃 |
| `MEMORY_MAX_LONG_TERM_CHARS` | `2000` | 贴进 system 的长期记忆字符上限（超限先丢摘要、保住画像） |
| `PROFILE_CONFLICT_TIMEOUT_S` | `60` | 画像冲突确认超时，超时**保旧** |
| `PROFILE_MAX_CONFLICTS_PER_TURN` | `3` | 一轮最多下发多少张确认卡，超出的下轮再评估 |

`PROFILE_FIELDS` 是**代码常量**（不读环境变量）：`["name", "preferences", "occupation"]`。
因为新增字段需要同步改提炼提示词与中文标签，不是改个环境变量就能生效的事。

### 13.8 调试日志
| 变量 | 默认值 | 说明 |
|---|---|---|
| `LOG_CONTEXT` | `1` | `0` 关闭上下文日志（控制台不打印，文件不写） |
| `CONTEXT_LOG_FILE` | `backend/logs/context.log` | 日志文件路径；设为空串则只输出控制台 |

### 13.9 常见场景调参
- 本机不能访问 Ark：`LLM_PROVIDER=ollama` 切回本场模型。
- 不想联网：`ENABLE_TOOLS=0`，同时可以从 system prompt 移除「优先联网」那句。
- 查看工具调用详情：保持 `LOG_CONTEXT=1`，在控制台会看到 `[工具 #<conn> 调用 <name> 参数=...` 这样的行。
- 会话污染内存：`SESSION_TTL=600`、`MAX_TURNS=6`、`ENABLE_SUMMARY=1` 三者联动。
- 联调 echo 工具：`TOOL_MAX_PERMISSION=dangerous`，同时设 `ENABLE_TOOLS=1`。
- 手机外网访问：`SHOW_REAL_IP=1` + cloudflared 代理（端口是 8000）。

## 14. 密钥管理与仓库协作（secrets / repo hygiene）

### 14.1 为什么改

此前 `ARK_API_KEY`、`TAVILY_API_KEY` 直接硬编码在 `backend/config.py` 的 `os.getenv` 默认值里。
这在单机 demo 阶段方便，但一旦仓库要推到 GitHub（哪怕 private）就有两个问题：

1. **明文泄露**：任何拿到仓库的人（含未来的协作者、CI 日志、误设为 public）都能直接用你的额度。
2. **历史残留**：删掉当前文件里的 key 没用，`git log -S <key>` 仍能从旧 commit 里翻出来。

### 14.2 方案：.env + 极简加载器

| 文件 | 是否入库 | 作用 |
|---|---|---|
| `.env` | ❌（`.gitignore` 忽略） | 本机真实密钥，每台机器各自维护 |
| `.env.example` | ✅ | 模板：列出所有键名 + 注释 + 申请地址，值留空 |
| `backend/config.py` | ✅ | 启动时读 `.env`，密钥默认值改为 `""` |

`config.py` 里的 `_load_env_file()` 是**不引入 python-dotenv 依赖**的最小实现：

- 只解析 `KEY=VALUE`，跳过空行与 `#` 注释，自动去掉值两侧引号；
- 用 `os.environ.setdefault()` 写入 —— 即**已显式导出的真实环境变量优先级高于 `.env`**，
  这样临时切模型可以直接 `$env:LLM_PROVIDER="ollama"` 而不用改文件；
- `.env` 不存在时静默跳过，不影响纯环境变量部署（Docker / systemd）。

### 14.3 启动自检

`server.py` 的 `_check_secrets()` 在加载完工具后运行，**只提示不阻断**：

- `LLM_PROVIDER=ark` 且 `ARK_API_KEY` 为空 → 警告并提示可改用 `ollama`；
- `TAVILY_API_KEY` 为空 → 提示 `web_search` 不可用（其余链路照常）。

设计原则：缺密钥应该给出**人能读懂的一行提示**，而不是等到第一次对话时抛 401 堆栈。

### 14.4 历史清洗

旧 commit 里的明文 key 用「`git fast-export` → 流内字节替换 → `git fast-import` 到新仓库」
的方式整体重写，明文被替换为 `REDACTED_ARK_API_KEY` / `REDACTED_TAVILY_API_KEY` 占位符。

选这条路而非 `git filter-repo` / `filter-branch` 的原因：本机无法访问 PyPI 安装 `git-filter-repo`，
而 `filter-branch` 依赖 Git Bash 的 `cat`/`git-sh-setup`，在当前 PowerShell 沙箱下 PATH 传递不可靠。
`fast-export | fast-import` 只需 git 本体 + Python，跨平台稳定。

> ⚠️ 重要：历史重写会改变**所有 commit 的 SHA**。若已有其他克隆，需重新 clone 或 `git reset --hard origin/master`。
> 另外，**密钥一旦进过 git 就应视为已泄露**，最稳妥的做法仍是去控制台**重新签发一份新 key**。

### 14.5 换机器开发流程

```powershell
git clone <repo> myrobot
cd myrobot
copy .env.example .env        # 填入自己的 ARK_API_KEY / TAVILY_API_KEY
pip install -r backend/requirements.txt
python backend\server.py
```

入库文件仅 30+ 个（`git ls-files` 可核对）：`backend/` 源码、`frontend/`（含 2.7MB `avatar.glb`）、
`docs/`、`README.md`。`.venv/`、`__pycache__/`、`.idea/`、`logs/`、`*.log`、`.env` 均已忽略。

## 15. 持续集成（GitHub Actions 静态检查）

### 15.1 为什么需要

开发模式是「本机改 → 提交 → 换台机器接着改」，此时有两个真实风险：

1. **编码事故**：本项目的 `.py` 含大量中文注释且多数由脚本生成（PowerShell 下写中文文件坑很多）。
   若某次写入弄坏了缩进或编码，要等到下次真正 `python server.py` 才暴露；
   而那时可能已经推上去、另一台机器已经 clone 了一份坏代码。
2. **密钥回流**：第 14 章刚把硬编码密钥清洗干净，但谁都可能为了调试方便又把 key 直接写回 `config.py`。

### 15.2 三个 job

配置在 `.github/workflows/checks.yml`，触发时机：`push` 到 master / 任意 PR / 手动触发。

| job | 做什么 | 能捕到的问题 |
|---|---|---|
| `python-syntax` | `compileall -q backend/ scripts/` + 工具 schema 自检 + 记忆模块自检 + 关键模块真实 `import` | 语法错、缩进错、编码错、循环引用、笔误的模块名、schema 与函数签名不一致、记忆层行为回退 |
| `secret-scan` | 跑 `scripts/check_no_hardcoded_secrets.py` | 密钥被硬编码回代码 |
| `js-syntax` | `node --check` 校验自写前端模块（含 `profile_card.js`）| 前端 JS 语法错 |

`compileall` 之后额外做一次 `import config, pipeline.text_router, pipeline.turn_policy, tools`：
编译通过只说明语法合法，真正 import 才能捕到循环引用与写错的模块路径。
注意 **不导入 `server.py`** —— 它在模块层面就会初始化日志、加载工具，属于运行时行为，不该在静态检查里做。

`python-syntax` 里还多跑了一步 `scripts/check_tool_schemas.py`：它会真的 `load_all()`
把工具全注册一遍，再用 `inspect` 逐个对比 schema 与函数签名（规则见 § 10.7）。
这一步**不需要任何 API Key**（工具只注册、不执行），所以能放在静态检查里。

同理还多跑了 `scripts/check_memory.py`（67 项断言）。它能进 CI 的关键在于两个设计：
**用假 summarizer 替掉真 LLM 调用**（不需密钥、不请求网络），
**把 `MEMORY_DATA_DIR` 指到 `tempfile.mkdtemp()`**（不碰开发机的真实记忆）。
它也是目前唯一个**断言行为而非仅断言语法**的检查，因为反思链路曾经静默失效过（见 § 16.11）。

### 15.3 刻意保持的边界

- **不需要任何 API Key**：三个 job 全是静态检查，不启服务、不请求 Ark / Tavily，
  因此**无需配置 GitHub Secrets**，也不会因为额度或网络抢错。
- **不做端到端测试**：真要验证对话链路得把密钥配成 Secrets 并调真模型，对 demo 阶段是过度设计。
- **不检三方库**：`three.module.js` / `GLTFLoader.js` / `BufferGeometryUtils.js` 是 vendored 依赖，不归本仓库维护。
- **额度**：private 仓库每月 2000 分钟免费，本工作流单次几十秒，实际用不完。

### 15.4 密钥扫描器的误报权衡

`scripts/check_no_hardcoded_secrets.py` 的原则是**宁可漏报也不误报**（误报会让人开始忽视 CI）。两层规则：

1. **厂商特征前缀**：`tvly-` / `sk-` / `ghp_` 等，命中几乎必定是真密钥；
2. **可疑赋值结构**：`*_API_KEY / *_TOKEN / *_SECRET / *_PASSWORD` 后面跟了个长度 ≥ 8 的非空字面量。

以下情况被显式判为安全：空字符串默认值、`REDACTED_*` 占位符、`your-` / `YOUR_` / `xxx` / `<...>` / `${...}` 模板占位、
以及全大写字面量（避开 `os.getenv("TAVILY_API_KEY", "")` 里把变量名本身误捕的情况）。

扫描范围用 `git ls-files` 取得，因此**天然排除** `.env` / `.venv/` / `__pycache__/`（都在 `.gitignore` 里），
也跳过二进制文件如 `avatar.glb`。本机随时可手动跑：

```powershell
python scripts/check_no_hardcoded_secrets.py
# OK：已扫描 35 个跟踪文件，未发现硬编码密钥。
```

> 该脚本已做过正/负向验证：正常仓库返回 0；人为写入一个 `tvly-dev-...` 假密钥后准确定位到 `backend/config.py` 行号并返回 1。

## 16. Agent 记忆模块设计（分层 · 沉淀 · 检索）

### 16.1 为什么一个 Agent 需要记忆

没有记忆的 Agent 跟每次都是「第一次见面」的人类一样：他会忘记你五分钟前说过的偏好，
反复犯同一个错，每次开机都要重新介绍自己。
记忆模块的设计水平，决定了 Agent 是“能用”还是“好用”。

本章参考《小红书大模型二面：Agent 记忆模块设计》的「四类记忆 × 三层存储 × 三维检索 × 反思提炼」框架，
但给本项目做了适配：单用户需求 / 本地 + 云端 LLM / 不上向量数据库。

### 16.2 四类记忆与本项目的对应

| 类型 | 是什么 | 在本项目中的实现 | 生命周期 |
|---|---|---|---|
| **工作记忆** Working | 当前任务正在处理的上下文：推理中间态、本轮工具返回 | LLM 上下文窗口（每次调用重新组装） | 一轮 |
| **情景记忆** Episodic | 过去发生过的具体事件：「上周帮你查过机票」「你一开始叫上帝」 | 会话的渐进历史（`Session.history`）+被裁减的待摘要缓冲（`Session.dropped_buffer`） | 会话期内（TTL） |
| **语义记忆** Semantic | 从事件中提炼出的通用知识：「你喜欢简洁风格」「你偏好靠窗座位」 | 会话摘要 + 用户画像（`Session.summary` 与新增的 `UserProfile`） | 持久（含存盘） |
| **程序记忆** Procedural | 固化的操作流程：「处理退款的标准流程是……」 | 系统提示词 + 工具注册表（`tools/REGISTRY`） | 随项目升级（本身不变） |

上表中三个点需要重点说明：

- **工作记忆不是一个独立存储**，它是「每次调 LLM 时重新拼出来的上下文」。沉丝成本为零。
- **情景记忆会沉淀成语义记忆**，这是反思机制在做的事（详见 16.6）。
- **程序记忆在本项目中几乎全部位于提示词与工具注册表**，独立抽出这一层是为了未来能在这里加 SOP 文件与动态加载。

### 16.3 三层存储（取舍向量数据库）

原文以 L1=Context / L2=Redis / L3=Vector DB+PostgreSQL 为例，对本项目过重，
采用「轻量等价品」：

| 层 | 原文抽象 | 本项目实现 | 何时升级 |
|---|---|---|---|
| L1 工作层 | LLM Context Window | 每轮重新拼接的 `messages` 列表 | 不需升级 |
| L2 会话缓存层 | Redis、过期机制 | `ConversationManager` 中的 `Session` 对象 + JSON 磁盘持久化 | 单机跨重启 |
| L3 长期记忆层 | 向量库 + 结构化存储 | 会话摘要与用户画像合并为一个 `long_term.json`，不上向量 | 当记忆超过 50 个会话 或 用户人为提出「记住 XXX」 |

**为什么不一开始就上向量库？**

- 当前只有**一个用户**，且记忆完全被 1 个 summary + profile 能覆盖，检索不是瓶颈。
- 向量化 + 依赖接入 embedding 服务，增加了「embedding 质量不稳、需要冷启动、多层缓存」等问题，对 demo 阶段性价比偏低。
- 设计上保留接口：`memory.retrieval` 模块遵循同一个 `MemoryQuery` 接口，未来换向量库只是换一个实现。

### 16.4 单轮对话中记忆是怎么拼的

```
[系统]  小柚的人设与口头禅（程序记忆的主体）              ← 常驻
[系统·贴片] 「你喜欢简洁风格」「你是 某某 公司的 CTO」  ← L3 语义记忆（profile）
[系统·贴片] 上次聊过什么（summary）                       ← L3 会话摘要
[user]    上下文中的某句话                                   ← L2 情景记忆（滑动窗口）
[assistant] 上下文中的某句答复
[user]    …
[assistant] …
[user]    【当前轮】你好说谁呢？                          ← 工作记忆的入口
```

LLM 看到的上下文长这个样子。**工具调用中间产生的 `role=tool` 消息不会入库**，
只有 `role=user/assistant` 的成对对话才会被往 L2/L3 写。这个边界在 `agent.py` 里已经隔出来了（只 append 最终 answer）。

### 16.5 写入侧：「过滤 → 提炼 → 冲突检查 → 存储」

**不全量记录**。本项目现在的写入点是 `Session.commit_turn()`，仅记录「成对」的 user+assistant，
这已经过滤了大部分噪音。未来加多层过滤需要两个东西：

1. **`何时触发写入`**：本项目采用「交付后写入」（最简单）。不采用「每句后写入」是为了避免被打断的残句污染记忆。
2. **`提炼出什么`**：不是「该轮完整对话」，而是「该轮里有什么能改变画像或摘要的信息」。详见 16.6。

**冲突检查**（可选，未来加）：用户之前说「我不吃辣」，后来说「今天吃了个火锅」。
需要推理以及决策以哪个为准（是改画像还是仅作为事实）。本阶段可以先不做。

### 16.6 反思机制：情景 → 语义的沉淀

**谁触发**：被裁减的老历史在 `dropped_buffer` 里积累到 `SUMMARY_TRIGGER_CHARS` 阈值（默认 1200），
异步调 LLM 一次，生成「要点摘要」并增量覆盖到之前的 summary 上。

**提炼出什么**：现在的 summary prompt 只要求「不超过 120 字的中文要点」，在改造中拆为两部分：

```
{「要点摘要」: 「上次聊了什么，完成了什么」,
 「用户画像补充」: {name: …, preferences: …, occupation: …}}
```

**画像只提炼 3 个字段**（已定稿，`config.PROFILE_FIELDS`）：

| 字段 | 含义 | 例子 |
|---|---|---|
| `name` | 称呼 | 「小王」「老师」 |
| `preferences` | 偏好 / 习惯 / 注意事项（影响回答风格与内容） | 「喜欢简洁回答」「对青霉素过敏」 |
| `occupation` | 职业 / 角色（影响解释的深浅） | 「CTO」「小学生」 |

**为什么不要 `key_facts`**：它会变成「什么都能往里塞」的垃圾桶，LLM 提炼时目标不清。
高代价信息（如过敏史）归到 `preferences` 下，结构更清楚、误判更少。
以后需要 `relationships`（重要人物）或 `todos`（待办）时直接加字段，向前兼容，不影响 B 冲突流程。

- **要点摘要** 上叠到 `summary`，作为 L3 会话记忆；
- **用户画像补充** 提取出以 `key:value` 形式与现有 `profile` 合并，冲突时以「**人工确认**」处理（选择 B 方案）：
  - 提炼出来后，服务端对比新旧值，仅对**字段值不同**的项下发 `profile_conflict` WS 帧；
  - 前端弹出确认卡（带旧值/新值/提炼出来的原句子）；
  - 用户点 确认 → 写入新值；点 拒绝 → 保留旧值并记录一条 `rejected_change`；
  - 超时（默认 60s）未回应，默认**保留旧值**（以保守为偏差，允许以环境变量调整）；
  - 其他无冲突字段依旧直接合并。
这是「从事件提炼为认知」的关键一步。

**不要频繁调 LLM**：沉淀仅在阈值达到时触发（默认累计 1200 字才调一次），
避免在多轮对话中重复调。

### 16.7 检索侧：本阶段仅需“全量拼接”

### 16.8 Profile 冲突交互流程（B 方案）

**发生位置**：仅在「反思」阶段（`maybe_summarize`）生成 pending diff，随下一轮问题一起下发。
不会占用主对话的帧位置，不会打断 LLM 正在生成的响应。

**流程**：

```
LLM 提炼出 「新画像」
    ├─ 同字段值不变 → 直接合并
    └─ 同字段值变化 → 生成 conflict_id，后台暂存为 pending diff
下一轮问题到来 → 服务端随 LLM 调用下发 profile_conflict 帧
                              ├─ 前端 确认 → apply diff，什继续主对话
                              ├─ 前端 拒绝 → 记录 rejected_change，不改
                              └─ 60s 超时 → 默认保旧
```

**新增 WS 消息类型**（详见第 5 章，补充定义）：

- 服务端 → 客户端：`profile_conflict` 帧，字段 `{field, old_value, new_value, source_quote, conflict_id}`
  - 同一轮可能多条冲突，用 `conflict_id` 与前端响应对应；
  - 超过 `PROFILE_MAX_CONFLICTS_PER_TURN` （默认 3）部分只写日志不下发，下轮重评估。
- 客户端 → 服务端：`profile_resolve` 帧，字段 `{conflict_id, action: "accept"|"reject"}`

**超时与结果处理**：60s 内未回应，服务端默认保旧值，但仍上报一条 `resolved` 帧让前端可选销毁确认卡。

**为什么超时保旧而不是保新**：B 方案的核心价值是「不让少数事件覆盖多数事件」。
对试错语义「我以前喜欢 X，现在不喜欢了」，保旧安全；对事实型语义「你今天吃了火锅」，
不会影响 profile（火锅是一次性事件，不是偏好，不该走 profile）。


原文提了「Recency × Relevance × Importance」三维打分，借鉴自 Generative Agents。
本项目由于唯一会话的记忆完全可被 1 个 summary + 1 个 profile 覆盖，临阶段检索采用「全量拼接」，
即 `build_messages()` 中总是将全部 L3 贴到 system 后面。为未来留下三个接口：

- `MemoryQuery`：「查什么」。在 `build_messages` 中被构造；
- `MemoryHit`：「查到了什么」；
- `MemoryRetriever`：接口，默认实现是 `AllMemoryRetriever`，上向量后换成 `VectorRetriever` 不动主体代码。

### 16.9 持久化与存盘设计（**已实现** `memory/store.py`）

**存储路径**（实际落地结果，比原设计更简）：

```
backend/data/memory/
├─ sessions/
│  └─ <session_id>.json      L2：滑动窗口历史 + dropped_buffer + summary + turn_count
└─ long_term.json           L3：跳会话的用户画像 UserProfile
```

与原设计的两处**刻意简化**：

- **不做 `index.json`**。会话索引可以直接 `sessions/` 目录枚举得到，多一份索引就多一份不一致风险。
- **不做 `.lock` 文件锁**。单进程单 worker（见第 12 章），写入只发生在对话 worker 协程里，不存在跳进程竞争。真正防的是**写到一半进程挂了**，那是原子写解决的事。

**三个工程上必需的细节**（都在 `FileStore` 里）：

1. **原子写**：先写 `<name>.tmp` 再 `os.replace()` 覆盖。`os.replace` 在同一文件系统内是原子的，
   因此**不会出现“写了一半的 JSON”**。直接 `open(path,"w")` 则会先清空文件，崩溃就永久丢记忆。
2. **坏文件降级**：读到无法解析的 JSON 时，把它改名为 `.broken.<时间戳>` 并返回 `None`，
   让程序**以空记忆继续跑**而不是启动失败。现场现场保留下来，事后还能人工捧回。
3. **路径穿越防护**：`session_id` 是**客户端传的**，直接拼进文件名等于把文件写入权交给外部（`../../x`）。
   `_safe_id()` 把非 `[A-Za-z0-9_-]` 全部替为 `_` 并截断到 64 字符。

**三个刷盘时机**：

| 时机 | 写什么 | 为何 |
|---|---|---|
| 每 `MEMORY_FLUSH_EVERY_N_TURNS`（默认 5）轮 | session json | 平衷 IO 与崩溃丢失量 |
| 会话被 TTL 踢出内存前 | session json | 否则 `_gc()` 一跑就白丢 |
| profile 发生变更（merge / accept）| long_term.json | 画像是高价值低频数据，立即落盘 |

`MEMORY_PERSIST=0` 时 `FileStore` 全部接口退化为**空操作**（读返回 `None`，写返回 `False`）。
好处是调用方不需要到处写 `if MEMORY_PERSIST:`，开关只在一个地方生效。

**隐私与仓库卫生**：

- `.gitignore` 已加 `backend/data/`（**已验证**：`git check-ignore` 命中）。
- `long_term.json` 含真实个人信息，**不入库、不跳机复制**。
- README 已补一句：「个人记忆不走 git，公共机器上用 `MEMORY_PERSIST=0`」。

### 16.10 与代码的映射（实现后）

| 代码位置 | 层级 | 职责 |
|---|---|---|
| `Session.build_messages()` | L1 工作记忆 | 拼 system（+ 长期记忆贴片）+ 滑动窗口 + 本轮 user；**长期记忆一律过检索器取**，不直读字段 |
| `Session.history` | L2 情景 | 滑动窗口（`MAX_TURNS`），随 session json 落盘 |
| `Session.dropped_buffer` | L2 待沉淀区 | 被窗口裁掉的老历史，累计过阀就去反思 |
| `Session.summary` | L3 会话摘要 | 反思产物，随 session json 落盘 |
| `Session.profile` (`UserProfile`) | L3 语义 | 3 个字段的用户画像，落 `long_term.json` |
| `Session.pending_conflicts` | 交互态 | 待用户拍板的画像变更，**不落盘**（重启就作废，等于保旧） |
| `ConversationManager.maybe_summarize()` | 写入侧 | 反思触发点，返回 `List[ProfileConflict]` |
| `memory/retriever.py` | 检索侧 | `AllMemoryRetriever`：画像在前、摘要在后；超 `max_chars` 时**先丢摘要、保住画像** |
| `memory/store.py` | 持久层 | 原子写 + 坏文件降级 + 路径穿越防护；全局单例 `STORE` |
| `memory/types.py` | 抽象 | `MemoryKind` / `MemoryQuery` / `MemoryHit` / `MemoryRetriever` ABC，为将来接向量库留口 |
| `server.py` WS 循环 | 交互 | 处理 `profile_resolve`，**不进对话队列** |
| `server_app/dialog.py` | 交互 | `_notify_profile_conflicts()` 在 `llm_done` 之后下发 `profile_conflict` |
| `frontend/src/profile_card.js` | UI | 确认卡（旧值删除线 / 新值高亮 / 提炼原句 / 确认与拒绍） |

### 16.11 一个真实踩到的坑：反思链路曾静默失效

实现本章时发现：**摘要/反思功能上线后从未真正运行过**。根因是两个错误叠在一起：

1. `ConversationManager` 的单例当时建在 `server.py` 里，而 `server_app/dialog.py` 写的是
   `from pipeline.conversation import conversations` —— 这个**模块级名字根本不存在**。
2. 这句 import 外面包了 `except Exception: pass`。于是 `ImportError` 被**静默吃掉**，日志里一行都没有。

**修法**：单例下沉到 `pipeline/conversation.py` 模块层（`conversations = ConversationManager()`），
`server.py` 改为 import 它而不再自己 new，两边必然是同一个对象；`dialog.py` 的 `except` 改为**打印原因**。

**教训（已沉为本项目约定）**：

- 共享状态的单例一律建在**定义它的模块**里，不要建在入口文件里再到处传。
- `except Exception: pass` 只允许用在**真的无所谓**的地方；涉及功能开关的一律打印。
- “看不到报错”不等于“在工作”。所以 `scripts/check_memory.py` 专门断言了**反思真的被调用到**。

### 16.12 未来扩展点（明确不在本期范围内）

- **向量检索**：接入 sentence-transformers / bge-m3 等本地 embedding。仅需实现 `VectorRetriever` 不动主体代码。
- ~~**冲突检查**：为 profile 加「后者优先」与「人工确认」两个优先级。~~ 已采纳为 B 方案（详见 16.8），**已实现**。
- **多人多机器**：增加 `user_id` 层，`long_term.json` 变成 `<user_id>/long_term.json`。
- **学习式程序记忆**：为常用 SOP 加一层 L0，接口类似于 tools 的资源文件加载。
- **信任度与可退**：给每条记忆加 `confidence`，检索时作为第四个权重。

### 16.13 新增的配置项（**已在 config.py 生效**）

完整含义见第 13.7 节。实际代码：

```python
MEMORY_DATA_DIR = os.getenv("MEMORY_DATA_DIR", str(Path(__file__).resolve().parent / "data" / "memory"))
MEMORY_PERSIST = os.getenv("MEMORY_PERSIST", "1") == "1"          # 0 = 纯内存，跑测试/公共机器用
MEMORY_FLUSH_EVERY_N_TURNS = int(os.getenv("MEMORY_FLUSH_EVERY_N_TURNS", "5"))
MEMORY_MAX_LONG_TERM_CHARS = int(os.getenv("MEMORY_MAX_LONG_TERM_CHARS", "2000"))
PROFILE_FIELDS = ["name", "preferences", "occupation"]            # 代码常量，不读环境变量
PROFILE_CONFLICT_TIMEOUT_S = int(os.getenv("PROFILE_CONFLICT_TIMEOUT_S", "60"))
PROFILE_MAX_CONFLICTS_PER_TURN = int(os.getenv("PROFILE_MAX_CONFLICTS_PER_TURN", "3"))
```

比原设计多了 `MEMORY_PERSIST`：自测脚本与 CI 需要一个**不碰真实记忆目录**的开关，
且公共机器上跱 demo 也不应该把别人的画像写到盘上。

### 16.14 本期范围（不上向量库）—— **已全部完成**

- [x] 接口：`MemoryRetriever` / `MemoryHit` / `MemoryQuery` 抽象 → `backend/memory/types.py`
- [x] 实现：`AllMemoryRetriever`（全量拼接，超限先丢摘要）→ `backend/memory/retriever.py`
- [x] 实现：`UserProfile` 与 `ProfileConflict` 数据类 → `backend/memory/profile.py`
- [x] 改造：`Session.build_messages` 经检索器接入 profile 贴片 → `backend/pipeline/conversation.py`
- [x] 新增：WS 消息 `profile_conflict` / `profile_resolve` / `profile_resolved` → `server.py` + `server_app/dialog.py`（定义见第 5 章）
- [x] 新增：前端确认卡 UI → `frontend/src/profile_card.js` + `index.html` 的 `#profileCards`
- [x] 改造：`maybe_summarize` 同步产出 pending diffs，于本轮 `llm_done` 后下发
- [x] 改造：`resolve_conflict(accept/reject)`，超时/重启均**保旧**
- [x] 改造：提炼提示词拆为「摘要 + profile」双字段 JSON，并带**坏 JSON 降级为纯文本摘要**
- [x] 实现：`FileStore` 持久化（原子写 / 坏文件降级 / 路径穿越防护）→ `backend/memory/store.py`
- [x] 改造：`ConversationManager` 内部接存储，`clear()` 同步删盘，`_gc()` 踢出前先落盘
- [x] `.gitignore` 补 `backend/data/`
- [x] README 补「个人记忆不走 git」
- [x] 自测：`scripts/check_memory.py`（67 项断言）已接入 CI 的 `python-syntax` job
- [x] 修复：反思链路因单例缺失静默失效的 bug（详见 16.11）

## 17. 远程访问与部署（cloudflared 隧道）

### 17.1 为什么需要

服务默认只监听 `127.0.0.1:8000`。想用**手机或其他设备**访问数字人时，
需要把本地端口暂时暴露到公网。本项目用 Cloudflare 快速隧道（不需账号）：

```powershell
cloudflared tunnel --url http://127.0.0.1:8000
# 输出形如：https://xxx-yyy-zzz.trycloudflare.com
# 访问：https://xxx-yyy-zzz.trycloudflare.com/app/index.html
```

### 17.2 HTTP 与 WebSocket 共用一个域名

**关键点**：cloudflared 隧道**同时代理 HTTP 和 WebSocket**，不需要单独映射 WS 端口。
因为 `/ws` 与静态页面同属一个 FastAPI 应用，走的是同一个 8000 端口。

### 17.3 混合内容（Mixed Content）坑

页面通过 `https://` 加载时，浏览器**禁止**连接不安全的 `ws://`：

```
Mixed Content: The page at 'https://…' was loaded over HTTPS, but attempted to
connect to the insecure WebSocket endpoint 'ws://…'. This request has been blocked.
```

**解法**：`frontend/src/main.js` **根据页面当前协议自动选择**，不硬编码域名：

```javascript
const WS_PROTO = location.protocol === 'https:' ? 'wss:' : 'ws:';
const WS_URL = `${WS_PROTO}//${location.host}/ws`;
```

（`ws.js` 只负责连接管理：自动重连、发送缓冲、心跳保活；URL 由调用方传入。）

这样本地 `http://127.0.0.1:8000` 走 `ws://`，隧道 `https://…trycloudflare.com` 自动走 `wss://`，
**域名每次变也不用改代码**（快速隧道的域名是随机分配的）。

### 17.4 真实来源 IP 与日志

经过隧道后，所有请求在 FastAPI 看来都来自 `127.0.0.1`。
`SHOW_REAL_IP=1`（默认）时信任转发头以显示真实客户端 IP，并做两个归一化：

- IPv6 的 v4-映射地址（`::ffff:1.2.3.4`）还原为纯 IPv4；
- 纯 IPv6 加方括号（`[2409:…]:0`）以便与端口区分。

设为 `SHOW_REAL_IP=0` 则不信任转发头，来源恒为本机（适合不想在日志里留客户端 IP 的场景）。

### 17.5 移动网络下的连接健壮性

`ws.js` 为隧道/移动网络场景做了三件事：

- **发送缓冲**：未连接时的消息先入 `outbox` 队列，连上后自动补发——"
  避免移动网络重连间隙丢消息（曾出现「前端有回显但服务端没收到」）。
- **心跳保活**：每 20s 发一个 `ping`，缓解 Cloudflare / 运营商 NAT 对空闲 WS 的主动断开。
- **自动重连 + 重发 hello**：断开后 2s 重试，并**重发记住的 `hello` 帧**，
  保证重连后仍绑定到同一个 `session`（上下文不丢）。

### 17.6 静态资源缓存

手机浏览器会激进缓存 JS，导致改了前端代码但行为不变。
`server.py` 的 `no_cache_static` 中间件对 `/app` 下的 `.js` / `.html` 强制不缓存：

```
Cache-Control: no-cache, no-store, must-revalidate
Pragma: no-cache
Expires: 0
```

### 17.7 常见故障对照

| 现象 | 原因 | 解法 |
|---|---|---|
| 页面能开，一直「连接中」 | 混合内容拦住了 `ws://` | 确认 `ws.js` 按页面协议选 `wss:` |
| `Unsupported upgrade request` | 缺 WebSocket 库 | `pip install 'uvicorn[standard]'` 或 `websockets` |
| 发送后后端没反应 | 未先发 `hello` 帧 | 前端连接后必须先发 `hello` |
| 多个设备上下文互相污染 | 共用了同一 `session` | 每个设备生成独立 sid（存 localStorage） |
| 端口 8000 被占 | 旧进程未退 | `netstat -ano \| findstr :8000` 后 `taskkill /PID <pid> /F` |

> 安全提醒：快速隧道是**公开可访问**的，任何拿到域名的人都能与你的数字人对话（并消耗你的 LLM 额度）。
> 调试完毕及时关掉 cloudflared。需要长期公网服务应改用具名隧道 + 访问控制。

---

## 18. TTS 语音合成（双后端 + 自动降级）

### 18.1 为什么换掉 Web Speech
Windows 上浏览器 Web Speech 只能调用本机 SAPI 音色，实测仅 3 个中文音色
（`Huihui` / `Kangkang` / `Yaoyao`），都是拼接式合成，机械感重、语调平。
Edge TTS 走微软 Azure 神经网络语音，**免费、无需 API Key**，中文有 14 个音色，
自然度高一个量级。

初版规格里「本机 edge-tts 取不到音频」的结论是错的，真实原因见 18.4。

### 18.2 架构
```
LLM 分句 ──▶ 前端 speak(text) ──▶ 入队即预取 POST /api/tts
                                        │
                       ┌────────────────┴────────────────┐
                       │ backend/pipeline/tts_edge.py    │
                       │  edge_tts.Communicate(          │
                       │    boundary="WordBoundary")     │
                       │  → mp3 chunks + WordBoundary    │
                       │  → 词时长按字均分成逐字 marks   │
                       └────────────────┬────────────────┘
                                        ▼
              {ok, audio(base64 mp3), mime, marks[], voice}
                                        │
   <audio> 播放 ──▶ rAF 按 currentTime 在 marks 上推进 ──▶ onBoundary({char})
                                        ▼
                          viseme.js ──▶ head3d.js 口型
```

- 后端：`backend/pipeline/tts_edge.py`（`synthesize` / `list_voices`）
- 路由：`GET /api/tts/config`、`GET /api/tts/voices`、`POST /api/tts`
- 前端：`frontend/src/tts.js`，对外接口与旧版完全一致
  （`speak/cancel/softStop`），上层 `main.js` 的 `driveMouth` 无需感知用的哪套引擎。

### 18.3 三个关键设计
1. **口型时间轴取代 boundary 事件**：音频播放没有 `boundary` 事件，
   改为拿服务端返回的 `marks`，在 `requestAnimationFrame` 里按 `audio.currentTime` 查表下发字符。
   时间戳来自**真实合成结果**而非浏览器估算，精度反而更高，也不受浏览器差异影响。
2. **预取（prefetch）**：Edge 单句合成约 0.5~1.7s。若等上一句播完才合成下一句，句间会有明显空档。
   所以 `speak()` **入队时就发起请求**，Promise 挂在 `item.pending` 上，与当前句播放并行。
3. **逐句降级而非整体降级**：任一句合成失败（断网 / 未装 edge-tts / 自动播放被拒）
   只让**这一句**改用 Web Speech，后续句仍走 Edge。宁可音色差一点，也不能让数字人变哑。

另外 `softStop`（被打断时的自然收尾）在 Edge 路径下能做**真正的音量渐弱**
（`HTMLAudioElement.volume` 可写，分 8 步淡出）；Web Speech 受 API 限制只能延迟 cancel 近似。

### 18.4 坑：edge-tts ≥ 7 默认不给词边界
`edge_tts.Communicate` 的 `boundary` 参数默认值是 `"SentenceBoundary"`，
一整句只回**一个** mark，口型全程只动一下。必须显式传 `boundary="WordBoundary"`：

```python
comm = edge_tts.Communicate(text, voice, boundary="WordBoundary", rate=..., pitch=...)
```

代码里对老版本做了 `TypeError` 兜底（老版本没这个参数，本身就按词回调）。

还有一层：中文一个「词」常含 2~4 字（`你好`、`小柚`），整词一个 mark 同样会让口型偷懒。
所以 `tts_edge.py` 把词时长**按字均分展开成逐字 mark**，并用 `text.find(word, cursor)`
把 mark 映射回原文字符下标（`cursor` 单向前进，避免同一个字反复命中开头那次）。

### 18.5 配置与音色挑选
| 变量 | 默认 | 说明 |
|---|---|---|
| `TTS_ENGINE` | `edge` | `edge` 神经语音 / `web` 浏览器 Web Speech |
| `TTS_VOICE` | `zh-CN-XiaoyiNeural` | 音色，共 14 个中文可选 |
| `TTS_RATE` | `+8%` | 语速，相对量百分比 |
| `TTS_PITCH` | `+10Hz` | 音高，单位 Hz |
| `TTS_VOLUME` | `+0%` | 音量 |

推荐音色：

| 音色 | 特点 | 适用 |
|---|---|---|
| `zh-CN-XiaoyiNeural` | 活泼少女，Cartoon/Novel·Lively | **当前默认**，最贴合「小柚」人设 |
| `zh-CN-XiaoxiaoNeural` | 温暖女声，News/Novel·Warm | 最稳最百搭 |
| `zh-CN-YunxiaNeural` | 男童声·Cute | 想换可爱男声 |
| `zh-CN-YunxiNeural` | 少年男声·阳光 | 男性人设 |
| `zh-CN-liaoning-XiaobeiNeural` | 东北方言·幽默 | 整活 |

挑选方式二选一：

- 跑 `python scripts/tts_preview.py` 生成 9 个样本到 `voice_preview/`（含 `README.txt` 对应参数），
  听完把参数写进 `.env` 重启后端。
- 直接在页面 `#exprTest` 面板的下拉框里现场切换（仅影响当前页面，不改后端默认值）。

`voice_preview/` 与 `*.mp3` 已在 `.gitignore` 中，音频不入库。
