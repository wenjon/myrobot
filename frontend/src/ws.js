// ws.js：WebSocket 客户端，自动重连 + 发送缓冲 + 心跳保活。
// - 未连接时发送的消息会入队，连接建立后自动补发（避免移动网络重连间隙丢消息）；
// - 定时 ping 保活，缓解 Cloudflare/移动网络对空闲 WS 的断开；
// - 控制台打印完整生命周期，便于隧道排查。
export function connect(url, handlers) {
  let ws = null;
  let reconnectTimer = null;
  let pingTimer = null;
  let attempts = 0;
  const outbox = [];          // 待发送队列（未连接时暂存）
  let helloMsg = null;        // 记住 hello，重连后自动重发

  function flushOutbox() {
    while (outbox.length && ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(outbox.shift()));
    }
  }

  function start() {
    attempts += 1;
    console.log(`[WS] 正在连接(#${attempts}): ${url}  (页面: ${location.href})`);
    ws = new WebSocket(url);

    ws.onopen = () => {
      console.log(`[WS] 已连接: ${url}`);
      handlers.onOpen && handlers.onOpen();
      flushOutbox();
      // 心跳保活：每 20s 发一个 ping（服务端可忽略未知类型）
      clearInterval(pingTimer);
      pingTimer = setInterval(() => {
        if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'ping' }));
      }, 20000);
    };
    ws.onclose = (e) => {
      console.warn(`[WS] 连接关闭: code=${e.code} reason="${e.reason || '无'}" wasClean=${e.wasClean}；2 秒后重连…`);
      clearInterval(pingTimer);
      handlers.onClose && handlers.onClose();
      reconnectTimer = setTimeout(start, 2000);
    };
    ws.onerror = () => {
      console.error(`[WS] 连接出错: ${url}（若为 wss/隧道，请确认隧道已转发到本地 8000 且允许 WebSocket 升级）`);
    };
    ws.onmessage = (e) => {
      try { const msg = JSON.parse(e.data); handlers.onMessage && handlers.onMessage(msg); }
      catch {}
    };
  }

  start();

  return {
    url,
    // 发送：连接就绪则直接发；否则入队，连上后自动补发。始终返回 true（不再丢消息）。
    send(msg) {
      if (msg && msg.type === 'hello') helloMsg = msg;  // 记住 hello 供重连补发
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify(msg));
      } else {
        console.warn('[WS] 连接未就绪，消息已入队，连接后自动补发', msg);
        outbox.push(msg);
      }
      return true;
    },
    close() {
      if (reconnectTimer) clearTimeout(reconnectTimer);
      clearInterval(pingTimer);
      if (ws) ws.close();
    }
  };
}
