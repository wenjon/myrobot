#!/usr/bin/env python3
"""扫描仓库，阻止密钥被硬编码回代码里。

背景：本项目曾把 ARK_API_KEY / TAVILY_API_KEY 写在 config.py 的 os.getenv 默认值里，
为推送 GitHub 做过一次历史清洗（见 docs 第 14 章）。这个脚本用来防止同样的坑再踩一次。

用法：
    python scripts/check_no_hardcoded_secrets.py
退出码：0 = 干净；1 = 发现可疑密钥（CI 会报错）。

设计取舍：
  - 只扫 git 跟踪的文本文件，不碰 .env / .venv / 二进制；
  - 宁可漏报也不误报：只匹配特征明确的厂商 key 前缀，以及
    「*_API_KEY 被赋了个非空字面量」这种结构；
  - REDACTED_* 占位符与空字串默认值是合法的。
"""
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# 只检查这些文本后缀（二进制如 avatar.glb 跳过）
TEXT_SUFFIXES = {".py", ".js", ".html", ".css", ".md", ".txt", ".json", ".yml", ".yaml", ".example", ".toml", ".cfg"}

# 不参与扫描的文件（本脚本自己包含模式字符串，否则会自我命中）
SELF = Path(__file__).name

# 已知厂商密钥的特征前缀：一旦出现就是真密钥，几乎不可能误报
VENDOR_PATTERNS = [
    ("Tavily API key", re.compile(r"tvly-[A-Za-z0-9_\-]{16,}")),
    ("OpenAI API key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
]

# 「*_API_KEY / *_TOKEN / *_SECRET 被赋了个非空字面量」。
# 同时覆盖两种写法：
#   KEY = "xxx"                 直接赋值
#   KEY = os.getenv("KEY", "xxx")  当默认值（当初就是这里泄的）
ASSIGN_PATTERN = re.compile(
    r"(?P<name>[A-Z0-9_]*(?:API_KEY|APIKEY|TOKEN|SECRET|PASSWORD))\s*="
    r"(?P<rhs>[^\n]*)"
)

# 赋值右侧出现这些内容视为安全（占位符 / 空值 / 变量引用）
SAFE_HINTS = (
    "REDACTED",
    "your-",
    "YOUR_",
    "xxx",
    "<",
    "${",
)

# 右侧字面量：捕获引号包裹的非空内容
LITERAL_PATTERN = re.compile(r"[\"']([^\"']{8,})[\"']")


def tracked_files():
    """用 git ls-files 拿跟踪文件，天然排除 .gitignore 里的 .env / .venv。"""
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files"],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout
    for rel in out.splitlines():
        rel = rel.strip()
        if not rel:
            continue
        path = REPO / rel
        if path.name == SELF:
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        yield rel, path


def scan_line(rel, lineno, line, findings):
    """单行检查：先查厂商特征前缀，再查可疑赋值。"""
    for label, pattern in VENDOR_PATTERNS:
        m = pattern.search(line)
        if m:
            findings.append((rel, lineno, label, m.group(0)[:12] + "..."))
            return

    m = ASSIGN_PATTERN.search(line)
    if not m:
        return
    rhs = m.group("rhs")
    if any(hint in rhs for hint in SAFE_HINTS):
        return
    lit = LITERAL_PATTERN.search(rhs)
    if not lit:
        # 没有字面量（如 os.getenv("K", "") 或引用变量）→ 安全
        return
    value = lit.group(1)
    # 把环境变量名本身当字面量误捕的情况排除（os.getenv("TAVILY_API_KEY", "")）
    if value == m.group("name") or value.isupper():
        return
    findings.append((rel, lineno, "hardcoded " + m.group("name"), value[:12] + "..."))


def main() -> int:
    findings = []
    checked = 0
    for rel, path in tracked_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        checked += 1
        for lineno, line in enumerate(text.splitlines(), 1):
            scan_line(rel, lineno, line, findings)

    if findings:
        print("发现 %d 处疑似硬编码密钥：" % len(findings))
        for rel, lineno, label, snippet in findings:
            print("  %s:%d  [%s]  %s" % (rel, lineno, label, snippet))
        print("")
        print("请改为从环境变量 / .env 读取，参考 .env.example 与 docs 第 14 章。")
        return 1

    print("OK：已扫描 %d 个跟踪文件，未发现硬编码密钥。" % checked)
    return 0


if __name__ == "__main__":
    sys.exit(main())
