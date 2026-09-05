// tts.js：语音合成封装。双后端 + 自动降级。
//
// 为什么是两套后端：
//   · edge  —— 服务端 /api/tts 用 Edge 神经语音合成（XiaoyiNeural 等），
//              自然度远高于 Windows 本机 SAPI；返回 base64 mp3 + 词边界时间轴。
//   · web   —— 浏览器 Web Speech API，离线可用，作为 edge 不可用时的兜底。
// 任一句 edge 合成失败都会就地降级到 web 播这一句，数字人不会变哑。
//
// 口型驱动的差异（关键）：
//   web  路径靠 utterance 的 boundary 事件按字回调；
//   edge 路径没有该事件，改为拿服务端返回的 marks 时间轴，
//        在 <audio> 播放期间用 requestAnimationFrame 按 currentTime 查表下发字符。
//        时间戳来自真实合成结果，比 boundary 的估算更准，且不受浏览器差异影响。
// 两条路径对外都是同一个 onBoundary({char, time, elapsedSinceLast}) 回调，
// 上层 main.js 的 driveMouth 完全不需要知道用的是哪套。

let queue = [];
let speaking = false;
let currentUtterance = null;
let currentAudio = null;      // edge 路径正在播的 <audio>
let currentRaf = 0;           // edge 路径的口型驱动帧循环
let currentSeq = -1;
let stopped = false;          // cancel/softStop 置位，避免在途请求回来后又开口

// ---- 引擎配置：启动时向后端拉一次 ----
let engine = 'web';           // 'edge' | 'web'
let ttsConfig = {};
export const ttsReady = fetch('/api/tts/config')
  .then((r) => r.json())
  .then((c) => { ttsConfig = c || {}; engine = c && c.engine === 'edge' ? 'edge' : 'web'; return c; })
  .catch(() => ({}));         // 拉不到就保持 web

export function currentEngine() { return engine; }
export function currentVoice() { return ttsConfig.voice || '(浏览器默认)'; }
// 运行时切音色/引擎（调试面板用），不改后端默认值
export function setVoice(voice) { ttsConfig = { ...ttsConfig, voice }; }
export function setEngine(name) { engine = name === 'edge' ? 'edge' : 'web'; }

// 找中文语音（web 路径）
function getVoice() {
  const vs = speechSynthesis.getVoices();
  return vs.find(v => /zh/i.test(v.lang) && /(Xiaoxiao|xiao|Huihui|Yunyang|local|female|Microsoft.*(Chinese|Han))/i.test(v.name))
      || vs.find(v => /zh/i.test(v.lang))
      || vs[0];
}

// 拉长发音让口型有时间展示（仅 web 路径；edge 的语速由后端 TTS_RATE 控制）
function calcRate(textLen) {
  if (textLen <= 3) return 1.0;
  if (textLen <= 8) return 1.1;
  return 1.3;
}

// 预取：Edge 合成一句约 0.5~1.7s（含网络往返）。若等"上一句播完才去合成下一句"，
// 句间就会出现明显空档。所以入队时立刻发起合成请求，让它与当前句的播放并行，
// 轮到播放时音频通常已就位。Promise 存在 item.pending 上，播放时直接 await。
export function speak(text, seq, onBoundary, onEnd) {
  const item = { text, seq, onBoundary, onEnd };
  if (engine === 'edge') item.pending = fetchAudio(text);
  queue.push(item);
  if (!speaking) next();
}

function fetchAudio(text) {
  return fetch('/api/tts', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, voice: ttsConfig.voice || '' }),
  })
    .then((r) => r.json())
    .catch(() => null);
}

function next() {
  if (queue.length === 0) { speaking = false; return; }
  speaking = true;
  stopped = false;
  const item = queue.shift();
  currentSeq = item.seq;
  if (engine === 'edge') speakEdge(item);
  else speakWeb(item);
}

// 一句话播完后的收尾：补一个 char:'' 的结束信号让嘴闭上，再播下一句
function finish(item) {
  item.onBoundary({ char: '', time: -1, elapsedSinceLast: 0 });
  item.onEnd && item.onEnd();
  next();
}

// ---------------- edge 路径 ----------------
async function speakEdge(item) {
  // 优先用入队时的预取结果；没有（如切换引擎后补发）则现场合成
  const data = await (item.pending || fetchAudio(item.text));
  if (stopped) { speaking = false; return; }   // 期间被打断，丢弃这次结果
  if (!data || !data.ok || !data.audio) {
    // 单句降级：这句改用 Web Speech，不影响后续句仍走 edge
    console.warn('[TTS] Edge 合成失败，本句降级 Web Speech:', data && data.error);
    speakWeb(item);
    return;
  }

  const audio = new Audio('data:' + (data.mime || 'audio/mpeg') + ';base64,' + data.audio);
  currentAudio = audio;
  const marks = data.marks || [];
  let mi = 0, lastTime = 0;

  // 口型驱动：按播放进度在 marks 时间轴上推进，每个 mark 下发一个字符
  function tick() {
    if (currentAudio !== audio) return;
    const ms = audio.currentTime * 1000;
    while (mi < marks.length && marks[mi].offset_ms <= ms) {
      const m = marks[mi++];
      const char = item.text[m.char_index] || m.text[0] || '';
      item.onBoundary({ char, time: m.offset_ms, elapsedSinceLast: m.offset_ms - lastTime });
      lastTime = m.offset_ms;
    }
    if (mi < marks.length) currentRaf = requestAnimationFrame(tick);
  }

  audio.onplay = () => { currentRaf = requestAnimationFrame(tick); };
  audio.onended = () => {
    cancelAnimationFrame(currentRaf);
    if (currentAudio === audio) currentAudio = null;
    finish(item);
  };
  audio.onerror = () => {
    cancelAnimationFrame(currentRaf);
    if (currentAudio === audio) currentAudio = null;
    finish(item);
  };
  try {
    await audio.play();
  } catch (e) {
    // 浏览器自动播放策略：用户未交互过时 play() 会被拒。
    // 本项目里用户必然先点过发送/麦克风，正常不会走到这；兜底降级到 web。
    console.warn('[TTS] 音频播放被拒，降级 Web Speech:', e);
    currentAudio = null;
    speakWeb(item);
  }
}

// ---------------- web 路径（Web Speech API 兜底） ----------------
function speakWeb(item) {
  const utt = new SpeechSynthesisUtterance(item.text);
  currentUtterance = utt;

  const voice = getVoice();
  if (voice) utt.voice = voice;
  utt.lang = 'zh-CN';
  utt.rate = calcRate(item.text.length);
  utt.pitch = 1.0;
  utt.volume = 1.0;

  let lastChar = { char: '', time: 0 };

  utt.onboundary = (e) => {
    if (e.name === 'word' || e.name === 'sentence') {
      const char = item.text[e.charIndex] || '';
      const time = e.elapsedTime || 0;
      item.onBoundary({ char, time, elapsedSinceLast: time - lastChar.time });
      lastChar = { char, time };
    }
  };

  utt.onend = () => { currentUtterance = null; finish(item); };
  utt.onerror = () => { currentUtterance = null; next(); };

  speechSynthesis.speak(utt);
}

// ---------------- 停止 ----------------
function stopAudio() {
  cancelAnimationFrame(currentRaf);
  if (currentAudio) {
    try { currentAudio.pause(); } catch {}
    currentAudio = null;
  }
}

export function cancel() {
  queue = [];
  stopped = true;
  stopAudio();
  if (speechSynthesis.speaking) speechSynthesis.cancel();
  currentUtterance = null;
  speaking = false;
}

// softStop：“被打断”时的自然收尾，不像 cancel() 那样硬生生戳断。
//   1) 立即丢掉还没开口的排队句（不再说新内容）；
//   2) 让当前正在念的那几个字自然说完一小段，再停（听感上像“话到嘴边收住”）。
// edge 路径能做真正的音量渐弱（HTMLAudioElement.volume 可写），
// web 路径受 Web Speech 限制无法中途改音量，只能延迟 cancel 近似。
let softStopTimer = null;
export function softStop(fadeMs = 220) {
  queue = [];
  stopped = true;
  clearTimeout(softStopTimer);

  if (currentAudio) {
    const audio = currentAudio;
    const steps = 8, dt = fadeMs / steps;
    let i = 0;
    const fade = setInterval(() => {
      i += 1;
      audio.volume = Math.max(0, 1 - i / steps);
      if (i >= steps) { clearInterval(fade); stopAudio(); speaking = false; }
    }, dt);
    return;
  }

  if (speechSynthesis.speaking) {
    softStopTimer = setTimeout(() => {
      try { speechSynthesis.cancel(); } catch {}
      currentUtterance = null;
      speaking = false;
    }, fadeMs);
  } else {
    currentUtterance = null;
    speaking = false;
  }
}
