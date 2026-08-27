# -*- coding: utf-8 -*-
"""server_app.logging: context/output logger (console + file)."""
from __future__ import annotations

import os
from datetime import datetime
from typing import Dict, List

from config import LOG_CONTEXT, CONTEXT_LOG_FILE

class ContextLogger:
    """Unified console + file logger."""
    def __init__(self) -> None:
        self._fh = None

    def init(self) -> None:
        """Process startup hook: open log file if configured."""
        if not (LOG_CONTEXT and CONTEXT_LOG_FILE):
            return
        try:
            os.makedirs(os.path.dirname(CONTEXT_LOG_FILE), exist_ok=True)
            self._fh = open(CONTEXT_LOG_FILE, 'a', encoding='utf-8')
        except Exception as e:  # noqa: BLE001
            print(f'[warn] cannot open context log file: {e}')
            self._fh = None

    def emit(self, line: str) -> None:
        """Emit a line to console (when enabled) and to file (if open)."""
        if not LOG_CONTEXT:
            return
        print(line, flush=True)
        if self._fh:
            self._fh.write(line + "\n")
            self._fh.flush()

    def close(self) -> None:
        if self._fh:
            try: self._fh.close()
            except Exception: pass
            self._fh = None

LOGGER = ContextLogger()

def get_logger() -> ContextLogger:
    """Return the process-wide logger."""
    return LOGGER

def _shorten(text: str, limit: int = 200) -> str:
    """Truncate text beyond `limit` chars, appending `...(+Nch)` marker."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"...(+{len(text) - limit}ch)"

def log_context(session, messages, user_text: str, who: str = '') -> None:
    """Print the full messages actually sent to LLM this turn (debug aid)."""
    ts = datetime.now().strftime('%H:%M:%S')
    body = [
        '\n' + '=' * 70,
        f'[{ts}][{who}][session {session.id[:8]}] user: {user_text}',
        f'[ctx sent to LLM  {len(messages)} msgs  {sum(len(m["content"]) for m in messages)} chars]',
    ]
    for i, m in enumerate(messages):
        role = {'system': 'SYS', 'user': 'U', 'assistant': 'BOT'}.get(m['role'], m['role'])
        content = m["content"].replace("\n", " ")
        body.append(f'  {i:>2}. [{role}] {_shorten(content)}')
    # 长期记忆摘要（L3）
    if session.summary:
        body.append(f'[summary] {session.summary}')
    # 用户画像（L3）：它已经拼在上面的 SYS 里，但 SYS 太长会被截断，
    # 所以单独再打一行，方便确认「机器人到底记住了我的什么」。
    try:
        profile_text = session.profile.as_prompt_text()
    except Exception:  # noqa: BLE001  日志不能因为取画像失败而拘累主流程
        profile_text = ''
    if profile_text:
        body.append(f'[profile] {profile_text}')
    # 待用户确认的画像冲突数量（B 方案）
    pending = len(getattr(session, 'pending_conflicts', {}) or {})
    if pending:
        body.append(f'[profile-pending] {pending} 条画像变更待用户确认')
    body.append('-' * 70)
    LOGGER.emit('\n'.join(body))

def log_output(session, assistant_raw: str, note: str = '', who: str = '') -> None:
    """Print the raw model output for this turn (debug aid)."""
    ts = datetime.now().strftime('%H:%M:%S')
    LOGGER.emit(
        f'[{ts}][{who}][session {session.id[:8]}] model{note}: {assistant_raw.strip()}\n' + '=' * 70
    )
