"""Demo 配置。可通过环境变量或仓库根目录的 .env 文件覆盖。

约定：
  1. 任何密钥（API Key）都不写死在代码里，只从环境变量 / .env 读取；
     .env 已被 .gitignore 忽略，换机器时照着 .env.example 重新填一份即可。
  2. 其余非敏感参数保留可读的默认值，方便开箱即跑。
"""
import os
from pathlib import Path

# ---- .env 极简加载器 ----
# 只做最小实现（KEY=VALUE / # 注释 / 可选引号），避免为 demo 引入 python-dotenv 依赖。
# 已存在的真实环境变量优先级更高，不会被 .env 覆盖。
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _load_env_file(path: Path = _ENV_FILE) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # setdefault 语义：外部已显式导出的环境变量优先
        os.environ.setdefault(key, value)


_load_env_file()

# ---- LLM 供应商切换 ----
# provider = "ark"（火山引擎 Ark，OpenAI 兼容）/ "ollama"（本地）/ "llamacpp"（本地 llama.cpp OpenAI 兼容服务）
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ark")

# 本地 Ollama
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma4:12b")

# 本地 llama.cpp server（OpenAI 兼容 /v1/chat/completions，默认无需 key）
LLAMACPP_URL = os.getenv("LLAMACPP_URL", "http://127.0.0.1:8080/v1")
LLAMACPP_MODEL = os.getenv("LLAMACPP_MODEL", "Qwen3.6-35B-A3B-MTP")
# 留空则不发 Authorization 头；某些中转/反代可能要求 Bearer 鉴权
LLAMACPP_API_KEY = os.getenv("LLAMACPP_API_KEY", "")

# Qwen3 思考模式开关：1=启用深度思考（响应慢但质量高）；0=关闭（秒回，适配语音对话）
ENABLE_THINKING = os.getenv("ENABLE_THINKING", "0") == "1"

# 火山引擎 Ark（OpenAI 兼容 /chat/completions）
ARK_BASE_URL = os.getenv("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/coding/v3")
# 密钥不落代码：从环境变量 / .env 读取，缺失时为空字符串（启动时会给出提示）
ARK_API_KEY = os.getenv("ARK_API_KEY", "")
ARK_MODEL = os.getenv("ARK_MODEL", "ark-code-latest")

# 智能分句参数
SENTENCE_MIN_LEN = int(os.getenv("SENTENCE_MIN_LEN", "8"))
SENTENCE_MAX_LEN = int(os.getenv("SENTENCE_MAX_LEN", "24"))

# 强制断句标点
STRONG_PUNCT = set("。！？!?…\n")
# 次级断句标点（达到 MIN_LEN 后可切）
WEAK_PUNCT = set("，,；;、：:")

SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    (
        "你是一个可爱的桌面机器人头像，名字叫小柚。"
        "用简短、口语化的中文回答，句子要短。"
        "你可以用行内标记表达情绪和动作，例如 [表情:开心]、[表情:悲伤]、[表情:生气]、[表情:惊讶]、[表情:疑惑]、[表情:平静]、[动作:点头]、[动作:摇头]。"
        "把标记直接写在相应文字前面。不要使用 markdown、emoji 或列表。"
        "涉及实时信息、最新新闻、当前时间/天气、以及你不确定或记不全的具体内容时，"
        "要优先调用 web_search 联网查证，不要凭记忆编造；查到后再用口语简短转述。"
        # 以下两项是之前出过现象的补充：长文本请求不要走工具路线，工具不是“说话工具”。
        "遇到背诵长文、大段录入类请求，不要拆成多句硬说："
        "能背出多少就背多少，背完后主动问要不要继续；记不全就直说记不全。"
        "工具只是取数据的手段，不要以为调个工具就能把内容说出去，不要用工具输出文本给用户。"
    ),
)

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))

# 日志中的客户端来源地址显示方式：
#   1（默认）= 显示真实来源 IP（信任 cloudflared 等本地反代的转发头）；
#             IPv6 的 v4-映射地址会被还原成纯 IPv4，纯 IPv6 会加方括号。
#   0        = 不信任转发头，来源恒为本机 127.0.0.1:<端口>（永远是 IPv4）。
SHOW_REAL_IP = os.getenv("SHOW_REAL_IP", "1") == "1"



# ---- 工具调用（tools / function calling）----
# 是否启用工具调用（让 LLM 能调用 get_time / web_search 等）
ENABLE_TOOLS = os.getenv("ENABLE_TOOLS", "1") == "1"
# 单轮对话中最多连续调用工具的回合数（防止无限循环）
TOOL_MAX_ROUNDS = int(os.getenv("TOOL_MAX_ROUNDS", "3"))
# 允许暴露给 LLM 的最高权限级别：read / write / dangerous
# 默认 read：只放开只读工具；硬件写操作等需显式提升。
TOOL_MAX_PERMISSION = os.getenv("TOOL_MAX_PERMISSION", "read")

# 联网搜索（Tavily）
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
TAVILY_URL = os.getenv("TAVILY_URL", "https://api.tavily.com/search")

# ---- 轮次策略（数字人说话时又收到新消息怎么办）----
# queue  = 排队，说完当前句再依次说（最稳，旧行为）
# always = 硬打断，任何新消息都立即中止当前播报
# smart  = 智能软打断（推荐）：附和词继续说，打断词/新提问才中止当前轮
INTERRUPT_MODE = os.getenv("INTERRUPT_MODE", "smart")

# ---- 上下文管理 ----
# 滑动窗口保留最近 N 轮（1 轮 = user + assistant）
MAX_TURNS = int(os.getenv("MAX_TURNS", "10"))
# token 预算（近似，用字符数估算），超过则触发裁剪/摘要
MAX_CONTEXT_CHARS = int(os.getenv("MAX_CONTEXT_CHARS", "4000"))
# 是否启用摘要式长期记忆
ENABLE_SUMMARY = os.getenv("ENABLE_SUMMARY", "1") == "1"
# 摘要触发：被裁掉的历史累计超过该字符数就压缩成摘要
SUMMARY_TRIGGER_CHARS = int(os.getenv("SUMMARY_TRIGGER_CHARS", "1200"))
# 会话空闲多久后回收（秒）
SESSION_TTL = int(os.getenv("SESSION_TTL", "3600"))

# ---- 记忆模块（第 16 章）----
# 记忆落盘目录。里面会存 sessions/<id>.json 与 long_term.json。
# 注意：long_term.json 含用户个人信息，已在 .gitignore 里忽略 backend/data/，
# 不要入库、不要跳机器复制。
MEMORY_DATA_DIR = os.getenv(
    "MEMORY_DATA_DIR", str(Path(__file__).resolve().parent / "data" / "memory")
)
# 是否启用磁盘持久化（0 = 纯内存，重启就忘，方便跑测试）
MEMORY_PERSIST = os.getenv("MEMORY_PERSIST", "1") == "1"
# 中量级刷盘：每 N 轮强制落盘一次，应对进程崩溃丢记忆
MEMORY_FLUSH_EVERY_N_TURNS = int(os.getenv("MEMORY_FLUSH_EVERY_N_TURNS", "5"))
# 贴入 system 的长期记忆（摘要 + 画像）字符上限，防止撑爆上下文
MEMORY_MAX_LONG_TERM_CHARS = int(os.getenv("MEMORY_MAX_LONG_TERM_CHARS", "2000"))

# 用户画像只提炼这 3 个字段（已定稿，见 docs § 16.6）。
# 为什么不要 key_facts：它会变成「什么都能往里塞」的垃圾桶，
# LLM 提炼时目标不清；高代价信息（如过敏史）归到 preferences 下。
PROFILE_FIELDS = ["name", "preferences", "occupation"]
# profile 冲突采用 B 方案（人工确认）：超时未回应则**保旧**
PROFILE_CONFLICT_TIMEOUT_S = int(os.getenv("PROFILE_CONFLICT_TIMEOUT_S", "60"))
# 一轮最多下发多少条冲突确认卡，超出的只写日志，下轮重评估
PROFILE_MAX_CONFLICTS_PER_TURN = int(os.getenv("PROFILE_MAX_CONFLICTS_PER_TURN", "3"))

# ---- 上下文日志 ----
# 是否在控制台/文件打印每轮上下文与输出
LOG_CONTEXT = os.getenv("LOG_CONTEXT", "1") == "1"
# 上下文日志文件路径（设为空字符串则不写文件，仅控制台）
CONTEXT_LOG_FILE = os.getenv(
    "CONTEXT_LOG_FILE",
    os.path.join(os.path.dirname(__file__), "logs", "context.log"),
)

