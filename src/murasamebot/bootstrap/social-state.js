// Local management only; this module never dispatches a platform message.
const socialFields={
  enabled:['记录社交线索与短期心情','bool'],
  public_posts_enabled:['允许角色心情用于公开动态','bool'],
  private_feelings_public:['允许主人的私人线索转为公开感受','bool'],
  ttl_hours:['心情有效期（小时）','number',72],
  retention_days:['来源保留期限（天）','number',30],
  context_limit:['每次最多参考状态数','number',10],
  max_active_states:['最多保留活跃状态数','number',100],
  max_followups:['每个状态最多后续提起次数','number',3],
  followup_min_minutes:['最早跟进（分钟）','number',720],
  followup_max_minutes:['最晚跟进（分钟）','number',4320],
  followup_gap_minutes:['再次提起的间隔（分钟）','number',1440]
};
const socialFeelings={curious:'好奇',happy:'开心',miss_owner:'想念主人',jealous:'轻微吃醋',concerned:'关心',relieved:'放心了'};
const socialAttention={notice:'在意，但不主动追问',ask:'想自然询问',later:'稍后再提起'};
const socialStatus={active:'有效',resolved:'已结束',expired:'已到期',forgotten:'已遗忘'};
function loadSocialConfig(){
  const node=document.getElementById('social-fields');if(!node||!config.social)return;
  node.innerHTML=Object.entries(socialFields).map(([key,[label,type,max]])=>`<div class="field"><label for="social-${key}">${esc(label)}</label>${type==='bool'?`<select id="social-${key}"><option value="true">开启</option><option value="false">关闭</option></select>`:`<input id="social-${key}" type="number" min="1" max="${max}">`}</div>`).join('');
  for(const [key,[,type]] of Object.entries(socialFields))document.getElementById('social-'+key).value=type==='bool'?String(config.social[key]):config.social[key];
}
async function saveSocialSettings(){
  try{const patch={};for(const [key,[,type]] of Object.entries(socialFields)){const value=document.getElementById('social-'+key).value;patch[key]=type==='bool'?value==='true':Number(value)}
    await api('config',{social:patch});config.social=patch;notice('心情与社交设置已保存');await refreshSocial();
  }catch(error){notice(error.message,true)}
}
async function refreshSocial(){
  const node=document.getElementById('social-records');if(!node)return;
  try{
    const data=await api('social');
    node.innerHTML=data.records.length?data.records.map(row=>{
      const s=row.source,state=row.state;
      return `<div class="item"><div><span class="pill">${esc(socialStatus[row.status]||row.status)}</span> <strong>${esc(socialFeelings[state.feeling]||state.feeling)}</strong> · ${esc(socialAttention[state.attention]||state.attention)}</div><p>${esc(state.note||'这段心情已经结束或遗忘。')}</p><div class="meta">${s.visibility==='public'?'公开来源':'私人来源'} · ${esc(({reported:'他人转述，未经核实',owner_statement:'主人自己的陈述',observation:'授权观察，不是对话'})[s.certainty]||s.certainty)} · ${row.public_allowed?'可按设置用于公开表达':'仅限允许的私人范围'}</div><div class="meta">记录于 ${esc(time(row.created_at))} · 有效至 ${esc(time(row.expires_at))} · 已提起 ${row.followups} 次</div>${row.status==='active'&&state.attention!=='notice'?`<div class="meta">最早后续提起 ${esc(time(row.next_at))}；仍需满足自主发布开关、时段、冷却和额度</div>`:''}<details><summary>查看来源</summary><p>${esc(s.summary||'来源已遗忘')}</p><blockquote>${esc(s.quote||'')}</blockquote><div class="meta">${esc(s.kind)} · ${esc(s.platform)} · 来源事件 ${esc(s.event_id||'授权观看记录')}</div>${s.url?`<div class="meta">${esc(s.url)}</div>`:''}</details>${row.status==='active'?`<button type="button" data-social-op="resolve" data-id="${esc(row.state_id)}">结束这段心情</button>`:''}${row.status!=='forgotten'?` <button type="button" data-social-op="forget" data-id="${esc(row.state_id)}">遗忘这条线索</button>`:''}</div>`;
    }).join(''):'<p class="notice">还没有值得记录的社交线索。正常聊天不会逐条变成心情记录。</p>';
    node.onclick=async event=>{const button=event.target.closest('[data-social-op]');if(!button)return;button.disabled=true;try{await api('social/manage',{id:button.dataset.id,operation:button.dataset.socialOp});await refreshSocial()}catch(error){button.disabled=false;notice(error.message,true)}};
  }catch(error){node.textContent='社交状态暂未读到：'+error.message}
}
