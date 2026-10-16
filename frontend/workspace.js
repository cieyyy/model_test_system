/* The original report renderer stays isolated; the workspace owns navigation and live data. */
let workspacePoll = null;
let workspaceGeneration = 0;
let workspaceRunId = null;
let workspaceFollowJob = null;
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

async function refreshWorkspaceHistory() {
  try {
    const runs = (await api('/suites/runs')).filter(r => r.suite_id === 'benchmark');
    const history = $('#workspaceHistory');
    if (!history) return runs;
    $('#workspaceHistoryCount').textContent = runs.length;
    history.innerHTML = runs.slice(0, 30).map(r => `<button class="workspace-history-item ${r.id === workspaceRunId ? 'active' : ''}" onclick="workspaceOpenRun(${r.id})" title="${esc(r.name)}"><strong>${esc(r.model || r.name)}${r.demo ? '<span class="workspace-demo">模拟</span>' : ''}</strong><small>${esc(r.started_at.replace('T', ' ').slice(5, 16))} · ${suiteStatus(r.status)}${r.stage ? ' · 第 ' + r.stage + ' 档' : ''}</small></button>`).join('') || '<div class="workspace-history-empty">暂无测试记录</div>';
    return runs;
  } catch (_) { return []; }
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
  $('#main').innerHTML = `<div class="workspace-toolbar"><div class="field"><label for="workspaceStage">测试记录 / 阶段</label><select id="workspaceStage" onchange="workspaceOpenRun(Number(this.value))"></select></div><div class="workspace-toolbar-actions"><button class="btn" id="workspaceCancel" hidden>取消测试</button><button class="btn" id="workspaceEvidence" hidden>运行证据</button><button class="btn primary" onclick="go('suites')">新建测试</button></div></div><div id="suiteError" role="alert" class="suite-error"></div><div id="workspaceReport"></div>`;
  const runs = await refreshWorkspaceHistory();
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
  await refreshWorkspaceHistory();
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
    else await refreshWorkspaceHistory();
  } catch (e) { suiteError(e); }
}

function workspaceNewTest() {
  go('suites');
}
