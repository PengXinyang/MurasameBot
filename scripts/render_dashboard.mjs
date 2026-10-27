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
  async function evaluate(expression){const result=await call('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(result.exceptionDetails)throw Error(result.exceptionDetails.text);return result.result.value}
  await call('Page.enable');await call('Runtime.enable');
  await call('Emulation.setEmulatedMedia',{features:[{name:'prefers-reduced-motion',value:'reduce'}]});
  await call('Emulation.setDeviceMetricsOverride',{width:1440,height:1260,deviceScaleFactor:1,mobile:false});
  await call('Page.navigate',{url:plan.url});
  await until(()=>evaluate("!!document.querySelector('#limit-daily_comments')&&document.querySelector('#model-state')?.textContent!=='读取中'&&[...document.images].every(image=>image.complete&&image.naturalWidth>0)"));
  for(const screen of plan.screens){
    await call('Emulation.setDeviceMetricsOverride',{width:screen.width,height:screen.height,deviceScaleFactor:1,mobile:screen.width<620});
    await evaluate(`chooseTab(${JSON.stringify(screen.tab)});chooseSettingsGroup(${JSON.stringify(screen.group||'browsing')});new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))`);
    const metrics=await evaluate("({width:innerWidth,scrollWidth:document.documentElement.scrollWidth,controls:document.querySelectorAll('#settings input,#settings select,#settings textarea').length,missingArt:[...document.images].filter(image=>!image.naturalWidth).length})");
    if(metrics.scrollWidth>metrics.width)throw Error('Horizontal page overflow: '+screen.tab+' '+screen.width+' '+JSON.stringify(metrics));
    const screenshot=await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});
    await writeFile(screen.output,Buffer.from(screenshot.data,'base64'));
    report.screens.push({tab:screen.tab,group:screen.group||'browsing',...metrics});
    console.log(screen.output);
  }
  // Round-trip only the isolated preview configuration, without a worker or model calls.
  await evaluate("chooseTab('settings');chooseSettingsGroup('messages');document.querySelector('#concurrency').value='7';document.querySelector('#concurrency').dispatchEvent(new Event('input',{bubbles:true}));saveSettings()");
  if(!await evaluate("config.processing.model_concurrency===7&&!document.querySelector('#save-status').classList.contains('dirty')"))throw Error('Settings save did not round-trip');
  await evaluate("document.querySelector('#concurrency').value='4';saveSettings()");
  report.settings_save_roundtrip=true;
  await evaluate("chooseTab('memories');document.querySelector('#memory-recent_messages').value='25';saveMemorySettings()");
  if(!await evaluate("config.memory.recent_messages===25"))throw Error('Memory settings did not round-trip');
  await evaluate("(async()=>{document.querySelector('#memory-recent_messages').value='20';await saveMemorySettings();document.querySelector('#memory-query').value='今天';await searchMemory()})()");
  if(!await evaluate("memoryResults.length===1&&document.querySelector('#memory-search-results').textContent.includes('今天有点累')"))throw Error('Memory search did not render scoped originals');
  report.memory_settings_save_roundtrip=true;
  report.memory_source_search=true;
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
