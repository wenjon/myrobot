// profile_card.js：用户画像冲突确认卡（docs § 16.8 的 B 方案）。
//
// 背景：反思阶段 LLM 会从旧对话里提炼用户画像（称呼/偏好/职业）。
// 当某个字段**已有旧值且新值不同**时，服务端不自作主张，
// 而是下发 profile_conflict 帧让用户拍板。本模块负责把它渲染成一张卡。
//
// 为什么要展示 source_quote：没有原句的话，用户看到「把职业从 CTO 改成设计师？」
// 根本不知道机器人为何这么想，也就无法判断该不该同意。

const FIELD_LABELS = { name: '称呼', preferences: '偏好', occupation: '职业' };

/**
 * 创建一个确认卡容器管理器。
 * @param {HTMLElement} host 卡片挂载的容器
 * @param {(payload:object)=>void} sendFn 发送 WS 消息的函数
 */
export function createProfileCards(host, sendFn) {
  // conflict_id -> 卡片 DOM，用于服务端回 profile_resolved 时精确销毁
  const cards = new Map();

  function removeCard(conflictId) {
    const el = cards.get(conflictId);
    if (el) { el.remove(); cards.delete(conflictId); }
  }

  /** 收到 profile_conflict：渲染一张卡 */
  function show(msg) {
    const cid = msg.conflict_id;
    if (!cid || cards.has(cid)) return;   // 重复帧直接忽略

    const label = msg.field_label || FIELD_LABELS[msg.field] || msg.field;
    const card = document.createElement('div');
    card.className = 'profile-card';

    const title = document.createElement('div');
    title.className = 'pc-title';
    title.textContent = `记忆更新：${label}`;
    card.appendChild(title);

    const diff = document.createElement('div');
    diff.className = 'pc-diff';
    // 旧值带删除线、新值高亮，一眼能看出改什么
    const oldEl = document.createElement('span');
    oldEl.className = 'pc-old';
    oldEl.textContent = msg.old_value || '（空）';
    const arrow = document.createElement('span');
    arrow.className = 'pc-arrow';
    arrow.textContent = ' → ';
    const newEl = document.createElement('span');
    newEl.className = 'pc-new';
    newEl.textContent = msg.new_value || '（空）';
    diff.append(oldEl, arrow, newEl);
    card.appendChild(diff);

    if (msg.source_quote) {
      const quote = document.createElement('div');
      quote.className = 'pc-quote';
      quote.textContent = `依据：“${msg.source_quote}”`;
      card.appendChild(quote);
    }

    const row = document.createElement('div');
    row.className = 'pc-actions';
    const yes = document.createElement('button');
    yes.className = 'pc-yes';
    yes.textContent = '确认修改';
    const no = document.createElement('button');
    no.className = 'pc-no';
    no.textContent = '保留原值';

    function resolve(action) {
      // 禁用按钮防重复点击，但不立即移除卡片——
      // 等服务端回 profile_resolved 再移除，这样用户能看到“已生效”。
      yes.disabled = true; no.disabled = true;
      card.classList.add('pc-pending');
      sendFn({ type: 'profile_resolve', conflict_id: cid, action });
      // 兼容服务端未回帧的情况：3s 后强制移除，不让卡片永久悬着
      setTimeout(() => removeCard(cid), 3000);
    }
    yes.onclick = () => resolve('accept');
    no.onclick = () => resolve('reject');
    row.append(yes, no);
    card.appendChild(row);

    host.appendChild(card);
    cards.set(cid, card);
  }

  /** 收到 profile_resolved：服务端已落地，销毁卡片 */
  function resolved(msg) {
    removeCard(msg.conflict_id);
  }

  /** 新对话/清空时把所有卡片清掉 */
  function clearAll() {
    cards.forEach((el) => el.remove());
    cards.clear();
  }

  return { show, resolved, clearAll };
}
