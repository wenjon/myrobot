// asr.js —— 语音输入（默认输入方式）：连续监听 + 静音自动提交 + 与手动打字共存。
//
// 为什么单独一个模块：
//   main.js 里原来把识别、提交、UI 状态揉在一起，改一处容易碰坏另一处。
//   这里只负责「听 → 出文本 → 回调」，是否发送、发什么由 main.js 决定。
//
// 三个关键设计：
//
// 1) **连续监听**：Web Speech 的 recognition 在静音后会自己 end（不是错误），
//    只 start 一次的话用户说一句话就再也听不到了。这里监听 onend 自动重启，
//    形成一个"常开"的麦克风。用户手动关闭（muted）时才不再重启。
//
// 2) **静音自动提交**：interimResults 拿到的是不断修订的临时结果，
//    isFinal 只代表"这一小段被浏览器定稿"，并不等于"用户说完了"。
//    所以不能一拿到 final 就发——用户长句中间换气会被切碎。
//    做法：每次收到识别结果就重置一个 silenceMs 定时器，静音超时才把手头
//    累积的文本整体提交。这样长句能攒成一句，短句也能及时发。
//
// 3) **播放时暂停收音**：数字人在说话时如果继续收音，会把小柚自己的声音听进去
//    （回声自激）。main.js 在收到 sentence 时调 pause()，说完调 resume()。
//    这里用手动 mute 标志实现，不销毁识别器（重建要重新申请麦克风权限，有延迟）。
//
// 降级：浏览器不支持 SpeechRecognition（Firefox / 部分移动端）时，
//   start() 会回调 onUnsupported，main.js 据此禁用麦克风按钮并提示改用打字。

export function createASR({ lang = 'zh-CN', silenceMs = 1200, onText, onState, onUnsupported }) {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) {
    // 不支持：直接告诉上层，别让它以为在听
    onUnsupported && onUnsupported();
    return null;
  }

  let rec = null;
  let running = false;      // 识别器是否处于 start 状态
  let muted = false;        // 不重启识别器（含播放期暂停与用户手动关闭）
  let userOff = false;      // 用户主动关闭：播放结束/打断后也不该自动恢复
  let denied = false;       // 麦克风权限被拒：保持该状态，等用户点按钮重试授权
  let silenceTimer = null;  // 静音判定定时器
  let pending = '';         // 累积待提交文本（final 段 + 最近的 interim）
  let lastSubmit = 0;       // 上次提交时刻，用于防抖

  function setState(s) { onState && onState(s); }

  function clearSilence() {
    if (silenceTimer) { clearTimeout(silenceTimer); silenceTimer = null; }
  }

  // 静音到点：把手头文本整体提交给上层
  function armSilence() {
    clearSilence();
    if (!pending.trim()) return;
    silenceTimer = setTimeout(() => {
      silenceTimer = null;
      const text = pending.trim();
      pending = '';
      if (!text) return;
      // 同一句话别重复发（识别器偶发重复触发 final）
      const now = Date.now();
      if (now - lastSubmit < 300) return;
      lastSubmit = now;
      onText && onText(text);
    }, silenceMs);
  }

  function build() {
    rec = new SR();
    rec.lang = lang;
    rec.interimResults = true;   // 要临时结果才能在说话时实时显示
    rec.continuous = true;       // 连续识别，配合 onend 重启

    rec.onstart = () => { running = true; setState(muted ? 'muted' : 'listening'); };

    rec.onresult = (e) => {
      let interim = '';
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const t = e.results[i][0].transcript;
        if (e.results[i].isFinal) {
          // final 段并入累积文本；注意浏览器可能重复给出同一段，这里按"追加"处理
          pending += t;
        } else {
          interim += t;
        }
      }
      // 把"已定稿 + 当前临时"整体给上层做实时回显（不发送）
      setState('listening');
      onText && onText(null, (pending + interim).trim());  // 第二参 = 预览文本
      armSilence();
    };

    rec.onerror = (e) => {
      // no-speech / aborted 属于正常现象（静音超时、主动 stop），不当作失败
      const benign = ['no-speech', 'aborted', 'audio-capture'];
      if (!benign.includes(e.error)) {
        console.warn('[ASR] 识别错误:', e.error);
      }
      if (e.error === 'not-allowed' || e.error === 'service-not-allowed') {
        muted = true;
        denied = true;
        setState('denied');
      }
    };

    rec.onend = () => {
      running = false;
      // 连续监听的核心：不是用户主动静音就自动重启，形成"常开麦克风"
      if (muted) {
        // 权限被拒时保留 denied，别被 muted 覆盖——否则用户看不到"去允许麦克风"的指引
        setState(denied ? 'denied' : 'muted');
        return;
      }
      try { rec.start(); } catch { /* 已在启动中，忽略 */ }
    };
  }

  build();

  return {
    // 开始监听（页面上首次调用会弹麦克风授权）
    start() {
      userOff = false;
      muted = false;
      denied = false;   // 用户此刻主动点开，视为重新尝试授权
      if (!running) {
        try { rec.start(); } catch { /* 已在运行 */ }
      }
      setState('listening');
    },
    // 停止并彻底关掉（不再自动重启）——这是"用户主动"的，播放结束也不恢复
    stop() {
      userOff = true;
      muted = true;
      denied = false;
      clearSilence();
      pending = '';
      try { rec.stop(); } catch {}
      setState('muted');
    },
    // 临时静音：数字人说话期间调用，避免把自己的声音听进去。不改变用户意图。
    pause() {
      if (userOff) return;      // 用户本来就关着，别把它算成"播放暂停"
      muted = true;
      clearSilence();
      try { rec.abort(); } catch {}
      setState('paused');
    },
    // 播放结束/打断后恢复监听；用户主动关掉的则保持关闭。
    resume() {
      if (userOff) return;
      muted = false;
      if (!running) {
        try { rec.start(); } catch {}
      }
      setState('listening');
    },
    // 外部（如用户点了发送/打断）丢弃当前累积，避免重复发送
    reset() {
      clearSilence();
      pending = '';
    },
    isMuted: () => muted,
    isUserOff: () => userOff,
  };
}
