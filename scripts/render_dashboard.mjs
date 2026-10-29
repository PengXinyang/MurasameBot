// Optional local visual verification: Node 22+ and the Edge path supplied by preview_dashboard.py.
import {spawn} from 'node:child_process';
import {readFile,writeFile} from 'node:fs/promises';
import {join} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';

const plan=JSON.parse(await readFile(process.argv[2],'utf8'));
if(new URL(plan.url).hostname!=='127.0.0.1')throw Error('Preview must use a loopback server');
const browser=spawn(plan.browser,['--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check',
  '--disable-background-networking','--disable-component-update','--disable-sync','--remote-debugging-port=0',
  '--user-data-dir='+plan.profile,'about:blank'],{windowsHide:true,stdio:'ignore'});
let socket;
const report={screens:[],errors:[]};
async function until(predicate,timeout=15000){const end=Date.now()+timeout;while(Date.now()<end){const value=await predicate();if(value)return value;await delay(70)}throw Error('Preview did not become ready')}
try{
  const port=await until(async()=>{try{return (await readFile(join(plan.profile,'DevToolsActivePort'),'utf8')).split('\n')[0]}catch{return null}});
  const pages=await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  const page=pages.find(p=>p.type==='page');
  socket=new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject});
  let id=0;const pending=new Map();
  socket.onmessage=event=>{const message=JSON.parse(event.data);if(message.id){const callback=pending.get(message.id);if(callback){pending.delete(message.id);message.error?callback.reject(Error(message.error.message)):callback.resolve(message.result)}}else if(message.method==='Runtime.exceptionThrown'){report.errors.push(message.params.exceptionDetails.text)}};
  function call(method,params={}){return new Promise((resolve,reject)=>{const sequence=++id;const timer=setTimeout(()=>{pending.delete(sequence);reject(Error('Browser command timed out: '+method))},15000);pending.set(sequence,{resolve:value=>{clearTimeout(timer);resolve(value)},reject:error=>{clearTimeout(timer);reject(error)}});socket.send(JSON.stringify({id:sequence,method,params}))})}
  async function evaluate(expression){const result=await call('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(result.exceptionDetails)throw Error(result.exceptionDetails.exception?.description||result.exceptionDetails.text);return result.result.value}
  await call('Page.enable');await call('Runtime.enable');
  await call('Emulation.setEmulatedMedia',{features:[{name:'prefers-reduced-motion',value:'reduce'}]});
  await call('Emulation.setDeviceMetricsOverride',{width:1440,height:1260,deviceScaleFactor:1,mobile:false});
  await call('Page.navigate',{url:plan.url});
  await until(()=>evaluate("!!document.querySelector('#limit-daily_comments')&&document.querySelector('#model-state')?.textContent!=='读取中'&&[...document.images].filter(image=>image.hasAttribute('src')).every(image=>image.complete&&image.naturalWidth>0)"));
  for(const screen of plan.screens){
    await call('Emulation.setDeviceMetricsOverride',{width:screen.width,height:screen.height,deviceScaleFactor:1,mobile:screen.width<620});
    await evaluate(`chooseTab(${JSON.stringify(screen.tab)});chooseSettingsGroup(${JSON.stringify(screen.group||'browsing')});new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))`);
    if(screen.tab==='actions'){
      await evaluate(`chooseActionGroup(${JSON.stringify(screen.group)})`);
      const grouped=await evaluate("({selected:document.querySelector('[data-action-group][aria-selected=true]').dataset.actionGroup,ids:[...document.querySelectorAll('#actionlist [data-action-id]')].map(n=>n.dataset.actionId)})");
      const expected={private:['preview-private','preview-proactive'],comments:['preview-comment','preview-repost','preview-art','preview-autonomous'],reactions:['preview-like','preview-coin']}[screen.group];
      if(grouped.selected!==screen.group||JSON.stringify(grouped.ids.sort())!==JSON.stringify(expected.sort()))throw Error('Action category lost or mixed records: '+JSON.stringify(grouped));
      if(screen.group!=='reactions'&&!await evaluate("document.querySelector('#actionlist').textContent.includes('计划时间')"))throw Error('Scheduled initiative time was not shown');
    }
    if(screen.tab==='social')await until(()=>evaluate("!!document.querySelector('#social-records [data-social-op=resolve]')&&document.querySelectorAll('#social-fields input,#social-fields select').length===11"));
    if(screen.tab==='settings'&&screen.group==='messages'){
      await evaluate("document.querySelector('#visionenabled').closest('.panel').scrollIntoView({block:'center'});new Promise(resolve=>requestAnimationFrame(resolve))");
      const frameControls=await evaluate("['videoframesenabled','videoframecount','videoframeparts'].map(id=>{const e=document.getElementById(id),r=e.getBoundingClientRect();return {id,value:e.value,width:r.width,left:r.left,right:r.right}})");
      if(frameControls.some(control=>control.width<=0||control.left<0||control.right>screen.width))throw Error('Video frame controls are hidden or overflow');
      report.video_frame_controls=frameControls;
    }
    const metrics=await evaluate("({width:innerWidth,scrollWidth:document.documentElement.scrollWidth,controls:document.querySelectorAll('#settings input,#settings select,#settings textarea').length,missingArt:[...document.images].filter(image=>image.hasAttribute('src')&&!image.naturalWidth).length})");
    if(metrics.scrollWidth>metrics.width)throw Error('Horizontal page overflow: '+screen.tab+' '+screen.width+' '+JSON.stringify(metrics));
    const screenshot=await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});
    await writeFile(screen.output,Buffer.from(screenshot.data,'base64'));
    report.screens.push({tab:screen.tab,group:screen.group||'browsing',...metrics});
    console.log(screen.output);
  }
  await evaluate("chooseTab('actions');chooseActionGroup('private');refresh()");
  if(!await evaluate("activeActionGroup==='private'&&document.querySelector('#actionlist [data-op=\"actions/send\"]')?.dataset.id==='preview-private'&&!document.querySelector('#actionlist [data-op=\"publication/check\"]')"))throw Error('Private approval controls or selected category changed on refresh');
  const controls=await evaluate(`(()=>{const row=actionRows.find(r=>r.action_id==='preview-private');const check=state=>{const node=document.createElement('div');node.innerHTML=renderActionRow({...row,state});return {send:node.querySelector('[data-op="actions/send"]')?.textContent||'',text:node.textContent}};return {draft:check('draft'),failed:check('failed'),unknown:check('unknown'),accepted:check('accepted'),sending:check('sending')}})()`);
  if(controls.draft.send!=='审核并发送'||controls.failed.send!=='重发'||controls.unknown.send||controls.accepted.send||controls.sending.send)throw Error('Manual send controls allowed an unsafe state');
  report.manual_send_controls={draft:true,failed:true,unknown_blocked:true,accepted_blocked:true,sending_blocked:true};
  await evaluate("document.querySelector('#action-tab-private').focus();document.querySelector('#action-tab-private').dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}))");
  if(!await evaluate("activeActionGroup==='comments'&&document.activeElement.id==='action-tab-comments'&&document.querySelector('#actionlist [data-op=\"publication/check\"]')?.dataset.id==='preview-comment'"))throw Error('Action keyboard navigation or comment check control failed');
  await evaluate("chooseActionGroup('reactions');document.querySelector('#actionlist [data-op=reject][data-id=preview-coin]').click()");
  await until(()=>evaluate("actionRows.some(row=>row.action_id==='preview-coin'&&row.state==='cancelled')"));
  if(!await evaluate("activeActionGroup==='reactions'&&document.querySelector('#action-count-reactions').textContent==='2'&&!document.querySelector('#actionlist [data-op=reject][data-id=preview-coin]')"))throw Error('Reaction cancellation did not preserve grouping');
  report.action_groups={categories:3,refresh_preserves_selection:true,keyboard_navigation:true,private_approval_control:true,public_check_control:true,reaction_cancel_roundtrip:true};
  await evaluate("actionRows=[];renderActionGroups()");
  if(!await evaluate("document.querySelector('#actionlist').textContent.includes('还没有点赞或投币记录')&&document.querySelector('#action-count-private').textContent==='0'&&document.querySelector('#action-count-comments').textContent==='0'&&document.querySelector('#action-count-reactions').textContent==='0'"))throw Error('Action category empty state failed');
  await evaluate("refresh()");
  await call('Page.reload');
  await until(()=>evaluate("typeof actionRows!=='undefined'&&actionRows.length===8&&!!document.querySelector('#limit-daily_comments')"));
  if(!await evaluate("activeActionGroup==='reactions'&&document.querySelector('#action-tab-reactions').getAttribute('aria-selected')==='true'"))throw Error('Action selection did not survive page reload');
  report.action_groups.empty_state=true;
  report.action_groups.reload_preserves_selection=true;
  // Round-trip only the isolated preview configuration, without a worker or model calls.
  await evaluate("chooseTab('settings');chooseSettingsGroup('messages');document.querySelector('#concurrency').value='7';document.querySelector('#concurrency').dispatchEvent(new Event('input',{bubbles:true}));saveSettings()");
  if(!await evaluate("config.processing.model_concurrency===7&&!document.querySelector('#save-status').classList.contains('dirty')"))throw Error('Settings save did not round-trip');
  await evaluate("document.querySelector('#concurrency').value='4';saveSettings()");
  report.settings_save_roundtrip=true;
  await evaluate("document.querySelector('#videoframesenabled').value='false';document.querySelector('#videoframecount').value='4';document.querySelector('#videoframeparts').value='2';saveSettings()");
  if(!await evaluate("config.vision.video_frames_enabled===false&&config.vision.video_frame_count===4&&config.vision.video_frame_max_parts===2"))throw Error('Video frame settings did not round-trip');
  await evaluate("document.querySelector('#videoframesenabled').value='true';document.querySelector('#videoframecount').value='6';document.querySelector('#videoframeparts').value='3';saveSettings()");
  report.video_frame_settings_save_roundtrip=true;
  await evaluate("document.querySelector('#autonomy-private_enabled').value='true';document.querySelector('#autonomy-max_delay_minutes').value='45';saveSettings()");
  if(!await evaluate("config.autonomy.private_enabled===true&&config.autonomy.max_delay_minutes===45"))throw Error('Autonomy settings did not round-trip');
  await evaluate("document.querySelector('#autonomy-private_enabled').value='false';document.querySelector('#autonomy-max_delay_minutes').value='120';saveSettings()");
  report.autonomy_settings_save_roundtrip=true;
  await evaluate("document.querySelector('#reviewstrategy').value='independent';saveSettings()");
  if(!await evaluate("config.review.strategy==='independent'"))throw Error('Review strategy did not round-trip');
  await evaluate("document.querySelector('#reviewstrategy').value='inline';saveSettings()");
  if(!await evaluate("config.review.strategy==='inline'"))throw Error('Inline judgement default was not restored');
  report.review_strategy_save_roundtrip=true;
  await evaluate("chooseTab('memories');document.querySelector('#memory-recent_messages').value='25';saveMemorySettings()");
  if(!await evaluate("config.memory.recent_messages===25"))throw Error('Memory settings did not round-trip');
  await evaluate("(async()=>{document.querySelector('#memory-recent_messages').value='20';await saveMemorySettings();document.querySelector('#memory-query').value='今天';await searchMemory()})()");
  if(!await evaluate("memoryResults.length===1&&document.querySelector('#memory-search-results').textContent.includes('今天有点累')"))throw Error('Memory search did not render scoped originals');
  report.memory_settings_save_roundtrip=true;
  report.memory_source_search=true;
  await until(()=>evaluate("document.querySelector('[data-history=poll_seconds]')?.value==='300'"));
  if(!await evaluate("document.querySelector('#owner-history-state').textContent.includes('等待扫码')&&document.querySelectorAll('[data-history]').length===13&&document.querySelector('#owner-history-panel').closest('section').id==='owner-history'&&!document.querySelector('#memories #owner-history-panel')"))throw Error('Owner history controls did not load on their own page');
  if(!await evaluate("[...document.querySelectorAll('#memory-products [data-product-kind]')].every(n=>!['diary','reflection'].includes(n.dataset.productKind))&&document.querySelector('#memory-diaries [data-product-kind=diary]')&&document.querySelector('#memory-reflections [data-product-kind=reflection]')"))throw Error('Diary and reflection were mixed into other memory feeds');
  await evaluate("chooseTab('diary');document.querySelector('#memory-diaries [data-memory-op=sources]').click()");
  await until(()=>evaluate("document.querySelector('#memory-source-dialog').open"));
  await evaluate("document.querySelector('#memory-source-dialog').close();chooseTab('owner-history')");
  report.journal_pages={diary:true,reflections:true,owner_history_independent:true,sources_dialog:true};
  await evaluate("(async()=>{document.querySelector('[data-history=poll_seconds]').value='180';document.querySelector('[data-history=recommendation_topics]').value='galgame\\n千恋万花';await document.querySelector('#owner-history-save').onclick()})()");
  if(!await evaluate("(async()=>{const c=await api('config');return c.owner_history.poll_seconds===180&&c.owner_history.recommendation_topics.length===2&&!c.owner_history.enabled&&!c.owner_history.recommendations_enabled})()"))throw Error('Owner history settings round-trip failed');
  await evaluate("(async()=>{document.querySelector('[data-history=poll_seconds]').value='300';await document.querySelector('#owner-history-save').onclick()})()");
  report.owner_history={controls:13,settings_save_roundtrip:true,collection_default_off:true,recommendations_default_off:true,diary_setting:true};
  await until(()=>evaluate("document.querySelector('#private-poll-seconds')?.value==='30'"));
  if(!await evaluate("document.querySelector('[data-request=quiet_before_seconds]').value==='60'&&document.querySelectorAll('[data-request]').length===9"))throw Error('Request controls did not load');
  await evaluate("(async()=>{document.querySelector('#private-poll-seconds').value='45';await document.querySelector('#request-save').onclick()})()");
  if(!await evaluate("(async()=>{const c=await api('config');return c.bilibili.private_poll_seconds===45&&c.requests.quiet_before_seconds===60})()"))throw Error('Request controls did not save');
  await evaluate("(async()=>{document.querySelector('#private-poll-seconds').value='30';await document.querySelector('#request-save').onclick()})()");
  report.request_controls={fields:9,private_poll_save_roundtrip:true,quiet_before_seconds:60};
  await evaluate("chooseTab('social');document.querySelector('#social-max_followups').value='1';saveSocialSettings()");
  if(!await evaluate("(async()=>{const c=await api('config');return c.social.max_followups===1&&c.social.private_feelings_public===true})()"))throw Error('Social state settings did not round-trip');
  await evaluate("document.querySelector('#social-max_followups').value='2';saveSocialSettings()");
  await evaluate("document.querySelector('#social-records [data-social-op=resolve]').click()");
  await until(()=>evaluate("document.querySelector('#social-records').textContent.includes('已结束')&&!document.querySelector('#social-records [data-social-op=resolve]')"));
  await evaluate("document.querySelector('#social-records [data-social-op=forget]').click()");
  await until(()=>evaluate("document.querySelector('#social-records').textContent.includes('已遗忘')&&!document.querySelector('#social-records [data-social-op=forget]')"));
  report.social_states={settings_save_roundtrip:true,source_visible:true,resolve:true,forget:true};
  if(report.errors.length)throw Error('Browser reported JavaScript exceptions');
  await writeFile(plan.report,JSON.stringify(report,null,2));
}finally{
  if(socket?.readyState===WebSocket.OPEN){
    socket.send(JSON.stringify({id:999999,method:'Browser.close'}));
    await Promise.race([new Promise(resolve=>browser.once('exit',resolve)),delay(1200)]);
    socket.close();
  }
  if(browser.exitCode===null)browser.kill();
}
