/** 插件页面：总览 / 健康告警 / 模式与动作说明的渲染。 */

import { $, $$, bridge, state, hooks, toast, escapeHtml, armedConfirm, show, hide } from './common.js';

export function renderStatus() {
  const chips = $('#statusChips');
  const g = state.global || {};
  const items = [
    ['插件总开关', g.enable ? '开' : '关', g.enable ? 'ok' : 'off'],
    ['默认启用', g.default_group_enabled ? '是' : '否', g.default_group_enabled ? 'ok' : 'off'],
    ['群主/管理放行', g.allow_owner_admin ? '开' : '关', g.allow_owner_admin ? 'ok' : 'warn'],
    ['模式', g.mode || '-', ''],
    ['拦截优先级', String(state.runtime?.intercept_priority ?? '-'), ''],
  ];
  chips.innerHTML = items
    .map(([label, value, kind]) => `<span class="chip ${kind}">${escapeHtml(label)}：<b>${escapeHtml(value)}</b></span>`)
    .join('');
}

export function renderOverview() {
  const g = state.global || {};
  const rt = state.runtime || {};
  const h = rt.handler || {};
  const handlerText = h.core_api === false
    ? '无法自检（拿不到内部 API）'
    : h.registered
      ? `已注册 · 第 ${(h.position ?? 0) + 1}/${h.total ?? '?'} 个 · 优先级 ${h.priority}`
      : '❌ 未注册（handler 丢了，群里不会生效）';
  const rows = [
    ['插件版本', rt.plugin_version || '-'],
    ['插件总开关', g.enable ? '开启' : '关闭'],
    ['默认对所有群启用', g.default_group_enabled ? '是' : '否'],
    ['放行群主 / 群管理', g.allow_owner_admin ? '开' : '关（他们也要靠白名单放行）'],
    ['放行 AstrBot 全局管理员', g.allow_global_admin ? '开' : '关'],
    ['未授权成员处理模式', g.mode || '-'],
    ['全局白名单人数', String((g.whitelist || []).length)],
    ['命令白名单', (g.command_whitelist || []).join('、') || '（空）'],
    ['角色识别不到时', g.unknown_role_action === 'allow' ? '直接放行' : '按未授权处理'],
    ['拦截器状态', handlerText],
    ['plugin_set', (rt.plugin_set && rt.plugin_set.ok === false)
      ? `❌ 不含本插件（${JSON.stringify(rt.plugin_set.value)}）`
      : '包含本插件 / 全部启用 ✅'],
    ['按群配置数据文件', rt.data_file || '-'],
  ];
  $('#overviewKv').innerHTML = rows
    .map(([k, v]) => `<div class="kv-row"><span>${escapeHtml(k)}</span><b>${escapeHtml(v)}</b></div>`)
    .join('');
}

export function renderHealth() {
  const el = $('#healthBanner');
  const rt = state.runtime || {};
  const h = rt.handler || {};
  const ps = rt.plugin_set || {};
  const problems = [];
  let fixButton = '';
  if (ps.ok === false) {
    problems.push(
      'AstrBot 配置里的 plugin_set 不包含本插件 —— 本插件的拦截器、兜底闸门和 gperm 指令' +
      '会被全部跳过（插件页面不受影响，所以看起来「装了但没反应」）。' +
      `当前 plugin_set：${JSON.stringify(ps.value)}`,
    );
    if (ps.fixable) {
      fixButton = '<button class="btn primary tiny" id="btnFixPluginSet">一键把本插件加进 plugin_set</button>';
    } else {
      problems.push('plugin_set 不是列表，请手工修改 AstrBot 配置。');
    }
  }
  if (h.core_api === false) {
    problems.push('拿不到 AstrBot 内部注册表，无法自检（拦截本身不受影响）。');
  } else if (h.registered === false) {
    problems.push('拦截器没有注册到 AstrBot 的 handler 注册表里 —— 群里不会生效。请重载插件，或用 gperm debug 查看。');
  } else if ((h.position ?? 0) > 0) {
    problems.push(
      `拦截器前面还有 ${h.position} 个 handler（${(h.ahead || []).join('、')}）。` +
      '它们一旦先 stop_event()，本插件就拦不到消息 —— 请到「分发顺序」页签确认。',
    );
  }
  if (h.self_healed) {
    problems.push('本次启动时发现 handler 曾经丢失，已自动补注册（通常是插件热重载导致的）。');
  }
  if (!problems.length) {
    hide(el);
    return;
  }
  el.innerHTML = problems.map((t) => `<p>⚠️ ${escapeHtml(t)}</p>`).join('')
    + (fixButton ? `<p>${fixButton}</p>` : '');
  show(el);
  const btn = $('#btnFixPluginSet');
  if (btn) {
    btn.addEventListener('click', async () => {
      if (!armedConfirm(btn, '再点一次确认修改全局配置')) return;
      try {
        const res = await bridge.apiPost('page/fix-plugin-set', {});
        toast(res?.message || '已修改 plugin_set', 'ok');
        await loadAll();
      } catch (err) {
        toast(`修改失败：${err?.message || err}`, 'err');
      }
    });
  }
}

export function renderModes() {
  const list = $('#modeList');
  list.innerHTML = (state.modes || [])
    .map((m) => `<li><code>${escapeHtml(m.value)}</code><span>${escapeHtml(m.label)}</span></li>`)
    .join('');
}

export function renderActions() {
  const kv = $('#actionKv');
  const labels = {
    allow: '放行',
    block_llm_only: '只拦 AI',
    allow_command: '放行命令',
    block_silent: '静默丢弃',
    block_reply: '回复后丢弃',
  };
  kv.innerHTML = Object.entries(state.actions || {})
    .map(([key, desc]) => `<div class="kv-row"><span><code>${escapeHtml(labels[key] || key)}</code></span><b>${escapeHtml(desc)}</b></div>`)
    .join('');
}
