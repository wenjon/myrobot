
// frontend/src/doa.js — 声源测向（DOA）→ 数字人注视转向
//
// 后端每 ~push_hz 发一条 {type:'doa_azimuth', az, active, status, ts}。
// 这里把方位角 az（度，相对屏幕正前方，逆时针为正）映射到屏幕横向坐标
// x = clamp(az/(FOV_X/2), -1, 1)，交给 head.turnTo() → 头+眼一起转向说话人。
// 与对话队列无关（旁路消息），数字人随时都能看向声源。
//
// 通过 getHead() 延迟取 head：main.js 里数字人是异步加载的，
// head 可能此时尚未就绪；这里只在中途有 head 时才驱动，头部就绪自然接上。

export function createDOA(getHead, opts = {}) {
  const fovX = opts.fovX || 70;      // 屏幕水平视野（度），与 DOA_FOV_X 对齐
  let enabled = true;

  function feed(msg) {
    if (!enabled || !msg || msg.type !== 'doa_azimuth') return;
    const h = (typeof getHead === 'function') ? getHead() : null;
    if (!h) return;

    if (msg.active) {
      const az = (typeof msg.az === 'number') ? msg.az : 0;
      // 方位角 → 屏幕横向坐标；超出视野就 clamp 到边缘，避免脑袋转到 90° 卡死
      const x = Math.max(-1, Math.min(1, az / (fovX / 2)));
      h.turnTo(x);
    }
    // active=false：后端在「说话/TTS冻结/无有效声源」时置位，
    // 前端保持当前姿态即可（不特意回正，避免 TTS 间隙乱摆）。
  }

  return {
    feed,
    setEnabled(v) { enabled = !!v; },
  };
}
