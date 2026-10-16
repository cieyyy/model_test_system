/* The original report renderer stays isolated; the workspace owns navigation and live data. */
let workspacePoll = null;
let workspaceGeneration = 0;
let workspaceRunId = null;
let workspaceFollowJob = null;
let workspaceHistoryRuns = [];
const benchmarkFrames = new WeakMap();

window.addEventListener('message', event => {
  const frame = [...document.querySelectorAll('iframe[data-benchmark-frame]')].find(f => f.contentWindow === event.source);
  if (!frame) return;
  const data = event.data;
  if (data?.type === 'benchmark:resize' && Number.isFinite(data.height)) {
    const height = Math.max(500, Math.min(30000, Math.ceil(data.height)));
    if (Math.abs(parseFloat(frame.style.height || '0') - height) > 2) frame.style.height = height + 'px';
  }
  if (data?.type === 'benchmark:ready') {
    studioSendTheme(frame);
    frame.dataset.ready = 'true';
    const report = benchmarkFrames.get(frame);
    if (report) frame.contentWindow.postMessage({type:'benchmark:update', report}, '*');
  }
});

async function workspaceBenchmark(container, runId, refresh = true) {
  if (!container) return;
  let frame = container.querySelector('iframe[data-benchmark-frame]');
  if (!frame || frame.dataset.runId !== String(runId)) {
    frame = document.createElement('iframe');
    frame.className = 'benchmark-view suite-report';
    frame.title = '性能指标、延迟曲线与逐请求明细';
    frame.setAttribute('sandbox', 'allow-scripts allow-downloads allow-modals');
    frame.dataset.benchmarkFrame = 'true';
    frame.dataset.runId = runId;
    frame.src = '/api/suites/runs/' + runId + '/view';
    container.replaceChildren(frame);
  }
  if (!refresh) return;
  const report = await api('/suites/runs/' + runId + '/benchmark', {cache:'no-store'});
  if (!frame.isConnected || frame.dataset.runId !== String(runId)) return;
  benchmarkFrames.set(frame, report);
  if (frame.dataset.ready) frame.contentWindow.postMessage({type:'benchmark:update', report}, '*');
  return report;
}

async function workspaceBenchmarkRuns() {
  try {
    return (await api('/suites/runs', {cache:'no-store'})).filter(r => r.suite_id === 'benchmark');
  } catch (_) { return []; }
}

async function renderWorkspaceHistory() {
  clearTimeout(workspacePoll);
  clearTimeout(suitePoll);
  suiteGeneration++;
  const generation = ++workspaceGeneration;
  workspaceHistoryRuns = [];
  $('#main').innerHTML = `${studioHeading('history')}
    <div class="workspace-history-toolbar">
      <div class="field"><label for="historySearch">搜索记录</label><input id="historySearch" type="search" placeholder="项目、模型或运行编号" oninput="workspaceHistoryRows()"></div>
      <div class="field"><label for="historyStatus">执行状态</label><select id="historyStatus" onchange="workspaceHistoryRows()"><option value="all">全部状态</option><option value="active">排队 / 运行中</option><option value="completed">执行完成</option><option value="cancelled">已取消</option><option value="error">执行异常 / 中断</option></select></div>
      <div class="workspace-history-actions"><button class="btn" onclick="workspaceHistoryRefresh(workspaceGeneration)">刷新记录</button><button class="btn primary" onclick="go('suites')">新建测试</button></div>
    </div>
    <div id="suiteError" role="alert" class="suite-error"></div>
    <div id="historySummary" class="workspace-history-summary" role="status">正在读取测试记录…</div>
    <div id="historyRecords" class="workspace-history-table"></div>`;
  await workspaceHistoryRefresh(generation);
}

async function workspaceHistoryRefresh(generation) {
  if (state.tab !== 'history' || generation !== workspaceGeneration) return;
  clearTimeout(workspacePoll);
  try {
    const runs = await api('/suites/runs', {cache:'no-store'});
    if (state.tab !== 'history' || generation !== workspaceGeneration) return;
    workspaceHistoryRuns = runs;
    $('#suiteError').textContent = '';
    workspaceHistoryRows();
    if (runs.some(r => ['queued','running'].includes(r.status))) {
      workspacePoll = setTimeout(() => workspaceHistoryRefresh(generation), 1500);
    }
  } catch (e) {
    if (state.tab !== 'history' || generation !== workspaceGeneration) return;
    $('#historySummary').textContent = '记录读取失败，请刷新重试';
    suiteError(e);
  }
}

function workspaceHistoryRows() {
  if (state.tab !== 'history') return;
  const query = $('#historySearch').value.trim().toLowerCase();
  const status = $('#historyStatus').value;
  const rows = workspaceHistoryRuns.filter(r => {
    const matchesStatus = status === 'all' || (status === 'active' ? ['queued','running'].includes(r.status) : status === 'error' ? ['error','interrupted'].includes(r.status) : r.status === status);
    return matchesStatus && (!query || `#${r.id} ${r.name} ${r.model}`.toLowerCase().includes(query));
  });
  const active = workspaceHistoryRuns.filter(r => ['queued','running'].includes(r.status)).length;
  $('#historySummary').textContent = `最近 ${workspaceHistoryRuns.length} 条记录 · 当前显示 ${rows.length} 条 · 排队 / 运行中 ${active} 条`;
  $('#historyRecords').innerHTML = rows.length ? `<table><thead><tr><th>运行 / 时间</th><th>测试项目</th><th>模型</th><th>档位 / 并发</th><th>状态</th><th>成功 / 已完成</th><th></th></tr></thead><tbody>${rows.map(r => {
    const tone = ['error','interrupted'].includes(r.status) ? 'error' : ['queued','running'].includes(r.status) ? 'active' : r.status === 'cancelled' ? 'muted' : 'complete';
    return `<tr><td><strong>#${r.id}</strong><small>${esc((r.started_at || '').replace('T',' ').slice(0,19))}</small></td><td>${esc(r.name)}${r.demo ? ' <span class="tag gray">模拟</span>' : ''}</td><td>${esc(r.model || '—')}</td><td>${r.stage ? `${esc(r.stage)} / ${esc(r.stage_count || 1)}` : '—'}<small>并发 ${esc(r.config?.concurrency ?? '—')}</small></td><td><span class="workspace-history-status ${tone}">${esc(suiteStatus(r.status))}</span></td><td>${suiteNumber(r.summary?.success)} / ${suiteNumber(r.summary?.attempted)}</td><td><button class="btn small" onclick="${r.suite_id === 'benchmark' ? 'workspaceOpenRun' : 'suiteOpenReport'}(${r.id})">查看结果</button></td></tr>`;
  }).join('')}</tbody></table>` : `<div class="empty">${workspaceHistoryRuns.length ? '没有符合筛选条件的测试记录' : '暂无测试记录'}</div>`;
}

function workspaceOpenRun(id) {
  workspaceFollowJob = null;
  workspaceRunId = id;
  go('dashboard');
}

async function renderDashboard() {
  clearTimeout(workspacePoll);
  clearTimeout(suitePoll);
  suiteGeneration++;
  const generation = ++workspaceGeneration;
  $('#main').innerHTML = `${studioHeading('dashboard')}<div class="workspace-toolbar"><div class="field"><label for="workspaceStage">测试记录 / 阶段</label><select id="workspaceStage" onchange="workspaceOpenRun(Number(this.value))"></select></div><div class="workspace-toolbar-actions"><button class="btn" id="workspaceCancel" hidden>取消测试</button><button class="btn" id="workspaceEvidence" hidden>运行证据</button><button class="btn primary" onclick="go('suites')">新建测试</button></div></div><div id="suiteError" role="alert" class="suite-error"></div><div id="workspaceReport"></div>`;
  const runs = await workspaceBenchmarkRuns();
  if (state.tab !== 'dashboard' || generation !== workspaceGeneration) return;
  if (!runs.length) {
    $('#workspaceReport').innerHTML = '<div class="empty">尚无压测记录</div>';
    return;
  }
  if (!workspaceRunId && localStorage.getItem('suiteJob')) {
    try {
      const job = await api('/suites/jobs/' + localStorage.getItem('suiteJob'));
      if (job.status === 'running') {
        workspaceFollowJob = job.id;
        workspaceRunId = job.runs.find(r => r.status === 'running')?.id || job.runs[0]?.id;
      }
    } catch (_) { /* The saved task may have been archived. */ }
  }
  if (state.tab !== 'dashboard' || generation !== workspaceGeneration) return;
  const run = runs.find(r => r.id === workspaceRunId) || runs[0];
  workspaceRunId = run.id;
  $('#workspaceStage').innerHTML = runs.map(r => `<option value="${r.id}" ${r.id === run.id ? 'selected' : ''}>#${r.id} · ${esc(r.model)} · 并发 ${esc(r.config?.concurrency ?? '—')}${r.demo ? ' · 模拟' : ''}</option>`).join('');
  $('#workspaceEvidence').hidden = false;
  $('#workspaceEvidence').onclick = () => suiteOpenReport(run.id);
  await workspaceLive(run, generation);
}

async function workspaceLive(run, generation) {
  if (state.tab !== 'dashboard' || generation !== workspaceGeneration) return;
  try {
    const report = await workspaceBenchmark($('#workspaceReport'), run.id);
    if (state.tab !== 'dashboard' || generation !== workspaceGeneration) return;
    let running = ['running', 'queued'].includes(report.status);
    if (workspaceFollowJob && report.job_id === workspaceFollowJob) {
      const job = await api('/suites/jobs/' + workspaceFollowJob, {cache:'no-store'});
      if (state.tab !== 'dashboard' || generation !== workspaceGeneration) return;
      running = job.status === 'running';
      const active = job.runs.find(r => r.status === 'running') || [...job.runs].reverse().find(r => r.status !== 'queued');
      if (!running) {
        workspaceFollowJob = null;
        localStorage.removeItem('suiteJob');
        if (!active || active.id === run.id) await workspaceBenchmark($('#workspaceReport'), run.id);
      }
      if (active && active.id !== run.id) {
        workspaceRunId = active.id;
        await renderDashboard();
        return;
      }
    }
    const cancel = $('#workspaceCancel');
    cancel.hidden = !running || !report.job_id;
    cancel.onclick = () => suiteCancel(report.job_id);
    if (running) workspacePoll = setTimeout(() => workspaceLive(run, generation), 750);
  } catch (e) { suiteError(e); }
}

function workspaceNewTest() {
  go('suites');
}
