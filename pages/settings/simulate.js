/** 插件页面：判定模拟与运行时分发顺序。 */

import { $, bridge, escapeHtml, show } from './common.js';
import { state } from './common.js';

export async function runSimulate() {
  const btn = $('#btnSimulate');
  btn.disabled = true;
  try {
    const res = await bridge.apiPost('page/simulate', {
      group_id: $('#simGroup').value.trim(),
      user_id: $('#simUser').value.trim(),
      role: $('#simRole').value,
      is_global_admin: $('#simGlobalAdmin').value === 'true',
      message: $('#simMessage').value,
      user_name: '',
    });
    const lines = [
      `判定动作：${res.action}`,
      `是否禁止 AI 对话：${res.blocks_llm ? '是（@ 与唤醒词都不会有 AI 回复）' : '否'}`,
      `是否中断事件（连其它插件一起拦）：${res.halts_event ? '是' : '否'}`,
      `命中命令白名单：${res.matched_command || '无'}`,
      `原因：${res.reason}`,
      '',
      `生效设置：模式=${res.effective?.mode}，启用=${res.effective?.enabled}，` +
        `群主管理放行=${res.effective?.allow_owner_admin}，白名单=${(res.effective?.whitelist || []).length} 人`,
    ];
    if (res.reply_preview) {
      lines.push('', '会回复的文案：', res.reply_preview);
    }
    const el = $('#simResult');
    el.textContent = lines.join('\n');
    show(el);
    $('#simHint').textContent = '';
  } catch (err) {
    $('#simHint').textContent = `判定失败：${err?.message || err}`;
  } finally {
    btn.disabled = false;
  }
}

/* ------------------------------------------------------------------ 分发顺序 */
export async function loadDispatch() {
  const tbody = $('#dispatchTable tbody');
  tbody.innerHTML = '<tr><td colspan="5" class="muted">读取中…</td></tr>';
  try {
    const res = await bridge.apiGet('page/dispatch');
    $('#dispatchNote').textContent = res.note || '';
    if (!res.items?.length) {
      tbody.innerHTML = `<tr><td colspan="5" class="muted">没有读到 handler${res.error ? `：${escapeHtml(res.error)}` : ''}</td></tr>`;
      return;
    }
    tbody.innerHTML = res.items
      .map((item) => {
        const cls = item.is_gate ? ' class="highlight"' : '';
        return `<tr${cls}>
          <td>${item.index}</td>
          <td>${escapeHtml(item.plugin)}${item.is_gate ? ' <span class="tag on">本插件</span>' : ''}</td>
          <td><code>${escapeHtml(item.handler)}</code></td>
          <td>${escapeHtml(item.priority)}</td>
          <td class="muted">${escapeHtml((item.filters || []).join(', '))}</td>
        </tr>`;
      })
      .join('');
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="5" class="muted">读取失败：${escapeHtml(err?.message || err)}</td></tr>`;
  }
}

/* ------------------------------------------------------------------ 加载 */
