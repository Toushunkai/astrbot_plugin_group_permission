/** 插件页面：按群配置（群列表 + 每群覆盖编辑）。 */

import { $, $$, bridge, state, hooks, toast, escapeHtml, armedConfirm, setSelectOptions, hide } from './common.js';
import { listToText, textToList } from './common.js';

export function renderGroups() {
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
export function fillGroupModeSelect() {
  setSelectOptions($('#editMode'), state.modes || [], '跟随全局');
}

export function openEditor(groupKey) {
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

export async function saveGroup() {
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
