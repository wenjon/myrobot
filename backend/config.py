"""Demo 配置。可通过环境变量覆盖。"""
import os

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma3:12b")

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
        "你可以用行内标记表达情绪和动作，例如 [表情:开心]、[表情:疑惑]、"
        "[表情:惊讶]、[动作:点头]、[动作:摇头]。"
        "把标记直接写在相应文字前面。不要使用 markdown、emoji 或列表。"
    ),
)

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))
