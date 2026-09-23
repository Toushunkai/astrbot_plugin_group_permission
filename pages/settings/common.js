/**
 * 插件页面：公共基础设施（bridge 桥、DOM 小工具、共享状态、toast、二次确认）。
 *
 * 页面脚本按功能拆成多个 ES module，由 app.js 统一入口加载；
 * 模块之间用这里的 hooks 做回调，避免循环 import。
 */

export const bridge = window.AstrBotPluginPage;

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** app.js 启动时接上这些回调，供其它模块在需要时触发整页刷新 / 打开编辑器。 */
export const hooks = { reload: () => {}, openEditor: () => {} };

export function show(el) {
  if (!el) return;
  el.hidden = false;
  el.style.removeProperty('display');
}

export function hide(el) {
  if (!el) return;
  el.hidden = true;
  el.style.setProperty('display', 'none', 'important');
}

export const state = {
  global: {},
  groups: [],
  modes: [],
  actions: {},
  runtime: {},
  editing: null,
};

/* ------------------------------------------------------------------ 小工具 */
export function toast(message, kind = '') {
  const el = $('#toast');
  el.textContent = message;
  el.className = `toast show ${kind}`;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => {
    el.className = 'toast';
  }, 2800);
}

export function escapeHtml(str) {
  return String(str ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

export function listToText(value) {
  return Array.isArray(value) ? value.join('\n') : String(value ?? '');
}

export function textToList(value) {
  return String(value ?? '')
    .replace(/[，,]/g, '\n')
    .split('\n')
    .map((s) => s.trim())
    .filter(Boolean);
}

export function setSelectOptions(select, items, placeholder) {
  if (!select) return;
  const current = select.value;
  select.innerHTML = '';
  if (placeholder !== undefined) {
    const opt = document.createElement('option');
    opt.value = '';
    opt.textContent = placeholder;
    select.appendChild(opt);
  }
  items.forEach((item) => {
    const opt = document.createElement('option');
    opt.value = item.value;
    opt.textContent = item.label;
    select.appendChild(opt);
  });
  if (current) select.value = current;
}

/* ------------------------------------------------------------------ 表单 */
export function armedConfirm(button, label = '再点一次确认', timeout = 4000) {
  if (!button) return true;
  if (button.dataset.armed === '1') {
    button.dataset.armed = '';
    button.textContent = button.dataset.originalLabel || button.textContent;
    button.classList.remove('armed');
    return true;
  }
  button.dataset.armed = '1';
  button.dataset.originalLabel = button.textContent;
  button.textContent = label;
  button.classList.add('armed');
  clearTimeout(button._armTimer);
  button._armTimer = setTimeout(() => {
    button.dataset.armed = '';
    button.textContent = button.dataset.originalLabel || button.textContent;
    button.classList.remove('armed');
  }, timeout);
  return false;
}

/* ------------------------------------------------------------------ 群编辑 */
