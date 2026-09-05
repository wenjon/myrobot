import { createHead3D } from './head3d.js';
import { connect } from './ws.js';
import { speak, cancel, softStop, ttsReady, currentEngine, currentVoice, setVoice,
         setEmotion, currentProsody, setProsody } from './tts.js';
import { visemeForChar, CLOSED } from './viseme.js';
import { createProfileCards } from './profile_card.js';

const canvas = document.getElementById('face');
const logEl = document.getElementById('log');
const statusEl = document.getElementById('status');
const input = document.getElementById('input');
const sendBtn = document.getElementById('send');
const interruptBtn = document.getElementById('interrupt');
const newBtn = document.getElementById('newchat');
const profileCardsEl = document.getElementById('profileCards');

// 会话 ID 持久化：刷新/重连不丢记忆
let sessionId = localStorage.getItem('robot_session') || '';
const micBtn = document.getElementById('mic');

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

// 进入“我在听”收尾状态：渐弱停口播 + 倾听表情；短暂保持后自动恢复（新一轮回答会接管表情）。
let listenTimer = null;
function enterListening() {
  softStop();                 // 渐弱软停当前语音
  if (head) head.setListening(true);
  statusEl.textContent = '我在听…';
  clearTimeout(listenTimer);
  // 兽底恢复：若新一轮回答因某种原因没接上，1.5s 后自动退出倾听表情。
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
      // 服务端自动打断（barge-in）：做自然收尾——声音渐弱 + 切“我在听”倾听表情。
      enterListening();
    }
  }
});

function sendMessage() {
  const text = input.value.trim();
  if (!text) return;
  cancel();
  const ok = ws.send({ type: 'user_message', text, session: sessionId });
  if (!ok) { statusEl.textContent = '未连接，无法发送（请稍候重连）'; statusEl.className = 'bad'; return; }
  log('user', text);
  input.value = '';
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

sendBtn.onclick = sendMessage;
input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); } });
interruptBtn.onclick = () => { cancel(); ws.send({ type: 'interrupt' }); statusEl.textContent = '已打断'; };
if (newBtn) newBtn.onclick = () => { cancel(); ws.send({ type: 'clear' }); };

// 语音识别（可选）
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
if (SR) {
  const rec = new SR();
  rec.lang = 'zh-CN'; rec.interimResults = true; rec.continuous = false;
  let finalText = '';
  micBtn.onclick = () => { finalText = ''; try { rec.start(); statusEl.textContent = '聆听中…'; } catch {} };
  rec.onresult = (e) => {
    let interim = '';
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const t = e.results[i][0].transcript;
      if (e.results[i].isFinal) finalText += t; else interim += t;
    }
    input.value = finalText + interim;
  };
  rec.onend = () => { if (input.value.trim()) sendMessage(); else statusEl.textContent = '已连接'; };
} else {
  micBtn.disabled = true; micBtn.title = '当前浏览器不支持语音识别';
}






