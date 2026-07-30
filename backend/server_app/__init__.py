"""server_app 子包：把 server.py 拆出来的服务侧辅助模块。

按职责切分：
- logging.py   上下文/输出日志（控制台 + 文件）
- peers.py     客户端地址格式化（IPv4/IPv6/转发头）
- dialog.py    单轮对话主循环（LLM → 中央调度 → WS 推送 → 提交/回滚）
- notify.py    主动通知前端的辅助消息（如自动打断的 "interrupted"）
"""