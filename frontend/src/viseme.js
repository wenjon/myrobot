// viseme.js —— 汉字/拼音 → Oculus viseme 映射
// 基于拼音韵母分组到 15 个 viseme 加上 ARKit 表情

// 中文拼音韵母 → Oculus viseme 映射
const PINYIN_VISEME = {
  // 张大嘴 a
  a: 'viseme_aa', e: 'viseme_E', // 额
  // 圆唇 o/u/ü
  o: 'viseme_O', u: 'viseme_U', v: 'viseme_U',
  // 扁平 i/ü 口型
  i: 'viseme_I',
  // 双唇音 b/p/m
  b: 'viseme_PP', p: 'viseme_PP', m: 'viseme_PP',
  // 唇齿音 f
  f: 'viseme_FF',
  // 舌齿音 zh/ch/sh/z/c/s
  zh: 'viseme_CH', ch: 'viseme_CH', sh: 'viseme_CH',
  z: 'viseme_SS', c: 'viseme_SS', s: 'viseme_SS',
  // 舌根音 g/k/h
  g: 'viseme_kk', k: 'viseme_kk', h: 'viseme_kk',
  // 鼻音 n
  n: 'viseme_nn', l: 'viseme_DD',
  // 卷舌音 r
  r: 'viseme_RR', er: 'viseme_RR',
  // 舌尖音 t/d
  t: 'viseme_DD', d: 'viseme_DD',
  // 闭口
  _: 'viseme_sil',
};

// 取一个汉字的拼音首字母韵母（简化版，后续可换完整拼音库）
// 这里用字符的 Unicode 范围做粗略分组
function charToPinyinFirst(ch) {
  const code = ch.charCodeAt(0);
  // 常见汉字映射到韵母（demo 级，后续可换 pypinyin）
  const map = {
    的: 'e', 一: 'i', 是: 'i', 不: 'u', 了: 'e', 人: 'e', 我: 'o', 在: 'a',
    有: 'u', 他: 'a', 她: 'a', 它: 'a', 这: 'e', 中: 'o', 大: 'a', 小: 'a',
    来: 'a', 上: 'a', 下: 'a', 出: 'u', 也: 'e', 你: 'i', 好: 'a', 吧: 'a',
    吗: 'a', 呢: 'e', 啊: 'a', 哦: 'o', 嗯: 'e', 哈: 'a', 嘿: 'e', 嗨: 'a',
    天: 'a', 气: 'i', 今: 'i', 明: 'i', 昨: 'o', 日: 'i', 月: 'e', 年: 'a',
    星: 'i', 期: 'i', 早: 'a', 晚: 'a', 午: 'u', 好: 'a', 吃: 'i', 喝: 'e',
    玩: 'a', 笑: 'a', 哭: 'u', 走: 'o', 跑: 'a', 说: 'o', 话: 'a', 见: 'a',
    想: 'a', 知: 'i', 道: 'a', 开: 'a', 心: 'i', 乐: 'e', 喜: 'i', 怒: 'u',
    哀: 'a', 惊: 'i', 疑: 'i', 问: 'e', 答: 'a', 做: 'o', 能: 'e', 会: 'i',
    可: 'e', 以: 'i', 是: 'i', 爱: 'a', 欢: 'a', 迎: 'i', 谢: 'e', 请: 'i',
    对: 'i', 不: 'u', 起: 'i', 没: 'e', 关: 'a', 系: 'i', 再: 'a', 见: 'a',
    真: 'e', 漂: 'a', 亮: 'a', 帅: 'a', 美: 'e', 甜: 'a', 可: 'e',
    头: 'o', 脸: 'a', 眼: 'a', 鼻: 'i', 嘴: 'i', 耳: 'e', 眉: 'i',
  };
  if (map[ch]) return map[ch];

  // 通用 fallback: 按 unicode 范围分
  if (code >= 0x4E00 && code <= 0x9FFF) {
    const idx = (code - 0x4E00) % 6;
    const fallback = ['a', 'e', 'i', 'o', 'u', 'i'][idx];
    return fallback;
  }
  return null;
}

// 还原：韵母 → viseme
function vowelToViseme(v) {
  if (!v) return 'viseme_sil';
  return PINYIN_VISEME[v] || 'viseme_sil';
}

// 从一个字获得 viseme
export function visemeForChar(ch) {
  if (!ch || /[，。！？；、：,.!?;:\s…]/.test(ch)) return 'viseme_sil';
  const v = charToPinyinFirst(ch);
  return vowelToViseme(v);
}

// 一个词的 viseme（取最后一个字的）
export function visemeForText(text) {
  if (!text) return 'viseme_sil';
  return visemeForChar(text[text.length - 1]);
}

export const CLOSED = 'viseme_sil';
