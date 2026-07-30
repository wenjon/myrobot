"""Demo 配置。可通过环境变量覆盖。"""
import os

# ---- LLM 供应商切换 ----
# provider = "ark"（火山引擎 Ark，OpenAI 兼容）或 "ollama"（本地）
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ark")

# 本地 Ollama
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma4:12b")

# 火山引擎 Ark（OpenAI 兼容 /chat/completions）
ARK_BASE_URL = os.getenv("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/coding/v3")
ARK_API_KEY = os.getenv("ARK_API_KEY", "REDACTED_ARK_API_KEY")
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
    ),
)

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))

# 日志中的客户端来源地址显示方式：
#   1（默认）= 显示真实来源 IP（信任 cloudflared 等本地反代的转发头）；
#             IPv6 的 v4-映射地址会被还原成纯 IPv4，纯 IPv6 会加方括号。
#   0        = 不信任转发头，来源恒为本机 127.0.0.1:<端口>（永远是 IPv4）。
SHOW_REAL_IP = os.getenv("SHOW_REAL_IP", "1") == "1"



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

# ---- 上下文日志 ----
# 是否在控制台/文件打印每轮上下文与输出
LOG_CONTEXT = os.getenv("LOG_CONTEXT", "1") == "1"
# 上下文日志文件路径（设为空字符串则不写文件，仅控制台）
CONTEXT_LOG_FILE = os.getenv(
    "CONTEXT_LOG_FILE",
    os.path.join(os.path.dirname(__file__), "logs", "context.log"),
)


# ---- 企业微信（自建应用 + 回调）----
# 企业 ID（我的企业 → 企业信息 → 企业ID）
WECOM_CORP_ID = os.getenv("WECOM_CORP_ID", "")
# 自建应用的 AgentId 与 Secret（应用管理 → 自建 → 你的应用）
WECOM_AGENT_ID = os.getenv("WECOM_AGENT_ID", "")
WECOM_SECRET = os.getenv("WECOM_SECRET", "")
# 接收消息服务器配置里的 Token 与 EncodingAESKey（用于回调签名校验 + 消息加解密）
WECOM_TOKEN = os.getenv("WECOM_TOKEN", "")
WECOM_AES_KEY = os.getenv("WECOM_AES_KEY", "")
# 是否启用企业微信回调路由（需以上参数齐全）
WECOM_ENABLED = bool(WECOM_CORP_ID and WECOM_AGENT_ID and WECOM_SECRET and WECOM_TOKEN and WECOM_AES_KEY)
