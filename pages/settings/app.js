/**
 * AstrBot 插件页面：群聊权限门禁
 *
 * 所有后端调用都走 window.AstrBotPluginPage bridge（apiGet / apiPost），
 * 不直接 fetch Dashboard 的接口。
 */

const bridge = window.AstrBotPluginPage;

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function show(el) {
  if (!el) return;
  el.hidden = false;
  el.style.removeProperty('display');
}

function hide(el) {
  if (!el) return;
  el.hidden = true;
  el.style.setProperty('display', 'none', 'important');
}

const state = {
  global: {},
  groups: [],
  modes: [],
  actions: {},
  runtime: {},
  editing: null,
};

/* ------------------------------------------------------------------ 小工具 */
function toast(message, kind = '') {
  const el = $('#toast');
  el.textContent = message;
  el.className = `toast show ${kind}`;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => {
    el.className = 'toast';
  }, 2800);
}

function escapeHtml(str) {
  return String(str ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

function listToText(value) {
  return Array.isArray(value) ? value.join('\n') : String(value ?? '');
}

function textToList(value) {
  return String(value ?? '')
    .replace(/[，,]/g, '\n')
    .split('\n')
    .map((s) => s.trim())
    .filter(Boolean);
}

function setSelectOptions(select, items, placeholder) {
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
function fillGlobalForm() {
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

function collectGlobalForm() {
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
function renderStatus() {
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

function renderOverview() {
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

function renderHealth() {
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

function renderModes() {
  const list = $('#modeList');
  list.innerHTML = (state.modes || [])
    .map((m) => `<li><code>${escapeHtml(m.value)}</code><span>${escapeHtml(m.label)}</span></li>`)
    .join('');
}

function renderActions() {
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

function renderGroups() {
  const tbody = $('#groupsTable tbody');
  const modeLabel = (value) =>
    (state.modes || []).find((m) => m.value === value)?.label || value || '-';
  const rows = state.groups || [];
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="muted">还没有见过任何群。机器人收到过群消息后这里会自动出现。</td></tr>';
    return;
  }
  tbody.innerHTML = rows
    .map((item) => {
      const eff = item.effective || {};
      const source = item.configured ? '<span class="tag on">本群配置</span>' : '<span class="tag">跟随全局</span>';
      const enabled = eff.enabled ? '<span class="tag on">启用</span>' : '<span class="tag off">停用</span>';
      const ownerAdmin = eff.allow_owner_admin ? '开' : '关';
      const wl = (eff.whitelist || []).length;
      return `<tr>
        <td>${escapeHtml(item.platform || '（任意/旧数据）')}</td>
        <td><code>${escapeHtml(item.group_id)}</code></td>
        <td>${escapeHtml(item.override?.note || item.name || '-')} ${source}</td>
        <td>${enabled}</td>
        <td>${escapeHtml(modeLabel(eff.mode))}</td>
        <td>${escapeHtml(ownerAdmin)}</td>
        <td>${wl}</td>
        <td class="right">
          <button class="btn tiny primary" data-edit="${escapeHtml(item.key)}">编辑</button>
          <button class="btn tiny" data-reset="${escapeHtml(item.key)}">恢复全局</button>
          <button class="btn tiny danger" data-forget="${escapeHtml(item.key)}">删除记录</button>
        </td>
      </tr>`;
    })
    .join('');
  $$('[data-edit]', tbody).forEach((btn) => {
    btn.addEventListener('click', () => openEditor(btn.dataset.edit));
  });
  $$('[data-forget]', tbody).forEach((btn) => {
    btn.addEventListener('click', async () => {
      if (!armedConfirm(btn, '再点一次彻底删除')) return;
      try {
        await bridge.apiPost('page/group/forget', { key: btn.dataset.forget });
        if (state.editing === btn.dataset.forget) {
          state.editing = null;
          state.editingItem = null;
          hide($('#groupEditorCard'));
        }
        toast('已从列表删除', 'ok');
        await loadAll();
      } catch (err) {
        toast(`删除失败：${err?.message || err}`, 'err');
      }
    });
  });
  $$('[data-reset]', tbody).forEach((btn) => {
    btn.addEventListener('click', async () => {
      if (!armedConfirm(btn, '再点一次恢复')) return;
      await bridge.apiPost('page/group/reset', { key: btn.dataset.reset });
      toast('已恢复跟随全局（这行记录还在，要整行删掉请点「删除记录」）', 'ok');
      await loadAll();
    });
  });
}

/**
 * 受限 iframe 里 window.confirm / alert 会被浏览器静默忽略（sandbox 没有 allow-modals），
 * 所以危险操作改成「按钮自己变成确认按钮，再点一次才真执行」。
 */
function armedConfirm(button, label = '再点一次确认', timeout = 4000) {
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
function fillGroupModeSelect() {
  setSelectOptions($('#editMode'), state.modes || [], '跟随全局');
}

function openEditor(groupKey) {
  const item = (state.groups || []).find((g) => g.key === groupKey);
  const override = item?.override || {};
  state.editing = groupKey;
  state.editingItem = item || null;
  $('#editorGroupId').textContent = item?.group_id || groupKey;
  $('#editorPlatform').textContent = item?.platform ? `（平台：${item.platform}）` : '（旧数据 / 任意平台）';
  $('#editNote').value = override.note || item?.name || '';
  $('#editEnabled').value = override.enabled === undefined ? '' : String(override.enabled);
  $('#editMode').value = override.mode || '';
  $('#editOwnerAdmin').value =
    override.allow_owner_admin === undefined ? '' : String(override.allow_owner_admin);
  $('#editWhitelist').value = override.whitelist ? listToText(override.whitelist) : '';
  $('#editCommands').value = override.command_whitelist ? listToText(override.command_whitelist) : '';
  $('#editReply').value = override.reply || '';
  $('#editorHint').textContent = item?.configured ? '本群当前有单独配置' : '本群当前跟随全局默认';
  show($('#groupEditorCard'));
  document.querySelector('[data-tab="groups"]').click();
}

async function saveGroup() {
  const groupId = state.editing;
  if (!groupId) {
    toast('先选一个群', 'err');
    return;
  }
  const values = {};
  const enabled = $('#editEnabled').value;
  const mode = $('#editMode').value;
  const ownerAdmin = $('#editOwnerAdmin').value;
  values.enabled = enabled === '' ? null : enabled === 'true';
  values.mode = mode || null;
  values.allow_owner_admin = ownerAdmin === '' ? null : ownerAdmin === 'true';
  values.whitelist = $('#editWhitelist').value.trim() === '' ? null : textToList($('#editWhitelist').value);
  values.command_whitelist = $('#editCommands').value.trim() === '' ? null : textToList($('#editCommands').value);
  values.reply = $('#editReply').value.trim() === '' ? null : $('#editReply').value;
  const res = await bridge.apiPost('page/group', {
    group_id: state.editingItem?.group_id || groupId,
    platform: state.editingItem?.platform || '',
    note: $('#editNote').value,
    values,
  });
  toast(res?.message || '已保存', 'ok');
  await loadAll();
  openEditor(groupId);
}

/* ------------------------------------------------------------------ 模拟 */
async function runSimulate() {
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
async function loadDispatch() {
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
async function loadAll() {
  const data = await bridge.apiGet('page/overview');
  state.global = data.global || {};
  state.groups = data.groups || [];
  state.modes = data.modes || [];
  state.actions = data.actions || {};
  state.runtime = data.runtime || {};
  fillGroupModeSelect();
  setSelectOptions($('#modeSelect'), state.modes || []);
  fillGlobalForm();
  renderStatus();
  renderOverview();
  renderHealth();
  renderModes();
  renderActions();
  renderGroups();
  if (state.global.mode) $('#modeSelect').value = state.global.mode;
}

async function saveGlobal() {
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
function bindTabs() {
  $$('#tabs .tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      $$('#tabs .tab').forEach((t) => t.classList.remove('active'));
      $$('.panel').forEach((p) => p.classList.remove('active'));
      tab.classList.add('active');
      const panel = $(`.panel[data-panel="${tab.dataset.tab}"]`);
      if (panel) panel.classList.add('active');
      if (tab.dataset.tab === 'dispatch') loadDispatch();
    });
  });
}

async function main() {
  const context = await bridge.ready();
  bindTabs();
  $('#btnSave').addEventListener('click', saveGlobal);
  $('#btnReset').addEventListener('click', loadAll);
  $('#btnReloadGroups').addEventListener('click', loadAll);
  $('#btnPruneGroups').addEventListener('click', async () => {
    const btn = $('#btnPruneGroups');
    if (!armedConfirm(btn, '再点一次清理')) return;
    try {
      const res = await bridge.apiPost('page/groups/prune', {});
      toast(res?.message || '已清理', 'ok');
      await loadAll();
    } catch (err) {
      toast(`清理失败：${err?.message || err}`, 'err');
    }
  });
  $('#btnReloadDispatch').addEventListener('click', loadDispatch);
  $('#btnSaveGroup').addEventListener('click', saveGroup);
  $('#btnCloseEditor').addEventListener('click', () => hide($('#groupEditorCard')));
  $('#btnResetGroup').addEventListener('click', async () => {
    if (!state.editing) return;
    if (!armedConfirm($('#btnResetGroup'), '再点一次确认')) return;
    await bridge.apiPost('page/group/reset', { key: state.editing });
    toast('已恢复跟随全局', 'ok');
    await loadAll();
    openEditor(state.editing);
  });
  $('#btnSimulate').addEventListener('click', runSimulate);
  $('#btnNewGroup').addEventListener('click', async () => {
    const gid = $('#newGroupId').value.trim();
    if (!gid) {
      toast('先填群号', 'err');
      return;
    }
    const platform = $('#newGroupPlatform').value.trim();
    const key = platform ? `${platform}:${gid}` : gid;
    await bridge.apiPost('page/group', {
      group_id: gid,
      platform,
      values: { enabled: true },
    });
    $('#newGroupId').value = '';
    await loadAll();
    openEditor(key);
  });

  try {
    await loadAll();
  } catch (err) {
    toast(`加载配置失败：${err?.message || err}`, 'err');
  }
  hide($('#loading'));
  show($('#app'));
  if (context?.locale) document.documentElement.lang = context.locale;
}

main().catch((err) => {
  $('#loading').innerHTML = `<p>插件页面初始化失败：${escapeHtml(err?.message || err)}</p>`;
});
