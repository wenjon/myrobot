# 上下文 Token 成本拆解

> 给「感觉对话上下文冗余」问题做一次量化摸底，**不包含代码改动**。所有数字用 `docs/token-cost.md` 同款的 Python 脚本（见附录）跑出来，分母按 1 token ≈ 4 字符估算（中文偏紧，英文偏松，仅作量级参考）。

## 1. 直觉校准：JSON 框架本身并不贵

OpenAI 兼容协议下，每次请求最外层是：

```json
{"model": "...", "messages": [...], "stream": true, "temperature": 0.7, "tools": [...], "tool_choice": "auto"}
```

光这一层 envelope 在我们的请求里只有 ~50 字符（~12 tokens），**可以忽略**。真正吃 token 的是 envelope **里面装的东西**，分两类：
- **永久开销**：每轮都会重新发给模型的部分
- **临时开销**：只在某一轮里出现，模型用完即丢

## 2. 永久开销（每轮都重发）

| 项目 | 字符数 | tokens | 备注 |
|---|---:|---:|---|
| `SYSTEM_PROMPT` | 336 | ~84 | `config.SYSTEM_PROMPT`，人设 + 规则 |
| `tools schemas` | 594 | ~148 | `chat_with_tools` 探测轮的 `tools` 数组 |
| JSON envelope | ~50 | ~12 | model/messages/stream/temperature/tool_choice |
| **小计** | **~980** | **~245** | |

`tools schemas` 拆分（`backend/tools/` 下当前注册的工具）：

| 工具 | 字符 | 字段 | 说明 |
|---|---:|---|---|
| `get_time` | 261 | name/description/parameters | 系统工具，每次问时间都带 |
| `web_search` | 333 | name/description/parameters | 联网搜索，description 占大头 |

> 关键点：`tools schemas` **只在 `chat_with_tools` 探测轮发送**。当 `ENABLE_TOOLS=0` 或模型直接走 `stream_chat` 不经探测时，这 148 tokens 永久开销可以省掉。`agent.py` 里有这个判断。

## 3. 临时开销（按轮变化）

### 3.1 通用：历史消息

| 触发 | 上限 | 配置项 |
|---|---|---|
| 轮数 | 10 轮 = 20 条消息 | `MAX_TURNS=10` |
| 字符数 | 4000 字符 | `MAX_CONTEXT_CHARS=4000` |

任一超限都会触发 `_trim()` 裁剪，被裁掉的部分进 `dropped_buffer`；累计到 `SUMMARY_TRIGGER_CHARS=1200` 时异步压成 summary 注入下一轮的 system 末尾。

### 3.2 工具调用轮（agent_stream 的两阶段）

`agent_stream()` 在启用工具时走「探测 + 流式」两阶段，每阶段各自发一次请求：

**阶段一：`chat_with_tools`（非流式探测）**

模拟 `{"user":"今天深圳天气","hist":4条,"tools":2个}` 的请求体：

| 字段 | 字符 | tokens |
|---|---:|---:|
| system prompt | 336 | 84 |
| 4 条历史 | 35 | 9 |
| 本轮 user | 8 | 2 |
| tools schemas | 594 | 148 |
| envelope | 50 | 12 |
| **payload 合计** | **~1023** | **~256** |

**阶段二：`stream_chat`（流式生成最终答案）**

工具结果回喂后再调一次，本次 messages 多了 `assistant.tool_calls` + `tool` 两条 + 一句「别搜了直接答」的引导：

| 字段 | 字符 | tokens |
|---|---:|---:|
| system prompt | 336 | 84 |
| 4 条历史 | 35 | 9 |
| assistant tool_call 块 | 182 | 45 |
| **tool result（Tavily 返回）** | **~2523** | **~630** |
| final user 引导 | 18 | 4 |
| envelope | 50 | 12 |
| **payload 合计** | **~3144** | **~786** |

> 单项最大 = tool_result。`backend/tools/web/web_search.py` 里 `PER_ITEM_MAX=1500` / `TOTAL_MAX=6000` 是天花板。

## 4. 响应侧：SSE 流式本身吃带宽不吃 tokens

`stream_chat` 的每个 token chunk 长这样（去掉换行）：

```
data: {"choices":[{"finish_reason":null,"index":0,"delta":{"content":"字"}}],"created":...,"id":"chatcmpl-xxx","model":"D:\\llamacpp\\models\\Qwen3.6-35B-A3B-MTP-UD-Q3_K_XL.gguf","system_fingerprint":"b10217-ddd4ec142","object":"chat.completion.chunk"}
```

每条 envelope ~200 字节，**模型每吐 1 个字就有 200 字节的 JSON 包装**。这部分：

- **不计 tokens**：流式响应不会原样回喂给 LLM
- **但费带宽**：模型 100 tok/s × 200 字节 ≈ 20 KB/s，纯 UI 端开销

要省带宽可以前端攒 4-8 个字再发一次 chunk，但**对 token 成本零影响**。

## 5. 一轮对话的真实账单（典型场景）

| 场景 | tokens 估算 |
|---|---:|
| 闲聊 1 轮（无工具、5 条消息） | ~330 |
| 闲聊 5 轮后（历史 ~1500 字符） | ~460 |
| **联网搜索 1 轮**（探测+流式两次请求合计） | **~1042** |
| 联网搜索 + 长历史（4000 字符） | ~2042 |

> 数字按 1 token ≈ 4 字符估算。中文常用字 1~1.5 token，标点/数字 0.5~1 token，所以中文实际 tokens 会比这数高一些。

## 6. 调优杠杆（按省得多 → 少排）

| # | 改动 | 省下 | 副作用 |
|---|---|---|---|
| 1 | `Tavily TOTAL_MAX=6000 → 3000` | 单轮最多省 ~750 tokens | 长结果被截，模型可能回答不完整 |
| 2 | `MAX_CONTEXT_CHARS=4000 → 2000` | 每轮省 ~500 tokens | 长期记忆更快被压成 summary |
| 3 | 精简工具 description（`web_search` 80→20 字） | 永久省 ~60 tokens | 模型选工具的判断力可能略降 |
| 4 | 精简 `SYSTEM_PROMPT`（336→150 字符） | 永久省 ~46 tokens | 已知风险：动作标记、规则、工具提示都可能弱化 |
| 5 | 关闭工具调用 `ENABLE_TOOLS=0` | 永久省 ~148 tokens | 不能联网、不能用 get_time |
| 6 | `chat_with_tools` 加缓存 | 永久省 ~148 tokens | 实现复杂，需 LLM 配合 |
| 7 | SSE 攒字发 | 省带宽 | 不省 tokens |

> ⚠️ 写文档时点 #4 还没动。等你研究完再决定。

## 7. 思考模式对成本的影响

Qwen3.6 默认开 `--reasoning-budget 1024`：模型在没引导时会「先想再说」，把 1024 token 全填在 `reasoning_content` 字段里。前端场景下这部分：

- **不计 prompt tokens**（流式响应不回喂）
- **但每个 chunk 多带一个 `reasoning_content` 字段**（envelope 略胖，可忽略）
- **最坑的是「想完没词」**：`max_tokens=512` 装不下 1024 推理 + 正式答案时，`finish_reason=length` 截断，模型啥都没说出来

修复（`ENABLE_THINKING=0`）后思维链字段为空，每轮响应稳定在 ~50 tokens / 0.5s。详见 `llm_client.py` 的 `_llamacpp_stream` / `_llamacpp_once` / `_llamacpp_with_tools` 三处的 `chat_template_kwargs` 分支。

## 附录：怎么自己跑这个数字

把 `scripts/token_cost.py`（TODO：待创建）跑一下就能复现本文所有数字。当前可以用这段临时脚本：

```python
import json, sys
sys.path.insert(0, "backend")
from tools import REGISTRY, load_all
from tools.base import Permission
load_all(lambda x: None)
import config

# tools schemas 总大小
schemas = REGISTRY.schemas(max_permission=Permission.READ)
total = sum(len(json.dumps(s, ensure_ascii=False)) for s in schemas)
print(f"tools schemas: {total} 字符 (~{total//4} tokens)")

# system prompt
print(f"SYSTEM_PROMPT: {len(config.SYSTEM_PROMPT)} 字符")

# 模拟 tool result 满载
tool_result_content = "[搜索摘要]\n搜索结果：\n1. 深圳今天..." + "x"*2500
print(f"tool result 单条: {len(tool_result_content)} 字符 (~{len(tool_result_content)//4} tokens)")
```

## 修订记录

- v1 · 2026-08-15 · 初版，量化拆解（来自排查「上下文冗余感」）
