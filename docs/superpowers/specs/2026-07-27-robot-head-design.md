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
- 用**屏幕上的 2D 虚拟头**替代物理伺服头部；伺服/时序补偿层留出接口占位。

非目标（本阶段不做）：
- 物理电机 / 伺服驱动 / AEC-AGC 硬件级音频前处理。
- 3D 拟人头（先做 2D 简版嘴型+表情，接口预留可升级 3D）。
- 生产级性能优化、模型量化、NPU 部署。

## 2. 关键技术选型（Demo）

| 环节 | 方案 | 说明 |
|------|------|------|
| LLM | 本地 **Ollama** (`/api/chat`, stream=true) | 已就绪，默认模型 `gemma3:12b`（中文好、无思维链、较快），可配置 |
| TTS | 浏览器 **Web Speech API** (`SpeechSynthesis`) | 完全离线、内置中文语音、自带 `boundary` 事件（字/词时间戳），零依赖 |
| 口型对齐 | TTS `boundary` 事件时间戳 → 拼音/音素 → viseme | 前端实时驱动，符合“时间戳=基准时钟” |
| 虚拟头 | **Three.js** 2D 平面（正交相机 + 精灵/形状） | 嘴型开合 + 基础表情，30FPS+ |
| ASR | 浏览器 **Web Speech Recognition**（可选）+ 文本输入兜底 | 无麦克风也能跑；后续可替换 faster-whisper |
| 后端 | **Python FastAPI + WebSocket** | 承载 LLM 流式转发 + 文本解析中央调度 |
| 通信 | WebSocket 全双工流式 | 边收边推边出 |

设计决策说明：
- **为什么 TTS 放前端**：本机 edge-tts 取不到音频、SAPI COM 在当前环境不稳定。Web Speech API 离线可用且天然带时间戳，最适合 demo，避免卡壳。TTS 做成**可替换接口**，后续可切到 Piper/本地音素引擎。
- **为什么 2D 头**：快速跑通全链路；BlendShape/viseme 概念用 2D 权重表达，接口对齐 ARKit 52，方便升级 3D。

## 3. 系统架构

```
浏览器 (frontend)                         Python 后端 (backend)              Ollama
┌───────────────────────────┐            ┌──────────────────────────┐      ┌────────┐
│ 麦克风/文本输入            │  WS 文本   │ /ws 对话端点             │ HTTP │ /api/  │
│  ├ Web Speech Recognition ─┼──────────▶ │  ├ TextRouter 中央调度   ─┼─────▶│ chat   │
│  └ 文本框                  │            │  │   · 流式接收 LLM token│stream│ (stream)│
│                            │ ◀──────────┼─ │   · 清洗/智能分句      │◀─────│        │
│ VisemeEngine (口型映射)    │  分句/指令 │  │   · 播报文本 vs 动作分流│      └────────┘
│  └ Web Speech 合成+boundary│            │  └ 发送: sentence/action │
│ Three.js Head (2D)         │            └──────────────────────────┘
│  └ 口型帧 + 表情           │
└───────────────────────────┘
```

### 数据流（流式并行）
1. 用户文本/语音 → 前端经 WS 发送 `user_message`。
2. 后端 `TextRouter` 向 Ollama 发起 stream 请求，**边收 token**。
3. `TextRouter` 增量清洗（去 emoji/markdown/思考内容），**智能分句**，识别行内动作标记 `[表情:...]`/`[动作:...]`。
4. 每就绪一个短句 → 立刻经 WS 推给前端（`sentence` 消息），动作指令用 `action` 消息**提前下发**。
5. 前端收到句子 → Web Speech 合成播报，`boundary` 事件按字触发 → `VisemeEngine` 计算口型权重 → Three.js 逐帧渲染嘴型；`action` 消息驱动表情。

## 4. 模块划分

后端 `backend/`
- `config.py` — 配置（Ollama URL、模型名、系统提示、分句参数）。
- `pipeline/llm_client.py` — Ollama 流式客户端（async 生成 token）。
- `pipeline/text_router.py` — 中央调度：清洗、智能分句、动作/文本分流。
- `server.py` — FastAPI + WebSocket 端点，编排流水线。

前端 `frontend/`
- `index.html` — 页面（画布 + 输入区 + 状态）。
- `src/ws.js` — WebSocket 客户端。
- `src/tts.js` — Web Speech 合成 + boundary 时间戳。
- `src/viseme.js` — 字/拼音 → viseme 权重映射。
- `src/head.js` — Three.js 2D 头渲染（嘴、眼、眉、表情）。
- `src/main.js` — 装配与事件流。

## 5. 接口契约（WebSocket JSON 消息）

客户端 → 服务端:
- `{"type":"user_message","text":"...", "session":"id"}`
- `{"type":"interrupt"}`  (barge-in 打断)

服务端 → 客户端:
- `{"type":"sentence","text":"这是一句播报","seq":0}`
- `{"type":"action","action":"表情","value":"开心"}`  (提前下发)
- `{"type":"llm_done"}`
- `{"type":"error","message":"..."}`

LLM 输出约定（system prompt 引导）:
- 短句口语化；可用行内标记表达动作，如 `[表情:开心]`、`[动作:点头]`。
- 中央调度负责把标记剥离成 `action` 消息，纯文本作为 `sentence`。

## 6. 分句算法（要点）
- 按标点（。！？；，、…）与长度阈值（默认 12–24 字）做均衡切割。
- 不足阈值且未遇强标点时缓冲，遇强标点或超长即 flush。
- 流结束时 flush 残留缓冲。

## 7. 口型/表情映射（2D viseme）
- 中文按拼音韵母粗分口型类别：`a`(大张)、`o/u`(圆唇)、`e/i`(扁平)、`闭合`(m/b/p/静音)。
- 无拼音库时用字符哈希兜底伪口型，保证动画连续。
- 表情层：`平静/开心/疑惑/惊讶`，由 `action` 消息叠加，插值过渡。

## 8. 验收标准（Demo 级）
- 输入一句中文，虚拟头能**流式**逐句播报并做口型动画，首句可见延迟主观“较快”。
- 口型与语音大致同步（demo 级，不追求 ≤120ms 硬指标）。
- 支持点击“打断”停止当前播报。
- LLM 输出的 `[表情:x]` 能驱动表情变化。
- 无网络（除 Ollama 本地）也能运行前端 TTS。

## 9. 后续可扩展（对齐原方案）
- TTS 换 Piper/本地音素引擎，拿真音素级时间戳。
- ASR 换 faster-whisper 流式，后端做 VAD/AEC。
- 2D 头升级 Three.js 3D + ARKit 52 BlendShape。
- 加入伺服时序补偿模块（当前为接口占位）。
- 全局时钟对齐、智能降级策略。


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
- `ENABLE_TOOLS`（默认 1）、`TOOL_MAX_ROUNDS`（默认 3）、`TOOL_MAX_PERMISSION`（默认 read）。
- `TAVILY_API_KEY` / `TAVILY_URL`：联网搜索后端。

### 10.6 扩展新工具的步骤
1. 在 `tools/<分类>/` 新建 `.py`，用 `@tool(...)` 装饰函数或继承 `Tool` 类；
2. 写清 `description`（何时用/不该用）与 `parameters`（JSON Schema）；
3. 有状态资源放进 `ResourceManager`，通过 `ctx.resources` 获取；
4. 若在新分类目录，在 `loader.py` 的 `_TOOL_PACKAGES` 登记一次。
- 文档类：解析后先切块/检索，只回喂相关片段，避免撑爆上下文。
- 数据库类：建议只读 + 参数化/白名单，防注入。
- 硬件类：标 `dangerous`，默认被闸门拦截，需显式提升权限或加确认流程。

### 10.7 供应商兼容性
- Ark（OpenAI 兼容）：已验证支持 `tools`（`ark-code-latest` 实测可用）。
- Ollama：`/api/chat` 支持 `tools`，但仅部分模型（qwen2.5 / llama3.1 等）；gemma 系列较弱。
- 兜底：不支持原生 tools 的模型可改用 ReAct 提示词模式，后端正则解析动作。
