// vision.js —— 左下角视觉浮层：相机实时画面 + 物体识别 + 深度/方位。
//
// 数据来源两个接口，刻意分开：
//   画面  GET /api/vision/stream   MJPEG 流，直接塞给 <img src>，浏览器自己解码，
//                                  前端零解码代码；走 multipart 而不是 WS，
//                                  因为它只要「一直显示最新帧」，不需要消息语义。
//   数据  GET /api/vision/objects  每 500ms 轮询一次 JSON（识别/方位/深度）。
//
// 为什么数据不用流里那帧自带的：画面要流畅（每帧都推），而物体列表一秒刷两次
// 就够看了；两个频率解耦，省得为了列表去解析视频帧。
//
// 状态三态，直接反映在标题栏小圆点上：
//   live  绿点  正在出帧
//   warn  黄点  子系统可用但暂时没画面（如相机被占用 / 模型还在加载）
//   err   红点  子系统不可用（缺依赖、VISION_ENABLED=0）
//   idle  灰点  未连接

const POLL_MS = 500;

export function initVision() {
  const panel = document.getElementById('visionPanel');
  const head = document.getElementById('visionHead');
  const dot = document.getElementById('visionDot');
  const toggle = document.getElementById('visionToggle');
  const video = document.getElementById('visionVideo');
  const meta = document.getElementById('visionMeta');
  const objsEl = document.getElementById('visionObjs');
  if (!panel || !video) return null;

  let videoStarted = false;
  let pollTimer = null;

  // 折叠：只留标题栏，点一下切换。状态本地存，刷新后保持。
  const collapsed = localStorage.getItem('vision_collapsed') === '1';
  setCollapsed(collapsed);
  head.onclick = () => setCollapsed(!panel.classList.contains('collapsed'));

  function setCollapsed(on) {
    panel.classList.toggle('collapsed', on);
    toggle.textContent = on ? '展开' : '收起';
    localStorage.setItem('vision_collapsed', on ? '1' : '0');
  }

  function setState(state, text) {
    dot.className = 'vp-dot' + (state === 'live' ? ' live'
      : state === 'err' ? ' err' : state === 'warn' ? ' warn' : '');
    if (text != null) meta.textContent = text;
  }

  // ---- 画面：先看状态，可用才挂 MJPEG（否则会一直转圈）----
  async function startVideo() {
    if (videoStarted) return;
    let st = null;
    try {
      st = await fetch('/api/vision/status').then((r) => r.json());
    } catch { /* 后端没起来 */ }

    if (!st || !st.available) {
      setState('err', st ? hintFor(st.status) : '后端未连接');
      return;
    }
    // 子系统在跑但相机还没出帧：先挂上流，出帧了自然就亮
    video.src = '/api/vision/stream?t=' + Date.now();
    videoStarted = true;
    video.onerror = () => { setState('err', '画面流中断'); videoStarted = false; };
    setState('warn', st.depth === false ? '无深度：只出方位' : '等待画面…');
  }

  function hintFor(status) {
    const s = String(status || '');
    if (s.startsWith('unavailable')) return '视觉不可用（缺依赖）';
    if (s === 'disabled') return '视觉已关闭（VISION_ENABLED=0）';
    if (s.startsWith('no-camera')) return '未找到相机';
    if (s.startsWith('error')) return '视觉出错';
    return '视觉未就绪';
  }

  // 深度来源的中文名：结构光(astra) / 双目视差(stereo) / 无(none)
  const SOURCE_NAME = { astra: '结构光测距', stereo: '双目测距', none: '单目（无深度）' };
  function sourceName(src) { return SOURCE_NAME[src] || '单目（无深度）'; }

  // ---- 物体列表 ----
  function renderObjects(list, depthOn) {
    objsEl.innerHTML = '';
    if (!list || !list.length) {
      const d = document.createElement('div');
      d.className = 'vp-empty';
      d.textContent = depthOn ? '未识别到物体' : '未识别到物体（无深度）';
      objsEl.appendChild(d);
      return;
    }
    for (const o of list) {
      const row = document.createElement('div');
      row.className = 'vp-obj';

      const id = document.createElement('span');
      id.className = 'vp-obj-id';
      id.textContent = '#' + (o.id != null ? o.id : '?');

      const type = document.createElement('span');
      type.className = 'vp-obj-type';
      type.textContent = o.type || '物体';
      if (o.confidence > 0) type.title = '置信度 ' + o.confidence;

      const dist = document.createElement('span');
      // 单目没有视差 → distance_m 为 null，显示「—」而不是假装有距离
      if (o.distance_m != null) {
        dist.className = 'vp-obj-dist';
        dist.textContent = o.distance_m.toFixed(1) + 'm';
      } else {
        dist.className = 'vp-obj-dist unknown';
        dist.textContent = '—';
      }

      const dir = document.createElement('span');
      dir.className = 'vp-obj-dir';
      const arrows = { left: '◀', right: '▶', center: '●' };
      const names = { left: '左', right: '右', center: '中' };
      dir.textContent = (arrows[o.direction] || '') + (names[o.direction] || '') +
        (o.angle_deg != null ? ' ' + Math.abs(o.angle_deg).toFixed(0) + '°' : '');

      row.append(id, type, dist, dir);
      objsEl.appendChild(row);
    }
  }

  async function poll() {
    try {
      const r = await fetch('/api/vision/objects');
      const d = await r.json();
      if (!d.available) {
        setState('err', hintFor(d.status));
        objsEl.innerHTML = '';
        return;
      }
      const depthOn = d.depth_available === true;
      renderObjects(d.objects, depthOn);
      if (d.objects && d.objects.length) {
        setState('live', `${sourceName(d.depth_source)} · ${d.objects.length} 个物体`);
      } else if (videoStarted) {
        setState('live', sourceName(d.depth_source));
      }
    } catch {
      setState('err', '后端未连接');
    }
  }

  startVideo().then(() => {
    poll();
    pollTimer = setInterval(poll, POLL_MS);
  });

  return {
    stop() { if (pollTimer) clearInterval(pollTimer); },
  };
}