import { createHead3D } from './head3d.js';
import { connect } from './ws.js';
import { speak, cancel, softStop, ttsReady, currentEngine, currentVoice, setVoice,
         setEmotion, currentProsody, setProsody, setOnIdle } from './tts.js';
import { visemeForChar, CLOSED } from './viseme.js';
import { createProfileCards } from './profile_card.js';
import { createASR } from './asr.js';
import { initVision } from './vision.js';

const canvas = document.getElementById('face');
const logEl = document.getElementById('log');
const statusEl = document.getElementById('status');
const input = document.getElementById('input');
const sendBtn = document.getElementById('send');
const interruptBtn = document.getElementById('interrupt');
const newBtn = document.getElementById('newchat');
const profileCardsEl = document.getElementById('profileCards');
const micBtn = document.getElementById('mic');
const voiceHintEl = document.getElementById('voiceHint');

// 会话 ID 持久化：刷新/重连不丢记忆
let sessionId = localStorage.getItem('robot_session') || '';

// 语音识别器实例（在下方初始化）。小柚说话期间由 asr.pause()/resume() 控制收音，
// 防止把它自己的声音当成用户输入（回声自激）。
let asr = null;

// 数字人在后台异步加载，不阻塞 WebSocket 连接与聊天。
// （手机上 glb 较大/WebGL 慢时，之前的 await 会卡住后续连接代码，导致发不出消息。）
let head = null;
statusEl.textContent = '连接中…';
createHead3D(canvas, './src/avatar.glb')
  .then((h) => { head = h; buildDebugPanel(h); console.log('[Head] 数字人加载完成'); })
  .catch((e) => { console.error('[Head] 数字人加载失败（不影响对话）:', e); });

function log(role, text) {
  const div = document.createElement('div');
  div.className = 'msg ' + role;
  div.textContent = (role === 'user' ? '你: ' : '小柚: ') + text;
  logEl.appendChild(div);
  logEl.scrollTop = logEl.scrollHeight;
}

// 口型驱动：每个字 boundary 触发一次 viseme，然后回落到闭口
let mouthTimer = null;
function driveMouth(char) {
  if (!head) return;
  if (char === '') { head.mouthClosed(); return; }
  const v = visemeForChar(char);
  head.setViseme(v, 1.0);
  clearTimeout(mouthTimer);
  mouthTimer = setTimeout(() => head.mouthClosed(), 130);
}

// 进入"我在听"收尾状态：渐弱停口播 + 倾听表情；短暂保持后自动恢复（新一轮回答会接管表情）。
let listenTimer = null;
function enterListening() {
  softStop();                 // 渐弱软停当前语音
  if (head) head.setListening(true);
  statusEl.textContent = '我在听…';
  clearTimeout(listenTimer);
  // 兜底恢复：若新一轮回答因某种原因没接上，1.5s 后自动退出倾听表情。
  listenTimer = setTimeout(() => { if (head) head.setListening(false); }, 1500);
}

// 表情白名单：必须与 head3d.js 的 EXPR 表、backend/config.py 的 SYSTEM_PROMPT 三处保持一致。
// 不在白名单里的值降级成"平静"，避免模型胡编标记导致表情卡死。
const EMOTION_SET = new Set([
  '平静', '开心', '悲伤', '生气', '惊讶', '疑惑',
  '害羞', '调皮', '无语', '思考', '尴尬', '得意',
  '委屈', '惊恐', '厌恶', '困倦', '撒娇', '期待',
]);

// 动作分发表：动作名 -> 作用于 head 的函数。用表驱动而不是 if-else 链，
// 新增动作只需在这里加一行 + 同步 config.py 的 prompt。
const ACTION_MAP = {
  // 头颈
  '点头': (h) => h.triggerNod(),
  '摇头': (h) => h.triggerShake(),
  '歪头': (h) => h.triggerTilt(1),
  // 注视（"左/右"以观众视角为准：看左 = 转向画面左侧）
  '看左':   (h) => h.turnTo(-0.9),
  '看右':   (h) => h.turnTo(0.9),
  '看上':   (h) => h.lookUp(),
  '看下':   (h) => h.lookDown(),
  '环视':   (h) => h.lookAround(),
  '看向对方': (h) => h.resetPose(),
  '对视':   (h) => h.resetPose(),
  // 眨眼
  '眨眼':   (h) => h.triggerBlink('both'),
  '眨左眼': (h) => h.triggerBlink('left'),
  '眨右眼': (h) => h.triggerBlink('right'),
  // 面部瞬时叠加动作（名称需与 head3d.js 的 OVERLAYS 对应）
  '挑眉':   (h) => h.triggerOverlay('挑眉'),
  '单挑眉': (h) => h.triggerOverlay('单挑眉'),
  '皱眉':   (h) => h.triggerOverlay('皱眉'),
  '鼓腮':   (h) => h.triggerOverlay('鼓腮'),
  '撅嘴':   (h) => h.triggerOverlay('撅嘴'),
  '吐舌':   (h) => h.triggerOverlay('吐舌'),
  '咬唇':   (h) => h.triggerOverlay('咬唇'),
  '努嘴':   (h) => h.triggerOverlay('努嘴'),
};

// 动作匹配：模型可能写成"轻轻点头""好奇地歪头"，所以用包含匹配。
// 按名字从长到短匹配，防止"眨眼"抢先命中"眨左眼"。
const ACTION_KEYS = Object.keys(ACTION_MAP).sort((a, b) => b.length - a.length);
function dispatchAction(h, value) {
  const v = String(value || '');
  for (const k of ACTION_KEYS) {
    if (v.includes(k)) { ACTION_MAP[k](h); return k; }
  }
  return null;
}

// WebSocket 地址：按当前页面协议自动选择 ws/wss，指向同源 /ws。
// 关键：https 页面（如 Cloudflare 隧道）必须用 wss，否则浏览器会拦截 ws:// 明文连接（混合内容）。
const WS_PROTO = location.protocol === 'https:' ? 'wss:' : 'ws:';
const WS_URL = `${WS_PROTO}//${location.host}/ws`;

// 画像冲突确认卡：用闭包延迟取 ws，因为 ws 在下面才定义。
const profileCards = createProfileCards(profileCardsEl, (payload) => ws.send(payload));

const ws = connect(WS_URL, {
  onOpen: () => {
    statusEl.textContent = '已连接';
    statusEl.className = 'ok';
    ws.send({ type: 'hello', session: sessionId });
    ttsReady.then(() => console.log(`[TTS] 引擎=${currentEngine()} 音色=${currentVoice()} 韵律=${currentProsody()}`));
  },
  onClose: () => { statusEl.textContent = '断开，重连中…'; statusEl.className = 'bad'; },
  onMessage: (msg) => {
    if (msg.type === 'sentence') {
      if (head) head.setListening(false);  // 新回答开口，退出倾听表情
      clearTimeout(listenTimer);
      log('bot', msg.text);
      // 小柚开口期间暂停收音，否则会把它自己的声音听进去（回声自激）。
      // 同时取消上一个「等待恢复」定时器——否则新一轮开口后它才到点，
      // 会在播报中途把麦克风打开，等于又放它进来听自己说话。
      clearTimeout(resumeTimer);
      if (asr) asr.pause();
      speak(msg.text, msg.seq, (b) => driveMouth(b.char), () => {});
    } else if (msg.type === 'action') {
      // 表情先同步给 TTS：数字人可能还在加载（head 为 null），但声音不该因此丢掉情绪
      if (msg.action === '表情' && EMOTION_SET.has(msg.value)) setEmotion(msg.value);
      if (!head) return;
      if (msg.action === '表情') {
        const e = EMOTION_SET.has(msg.value) ? msg.value : '平静';
        head.setExpression(e);
        setEmotion(e);     // 同步给 TTS：声音的语速/音高也跟着情绪走
      } else if (msg.action === '动作') {
        dispatchAction(head, msg.value);
      }
    } else if (msg.type === 'status') {
      // 工具执行状态（如“正在联网搜索…”），显示在状态栏
      statusEl.textContent = msg.text || '处理中…';
    } else if (msg.type === 'llm_done') {
      statusEl.textContent = '已连接';
      // 这里**不**恢复收音：llm_done 只代表 LLM 生成完了，前端往往还有好几句在
      // 排队播放。恢复交给 TTS 播完的通知（setOnIdle），否则会把小柚自己的话
      // 听成用户输入（回声自激，实测出现过）。
    } else if (msg.type === 'error') {
      log('bot', '[出错] ' + msg.message);
    } else if (msg.type === 'session') {
      sessionId = msg.session; localStorage.setItem('robot_session', sessionId);
    } else if (msg.type === 'profile_conflict') {
      // 服务端提炼出的画像变更与旧值冲突，弹卡让用户拍板（docs § 16.8）
      profileCards.show(msg);
    } else if (msg.type === 'profile_resolved') {
      profileCards.resolved(msg);
    } else if (msg.type === 'cleared') {
      logEl.innerHTML = ''; profileCards.clearAll(); statusEl.textContent = '已连接（新对话）';
    } else if (msg.type === 'interrupted') {
      // 服务端自动打断（barge-in）：做自然收尾——声音渐弱 + 切"我在听"倾听表情。
      // 收音不在这里恢复：软停是渐弱收尾，等它真正静下来再由 setOnIdle 恢复，
      // 否则渐弱尾巴会被麦克风听进去。
      enterListening();
    }
  }
});

function sendMessage(text, source = 'text') {
  text = (text != null ? text : input.value).trim();
  if (!text) return;
  cancel();
  const ok = ws.send({ type: 'user_message', text, session: sessionId });
  if (!ok) { statusEl.textContent = '未连接，无法发送（请稍候重连）'; statusEl.className = 'bad'; return; }
  // 语音来源加个前缀，方便在对话区一眼区分"我刚说的话"和"我打的字"
  log('user', source === 'voice' ? '🎤 ' + text : text);
  input.value = '';
  if (asr) asr.reset();   // 已提交，清掉识别缓冲，避免同句再发一次
  statusEl.textContent = '思考中…';
}

// 调试面板：把所有表情/动作渲染成按钮，方便逐个肉眼预览（无需让模型配合）。
// 数字人异步加载，所以延迟到 head 就绪后再建面板。
const exprTestEl = document.getElementById('exprTest');
function buildDebugPanel(h) {
  if (!exprTestEl) return;
  buildVoicePicker();
  const add = (label, fn) => {
    const b = document.createElement('button');
    b.textContent = label;
    b.onclick = fn;
    exprTestEl.appendChild(b);
  };
  for (const name of h.expressionNames()) add(name, () => h.setExpression(name));
  for (const name of ACTION_KEYS.slice().sort()) add('▸' + name, () => dispatchAction(h, name));
}

// 音色选择器：Edge 神经语音有 14 个中文音色，听感差别很大，
// 放个下拉框现场切换比改配置重启方便得多（仅影响当前页面，不改后端默认值）。
function buildVoicePicker() {
  if (currentEngine() !== 'edge') return;
  const sel = document.createElement('select');
  sel.id = 'voiceSel';
  sel.title = '语音音色';
  fetch('/api/tts/voices?locale=zh')
    .then((r) => r.json())
    .then((d) => {
      if (!d.ok) return;
      for (const v of d.voices) {
        const o = document.createElement('option');
        o.value = v.name;
        const tag = [...(v.personalities || []), ...(v.categories || [])].join('/');
        o.textContent = `${v.name.replace('zh-', '')}${tag ? ' · ' + tag : ''}`;
        if (v.name === currentVoice()) o.selected = true;
        sel.appendChild(o);
      }
    })
    .catch(() => {});
  sel.onchange = () => setVoice(sel.value);
  exprTestEl.appendChild(sel);

  // 韵律风格：broadcast 播音腔 / natural 日常口语 / flat 关闭
  const ps = document.createElement('select');
  ps.id = 'prosodySel';
  ps.title = '韵律风格（抑扬顿挫强度）';
  for (const [val, label] of [['broadcast', '播音腔'], ['natural', '日常口语'], ['flat', '无韵律']]) {
    const o = document.createElement('option');
    o.value = val; o.textContent = label;
    if (val === currentProsody()) o.selected = true;
    ps.appendChild(o);
  }
  ps.onchange = () => setProsody(ps.value);
  exprTestEl.appendChild(ps);
}

// 发消息按钮：按住回车/点击都走文字链路（默认语音，但打字随时可用）
sendBtn.onclick = () => sendMessage();
input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); } });
interruptBtn.onclick = () => {
  cancel(); ws.send({ type: 'interrupt' }); statusEl.textContent = '已打断';
  if (asr) asr.reset();
};
if (newBtn) newBtn.onclick = () => { cancel(); ws.send({ type: 'clear' }); if (asr) asr.reset(); };

// =====================================================================
// 语音输入（默认输入方式）：页面加载即尝试常开麦克风，识别到停顿自动发送。
// 与打字输入完全并存——想打字直接敲输入框回车即可，两条路走同一个 sendMessage。
// =====================================================================
let asrConfig = { autostart: true, silence_ms: 1200, resume_delay_ms: 400, lang: 'zh-CN' };

function setMicUI(state) {
  if (!micBtn) return;
  const label = { listening: '🎤 聆听中', muted: '🎤 已暂停', paused: '🎤 小柚说话中',
                  denied: '🎤 无权限', unsupported: '🎤 不支持' };
  micBtn.textContent = label[state] || '🎤 语音';
  micBtn.title = state === 'listening' ? '点击暂停语音输入' : '点击开启语音输入';
  micBtn.classList.toggle('on', state === 'listening');
  if (voiceHintEl) {
    voiceHintEl.textContent = state === 'listening' ? '正在听你说话，说完停顿一下即可发送'
      : state === 'paused' ? '小柚正在回答，说完自动继续听'
      : state === 'denied' ? '麦克风被拒绝：请在浏览器地址栏允许麦克风，或直接用下方输入框打字'
      : state === 'unsupported' ? '当前浏览器不支持语音识别，请用下方输入框打字'
      : state === 'muted' ? '语音已暂停，点「🎤 语音」恢复；也可以直接打字'
      : '';
  }
}

function initASR() {
  asr = createASR({
    lang: asrConfig.lang,
    silenceMs: asrConfig.silence_ms,
    onText: (finalText, preview) => {
      if (finalText) {
        // 静音判定到点：把整句交给大模型
        sendMessage(finalText, 'voice');
        return;
      }
      // 实时回显：识别中的临时文本显示在输入框上方，让用户看到"听到了什么"
      if (voiceHintEl && preview != null) {
        const base = asr && asr.isMuted() ? '（已暂停）' : '正在听你说话，说完停顿一下即可发送';
        voiceHintEl.textContent = preview ? `🎤 ${preview}` : base;
      }
    },
    onState: (s) => setMicUI(s),
    // 浏览器不支持语音识别：禁用麦克风按钮，引导改用打字（两条输入路径本就并存）
    onUnsupported: () => { setMicUI('unsupported'); if (micBtn) micBtn.disabled = true; },
  });

  if (micBtn) {
    micBtn.onclick = () => {
      if (!asr) return;
      if (asr.isUserOff()) asr.start(); else asr.stop();
    };
  }

  // 默认自动开麦。浏览器要求"用户手势"后才能开麦克风，
  // 所以首次如果被策略拦住，就在用户第一次点击页面任意处时补开。
  if (asrConfig.autostart) {
    asr.start();
    const kick = () => {
      if (asr && asr.isUserOff()) return;
      asr.resume();
      window.removeEventListener('pointerdown', kick);
      window.removeEventListener('keydown', kick);
    };
    window.addEventListener('pointerdown', kick, { once: true });
    window.addEventListener('keydown', kick, { once: true });
  } else {
    setMicUI('muted');
  }
}

// 视觉浮层（左下角）：相机画面 + 识别 + 深度。
// 与对话链路完全独立：视觉挂了也不影响说话，所以这里不 await、不报错上抛。
initVision();

// TTS 播完（队列真正排空）→ 恢复收音。
// 防回声自激的两个要点：
//   1) 时机以「声音播完」为准，而不是 llm_done（见 ws.onMessage 里的说明）；
//   2) 播完后再等 resume_delay_ms 才开麦——喇叭余音与房间混响会拖一小截尾巴，
//      立刻开麦会把这段尾巴听成用户输入。这一层「ASR 门控」是最有效也最省成本的做法。
let resumeTimer = null;
setOnIdle(() => {
  clearTimeout(resumeTimer);
  resumeTimer = setTimeout(() => {
    if (asr && !asr.isUserOff()) asr.resume();
  }, asrConfig.resume_delay_ms || 0);
});

// 先向后端拉一次 ASR 配置（.env 可调），再初始化识别器
fetch('/api/asr/config')
  .then((r) => r.json())
  .then((c) => { asrConfig = { ...asrConfig, ...(c || {}) }; })
  .catch(() => {})
  .finally(() => { initASR(); });






