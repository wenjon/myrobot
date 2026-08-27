"""记忆模块 · 磁盘持久化（FileStore）。

目录结构（docs § 16.9）：

    backend/data/memory/
    ├─ sessions/<session_id>.json    L2 会话：滑动窗口历史 + 待摘要缓冲 + 摘要
    └─ long_term.json               L3 长期：跳会话的用户画像

为什么用 JSON 文件而不是 SQLite：
    单用户、单进程、数据量极小（几 KB），而 JSON 能直接用记事本打开看——
    调试记忆问题时这一点比什么都重要。数据上去了再换 SQLite。

两个必须处理好的工程细节：

1. **原子写**：先写 .tmp 再 os.replace 替换。
   直接覆盖写的话，进程在 write 中途被杀会留下一个截断的、**无法解析的** JSON，
   下次启动直接丢掉所有记忆。os.replace 在同一分区内是原子操作，要么旧的要么新的。

2. **读失败不能炸服务**：记忆文件坏了就当没有记忆（降级），
   绝不能让一个壍了的 JSON 导致整个对话服务启不起来。
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from config import MEMORY_DATA_DIR, MEMORY_PERSIST

# 会话 id 合法字符：只允许十六进制/字母数字与 -_，
# 防止 session_id 里的 ../ 穿越到其他目录（路径穿越）。
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_-]")


def _safe_id(session_id: str) -> str:
    return _SAFE_ID_RE.sub("_", session_id)[:64] or "default"


class FileStore:
    """记忆落盘。MEMORY_PERSIST=0 时所有方法退化为空操作。"""

    def __init__(self, root: Optional[str] = None, log_fn: Optional[Callable[[str], None]] = None):
        self.root = Path(root or MEMORY_DATA_DIR)
        self.enabled = MEMORY_PERSIST
        self._log = log_fn

    def set_logger(self, log_fn: Callable[[str], None]) -> None:
        self._log = log_fn

    def _emit(self, line: str) -> None:
        if self._log:
            self._log(line)

    # ---------------- 路径 ----------------
    @property
    def sessions_dir(self) -> Path:
        return self.root / "sessions"

    @property
    def long_term_path(self) -> Path:
        return self.root / "long_term.json"

    def session_path(self, session_id: str) -> Path:
        return self.sessions_dir / f"{_safe_id(session_id)}.json"

    # ---------------- 底层读写 ----------------
    def _read_json(self, path: Path) -> Optional[Dict[str, Any]]:
        """读一个 JSON；不存在或壍了都返回 None（降级，不抛异常）。"""
        if not self.enabled or not path.is_file():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except Exception as e:  # noqa: BLE001
            # 文件坏了：改名备份一份再当作没记忆，便于事后人工护回
            self._emit(f"[记忆] 读取失败 {path.name}: {e}（已降级为无记忆）")
            try:
                path.rename(path.with_suffix(path.suffix + f".broken.{int(time.time())}"))
            except Exception:
                pass
            return None

    def _write_json(self, path: Path, data: Dict[str, Any]) -> bool:
        """原子写：先写 .tmp 再 replace。失败只记日志不抛异常。"""
        if not self.enabled:
            return False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)   # 同分区内原子替换
            return True
        except Exception as e:  # noqa: BLE001
            self._emit(f"[记忆] 写入失败 {path.name}: {e}")
            return False

    # ---------------- L2 会话 ----------------
    def load_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        return self._read_json(self.session_path(session_id))

    def save_session(self, session_id: str, data: Dict[str, Any]) -> bool:
        return self._write_json(self.session_path(session_id), data)

    def delete_session(self, session_id: str) -> None:
        """clear 时同步删盘：否则下次启动又把「已清空」的历史读回来。"""
        if not self.enabled:
            return
        try:
            self.session_path(session_id).unlink(missing_ok=True)
        except Exception as e:  # noqa: BLE001
            self._emit(f"[记忆] 删除会话文件失败: {e}")

    # ---------------- L3 长期 ----------------
    def load_long_term(self) -> Optional[Dict[str, Any]]:
        return self._read_json(self.long_term_path)

    def save_long_term(self, data: Dict[str, Any]) -> bool:
        return self._write_json(self.long_term_path, data)

    def delete_long_term(self) -> None:
        if not self.enabled:
            return
        try:
            self.long_term_path.unlink(missing_ok=True)
        except Exception as e:  # noqa: BLE001
            self._emit(f"[记忆] 删除长期记忆失败: {e}")


# 全局单例：与 tools.REGISTRY 一样的约定，整个进程共用一个。
STORE = FileStore()
