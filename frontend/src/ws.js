// ws.js：WebSocket 客户端，自动重连。
export function connect(url, handlers) {
  let ws = null;
  let reconnectTimer = null;

  function start() {
    ws = new WebSocket(url);
    ws.onopen = () => { handlers.onOpen && handlers.onOpen(); };
    ws.onclose = () => {
      handlers.onClose && handlers.onClose();
      reconnectTimer = setTimeout(start, 2000);
    };
    ws.onerror = () => {};
    ws.onmessage = (e) => {
      try { const msg = JSON.parse(e.data); handlers.onMessage && handlers.onMessage(msg); }
      catch {}
    };
  }

  start();

  return {
    send(msg) { if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg)); },
    close() { if (reconnectTimer) clearTimeout(reconnectTimer); if (ws) ws.close(); }
  };
}
