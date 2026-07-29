// ws.js：WebSocket 客户端，自动重连。
// 会在控制台打印完整的 WS 地址与连接生命周期，方便在隧道环境排查。
export function connect(url, handlers) {
  let ws = null;
  let reconnectTimer = null;
  let attempts = 0;

  function start() {
    attempts += 1;
    console.log(`[WS] 正在连接(#${attempts}): ${url}  (页面: ${location.href})`);
    ws = new WebSocket(url);

    ws.onopen = () => {
      console.log(`[WS] 已连接: ${url}`);
      handlers.onOpen && handlers.onOpen();
    };
    ws.onclose = (e) => {
      console.warn(`[WS] 连接关闭: ${url}  code=${e.code} reason="${e.reason || '无'}" wasClean=${e.wasClean}；2 秒后重连…`);
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
    send(msg) { if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg)); },
    close() { if (reconnectTimer) clearTimeout(reconnectTimer); if (ws) ws.close(); }
  };
}
