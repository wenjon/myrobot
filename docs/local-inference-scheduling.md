# 本地推理并发与调度：单 NPU 到底能不能并发

> 适用场景：机器人头部 Demo 切到本地模型（FastFlowLM / Ollama / llama.cpp）后，
> 多个页面同时说话、或后台摘要与用户对话撞车时，如何保证「人说话优先」。
>
> 实测环境：AMD Radeon 8060S（gfx1151，20 CU）+ XDNA2 NPU，Windows 11，
> ROCm 7.2，FastFlowLM v0.9.38，Python 3.12。数据采集于 2026-11-04。

## 1. 结论先说

1. **一个 NPU 实例没有真并发。** 同一 FLM 实例同时发 3 个请求，墙钟 9.46s，
   而串行 3 次是 8.83s——并发不仅没加速，还略慢。
2. **多开实例也没用。** 两个 FLM 进程同时推理时，TTFT 分别从 0.96s / 0.58s
   涨到 3.71s / 1.78s，总耗时接近串行，只是多占 11.4GB 内存。
3. **机器人不需要「多个 LLM 请求并行」，需要的是「流水线重叠」。**
   ASR、LLM、TTS、口型分别跑在浏览器 / NPU / 云端 / GPU 上，只要流式衔接，
   用户感知的是「一句话没说完就开始回应」，而不是「同时回答三个人」。
4. **本地部署的正确姿势：单实例 + 应用层排队 + 前台优先 + 后台可等。**
   已在 `backend/pipeline/llm_gate.py` 落地，云端供应商默认不启用。

## 2. 实测数据

### 2.1 单请求基线

提示词「请用三句话介绍杭州」，`max_tokens=120`：

| 模型 | TTFT | 解码速度 | 约 55~62 字耗时 |
|---|---|---|---|
| `qwen3.5:2b` | **0.58 s** | **24.2 tok/s** | 3.0 s |
| `qwen3.5:4b` | 0.96 s | 12.8 tok/s | 6.0 s |

短任务（2b，`max_tokens=16`，情绪判定）：**0.72 s**。

### 2.2 同实例 3 并发（本次补测）

条件：`flm serve qwen3.5:2b -p 52625`，同一提示词，`max_tokens=60`，
3 个线程用 `Barrier` 同时发车，统计 SSE 首字时间。

| 请求 | TTFT | 总耗时 |
|---|---:|---:|
| 串行 #1 | 0.622 s | 2.952 s |
| 串行 #2 | 0.604 s | 2.935 s |
| 串行 #3 | 0.610 s | 2.940 s |
| 并发 #1 | **3.866 s** | 6.199 s |
| 并发 #2 | **7.128 s** | 9.456 s |
| 并发 #3 | 0.602 s | 2.936 s |

- 串行墙钟：**8.827 s**
- 并发墙钟：**9.460 s**
- 加速比：**0.93×**（小于 1）

三个并发请求拿到的是三个**首尾相接的时间片**，不是三份算力。
第一个请求 0.6s 开口，第二个要等前一个跑完（约 3.9s），第三个再等一轮（约 7.1s）。

### 2.3 双实例并发（此前实测）

条件：`qwen3.5:4b` 占 52625，`qwen3.5:2b` 占 52626，两个进程同时推理。

| 指标 | 单跑 | 同时跑 |
|---|---:|---:|
| 4b TTFT | 0.96 s | 3.71 s（3.9×） |
| 2b TTFT | 0.58 s | 1.78 s（3.1×） |
| 总耗时 | 约 6.0 + 3.0 s | 9.57 s（≈ 串行） |
| 内存 | — | 6.98 + 4.41 = **11.4 GB** |

另一个实例**空转**时完全不影响性能；一旦同时推理，就回到分时切片。
结论：多开实例只增加内存占用，不增加吞吐。

## 3. 为什么会这样

### 3.1 NPU 是一个队列，不是一组核

FLM 的 `serve` 参数里：

- `-q / --q-len`：**NPU 队列长度**（默认 10），不是并行度；
- `-s / --socket`：最大 socket 连接数，也不是并行度；
- `--preemption`：是否允许抢占，改变的是排队顺序，不增加算力。

请求进来后由同一个 NPU 队列串行消费。连接可以有很多，执行只有一个。

### 3.2 两个 FLM 实例仍然共享同一块 NPU

进程隔离了内存，但没有隔离计算单元。操作系统把 NPU 时间片分给两个进程，
于是两边都变慢，总吞吐不变。

### 3.3 对机器人产品意味着什么

「并发」有两种，别混：

| 类型 | 含义 | 单 NPU 是否支持 |
|---|---|---|
| 吞吐并发 | 同时服务多个用户，各自尽快得到回答 | ❌ 只能排队 |
| 流水线并发 | ASR / LLM / TTS / 渲染同时推进，掩盖单段延迟 | ✅ 本来就在做 |

机器人只有一张嘴、一个用户在场时，**首字延迟**比吞吐重要得多。
真正该优化的是：别让后台任务（摘要、画像提炼）挡住下一句话。

## 4. 四种方案对比

| 方案 | 做法 | 优点 | 代价 | 建议 |
|---|---|---|---|---|
| A. 单实例串行 + 优先级 | 一个 FLM 进程，应用层排队 | 内存最省、行为可预测、前台优先 | 多用户时后者要等 | **本地首选** |
| B. 多实例 | 2b/4b 各起一个进程 | 看似能分工 | 内存翻倍、仍然串行、切换模型有加载成本 | 不推荐 |
| C. 混合后端 | NPU 跑主对话，iGPU/CPU/云端跑辅助 | 真正并行 | 需两套模型与路由，共享内存带宽 | P1 验证 |
| D. 云端主 + 本地兜底 | 默认 MaaS/Ark，断网切本地 | 并发与质量最好 | 依赖网络与额度 | 当前 Demo 默认 |

方案 C 的依据：本机 Ollama 0.40 已能把 `gfx1151` 识别为 ROCm 设备，
而 FLM 走的是独立的 XDNA2 NPU——**计算单元不同**，理论可以重叠。
但两者共享统一内存带宽，实际干扰尚未在本机量化，所以标为「待实测」，
不要当成已验证结论。

## 5. 推荐架构

### 5.1 单机本地版

```text
麦克风/浏览器 ASR
      │  文本
      ▼
上下文拼接（滑动窗口 + 摘要 + 画像）
      │
      ▼
┌─────────────────────────────┐
│  LLM 闸门（单槽，前台优先）  │
│  foreground: 用户本轮回答    │
│  background: 摘要/画像提炼   │
└─────────────────────────────┘
      │  SSE 流
      ▼
中央调度（表情/动作标记） → TTS 分句 → 3D 口型
```

- 用户本轮：`priority=0`，拿不到槽位时最多等一个正在生成的请求；
- 后台摘要：`priority=10`，永远排在所有前台请求之后；
- 摘要失败/被推迟不影响本轮对话，`dropped_buffer` 会保留到下次。

### 5.2 模型怎么选

| 目标 | 选择 | 理由 |
|---|---|---|
| 语音秒回、Demo 默认 | `qwen3.5:2b` | TTFT 0.6s，一句话 3s 内说完 |
| 回答质量优先 | `qwen3.5:4b` | TTFT 1.0s，仍可接受，但长答要 6s |
| 需要联网/工具 | 云端 MaaS/Ark | FLM 2b/4b **不支持原生 tools** |

实测：给 FLM 2b 传 `tools` 数组，它不会返回 `tool_calls`，而是直接编一段文字回答。
所以本地 FLM 适合纯聊天；联网搜索、文档、数据库、硬件 API 仍走云端工具链，
或另起一个支持 tool calling 的 llama.cpp 模型。

### 5.3 接法

FLM 同时兼容 OpenAI `/v1/chat/completions` 与 Ollama `/api/tags`，
因此可以直接复用现有 `llamacpp` 供应商：

```powershell
# 1) 起 NPU 服务（一次只加载一个模型）
& "D:\Program Files\flm\flm.exe" serve qwen3.5:2b -p 52625 --pmode performance
```

```dotenv
# 2) .env
LLM_PROVIDER=llamacpp
LLAMACPP_URL=http://127.0.0.1:52625/v1
LLAMACPP_MODEL=qwen3.5:2b
LLM_MAX_CONCURRENCY=0        # 自动：本地串行
```

已验证：FLM 会忽略 `chat_template_kwargs` 这类额外字段，不会因此报错；
流式 SSE 正常。

## 6. 代码落点

| 文件 | 作用 |
|---|---|
| `backend/pipeline/llm_gate.py` | `PriorityGate`：单槽/多槽 + 优先级 + FIFO + 取消不泄漏 |
| `backend/pipeline/llm_client.py` | 所有 `stream_chat / chat_once / chat_with_tools` 统一过闸门 |
| `backend/config.py` | `LLM_MAX_CONCURRENCY`：0=自动（本地 1、云端不限） |
| `backend/server_app/dialog.py` | 摘要/画像提炼显式走 `PRIORITY_BACKGROUND`；两层流式生成器用 `aclosing` 关闭 |
| `scripts/check_llm_gate.py` | 串行 / 前台优先 / FIFO / 取消 / 异常 / 打断归还 自检 |

注意：闸门**不提高吞吐**，也不抢占已经开始的后台请求。
它的价值是「顺序可控 + 人优先」，不是「单卡变多卡」。

### 6.1 参数

| 变量 | 默认 | 说明 |
|---|---|---|
| `LLM_MAX_CONCURRENCY` | `0` | 0=自动（本地 1、云端不限）；-1=强制不限；>0=显式限制 |
| FLM `-q` | `10` | NPU 队列长度；排队上限，不是并行度 |
| FLM `--preemption` | `0` | 允许抢占；只改排队策略，不加算力 |

### 6.2 打断时槽位怎么释放

这是单槽闸门最容易踩的坑：槽位不是被 LLM 占住，而是被**没关闭的异步生成器**占住。

`run_dialog` 里是两层 async generator 套着用：

```
route(token_stream())  →  token_stream()  →  agent_stream()  →  stream_chat()
```

Python 的 `async for ... : break` **不会**自动关闭被迭代的异步生成器（PEP 525 明确如此，
和 `yield from` 不一样）。于是：

1. 用户打断 → `break` → `route` 和 `agent_stream` 停在 `yield` 处不关闭；
2. `stream_chat` 里的 `async with _slot(...)` 永远走不到 `finally`；
3. 单槽闸门 `in_use` 一直是 1 → **之后所有提问都在排队**，表现为「说一次话之后就不理人了」。

修法是显式关闭，且要**从外到内**：

```python
from contextlib import aclosing

tokens = token_stream()
events = route(tokens)
async with aclosing(tokens), aclosing(events) as stream:   # 退出顺序：events → tokens
    async for event in stream:
        if cancel.is_set():
            break
```

`async with` 的退出顺序与书写顺序相反，所以内层写前面、外层写后面，
保证先关 router 再关 token 流。`token_stream` 内部再包一层
`aclosing(agent_stream(...))`，把 `GeneratorExit` 一路传到 `_slot` 的 `finally`。

回归用例：`scripts/check_llm_gate.py` 的 `check_break_releases_slot` /
`check_nested_break_releases_slot`。把测试里的 `aclosing` 去掉，
`in_use` 会停在 1，用例立刻失败——这条断言就是防这个坑的。

## 7. 复现实验

```powershell
# 终端 1：起 FLM
& "D:\Program Files\flm\flm.exe" serve qwen3.5:2b -p 52625

# 终端 2：同口径压测（串行 3 次 + 同时 3 次）
D:\Python\Python312\python.exe scripts\bench_flm_concurrency.py 52625 3 qwen3.5:2b
```

脚本用标准库 `http.client` 读 SSE，统计首字时间与总耗时；
提示词、`max_tokens` 都写死在文件顶部，改动后重跑即可对比。

## 8. 后续（P1）

1. **混合后端实测**：FLM（NPU）+ Ollama（iGPU/CPU）同时跑，量化 TTFT 干扰；
   若可接受，把摘要/画像/情绪判定挪出 NPU。
2. **后台请求可抢占**：新用户消息到达时取消正在跑的摘要请求，并回滚 buffer；
   当前只做到「排队时低优先级」，没做到「跑起来也能让路」。
3. **FLM 升级评估**：本机是 v0.9.38，上游已到 v1.0.11；
   升级后需重跑本页数据，确认 TTFT 与并发行为是否变化。
4. **多用户公平性**：当前是全局 FIFO + 优先级；若以后做多用户 kiosk，
   需要按会话轮转，避免一个会话连续发问饿死其他人。

## 9. 一句话总结

单 NPU 的物理事实改不了，但可以改**排队规则**：
让用户的下一句话永远排在后台摘要前面，让流式管线把首字之后的等待藏起来。
这就是这个 Demo 里「并发」的正确解法。
