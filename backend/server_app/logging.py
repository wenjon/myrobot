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
    if session.summary:
        body.append(f'[summary] {session.summary}')
    body.append('-' * 70)
    LOGGER.emit('\n'.join(body))

def log_output(session, assistant_raw: str, note: str = '', who: str = '') -> None:
    """Print the raw model output for this turn (debug aid)."""
    ts = datetime.now().strftime('%H:%M:%S')
    LOGGER.emit(
        f'[{ts}][{who}][session {session.id[:8]}] model{note}: {assistant_raw.strip()}\n' + '=' * 70
    )
