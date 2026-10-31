/* QQ owns its controls and projections; memory remains in shared-memory.js. */
function loadQQSettings() {
  for(const n of document.querySelectorAll('[data-qq-learning]'))n.value=String(config.qq_learning[n.dataset.qqLearning]);
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
  for(const node of document.querySelectorAll('[data-qq-group]')){
    const key=node.dataset.qqGroup,value=config.qq_groups[key];node.value=key==='group_ids'?value.join(','):String(value);
  }

}

async function saveQQGroups(){try{
 const settings=Object.fromEntries([...document.querySelectorAll('[data-qq-group]')].map(n=>[n.dataset.qqGroup,
  n.dataset.qqGroup==='group_ids'?n.value.split(/[,，\s]+/).filter(Boolean):n.type==='number'?Number(n.value):n.value==='true']));
 await api('config',{qq_groups:settings});await loadConfig();await refresh();notice('群聊设置已保存；请保持QQ平台服务运行，自主参与由独立开关控制。');
}catch(e){notice(e.message,true)}}

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
  renderGroupLearning(data);
  const connection = data.connection, buffered = data.buffered;
  document.querySelector('#qq-group-status').textContent=config.qq_groups.enabled?'群聊已开启 · 指定 '+config.qq_groups.group_ids.length+' 个群 · '+(config.qq_groups.autonomous_enabled?'自主判断接话或旁观':'仅被@或引用时回复')+(config.qq_learning.enabled?'；语言学习已开启（顺序自检）':'；语言学习已暂停'):'被动群聊已关闭。';
  rows('qq-group-records',data.groups?.records||[],r=>`<div class="row"><span class="pill">${esc(r.speaker_name||'群成员')} · ${esc(r.speaker_id)}</span><div class="message-body">${esc(r.text)}</div><div class="meta">群 ${esc(r.group_id)} · ${esc(time(r.occurred_at))}</div></div>`);

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
  rows('qq-conversations', [...data.records].filter(r=>r.visibility==='private').reverse(), r =>
    `<div class="row conversation-${esc(r.role)}"><span class="pill">${r.role === 'user' ? '主人' : '丛雨 · 已受理'}</span><div class="message-body">${esc(r.text)}</div><div class="meta">QQ · ${esc(time(r.occurred_at))}</div></div>`);
  rows('qq-pending-events', data.pending_events, r => {
    const event = JSON.parse(r.payload);
    const scope=event.visibility==='group'?`群 ${esc(event.thread_root)} · ${esc(event.actor_name||event.actor_id)}（${esc(event.actor_id)}）`:'主人私聊';
    return `<div class="row"><span class="pill state-${esc(r.state)}">${esc(({pending:'等待处理',processing:'正在生成',failed:'处理失败'})[r.state])}</span><div class="message-body">${esc(event.text || (event.kind==='group_window'?'群讨论窗口：判断接话或旁观':''))}</div><div class="meta">${scope} · ${esc(time(event.occurred_at))} · 图片 ${event.images?.length || 0} 张${r.next_at ? ' · 下次重试 '+esc(time(r.next_at)) : ''}${r.reason ? ' · '+esc(r.reason) : ''}</div></div>`;
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
  'qq-pending-events': ['没有等待处理的消息', '私聊先进入静默窗口；群聊被@或引用丛雨时进入处理队列。'],
  'qq-actions': ['还没有QQ相关发送记录', 'QQ私聊回复、视频结果及由QQ委托产生的公开投递会单独显示。'],
  'qq-tasklist': ['还没有来自QQ的视频委托', '在QQ告诉丛雨想看什么；默认回QQ，也可以明确要求在B站提及。']
});

emptyCopies['qq-group-records']=['尚无群聊归档','启用指定群后，被动接话及被平台受理的回复会在这里显示。'];

async function saveQQLearning(){try{
 const settings=Object.fromEntries([...document.querySelectorAll('[data-qq-learning]')].map(n=>[n.dataset.qqLearning,n.type==='number'?Number(n.value):n.value==='true']));
 await api('config',{qq_learning:settings});await loadConfig();await refresh();notice('群学习设置已保存');
}catch(e){notice(e.message,true)}}
function renderGroupLearning(data){
 document.querySelector('#qq-learning-state').textContent='批次阶段：'+JSON.stringify(data.groups?.learning_stages||{})+' · 新消息优先，空闲时学习';
 rows('qq-learning-records',data.groups?.learning||[],r=>`<div class="row"><span class="pill">${esc(r.kind==='jargon'?'群内用语':'表达')} · ${esc(r.status)} · ${r.hits}批${r.manual?' · 人工':''}</span><p>${esc(r.term)}：${esc(r.meaning)}</p><div class="meta">群 ${esc(r.group_id)} · 顺序自检，非独立核验</div><div class="actions">${['sources','correct','disable','forget'].map(op=>`<button data-learning-op="${op}" data-room="${esc(r.group_id)}" data-key="${esc(r.id)}">${({sources:'查看来源',correct:'人工更正',disable:'停用',forget:'遗忘'})[op]}</button>`).join('')}</div></div>`);
 document.querySelectorAll('[data-learning-op]').forEach(b=>b.onclick=async()=>{try{
  const request={id:b.dataset.key,room:b.dataset.room,operation:b.dataset.learningOp};
  if(request.operation==='correct'){request.meaning=prompt('填写准确释义或表达说明');if(request.meaning===null)return}
  if(request.operation==='sources'){const r=await api('qq-learning/sources',request);document.querySelector('#qq-learning-sources').textContent=JSON.stringify(r,null,2);return}
  await api('qq-learning/manage',request);await refresh();
 }catch(e){notice(e.message,true)}});
}
emptyCopies['qq-learning-records']=['尚无语言学习记录','指定群有足量实时文字后，在空闲维护时提取和自检；观察中不代表已掌握。'];
