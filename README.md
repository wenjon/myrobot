# myrobot — 机器人头部交互式对话（软件模拟 Demo）

基于《机器人头部交互式对话全链路技术方案（流式低延迟架构正式版）》的**纯软件模拟**实现。
无需物理零件，用浏览器里的 2D 虚拟头替代物理伺服头部，跑通全链路流式对话。

链路：`文本/语音输入 → (ASR) → LLM(Ollama) → 文本解析中央调度 → TTS(Web Speech) → 2D 口型/表情`

## 依赖
- **Ollama**（本地已运行，默认模型 `gemma3:12b`）
- **Python 3.10+**：`pip install -r backend/requirements.txt`
- 现代浏览器（Chrome/Edge 推荐，含 Web Speech 合成与识别）

## 运行
```powershell
# 1) 确认 Ollama 在跑： http://127.0.0.1:11434
# 2) 启动后端（同时提供前端静态页）
cd backend
python server.py
# 3) 打开浏览器
#    http://127.0.0.1:8000/app/index.html
```

可用环境变量覆盖配置（见 `backend/config.py`）：
- `LLM_PROVIDER`（`ark` 火山引擎 / `ollama` 本地，默认 `ark`）
- Ark：`ARK_BASE_URL`、`ARK_API_KEY`、`ARK_MODEL`（默认 `ark-code-latest`）
- `OLLAMA_MODEL`（默认 `gemma3:12b`）
- `OLLAMA_URL`、`PORT`、`SYSTEM_PROMPT`、`SENTENCE_MIN_LEN`、`SENTENCE_MAX_LEN`

## 交互
- 输入中文回车发送；机器人流式逐句播报并做口型动画。
- 🎤 语音：浏览器语音识别（需浏览器支持）。
- 打断：停止当前播报（barge-in 模拟）。
- LLM 输出的 `[表情:开心]`/`[动作:点头]` 会驱动表情与点头。

## 目录
- `docs/superpowers/specs/` — 设计规格（superpowers 流程产出）
- `backend/` — FastAPI + WebSocket 流式后端
  - `pipeline/llm_client.py` — Ollama 流式客户端
  - `pipeline/text_router.py` — 文本解析中央调度（清洗/分句/动作分流）
  - `server.py` — WS 编排 + 静态托管
- `frontend/` — Three.js 3D 数字人（Ready Player Me 风格头像 + ARKit blendshape/Oculus viseme）
  - `src/head.js` `src/tts.js` `src/viseme.js` `src/ws.js` `src/main.js`

## 与原方案的对应 & 简化
| 原方案环节 | Demo 实现 | 说明 |
|---|---|---|
| 流式 ASR | 浏览器语音识别 / 文本输入 | 后续可换 faster-whisper |
| 流式 LLM | Ollama stream | ✅ 真流式 |
| 文本解析中央调度 | `text_router.py` | ✅ 清洗/智能分句/动作提前下发 |
| 流式 TTS + 时间戳 | Web Speech + `boundary` 事件 | 字级时间戳驱动口型 |
| Visme 口型模型 | `viseme.js` 2D 口型映射 | 简化；接口可升级 3D BlendShape |
| 伺服时序补偿 | 前端插值占位 | 物理层留待后续 |

## 已知限制（Demo）
- TTS 用浏览器 Web Speech（本机 edge-tts/SAPI 在此环境不稳定）；口型对齐为 demo 级。
- 中文口型用拼音粗分/字符兜底，非真音素级。
- 首次回答含 Ollama 模型加载耗时，二次更快。


## 数字人（3D 版）
- 前端已从 2D 圆脸升级为 **3D 数字人**（`frontend/src/avatar.glb`，含 15 个 Oculus viseme + ARKit 表情 blendshape）。
- 口型：TTS 字级时间戳 → 拼音韵母 → viseme（`frontend/src/viseme.js`）→ blendshape 实时驱动（`frontend/src/head3d.js`）。
- 表情：LLM 输出 `[表情:开心/悲伤/生气/惊讶/疑惑/平静]` → blendshape 组合；`[动作:点头/摇头]` → 头部旋转。
- 全部 WebGL 渲染，AMD/集显均可，无需 CUDA/GPU 推理，保留全链路流式与打断。

### 为什么没用 Wav2Lip/SadTalker/MuseTalk
- 它们依赖 NVIDIA CUDA；AMD+Windows 无法使用（ROCm 不支持 Windows，DirectML 未适配这些仓库）。
- 且它们是「整段音频→出 mp4」的离线批处理，非流式，与本方案的低延迟/可打断目标冲突。
- 3D 数字人方案在无 GPU 推理下即可实时驱动口型与表情，是更契合的替代。

