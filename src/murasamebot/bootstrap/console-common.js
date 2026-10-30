/* Shared presentation and common controls; platform protocols stay server-side. */
const platformName = value => ({qq:'QQ',bilibili:'B站',wechat:'微信'})[value] || value || '未知平台';

function bindConsoleOperations(root) {
  root.querySelectorAll('[data-op]').forEach(button => button.onclick = async () => {
    button.disabled = true;
    try { await operate(button.dataset.op, {id:button.dataset.id}); }
    finally { button.disabled = false; }
  });
}

function renderTaskRow(row) {
  const route = row.route || {}, target = route.platform || row.target_platform;
  const terminal = ['completed','cancelled','expired','failed','delivery_unknown'].includes(row.state);
  const source = row.source_platform || route.source_platform || row.target_platform;
  const delivery = route.mode === 'private' ? '私聊回传' : '公开提及';
  return `<div class="row" data-task-id="${esc(row.task_id)}"><span class="pill state-${esc(row.state)}">${esc(names[row.state] || row.state)}</span><strong>${esc(row.topic)}</strong>
    <div class="meta">${esc(platformName(source))} 发起 → ${esc(platformName(target))} ${delivery} · 内容来源 ${esc(platformName(route.provider || row.target_platform))}</div>
    <div class="meta">${row.state === 'scheduled' ? '预约查找' : '计划 / 下次查找'} ${esc(time(row.due_at))} · 有效至 ${esc(time(row.expires_at))} · 已查找 ${row.attempts} 次</div>
    ${row.reason ? `<div class="meta">${esc(row.reason)}</div>` : ''}
    ${terminal ? '' : `<div class="actions"><button data-op="cancel" data-id="${esc(row.task_id)}">取消委托</button></div>`}</div>`;
}

function readCommonSettings() {
  const value = id => document.getElementById(id).value;
  const number = id => Number(value(id));
  const yes = id => value(id) === 'true';
  const split = id => value(id).split(/[,，]/).map(s => s.trim()).filter(Boolean);
  return {
    browsing:{interval_minutes:number('interval'),weekdays:split('weekdays').map(Number),
      windows:value('windows').split('\n').filter(s=>s.trim()).map(s=>{const [start,end]=s.trim().split('-');return {start,end};})},
    review:{strategy:value('reviewstrategy')}, mode:value('mode'),
    processing:{model_concurrency:number('concurrency')}, model:{daily_call_limit:number('modeldaily')},
    limits:Object.fromEntries([...document.querySelectorAll('[data-limit]')].map(n => [n.dataset.limit,Number(n.value)])),
    expressions:{enabled:yes('emojienabled'),kaomoji_enabled:yes('kaomojienabled'),max_per_message:number('emojimax'),cache_minutes:number('emojicache'),preferred_keywords:split('emojikeywords')},
    vision:{video_frames_enabled:yes('videoframesenabled'),video_frame_count:number('videoframecount'),video_frame_max_parts:number('videoframeparts'),enabled:yes('visionenabled'),max_images:number('visionimages'),detail:value('visiondetail')},
    network:{proxy_enabled:yes('proxyenabled'),proxy_url:value('proxyurl'),proxy_mode:value('proxymode'),gfwlist_refresh_hours:number('proxyrefresh'),direct_domains:split('proxydirect')}
  };
}

function loadCommonSettings() {
  document.querySelector('#shared-model-config').textContent = `共用模型：${config.model.name || '待配置'} · 地址与密钥在本地私密配置中管理。`;
}

async function saveCommonSettings() {
  try {
    await api('config', readCommonSettings());
    await loadConfig(); await refresh();
    notice('共同设置已保存，QQ和B站共用这些规则。');
  } catch (e) { notice(e.message, true); }
}

function publicationDetail(row){const check=row.receipt?JSON.parse(row.receipt).publication_check:null;return check?`<div class="meta">评论状态：${esc(check.detail)}${check.platform_state!==null&&check.platform_state!==undefined?' · state='+esc(check.platform_state):''} · ${esc(time(check.checked_at))}</div>`:''}
function receiptDetail(row,action){if(!row.receipt)return '';const receipt=JSON.parse(row.receipt);return action.kind==='private_reply'&&row.state==='accepted'&&receipt.state==='accepted'&&receipt.detail==='平台已受理私信，客户端送达尚未独立验证'?'平台已受理私信':receipt.detail||''}
function renderActionRow(row){let a=JSON.parse(row.payload);return `<div class="row" data-action-kind="${esc(a.kind)}" data-action-id="${esc(row.action_id)}"><span class="pill state-${esc(row.state)}">${esc(names[row.state]||row.state)}</span>${['private_reply','proactive_private','task_result'].includes(a.kind)?`<span class="pill">私信 → ${esc(a.recipient_id)}</span>`:''}${esc(a.text||({like:"为感兴趣的内容点赞",coin:"为感兴趣的视频投币 1 枚"}[a.kind]||""))}${a.review_state?`<div class="meta">${esc(a.approved_manually?'人工已批准':({passed:'模型审核通过',rejected:'模型未通过，待人工审核',unavailable:'模型审核未完成，待人工审核'}[a.review_state]||a.review_state))} · ${esc(a.review_reason)}</div>`:''}${a.artwork?.source_url?`<div class="meta">画师：${esc(a.artwork.artist_name)} · 原作：${esc(a.artwork.source_url)}</div>`:''}<div class="meta">${esc({reply:'评论 / @ 回复',owner_comment:'主人作品评论',browse_comment:'主动视频评论',browse_reply:'主动评论区接话',recommendation:'向主人推荐',like:'内容点赞',coin:'视频投币（1 枚）',task_result:'视频委托结果',private_reply:'私信回复',proactive_private:'主动私信',autonomous_post:'自主动态',owner_repost:'主人转发',artwork_share:'插画分享'}[a.kind]||a.content?.kind||'会话')} · ${esc(time(row.created_at))} · ${esc(a.decision_reason)} · ${esc(({metadata:"标题与简介",text_visual:"文字与图片",visual:"图片分析",text:"文字",transcript:"字幕",transcript_visual:"字幕与视频预览帧",conversation:"会话"})[a.evidence_level]||a.evidence_level)}</div>${row.receipt?`<div class="meta">${esc(receiptDetail(row,a))}</div>`:""}${publicationDetail(row)}${a.not_before?`<div class="meta">计划时间 ${esc(time(a.not_before))}</div>`:""}${["like","coin"].includes(a.kind)&&a.content?`<div class="meta">${esc(a.content.kind==='video'?'视频':'动态 / 图文')} · 内容 ID ${esc(a.content.external_id)}</div>`:""}${row.state==='accepted'&&['reply','owner_comment','browse_comment','browse_reply','recommendation'].includes(a.kind)&&a.content?`<div class="actions"><button data-op="publication/check" data-id="${esc(row.action_id)}">核查评论状态（只读）</button></div>`:""}${a.manual_attempts?`<div class="meta">人工发送入队 ${esc(a.manual_attempts)} 次 · 仍遵守权限、额度和间隔</div>`:''}${['draft','failed'].includes(row.state)?`<div class="actions"><button data-op="actions/send" data-id="${esc(row.action_id)}" title="按当前权限重新检查并加入发送队列，需要工作循环运行">${row.state==='draft'?'审核并发送':'重发'}</button>${row.state==='draft'?`<button data-op="reject" data-id="${esc(row.action_id)}">取消</button>`:''}</div>`:''}${row.state==='unknown'?'<div class="meta">发送结果未知，请先核对平台结果，不能直接重发。</div>':''}</div>`}
