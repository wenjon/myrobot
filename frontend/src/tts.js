// tts.js：Web Speech API 封装，发送句子时触发 boundary 回调。
// 可复用同一 SpeechSynthesisUtterance 池。
// 暴露 speak(sentence, onBoundary, onEnd) 和 cancel()

let queue = [];
let speaking = false;
let currentUtterance = null;
let currentSeq = -1;

// 找中文语音
function getVoice() {
  const vs = speechSynthesis.getVoices();
  return vs.find(v => /zh/i.test(v.lang) && /(Xiaoxiao|xiao|Huihui|Yunyang|local|female|Microsoft.*(Chinese|Han))/i.test(v.name))
      || vs.find(v => /zh/i.test(v.lang))
      || vs[0];
}

// 拉长发音让口型有时间展示
function calcRate(textLen) {
  if (textLen <= 3) return 1.0;
  if (textLen <= 8) return 1.1;
  return 1.3;
}

export function speak(text, seq, onBoundary, onEnd) {
  queue.push({ text, seq, onBoundary, onEnd });
  if (!speaking) next();
}

function next() {
  if (queue.length === 0) { speaking = false; return; }
  speaking = true;
  const item = queue.shift();
  currentSeq = item.seq;
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

  utt.onend = () => {
    currentUtterance = null;
    item.onBoundary({ char: '', time: -1, elapsedSinceLast: 0 }); // end signal
    item.onEnd && item.onEnd();
    next();
  };

  utt.onerror = () => {
    currentUtterance = null;
    next();
  };

  speechSynthesis.speak(utt);
}

export function cancel() {
  queue = [];
  if (speechSynthesis.speaking) {
    speechSynthesis.cancel();
  }
  currentUtterance = null;
  speaking = false;
}

// softStop：“被打断”时的自然收尾，不像 cancel() 那样硬生生戳断。
// Web Speech API 限制：已开口的 utterance 无法中途改音量，
// 所以用“先清空后续排队 + 略延迟再 cancel”模拟渐弱/拖尾收音：
//   1) 立即丢掉还没开口的排队句（不再说新内容）；
//   2) 让当前正在念的那几个字自然说完一小段，再 cancel（听感上像“话到嘴边收住”）。
let softStopTimer = null;
export function softStop(fadeMs = 220) {
  queue = [];                 // 丢掉后续排队，不再开口新句
  clearTimeout(softStopTimer);
  if (speechSynthesis.speaking) {
    // 给当前句一个短暂的“说完余音”窗口，再戳断。
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
