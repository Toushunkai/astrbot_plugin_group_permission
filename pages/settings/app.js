/**
 * 插件页面入口：加载配置、页签切换、底部保存栏、以及按钮事件绑定。
 *
 * 其它模块见 common.js / global.js / render.js / groups.js / simulate.js。
 */

import { $, $$, bridge, state, hooks, toast, show, hide, armedConfirm, setSelectOptions } from './common.js';
import { fillGlobalForm, saveGlobal } from './global.js';
import { renderStatus, renderOverview, renderHealth, renderModes, renderActions } from './render.js';
import { renderGroups, fillGroupModeSelect, openEditor, saveGroup } from './groups.js';
import { runSimulate, loadDispatch } from './simulate.js';

export async function loadAll() {
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

export function bindTabs() {
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

hooks.reload = loadAll;
hooks.openEditor = openEditor;

export async function main() {
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
main().catch((err) => {
  $('#loading').innerHTML = `<p>插件页面初始化失败：${escapeHtml(err?.message || err)}</p>`;
});
