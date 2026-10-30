/* Service lifetime belongs to the dashboard; AI work stays in one coordinator. */
let receiverBusy = false;

function renderReceivers(data) {
  const node = document.querySelector('#receiver-services');
  if (!data.managed) {
    node.textContent = '当前预览未装配平台服务管理；请通过 serve 启动。';
    return;
  }
  const labels = {qq: 'QQ', bilibili: 'B站'};
  node.innerHTML = Object.entries(data.platforms).map(([name, service]) => {
    const state = service.state === 'error' ? '启动异常' : service.running ? '服务运行中' : '未运行';
    const description = service.mode === 'http' ? '签名事件接收 · 共用工作循环' : '通知与私信轮询 · 共用工作循环';
    return `<div class="panel settings-card"><div class="panel-title"><h3>${esc(labels[name] || name)}</h3><span class="pill">${esc(state)}</span></div><p class="notice">${esc(description)}</p>${service.error ? `<p class="notice" role="alert">${esc(service.error)}</p>` : ''}<div class="actions"><button type="button" data-receiver="${esc(name)}" data-operation="start" ${receiverBusy || service.running ? 'disabled' : ''}>启动${esc(labels[name] || name)}</button><button type="button" data-receiver="${esc(name)}" data-operation="stop" ${receiverBusy || (!service.selected && !service.enabled) ? 'disabled' : ''}>停止${esc(labels[name] || name)}</button></div></div>`;
  }).join('');
  node.querySelectorAll('[data-receiver]').forEach(button => {
    button.onclick = () => controlReceiver(button.dataset.receiver, button.dataset.operation);
  });
  document.querySelector('#receiver-worker').textContent = data.worker_running
    ? '共用工作循环已运行；各平台按上方选择接入。' : '工作循环未运行；启动任一平台后自动创建。';
}

async function controlReceiver(platform, operation) {
  if (receiverBusy) return;
  receiverBusy = true;
  document.querySelectorAll('[data-receiver]').forEach(button => { button.disabled = true; });
  try {
    const result = await api('receivers', {platform, operation});
    receiverBusy = false;
    renderReceivers(result);
    await loadConfig();
    await refresh();
    notice(operation === 'start' ? '已请求启动；可在平台服务中查看运行状态。' : '已停止该平台；未完成委托和记忆仍保留。');
  } catch (e) {
    notice(e.message, true);
  } finally {
    receiverBusy = false;
    try { renderReceivers(await api('receivers')); } catch (_) { /* Keep the original error visible. */ }
  }
}
