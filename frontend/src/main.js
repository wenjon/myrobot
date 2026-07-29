import { createHead3D } from './head3d.js';
import { connect } from './ws.js';
import { speak, cancel } from './tts.js';
import { visemeForChar, CLOSED } from './viseme.js';

const canvas = document.getElementById('face');
const logEl = document.getElementById('log');
const statusEl = document.getElementById('status');
const input = document.getElementById('input');
const sendBtn = document.getElementById('send');
const interruptBtn = document.getElementById('interrupt');
const newBtn = document.getElementById('newchat');

// 会话 ID 持久化：刷新/重连不丢记忆
let sessionId = localStorage.getItem('robot_session') || '';
const micBtn = document.getElementById('mic');

// 数字人在后台异步加载，不阻塞 WebSocket 连接与聊天。
// （手机上 glb 较大/WebGL 慢时，之前的 await 会卡住后续连接代码，导致发不出消息。）
let head = null;
statusEl.textContent = '连接中…';
createHead3D(canvas, './src/avatar.glb')
  .then((h) => { head = h; console.log('[Head] 数字人加载完成'); })
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

const EMOTION_SET = new Set(['平静', '开心', '悲伤', '生气', '惊讶', '疑惑']);

// WebSocket 地址：按当前页面协议自动选择 ws/wss，指向同源 /ws。
// 关键：https 页面（如 Cloudflare 隧道）必须用 wss，否则浏览器会拦截 ws:// 明文连接（混合内容）。
const WS_PROTO = location.protocol === 'https:' ? 'wss:' : 'ws:';
const WS_URL = `${WS_PROTO}//${location.host}/ws`;

const ws = connect(WS_URL, {
  onOpen: () => { statusEl.textContent = '已连接'; statusEl.className = 'ok'; ws.send({ type: 'hello', session: sessionId }); },
  onClose: () => { statusEl.textContent = '断开，重连中…'; statusEl.className = 'bad'; },
  onMessage: (msg) => {
    if (msg.type === 'sentence') {
      log('bot', msg.text);
      speak(msg.text, msg.seq, (b) => driveMouth(b.char), () => {});
    } else if (msg.type === 'action') {
      if (!head) return;
      if (msg.action === '表情') {
        const e = EMOTION_SET.has(msg.value) ? msg.value : '平静';
        head.setExpression(e);
      } else if (msg.action === '动作') {
        if (msg.value.includes('点头')) head.triggerNod();
        else if (msg.value.includes('摇头')) head.triggerShake();
      }
    } else if (msg.type === 'llm_done') {
      statusEl.textContent = '已连接';
    } else if (msg.type === 'error') {
      log('bot', '[出错] ' + msg.message);
    } else if (msg.type === 'session') {
      sessionId = msg.session; localStorage.setItem('robot_session', sessionId);
    } else if (msg.type === 'cleared') {
      logEl.innerHTML = ''; statusEl.textContent = '已连接（新对话）';
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






