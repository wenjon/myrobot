import { createHead } from './head.js';
import { connect } from './ws.js';
import { speak, cancel } from './tts.js';
import { shapeForText, CLOSED } from './viseme.js';

const canvas = document.getElementById('face');
const head = createHead(canvas);

const logEl = document.getElementById('log');
const statusEl = document.getElementById('status');
const input = document.getElementById('input');
const sendBtn = document.getElementById('send');
const interruptBtn = document.getElementById('interrupt');

function log(role, text) {
  const div = document.createElement('div');
  div.className = 'msg ' + role;
  div.textContent = (role === 'user' ? '你: ' : '小柚: ') + text;
  logEl.appendChild(div);
  logEl.scrollTop = logEl.scrollHeight;
}

// 口型驱动：每个字 boundary 触发一次张口，然后自动回落
let mouthTimer = null;
function driveMouth(char) {
  if (char === '') { head.setMouth(CLOSED.open, CLOSED.wide); return; }
  const s = shapeForText(char);
  head.setMouth(s.open, s.wide);
  clearTimeout(mouthTimer);
  mouthTimer = setTimeout(() => head.setMouth(CLOSED.open, CLOSED.wide), 120);
}

const EMOTION_MAP = { '开心': '开心', '疑惑': '疑惑', '惊讶': '惊讶', '平静': '平静' };

const ws = connect(`ws://${location.host}/ws`, {
  onOpen: () => { statusEl.textContent = '已连接'; statusEl.className = 'ok'; },
  onClose: () => { statusEl.textContent = '断开，重连中…'; statusEl.className = 'bad'; },
  onMessage: (msg) => {
    if (msg.type === 'sentence') {
      log('bot', msg.text);
      speak(msg.text, msg.seq,
        (b) => driveMouth(b.char),
        () => {});
    } else if (msg.type === 'action') {
      if (msg.action === '表情') head.setExpression(EMOTION_MAP[msg.value] || '平静');
      else if (msg.action === '动作') { if (msg.value.includes('点头')) head.triggerNod(); }
    } else if (msg.type === 'llm_done') {
      statusEl.textContent = '已连接';
    } else if (msg.type === 'error') {
      log('bot', '[出错] ' + msg.message);
    }
  }
});

function sendMessage() {
  const text = input.value.trim();
  if (!text) return;
  log('user', text);
  cancel();
  ws.send({ type: 'user_message', text });
  input.value = '';
  statusEl.textContent = '思考中…';
}

sendBtn.onclick = sendMessage;
input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); } });
interruptBtn.onclick = () => { cancel(); ws.send({ type: 'interrupt' }); statusEl.textContent = '已打断'; };

// 语音输入（可选，Web Speech Recognition）
const micBtn = document.getElementById('mic');
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

// 预热语音列表
if (speechSynthesis.getVoices().length === 0) {
  speechSynthesis.onvoiceschanged = () => {};
}
