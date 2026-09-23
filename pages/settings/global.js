/** 插件页面：全局默认配置表单（读写 + 保存）。 */

import { $, $$, bridge, state, hooks, toast } from './common.js';
import { listToText, textToList } from './common.js';

export function fillGlobalForm() {
  $$('[data-path]').forEach((el) => {
    const path = el.dataset.path;
    const type = el.dataset.type || 'string';
    const value = state.global[path];
    if (value === undefined || value === null) return;
    if (type === 'bool') el.checked = Boolean(value);
    else if (type === 'list') el.value = listToText(value);
    else el.value = value;
  });
}

export function collectGlobalForm() {
  const payload = {};
  $$('[data-path]').forEach((el) => {
    const path = el.dataset.path;
    const type = el.dataset.type || 'string';
    if (type === 'bool') payload[path] = el.checked;
    else if (type === 'list') payload[path] = textToList(el.value);
    else if (type === 'int') payload[path] = Number(el.value || 0);
    else payload[path] = el.value;
  });
  return payload;
}

/* ------------------------------------------------------------------ 渲染 */
export async function saveGlobal() {
  const btn = $('#btnSave');
  btn.disabled = true;
  try {
    const res = await bridge.apiPost('page/global', collectGlobalForm());
    toast(res?.message || '已保存', 'ok');
    await loadAll();
  } catch (err) {
    toast(`保存失败：${err?.message || err}`, 'err');
  } finally {
    btn.disabled = false;
  }
}

/* ------------------------------------------------------------------ 初始化 */
