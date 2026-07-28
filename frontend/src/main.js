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
const micBtn = document.getElementById('mic');

let head = null;
statusEl.textContent = '加载数字人…';
try {
  head = await createHead3D(canvas, './src/avatar.glb');
  statusEl.textContent = '连接中…';
} catch (e) {
  statusEl.textContent = '数字人加载失败: ' + e.message;
  console.error(e);
}

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

const ws = connect(`ws://${location.host}/ws`, {
  onOpen: () => { if (head) { statusEl.textContent = '已连接'; statusEl.className = 'ok'; } },
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
