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
| LLM | **Ark**（火山引擎 OpenAI 兼容，`/chat/completions`），默认 `ark-code-latest`；本地 **Ollama**（`/api/chat`）作为备选 | 供应商可切换（`config.LLM_PROVIDER`）；默认 Ark 是因为本地不需 GPU 也能走、响应快 |
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
  - `conversation.py` — 多轮上下文管理（P0 安全提交 + P1 滑动窗口 + 摘要式长期记忆 + TTL 回收）。
  - `agent.py` — 工具调用编排（两阶段：探测/工具循环 → 流式最终答）。
  - `turn_policy.py` — smart 轮次策略分类（`classify_incoming`：interrupt / backchannel / question）。
- `tools/` — 插件式工具框架（详见第 10 章）：
  - `base.py` — `Tool` / `Permission` / `ToolContext` / `ToolResult` 抽象。
  - `registry.py` — 全局 `REGISTRY`（注册 / 权限阈门 / 超时 / 日志）与 `@tool` 装饰器。
  - `loader.py` — 启动时自动扫描子包（builtin/web/document/database/hardware）完成注册。
  - `context.py` — `RESOURCES`（跨工具复用的资源池，当前仅 httpx.AsyncClient）。
  - `builtin/basic.py` — `get_time` / `echo`（零依赖）。
  - `web/web_search.py` — `web_search`（Tavily）。

前端 `frontend/`
- `index.html` — 页面（画布 + 输入区 + 状态）。
- `src/ws.js` — WebSocket 客户端。
- `src/tts.js` — Web Speech 合成 + `boundary` 时间戳 + `softStop`（渐弱软停）。
- `src/viseme.js` — 字/拼音 → viseme 权重映射。
- `src/head3d.js` — Three.js 加载 GLB（ARKit 52 动画 + Oculus viseme），表情 / 口型 / 点头 / 「我在听」倾听表情。
- `src/main.js` — 装配与事件流（含 `enterListening`）。

## 5. 接口契约（WebSocket JSON 消息）

以下是完整定义，其中后述个别是后期增量。

客户端 → 服务端:
- `{"type":"hello","session":"<sid>"}` — 必须首发；`session` 为空则服务端分配新 sid。未发 hello 前的其他帧会被忽略（保证不会错误绑定到别人的会话）。
- `{"type":"user_message","text":"...","session":"<sid>"}` — 发送一句用户输入。
- `{"type":"interrupt"}` — 手动打断（点「打断」按钮）：立即中止当前轮 + 清空队列。不会触发「自然收尾」。
- `{"type":"clear"}` — 打断 + 清队 + 清空该 sid 的会话历史。
- `{"type":"ping"}` — 心跳；服务端不回复但会记日志。

服务端 → 客户端:
- `{"type":"session","session":"<sid>"}` — 对 hello 的回应，供前端写入 localStorage 以持久化 sid。
- `{"type":"sentence","text":"这是一句播报","seq":0}` — 中央调度分击后的一句表达。
- `{"type":"action","action":"表情|动作","value":"开心"}` — 提前下发的表情/动作指令，让前端表情与语音同步。
- `{"type":"status","text":"正在联网搜索…"}` — 工具执行状态（如 web_search），让前端状态栏显示。
- `{"type":"llm_done"}` — 本轮正常结束（已入库）。被打断不发。
- `{"type":"error","message":"..."}` — LLM/网络/工具等异常。
- `{"type":"cleared"}` — 对 clear 的确认。
- `{"type":"interrupted","reason":"question|interrupt|always"}` — 自动 barge-in 时下发（仅 `INTERRUPT_MODE=always/smart` 触发），前端做自然收尾（渐弱 + 「我在听」倾听表情）。详见第 11 章。

协议小结：
- 连接必须先 `hello` 后才能处理其他帧；
- 同一个 sid 可以被多个连接共享（不同设备接入）；
- 会话隔离在 sid 层，不在 ws 连接层；为了上下文不串用户，前端需每个会话独立生成 sid。

LLM 输出约定（system prompt 引导）:
- 短句口语化；可用行内标记表达动作，如 `[表情:开心]` / `[动作:点头]`。
- 中央调度（text_router.route）负责把标记剥离成 `action` 消息，纯文本作为 `sentence`。
- 涉及实时 / 不确定内容需优先调用 web_search 进行联网查证。

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

`backend/config.py` 中的环境变量，分组列出（第 13 章会给出「所有环境变量总览」，这里只列与工具相关的）：

- `ENABLE_TOOLS`（默认 1）：总开关。设为 0 则走原有流式链路（不调工具）。
- `TOOL_MAX_ROUNDS`（默认 3）：工具循环最多轮次，防止无限调用。
- `TOOL_MAX_PERMISSION`（默认 `read`）：可暴露给 LLM 的最高权限；`write`/`dangerous` 需显式提升。
- `TAVILY_API_KEY` / `TAVILY_URL`：联网搜索后端。

### 10.5b 联网搜索检索参数（web_search / Tavily 调优）
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
