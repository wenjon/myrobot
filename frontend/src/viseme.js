// viseme.js：把一个字/词映射到口型参数 {open, wide}
// 简化中文口型：根据拼音韵母粗分。无拼音表时用字符兜底。
const VOWEL_SHAPE = {
  a: { open: 1.0, wide: 0.6 },
  o: { open: 0.7, wide: 0.2 },
  e: { open: 0.5, wide: 0.8 },
  i: { open: 0.3, wide: 0.9 },
  u: { open: 0.5, wide: 0.15 },
  v: { open: 0.4, wide: 0.2 }, // ü
};

// 极简常用字->韵母 首字母 映射兜底（demo 用字符编码做伪随机口型）
function pseudoShape(ch) {
  const code = ch.charCodeAt(0);
  const keys = Object.keys(VOWEL_SHAPE);
  const k = keys[code % keys.length];
  return VOWEL_SHAPE[k];
}

// 从一个词/字里取主要口型（取最后一个有韵母的字）
export function shapeForText(text) {
  if (!text) return { open: 0.1, wide: 0.5 };
  const ch = text[text.length - 1];
  // 标点=闭嘴
  if (/[，。！？；、：,.!?;:\s…]/.test(ch)) return { open: 0.05, wide: 0.5 };
  return pseudoShape(ch);
}

// 闭嘴
export const CLOSED = { open: 0.03, wide: 0.5 };
