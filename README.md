# myrobot — 机器人头部交互式对话（软件模拟 Demo）

基于《机器人头部交互式对话全链路技术方案（流式低延迟架构正式版）》的**纯软件模拟**实现。
无需物理零件，用浏览器里的 **Three.js 3D 数字人**替代物理伺服头部，跑通全链路流式对话。

链路：`文本/语音输入 → (ASR) → LLM(Ark / llama.cpp / Ollama) → 文本解析中央调度(+工具调用) → TTS(Web Speech) → 3D viseme 口型/表情`

## 本地自检

推送前可以先本机跑一遗（与 CI 完全一致的三项检查）：

```powershell
python -m compileall -q backend/ scripts/          # Python 语法
python scripts/check_no_hardcoded_secrets.py       # 密钥硬编码扫描
node --check frontend/src/main.js                  # 前端 JS 语法
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

## 交互
- 输入中文回车发送；机器人流式逐句播报并做口型动画。
- 🎤 语音：浏览器语音识别（需浏览器支持）。
- 打断：停止当前播报（barge-in 模拟）。
- LLM 输出的 `[表情:开心]`/`[动作:点头]` 会驱动表情与点头。

## 设计文档

完整设计规格（共 17 章）：`docs/superpowers/specs/2026-07-27-robot-head-design.md`

常用章节速查：

| 想了解 | 看第几章 |
|---|---|
| 整体架构与数据流 | 3 |
| WebSocket 消息完整定义 | 5 |
| 口型/表情怎么驱动的 | 7 |
| 工具调用（联网搜索等） | 10 |
| 打断策略与自然收尾 | 11 |
| 所有配置项含义 | 13 |
| 密钥管理 | 14 |
| 记忆模块设计（尚未实现） | 16 |
| 手机/外网访问与排障 | 17 |

## 目录
- `docs/superpowers/specs/` — 设计规格（superpowers 流程产出）
- `backend/` — FastAPI + WebSocket 流式后端
  - `pipeline/llm_client.py` — LLM 流式客户端（Ark / llama.cpp / Ollama 三选一）
  - `pipeline/text_router.py` — 文本解析中央调度（清洗/分句/动作分流）
  - `server.py` — WS 编排 + 静态托管
- `frontend/` — Three.js 3D 数字人（Ready Player Me 风格头像 + ARKit blendshape/Oculus viseme）
  - `src/head3d.js` `src/tts.js` `src/viseme.js` `src/ws.js` `src/main.js`

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

