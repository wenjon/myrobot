# myrobot — 机器人头部交互式对话（软件模拟 Demo）

基于《机器人头部交互式对话全链路技术方案（流式低延迟架构正式版）》的**纯软件模拟**实现。
无需物理零件，用浏览器里的 **Three.js 3D 数字人**替代物理伺服头部，跑通全链路流式对话。

链路：`文本/语音输入 → (ASR) → 记忆拼接(滑动窗口+摘要+用户画像) → LLM(Ark / llama.cpp / Ollama) → 文本解析中央调度(+工具调用) → TTS(Edge 神经语音 + 韵律引擎 / Web Speech 兜底) → 3D viseme 口型/表情 + 注视转头`

## 特性

- 🗣️ **18 种表情 + 21 种动作**（ARKit 52 blendshape 全部启用）
- 👀 **眼球注视 + 颈骨分层转头** — 眼睛先到、头后跟上，随机扫视 + 呼吸微摆
- 🎤 **Edge 神经语音** + 句内抑扬顿挫韵律引擎 + 表情驱动声音情绪
- 😮‍💨 **15 个 viseme 口型同步** — 逐字时间轴驱动，不依赖浏览器 boundary 事件
- 🛠️ **工具调用框架** — 联网搜索等插件式工具，加一个函数就够
- 🧠 **三层记忆** — 工作记忆 / 情景记忆 / 语义画像（含冲突确认）
- 🎭 **可换脸 + 预览页** — `/app/preview.html` 试驱表情口型后再决定换哪个
- ⚡ **流式对话 + 可打断** — 首句延迟 < 2s，随时打断自然收尾
- 🔌 **多 LLM 供应商** — 火山引擎 Ark / llama.cpp / Ollama 一行配置切换

## 快速开始

```powershell
# 1. 装依赖
pip install -r backend/requirements.txt

# 2. 填密钥
copy .env.example .env
# 编辑 .env，至少填 ARK_API_KEY 或 LLM_PROVIDER 切成本地模型

# 3. 启动
cd backend
python server.py

# 4. 打开浏览器
# http://127.0.0.1:8000/app/index.html
```

更多说明见 `docs/superpowers/`。

## 目录结构

```
myrobot/
├── backend/                    Python 后端
│   ├── pipeline/               核心管线（llm / agent / text_router / tts / prosody ...）
│   ├── tools/                  插件式工具
│   ├── memory/                 三层记忆
│   └── server.py               FastAPI 入口
├── frontend/                   浏览器前端
│   └── src/                    head3d.js / tts.js / viseme.js / ws.js ...
├── docs/superpowers/
│   ├── prd.md                  需求文档
│   └── specs/...               设计规格
├── scripts/                    CI 检查脚本 + 工具
└── avatar_candidates/          候选 3D 模型（不入库，本地预览用）
```

## 本地自检

推送前可以先本机跑一遗（与 CI 完全一致的检查）：

```powershell
python -m compileall -q backend/ scripts/          # Python 语法
python scripts/check_no_hardcoded_secrets.py       # 密钥硬编码扫描
python scripts/check_tool_schemas.py               # 工具 JSON Schema 自检
python scripts/check_memory.py                     # 记忆模块自检（67 项断言）
python scripts/check_expressions.py                # 表情/动作 三处一致性 + blendshape 覆盖率
python scripts/check_tts.py                        # TTS 帧解析 + 韵律规划 + 在线合成
node --check frontend/src/*.js                     # 前端 JS 语法
```

每次 `git push` 后 GitHub Actions 会自动跑同样的检查（`.github/workflows/checks.yml`），
全是静态检查、**不需要配置任何 Secrets**。详见 docs 第 15 章。

## 密钥配置（换机器必做）

仓库**不包含任何 API Key**。首次在一台新机器上跑，需要自己建 `.env`：

```powershell
# 仓库根目录
copy .env.example .env
# 然后编辑 .env，填入 ARK_API_KEY（火山引擎）和 TAVILY_API_KEY（联网搜索，可留空）
```

- `.env` 已被 `.gitignore` 忽略，永远不会被提交。
- `backend/config.py` 启动时自动读取根目录 `.env`；**已导出的真实环境变量优先级更高**。
- 服务启动时会自检：缺 `ARK_API_KEY` 或 `TAVILY_API_KEY` 会在控制台打印警告，但不阻断启动。
- 完全不想用云端 LLM？把 `.env` 里改成 `LLM_PROVIDER=llamacpp`（llama.cpp 的 OpenAI 兼容服务）或 `LLM_PROVIDER=ollama` 即可走本地模型，无需任何密钥。

## 记忆与隐私（重要）

机器人会把对话记忆写到本机：

```
backend/data/memory/
├─ sessions/<session_id>.json   滑动窗口历史 + 会话摘要
└─ long_term.json              跳会话的用户画像（称呼 / 偏好 / 职业）
```

- **个人记忆不走 git**：`backend/data/` 已被 `.gitignore` 忽略，不会被提交、也不要跳机拷贝。
- **公共机器 / 展示场合**请设 `MEMORY_PERSIST=0`，这时全部记忆只在内存，重启就忘。
- 想手动清空：删掉 `backend/data/memory/` 目录即可；或在页面上点「清空上下文」（会同步删盘）。
- 机器人提炼到的画像变更（如「以后叫我老王」）不会默默生效：
  与旧值冲突时前端会弹**确认卡**，你不拍板就**保旧**。设计缘由见 docs 第 16 章。

## 在另一台机器上继续开发

```powershell
git clone <你的仓库地址> myrobot
cd myrobot
copy .env.example .env      # 填入密钥
pip install -r backend/requirements.txt
python backend\server.py
# 浏览器打开 http://127.0.0.1:8000/app/index.html
```

需要手机/外网访问时，用 Cloudflare 快速隧道把 8000 端口暴露出去（WS 与 HTTP 共用同一域名，前端会按页面协议自动选 `ws`/`wss`）：

```powershell
cloudflared tunnel --url http://127.0.0.1:8000
# 访问输出的 https://xxx.trycloudflare.com/app/index.html
```

## 依赖
- **LLM**：默认火山引擎 Ark（需 `ARK_API_KEY`）；或本地 **llama.cpp**（`LLM_PROVIDER=llamacpp`，默认 `http://127.0.0.1:8080/v1`）/ **Ollama**（`LLM_PROVIDER=ollama`），两者均无需密钥
- **Python 3.10+**：`pip install -r backend/requirements.txt`
- 现代浏览器（Chrome/Edge 推荐，含 Web Speech 合成与识别）

## 运行
```powershell
# 1) 填好 .env（见上一节）；若用 llama.cpp 确认 http://127.0.0.1:8080 在跑，若用 Ollama 确认 http://127.0.0.1:11434
# 2) 启动后端（同时提供前端静态页）
python backend\server.py
# 3) 打开浏览器
#    http://127.0.0.1:8000/app/index.html
```

可用环境变量覆盖配置（见 `backend/config.py`）：
- `LLM_PROVIDER`（`ark` 火山引擎 / `llamacpp` 本地 llama.cpp / `ollama` 本地 Ollama，默认 `ark`）
- Ark：`ARK_BASE_URL`、`ARK_API_KEY`、`ARK_MODEL`（默认 `ark-code-latest`）
- llama.cpp：`LLAMACPP_URL`（默认 `http://127.0.0.1:8080/v1`）、`LLAMACPP_MODEL`（默认 `Qwen3.6-35B-A3B-MTP`）、`LLAMACPP_API_KEY`（可空）
- `ENABLE_THINKING`（默认 `0`）— Qwen3 深度思考开关；关掉后首句延迟从 ~4s 降到 ~0.7s
- `OLLAMA_MODEL`（默认 `gemma4:12b`）
- `OLLAMA_URL`、`PORT`、`SYSTEM_PROMPT`、`SENTENCE_MIN_LEN`、`SENTENCE_MAX_LEN`
- 记忆：`MEMORY_PERSIST`（默认 `1`，设 `0` 则纯内存）、`MEMORY_DATA_DIR`、`MEMORY_FLUSH_EVERY_N_TURNS`（默认 `5`）、`MEMORY_MAX_LONG_TERM_CHARS`（默认 `2000`）
- 画像冲突：`PROFILE_CONFLICT_TIMEOUT_S`（默认 `60`，超时保旧）、`PROFILE_MAX_CONFLICTS_PER_TURN`（默认 `3`）

## 交互
- 输入中文回车发送；机器人流式逐句播报并做口型动画。
- 🎤 语音：浏览器语音识别（需浏览器支持）。
- 打断：停止当前播报（barge-in 模拟）。
- LLM 输出的 `[表情:开心]`/`[动作:点头]` 会驱动表情与点头。

## 设计文档

- 需求文档（做什么、为什么、优先级）：`docs/superpowers/prd.md`
- 设计规格（怎么实现，共 22 章）：`docs/superpowers/specs/2026-07-27-robot-head-design.md`
- 专题文档：`docs/rag-design.md`（RAG 知识库设计）、`docs/token-cost.md`（上下文 token 成本拆解）

常用章节速查（设计规格）：

| 想了解 | 看第几章 |
|---|---|
| 整体架构与数据流 | 3 |
| WebSocket 消息完整定义 | 5 |
| 口型/表情/动作/分层驱动 | 7 |
| 工具调用（联网搜索等） | 10 |
| 打断策略与自然收尾 | 11 |
| 所有配置项含义 | 13 |
| 密钥管理 | 14 |
| CI 静态检查 | 15 |
| 记忆分层 / 用户画像 / 持久化 | 16 |
| 手机/外网访问与排障 | 17 |
| TTS 音色与韵律（抑扬顿挫） | 18 |
| 摄像头感知与注视跟随（设计中） | 19 |
| RAG 知识库接入（设计中） | 20 |
| 日志与可观测性 | 21 |
| 上下文 token 成本 | 22 |

## 目录
- `docs/superpowers/specs/` — 设计规格（superpowers 流程产出）
- `scripts/` — 静态自检：密钥扫描 / 工具 Schema / 记忆模块
- `backend/` — FastAPI + WebSocket 流式后端
  - `pipeline/llm_client.py` — LLM 流式客户端（Ark / llama.cpp / Ollama 三选一）
  - `tools/schema.py` — 从函数签名+docstring 自动推导工具 JSON Schema
  - `pipeline/text_router.py` — 文本解析中央调度（清洗/分句/动作分流）
  - `pipeline/conversation.py` — 上下文拼接（滑动窗口 + 摘要反思 + 用户画像）
  - `memory/` — 记忆模块：`types.py` 抽象 / `profile.py` 用户画像与冲突 / `retriever.py` 检索 / `store.py` 原子写持久化
  - `server.py` — WS 编排 + 静态托管
- `frontend/` — Three.js 3D 数字人（Ready Player Me 风格头像 + ARKit blendshape/Oculus viseme）
  - `src/head3d.js` `src/tts.js` `src/viseme.js` `src/ws.js` `src/main.js` `src/profile_card.js`（画像确认卡）

## 与原方案的对应 & 简化
| 原方案环节 | Demo 实现 | 说明 |
|---|---|---|
| 流式 ASR | 浏览器语音识别 / 文本输入 | 后续可换 faster-whisper |
| 流式 LLM | Ark SSE / llama.cpp / Ollama stream | ✅ 真流式 |
| 文本解析中央调度 | `text_router.py` | ✅ 清洗/智能分句/动作提前下发 |
| 流式 TTS + 时间戳 | Web Speech + `boundary` 事件 | 字级时间戳驱动口型 |
| Visme 口型模型 | `viseme.js` → `head3d.js` Oculus viseme blendshape | 已升级 3D；拼音粗分，非真音素级 |
| 伺服时序补偿 | 前端插值占位 | 物理层留待后续 |

## 已知限制（Demo）
- TTS 用浏览器 Web Speech（本机 edge-tts/SAPI 在此环境不稳定）；口型对齐为 demo 级。
- 中文口型用拼音粗分/字符兜底，非真音素级。
- 用本地模型（llama.cpp / Ollama）时首次回答含模型加载耗时，二次更快；Ark 为云端，首包延迟取决于网络。
- 用 Qwen3 类模型务必保持 `ENABLE_THINKING=0`，否则模型先输出一大段思考，数字人会张着嘴呆等好几秒。


## 数字人（3D 版）
- 前端已从 2D 圆脸升级为 **3D 数字人**（`frontend/src/avatar.glb`，含 15 个 Oculus viseme + ARKit 表情 blendshape）。
- 口型：TTS 字级时间戳 → 拼音韵母 → viseme（`frontend/src/viseme.js`）→ blendshape 实时驱动（`frontend/src/head3d.js`）。
- 表情：LLM 输出 `[表情:开心/悲伤/生气/惊讶/疑惑/平静]` → blendshape 组合；`[动作:点头/摇头]` → 头部旋转。
- 全部 WebGL 渲染，AMD/集显均可，无需 CUDA/GPU 推理，保留全链路流式与打断。

### 为什么没用 Wav2Lip/SadTalker/MuseTalk
- 它们依赖 NVIDIA CUDA；AMD+Windows 无法使用（ROCm 不支持 Windows，DirectML 未适配这些仓库）。
- 且它们是「整段音频→出 mp4」的离线批处理，非流式，与本方案的低延迟/可打断目标冲突。
- 3D 数字人方案在无 GPU 推理下即可实时驱动口型与表情，是更契合的替代。

