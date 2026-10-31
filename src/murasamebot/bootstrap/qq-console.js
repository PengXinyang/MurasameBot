/* QQ owns its controls and projections; memory remains in shared-memory.js. */
function loadQQSettings() {
  const q = config.qq || {};
  document.querySelector('#qq-enabled').value = String(config.platforms?.qq?.enabled ?? false);
  for (const key of ['quiet_seconds', 'max_wait_seconds', 'max_reply_messages']) {
    document.querySelector(`[data-qq-setting="${key}"]`).value = q[key];
  }
  for (const node of document.querySelectorAll('[data-qq-autonomy]')) {
    const key = node.dataset.qqAutonomy, value = config.qq_autonomy[key];
    node.value = key === 'windows' ? value.map(w => w.start+'-'+w.end).join('\n')
      : key === 'weekdays' ? value.join(',') : key === 'topics' ? value.join('\n') : String(value);
  }
  for (const node of document.querySelectorAll('[data-qq-space]')) {
    const key = node.dataset.qqSpace, value = config.qq_space[key];
    node.value = key === 'windows' ? value.map(w => w.start+'-'+w.end).join('\n')
      : key === 'weekdays' ? value.join(',') : key === 'topics' ? value.join('\n') : String(value);
  }
}

async function saveQQAutonomy() {
  try {
    const settings = Object.fromEntries([...document.querySelectorAll('[data-qq-autonomy]')].map(node => {
      const key = node.dataset.qqAutonomy, value = node.value;
      return [key, key === 'windows' ? value.split('\n').filter(s=>s.trim()).map(s=>{const [start,end]=s.trim().split('-');return {start,end};})
        : key === 'weekdays' ? value.split(/[,，]/).filter(s=>s.trim()).map(Number)
        : key === 'topics' ? value.split('\n').map(s=>s.trim()).filter(Boolean)
        : node.type === 'number' ? Number(value) : value === 'true'];
    }));
    await api('config', {qq_autonomy:settings});
    await loadConfig(); await refresh();
    notice('QQ主动私聊设置已保存；保持QQ平台服务运行，丛雨会选择发送、稍后发送或跳过。');
  } catch (e) { notice(e.message, true); }
}

async function saveQQSpace() {
  try {
    const settings = Object.fromEntries([...document.querySelectorAll('[data-qq-space]')].map(node => {
      const key = node.dataset.qqSpace, value = node.value;
      return [key, key === 'windows' ? value.split('\n').filter(s=>s.trim()).map(s=>{const [start,end]=s.trim().split('-');return {start,end};})
        : key === 'weekdays' ? value.split(/[,，]/).filter(s=>s.trim()).map(Number)
        : key === 'topics' ? value.split('\n').map(s=>s.trim()).filter(Boolean)
        : node.type === 'number' ? Number(value) : value === 'true'];
    }));
    await api('config', {qq_space:settings});
    await loadConfig(); await refresh();
    notice('空间设置已保存；保持QQ服务运行，可在下方查看说说计划和回执。');
  } catch (e) { notice(e.message, true); }
}

async function saveQQSettings() {
  try {
    const qq = Object.fromEntries([...document.querySelectorAll('[data-qq-setting]')]
      .map(n => [n.dataset.qqSetting, Number(n.value)]));
    await api('config', {qq, platforms: {qq: {enabled: document.querySelector('#qq-enabled').value === 'true'}}});
    await loadConfig();
    await refresh();
    notice('QQ设置已保存，管理台已同步模块启停状态；NapCat需保持运行。');
  } catch (e) { notice(e.message, true); }
}

async function checkQQConnection() {
  const button = document.querySelector('#qq-check');
  button.disabled = true;
  try {
    const result = await api('qq/check', {});
    notice(result.owner_is_friend ? '机器人账号及主人好友关系核验通过；未发送消息。' : '机器人账号匹配，但未确认主人好友关系。', !result.owner_is_friend);
    renderQQConsole(await api('platforms/qq'));
  } catch (e) { notice(e.message, true); }
  finally { button.disabled = false; }
}

function renderQQConsole(data) {
  const connection = data.connection, buffered = data.buffered;
  const space = data.space?.status || {};
  document.querySelector('#qq-space-status').textContent = !config.qq_space.dynamic_enabled ? '空间自主说说已关闭。'
    : `最近判断：${space.state || '等待首次检查'}${space.planned_at ? ' · 计划时间 '+time(space.planned_at) : ''}${space.next_at ? ' · 下次检查 '+time(space.next_at) : ''}`;
  const initiative = data.autonomy || {}, status = initiative.status || {};
  const stateNames = {waiting:'等待合适时机',skipped:'本轮选择不发送',planned:'已安排主动私聊',error:'检查失败，等待重试',conversation_busy:'正在聊天，暂不打扰',relationship_blocked:'账号或主人好友关系未确认',budget_exhausted:'今日判断额度已用完',duplicate_skipped:'跳过重复内容',outside_window:'不在允许时段',invalidated:'来源或授权已变化',private_disabled:'主动私聊已关闭'};
  document.querySelector('#qq-autonomy-status').textContent = !config.qq_autonomy.private_enabled ? '主动私聊已关闭。'
    : !data.enabled ? '主动私聊已开启；请先启动QQ平台服务。'
    : status.state ? `${stateNames[status.state] || status.state} · 上次判断 ${time(status.at)}${status.planned_at ? ' · 计划时间 '+time(status.planned_at) : ''} · 下次检查 ${time(status.next_at)}`
    : '主动私聊已开启，等待工作循环首次判断。';
  document.querySelector('#qq-module-state').textContent = data.enabled ? '模块已启用' : '模块未启用';
  document.querySelector('#qq-overview-state').textContent = data.enabled ? '已启用 · 私聊与消息中管理' : '未启用';
  document.querySelector('#qq-robot').textContent = connection.robot_id || '未配置';
  document.querySelector('#qq-owner').textContent = data.owner_id || '未绑定';
  document.querySelector('#qq-api').textContent = connection.api_url;
  document.querySelector('#qq-config-state').textContent = connection.configured ? '账号、绑定与凭证已配置' : '账号、绑定或凭证待配置';
  const check = connection.last_check;
  document.querySelector('#qq-connection-check').textContent = check.checked_at
    ? `上次核验 ${time(check.checked_at)} · ${check.owner_is_friend ? '机器人身份及主人好友通过' : '主人好友未确认'}`
    : '尚未核验真实连接；已配置不等于网关在线。';
  document.querySelector('#qq-buffer-note').textContent = buffered.messages
    ? `${buffered.messages} 条消息正在等待合并 · 最后一条 ${time(buffered.last_at)} · 静默 ${data.batching.quiet_seconds} 秒后处理`
    : `当前没有等待合并的消息 · 默认静默窗口 ${data.batching.quiet_seconds} 秒`;
  const stats = [
    ['合并中的消息', buffered.messages],
    ['待处理 / 生成中', (data.event_counts.pending || 0) + (data.event_counts.processing || 0)],
    ['等待发送 / 审核', ['queued','draft','reviewing','sending'].reduce((n,k) => n + (data.action_counts[k] || 0), 0)],
    ['平台已受理', data.action_counts.accepted || 0]
  ];
  document.querySelector('#qq-metrics').innerHTML = stats.map(([label,value]) =>
    `<div class="metric"><small>${esc(label)}</small><strong>${value}</strong></div>`).join('');
  rows('qq-conversations', [...data.records].reverse(), r =>
    `<div class="row conversation-${esc(r.role)}"><span class="pill">${r.role === 'user' ? '主人' : '丛雨 · 已受理'}</span><div class="message-body">${esc(r.text)}</div><div class="meta">QQ · ${esc(time(r.occurred_at))}</div></div>`);
  rows('qq-pending-events', data.pending_events, r => {
    const event = JSON.parse(r.payload);
    return `<div class="row"><span class="pill state-${esc(r.state)}">${esc(({pending:'等待处理',processing:'正在生成',failed:'处理失败'})[r.state])}</span><div class="message-body">${esc(event.text)}</div><div class="meta">${esc(time(event.occurred_at))} · 图片 ${event.images?.length || 0} 张${r.next_at ? ' · 下次重试 '+esc(time(r.next_at)) : ''}${r.reason ? ' · '+esc(r.reason) : ''}</div></div>`;
  });
  rows('qq-actions', data.actions, renderActionRow);
  rows('qq-tasklist', data.tasks, renderTaskRow);
  document.querySelector('#qq-task-count').textContent = `最近 ${data.tasks.length} 条委托（最多 ${data.limit} 条）`;
  document.querySelector('#qq-task-window').textContent = `共同查找时段：${config.browsing.windows.map(w=>w.start+'–'+w.end).join('、')} · 未命中重试间隔 ${config.browsing.interval_minutes} 分钟；仍受任务有效期和额度约束。`;
  document.querySelector('#qq-record-count').textContent = `最近 ${data.records.length} 条归档 · 原文与来源在共同记忆中查询`;
  bindConsoleOperations(document.querySelector('#qq'));
  bindConsoleOperations(document.querySelector('#qq-tasks'));
}

Object.assign(emptyCopies, {
  'qq-conversations': ['还没有QQ聊天归档', '启用接收器与工作循环后，已处理来信及被平台受理的回复会出现在这里。'],
  'qq-pending-events': ['没有等待处理的合并消息', '连续来信会先进入静默窗口，再交给丛雨处理。'],
  'qq-actions': ['还没有QQ相关发送记录', 'QQ私聊回复、视频结果及由QQ委托产生的公开投递会单独显示。'],
  'qq-tasklist': ['还没有来自QQ的视频委托', '在QQ告诉丛雨想看什么；默认回QQ，也可以明确要求在B站提及。']
});
