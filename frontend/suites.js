/* Separate suite adapters preserve the existing management and evidence screens. */
let suiteCatalog = null;
let suitePlan = null;
let suitePoll = null;
let suiteGeneration = 0;
let suiteDrafts = (()=>{try{return JSON.parse(localStorage.getItem('suiteDrafts')||'{}');}catch{return {};}})();
let suiteEditing = null;
let suiteSelected = new Set();
let suiteAxes = (()=>{try{return JSON.parse(localStorage.getItem('suiteAxes')||'{}');}catch{return {};}})();
const suiteStatus = s => ({queued:'排队',running:'运行中',completed:'执行完成',cancelled:'已取消',error:'执行异常',interrupted:'重启中断',imported:'历史导入',archived:'已归档'}[s] || s);
const suiteNumber = v => v == null ? '—' : typeof v === 'number' ? v.toLocaleString('zh-CN',{maximumFractionDigits:2}) : esc(v);
const suitePost = (path,data={}) => api('/suites'+path,{method:'POST',body:JSON.stringify(data)});
const suiteError = e => { const el=$('#suiteError'); if(el) el.textContent=typeof e==='string'?e:e.message; else toast(e.message||e); };
function suiteField(id,label,value,type='text',extra='') {
  return `<div class="field"><label for="${id}">${esc(label)}</label><input id="${id}" type="${type}" value="${esc(value)}" ${extra}></div>`;
}
async function renderSuites() {
  clearTimeout(suitePoll);
  suiteGeneration++;
  suitePlan=null;
  $('#main').innerHTML=studioHeading('suites')+'<div class="suite-status">正在读取测试项目…</div>';
  try {
    suiteCatalog=await api('/suites');
    suiteEditing=null;
    suiteSelected=new Set(suiteCatalog.suites.map(s=>s.id));
    if(state.tab!=='suites') return;
    $('#main').innerHTML=`${studioHeading('suites')}<div class="suite-toolbar"><h2>测试配置</h2><button class="btn" onclick="suiteAll()">一键测试全部项目</button></div>
      <form id="suiteForm" onsubmit="event.preventDefault();suitePreview()">
        <div class="suite-form">
          <div class="field"><label for="sqScope">执行范围</label><select id="sqScope" onchange="suiteScopeChanged()"><option value="single">单个项目</option><option value="all">所有项目</option></select></div>
          <div class="field"><label for="sqProvider">渠道</label><select id="sqProvider" onchange="suiteTargets()">${state.providers.filter(p=>p.enabled).map(p=>`<option value="${p.id}">${esc(p.name)}</option>`).join('')}</select></div>
          <div class="field"><label for="sqModel">模型</label><select id="sqModel"></select></div>
          <div class="field"><label for="sqCredential">密钥</label><select id="sqCredential"></select></div>
        </div>
        <div id="suiteSelection" hidden></div>
        <section id="suiteProjectConfig">
        <div class="suite-toolbar"><div class="field suite-config-picker"><label id="suitePickerLabel" for="sqSuite">选择测试项目</label><select id="sqSuite" onchange="suiteParameters()">${suiteCatalog.suites.map(s=>`<option value="${esc(s.id)}" ${s.id==='basic'?'selected':''} ${s.available?'':'disabled'}>${esc(s.name)}${s.available?'':' · 源文件缺失'}</option>`).join('')}</select></div><button type="button" class="btn small" onclick="suiteReset()">恢复本机默认值</button><button type="button" id="suiteCloseConfig" class="btn small" hidden onclick="suiteRemember();document.getElementById('suiteProjectConfig').hidden=true">收起</button></div>
        <div class="checkline"><input id="sqDemo" type="checkbox" onchange="suiteDemoChanged()"><label for="sqDemo">本地模拟（不请求模型）</label></div>
        <div id="suiteParameters"></div>
        </section>
        <div class="suite-form">${suiteField('sqJobTimeout','整个计划超时 / 秒',86400,'number','min="1" max="86400"')}${suiteField('sqCooldown','限流冷却 / 秒',70,'number','min="0" max="600"')}<div class="field"><label for="sqFailure">项目异常时</label><select id="sqFailure"><option value="continue">继续其余项目</option><option value="stop">停止后续项目</option></select></div></div>
        <div class="suite-toolbar"><button class="btn primary" type="submit">预览计划</button></div>
      </form>
      <div id="suiteError" role="alert" class="suite-error"></div><section id="suitePreview" class="suite-section"></section>
      <section id="suiteJob" class="suite-section" aria-live="polite"></section>
      <details class="suite-section"><summary>双目录来源与覆盖</summary><div style="margin-top:14px">${suiteCatalog.sources.map(s=>`<div class="suite-source"><b>${esc(s.name)}</b> · ${esc(s.status)}${s.available?'':' · 路径缺失'}<code>${esc(s.path)}</code></div>`).join('')}</div></details>`;
    $('#suiteForm').addEventListener('input',()=>{suiteInvalidate();suiteRemember();});
    $('#suiteForm').addEventListener('change',suiteInvalidate);
    suiteParameters();
    await suiteTargets();
    const last=localStorage.getItem('suiteJob');
    if(last) suiteWatch(last,suiteGeneration);
  } catch(e) { suiteError(e); }
}
function suiteRemember() {
  if(!suiteEditing) return;
  const s=suiteCatalog.suites.find(s=>s.id===suiteEditing), parameters={};
  for(const [k,f] of Object.entries(s.fields)) {const el=$('#sp_'+k);if(!el)return;parameters[k]=f.type==='string'?el.value:Number(el.value);}
  suiteDrafts[s.id]=parameters;
  localStorage.setItem('suiteDrafts',JSON.stringify(suiteDrafts));
  if(s.id==='benchmark'&&$('#sqConcurrency')) {
    for(const id of ['sqConcurrencyMode','sqConcurrency','sqInput','sqOutput','sqStart','sqEnd','sqStep','sqStrategy','sqRepeat']) suiteAxes[id]=$('#'+id).value;
    localStorage.setItem('suiteAxes',JSON.stringify(suiteAxes));
  }
}
function suiteScopeChanged() {
  const scope=$('#sqScope').value;
  suiteRemember();
  suiteSelected=new Set(suiteCatalog.suites.map(s=>s.id));
  $('#suiteProjectConfig').hidden=scope!=='single';
  $('#suitePickerLabel').textContent=scope==='single'?'选择测试项目':'配置测试项目';
  $('#suiteCloseConfig').hidden=scope==='single';
  $('#suiteSelection').hidden=scope==='single';
  $('#suiteSelection').innerHTML=`<div class="suite-selection-heading"><strong>所有测试项目</strong><span>共 ${suiteSelected.size} 项</span></div><div class="suite-selection-grid">${suiteCatalog.suites.map(s=>`<div class="suite-select-item"><span>${esc(s.name)}<small>${s.group==='common'?'本机通用':'附加专项'}${s.available?'':' · 脚本缺失'}</small></span><button type="button" class="suite-config-icon" title="配置 ${esc(s.name)}" aria-label="配置 ${esc(s.name)}" onclick="suiteConfigure('${s.id}')">⚙</button></div>`).join('')}</div>`;
  $('#sqDemo').disabled=scope!=='single'||$('#sqSuite').value!=='benchmark';
  if($('#sqDemo').disabled) {$('#sqDemo').checked=false;suiteDemoChanged();}
  suiteInvalidate();
}
function suiteConfigure(id) {$('#sqSuite').value=id;suiteParameters();$('#suiteProjectConfig').hidden=false;$('#suiteProjectConfig').scrollIntoView({block:'center',behavior:'smooth'});}
function suiteAll() {$('#sqScope').value='all';suiteScopeChanged();suitePreview();}
function suiteReset() {delete suiteDrafts[$('#sqSuite').value];if($('#sqSuite').value==='benchmark'){suiteAxes={};localStorage.removeItem('suiteAxes');}suiteEditing=null;suiteParameters();suiteRemember();}
function suiteInvalidate() { suitePlan=null; if($('#suitePreview')) $('#suitePreview').innerHTML=''; if($('#suiteError')) $('#suiteError').textContent=''; }
async function suiteTargets() {
  const pid=Number($('#sqProvider').value);
  $('#sqModel').innerHTML=state.models.filter(m=>m.provider_id===pid).map(m=>`<option value="${m.id}">${esc(m.model_name)}</option>`).join('');
  $('#sqCredential').innerHTML='<option value="">读取中…</option>';
  try {
    const creds=pid?await api(`/providers/${pid}/credentials`):[];
    if(!$('#sqProvider')||Number($('#sqProvider').value)!==pid) return;
    $('#sqCredential').innerHTML=creds.length?creds.map(c=>`<option value="${c.id}">${esc(c.label)} · ${esc(c.key_hint)}</option>`).join(''):'<option value="">尚未登记密钥</option>';
  } catch(e) { suiteError(e); }
}
function suiteDemoChanged() {
  for(const id of ['sqProvider','sqModel','sqCredential']) $('#'+id).disabled=$('#sqDemo').checked;
  suiteInvalidate();
}
function suiteParameters() {
  suiteRemember();
  suiteInvalidate();
  const s=suiteCatalog.suites.find(s=>s.id===$('#sqSuite').value);
  suiteEditing=s.id;
  $('#sqDemo').disabled=s.id!=='benchmark'||$('#sqScope').value!=='single';
  if($('#sqDemo').disabled) { $('#sqDemo').checked=false; suiteDemoChanged(); }
  const defaults=suiteDrafts[s.id]||{};
  $('#suiteParameters').innerHTML=`<p class="suite-note">${esc(s.profile_source)} · ${esc(s.note||'保留源脚本的用例断言与原始报告。')}</p><div class="suite-form">${Object.entries(s.fields).map(([k,f])=>f.choices?
    `<div class="field"><label for="sp_${k}">${esc(f.label)}</label><select id="sp_${k}" data-param="${k}">${f.choices.map(v=>`<option value="${esc(v)}" ${v===(defaults[k]??f.default)?'selected':''}>${esc(v)}</option>`).join('')}</select></div>`:
    suiteField('sp_'+k,f.label,defaults[k]??f.default,f.type==='string'?'text':'number',`data-param="${k}" ${f.type!=='string'?`min="${f.min}" max="${f.max}" step="${f.type==='integer'?1:'any'}"`:''}`)).join('')}</div>`;
  if(s.id==='benchmark') $('#suiteParameters').insertAdjacentHTML('beforeend',`<div class="suite-section"><h2>压测档位</h2><div class="suite-form">
    <div class="field"><label for="sqConcurrencyMode">并发设置</label><select id="sqConcurrencyMode" onchange="suiteRangeMode()"><option value="levels">指定档位</option><option value="range">起止范围</option></select></div>
    ${suiteField('sqConcurrency','并发档位', '1,5,10,20,50')}
    ${suiteField('sqInput','输入 Token 档位','1000')}${suiteField('sqOutput','最大输出 Token 档位','256')}
    ${suiteField('sqStart','并发起始值',10,'number','min="1" max="500" disabled')}${suiteField('sqEnd','并发结束值',50,'number','min="1" max="500" disabled')}${suiteField('sqStep','并发递增步长',10,'number','min="1" max="500" disabled')}
    <div class="field"><label for="sqStrategy">组合方式</label><select id="sqStrategy"><option value="one_factor">逐变量（其余取首档）</option><option value="cartesian">所有输入 × 输出 × 并发组合</option></select></div>
    ${suiteField('sqRepeat','每组重复次数',1,'number','min="1" max="10"')}</div></div>`);
  if(s.id==='benchmark') {for(const [id,value] of Object.entries(suiteAxes))if($('#'+id))$('#'+id).value=value;suiteRangeMode();}
}
function suiteRangeMode() {
  const range=$('#sqConcurrencyMode').value==='range';
  $('#sqConcurrency').disabled=range;
  for(const id of ['sqStart','sqEnd','sqStep']) $('#'+id).disabled=!range;
}
function suiteLevels(value,label) {
  const parts=value.split(/[,，\s]+/).filter(Boolean);
  if(!parts.length||parts.some(v=>!/^\d+$/.test(v))) throw Error(label+'必须填写正整数档位');
  return parts.map(Number);
}
function suiteRequest() {
  suiteRemember();
  const s=suiteCatalog.suites.find(s=>s.id===$('#sqSuite').value);
  const parameters={};
  for(const [k,f] of Object.entries(s.fields)) parameters[k]=f.type==='string'?$('#sp_'+k).value:Number($('#sp_'+k).value);
  const req={suite_id:s.id,provider_id:Number($('#sqProvider').value)||null,model_id:Number($('#sqModel').value)||null,
    credential_id:Number($('#sqCredential').value)||null,demo:$('#sqDemo').checked,parameters,job_timeout_seconds:Number($('#sqJobTimeout').value)};
  if(s.id==='benchmark'||($('#sqScope').value!=='single'&&suiteSelected.has('benchmark'))) {
    let levels=suiteLevels(suiteAxes.sqConcurrency||'1,5,10,20,50','并发');
    if(suiteAxes.sqConcurrencyMode==='range') {
      const a=Number(suiteAxes.sqStart),b=Number(suiteAxes.sqEnd),step=Number(suiteAxes.sqStep);
      if(![a,b,step].every(Number.isInteger)||a<1||b>500||b<a||step<1) throw Error('并发起止范围或步长无效');
      levels=[]; for(let n=a;n<=b;n+=step) levels.push(n);
      if(levels[levels.length-1]!==b) levels.push(b);
    }
    Object.assign(req,{concurrency_levels:levels,input_token_levels:suiteLevels(suiteAxes.sqInput||'1000','输入 Token'),output_token_levels:suiteLevels(suiteAxes.sqOutput||'256','输出 Token'),strategy:suiteAxes.sqStrategy||'one_factor',repeats:Number(suiteAxes.sqRepeat||1)});
  }
  if($('#sqScope').value!=='single') {
    if(!suiteSelected.size) throw Error('请至少选择一个项目');
    req.suite_ids=[...suiteSelected];
    req.suite_parameters=Object.fromEntries([...suiteSelected].map(id=>[id,suiteDrafts[id]||{}]));
    req.continue_on_error=$('#sqFailure').value==='continue';
    req.cooldown_seconds=Number($('#sqCooldown').value);
  }
  return req;
}
async function suitePreview() {
  try {
    suiteInvalidate();
    const request=suiteRequest();
    const p=await suitePost('/preview',request);
    suitePlan={request,plan:p};
    $('#suitePreview').innerHTML=`<h2>计划预览 ${p.demo?'<span class="tag warn">本地模拟</span>':''}</h2>
      <div class="suite-metrics"><div><b>${p.stage_count}</b><span>阶段数</span></div><div><b>${suiteNumber(p.request_budget??p.known_request_budget)}</b><span>${p.unknown_budget_projects?`已知请求计划；另 ${p.unknown_budget_projects} 项由用例决定`:esc(p.budget_kind)}</span></div><div><b>${suiteNumber(p.token_target_upper_bound)}</b><span>输入目标 + 输出上限（非账单）</span></div><div><b>${esc(p.model)}</b><span>${esc(p.provider_name)}</span></div></div>
      <div class="suite-note">${esc(p.endpoint)}</div>
      ${p.kind==='batch'?`<p class="suite-note">${p.project_count} 个项目 · 异常后${p.continue_on_error?'继续其余项目':'停止'} · 限流冷却 ${p.cooldown_seconds}s</p><div class="suite-preview"><table><thead><tr><th>顺序</th><th>项目</th><th>阶段数</th><th>请求计划</th><th>参数</th></tr></thead><tbody>${p.projects.map((v,i)=>`<tr><td>${i+1}</td><td>${esc(v.name)}</td><td>${v.stage_count}</td><td>${suiteNumber(v.request_budget)}</td><td><details><summary>查看</summary><pre class="mono">${esc(JSON.stringify(v.parameters,null,2))}</pre></details></td></tr>`).join('')}</tbody></table></div>`:p.kind==='benchmark'?`<div class="suite-preview"><table><thead><tr><th>阶段</th><th>并发</th><th>输入 Token</th><th>最大输出</th><th>请求上限</th><th>重复</th></tr></thead><tbody>${p.stages.map((s,i)=>`<tr><td>${i+1}</td><td>${s.config.concurrency}</td><td>${s.config.prompt_tokens}</td><td>${s.config.max_tokens}</td><td>${s.config.total_requests}</td><td>${s.repeat}</td></tr>`).join('')}</tbody></table></div>`:`<p class="suite-note">${p.stage_count} 个阶段 · ${esc(p.profile_source)}</p><details><summary>脚本命令</summary><pre class="mono">${esc(p.command.map(v=>JSON.stringify(v)).join(' '))}</pre></details>`}
      <details class="suite-note"><summary>指标口径与脚本版本</summary>${esc(p.note)}<br>源文件 SHA256：${esc(p.source_sha256)}</details>
      ${p.demo?'':`<div class="checkline"><input type="checkbox" id="suiteConfirm" onchange="document.getElementById('suiteExecute').disabled=!this.checked"><label for="suiteConfirm">确认以上计划，真实请求可能产生费用</label></div>`}
      <div class="suite-toolbar"><button id="suiteExecute" class="btn primary" onclick="suiteExecute()" ${p.demo?'':'disabled'}>${p.demo?'运行本地模拟':'执行计划'}</button></div>`;
  } catch(e) { suiteError(e); }
}
async function suiteExecute() {
  if(!suitePlan) return;
  const p=suitePlan;
  $('#suiteExecute').disabled=true;
  try {
    const result=await suitePost('/execute',{...p.request,confirmed:true,plan_hash:p.plan.plan_hash});
    localStorage.setItem('suiteJob',result.job_id);
    suitePlan=null;
    $('#suitePreview').innerHTML='';
    if(p.plan.kind==='benchmark') {
      workspaceRunId=result.run_ids[0];
      workspaceFollowJob=result.job_id;
      go('dashboard');
      return;
    }
    suiteWatch(result.job_id,++suiteGeneration);
  } catch(e) { suiteError(e); $('#suiteExecute').disabled=false; }
}
function suiteMetrics(summary) {
  if(!summary) return '';
  const cells=[['完成 / 成功',`${summary.attempted??'—'} / ${summary.success??'—'}`],['成功率 / %',summary.success_rate],['TTFT P50 / ms',summary.ttft_ms?.p50],['TTFT P95 / ms',summary.ttft_ms?.p95],['TPOT P50 / ms',summary.tpot_ms?.p50],['总 TPM（全程折算）',summary.total_tokens_tpm],['输出 Token/s',summary.system_gen_tps],['缓存 Token 比率 / %',summary.cache_hit_rate]];
  return `<div class="suite-metrics">${cells.map(([k,v])=>`<div><b>${suiteNumber(v)}</b><span>${k}</span></div>`).join('')}</div>`;
}
async function suiteWatch(id,generation) {
  clearTimeout(suitePoll);
  if(state.tab!=='suites'||generation!==suiteGeneration) return;
  try {
    const j=await api('/suites/jobs/'+id);
    if(state.tab!=='suites'||generation!==suiteGeneration) return;
    if(!$('#suiteJobStatus')) $('#suiteJob').innerHTML='<div id="suiteJobStatus"></div><div id="suiteLive"></div><details class="suite-run-log"><summary>执行日志</summary><pre id="suiteJobLog" class="terminal"></pre></details>';
    $('#suiteJobStatus').innerHTML=`<div class="suite-toolbar"><h2>任务进度 · ${suiteStatus(j.status)}</h2>${j.status==='running'?`<button class="btn" onclick="suiteCancel('${id}')">取消任务</button>`:`<button class="btn primary" onclick="reportFromJob('${id}')">编辑汇总报告</button>`}</div>
      <div class="suite-preview"><table><thead><tr><th>运行 / 项目</th><th>阶段</th><th>状态</th><th>结论</th><th>报告</th></tr></thead><tbody>${j.runs.map(r=>`<tr><td>#${r.id} · ${esc(r.name)}</td><td>${r.stage}/${r.stage_count}</td><td>${suiteStatus(r.status)}</td><td>${esc(r.verdict||'—')}</td><td>${['running','queued'].includes(r.status)?'—':`<button class="btn small" onclick="suiteOpenReport(${r.id})">查看</button>`}</td></tr>`).join('')}</tbody></table></div>`;
    $('#suiteJobLog').textContent=j.lines.join('\n');
    const live=j.runs.find(r=>r.status==='running')||[...j.runs].reverse().find(r=>!['queued'].includes(r.status));
    if(live?.suite_id==='benchmark') await workspaceBenchmark($('#suiteLive'),live.id);
    if(j.status==='running') suitePoll=setTimeout(()=>suiteWatch(id,generation),1000);
    else { localStorage.removeItem('suiteJob'); }
  } catch(e) { suiteError(e); }
}
async function suiteCancel(id) { try {await suitePost('/jobs/'+id+'/cancel');toast('已请求取消');} catch(e) {suiteError(e);} }
let suiteReportRuns=[];
let suiteReportExpanded=false;
const suiteCategory = id => ['benchmark','fixed_rate','tpm_benchmark','stream_metrics','ttft_long_context','latency','text_peak','rate_cache_metrics'].includes(id)?'性能与限流':['api_features','video_understanding','mixed_vision','body_100mb'].includes(id)?'多模态与媒体':['long_conversation','kvv_rounds','long_output','cache','context','stability'].includes(id)?'上下文与稳定性':['acceptance','az_probe'].includes(id)?'验收与渠道':'功能与协议';
async function renderSuiteReports() {
  clearTimeout(suitePoll);
  suiteGeneration++;
  $('#main').innerHTML=studioHeading('reports')+'<div id="suiteError" class="suite-error"></div><div id="suiteReportList"></div><section id="reportEditor"></section><section id="suiteReport"></section>';
  try {
    const [runs,legacy,catalog]=await Promise.all([api('/suites/runs'),api('/suites/legacy-reports'),api('/suites')]);
    if(state.tab!=='reports') return;
    suiteReportRuns=runs;
    const groups=[...new Set(catalog.suites.map(s=>suiteCategory(s.id)))];
    $('#suiteReportList').innerHTML=`<div class="suite-toolbar"><button class="btn primary" onclick="go('suites')">新建测试</button><button class="btn" onclick="renderSuiteReports()">刷新报告</button><span class="suite-status">${catalog.suites.length} 类项目 · 最近 ${runs.length} 次运行</span></div>
      <div class="suite-metrics">${groups.map(group=>`<div><b>${runs.filter(r=>suiteCategory(r.suite_id)===group).length}</b><span>${group}</span></div>`).join('')}</div>
      <div class="suite-form"><div class="field"><label for="reportCategory">测试分类</label><select id="reportCategory" onchange="suiteReportRecords()"><option value="">全部测试项目</option>${groups.map(g=>`<option>${g}</option>`).join('')}</select></div><div class="field"><label for="reportRunSearch">搜索测试结果</label><input id="reportRunSearch" type="search" placeholder="项目、模型或运行编号" oninput="suiteReportRecords()"></div></div>
      <div id="suiteRunRecords"></div>
      ${legacy.length?`<section class="suite-section"><h3>历史压测报告</h3><div class="suite-links">${legacy.map(r=>`<button class="btn small" onclick="suiteImport('${esc(r.id)}')">${esc(r.model)} · ${esc(r.id)}</button>`).join('')}</div></section>`:''}<div id="editableReportList"></div>`;
    suiteReportRecords();
    await renderEditableReports();
  } catch(e) {suiteError(e);}
}
function suiteReportRecords() {
  const group=$('#reportCategory').value,query=$('#reportRunSearch').value.trim().toLowerCase();
  const runs=suiteReportRuns.filter(r=>(!group||suiteCategory(r.suite_id)===group)&&(!query||`#${r.id} ${r.name} ${r.model}`.toLowerCase().includes(query)));
  $('#suiteRunRecords').innerHTML=runs.length?`<div class="suite-preview"><table><thead><tr><th>运行 / 时间</th><th>测试项目 / 模型</th><th>分类</th><th>执行状态</th><th>源脚本结果</th><th></th></tr></thead><tbody>${runs.map(r=>`<tr><td>#${r.id}<br>${esc(r.started_at.replace('T',' ').slice(0,19))}</td><td>${esc(r.name)}${r.demo?' <span class="tag warn">模拟</span>':''}<br>${esc(r.model)}</td><td>${suiteCategory(r.suite_id)}</td><td>${suiteStatus(r.status)}</td><td>${suiteRunOutcome(r)}</td><td><button class="btn small" onclick="suiteShowReport(${r.id})">查看报告</button></td></tr>`).join('')}</tbody></table></div>`:'<div class="empty">暂无对应测试结果</div>';
}
function suiteRunOutcome(r) {
  if(r.suite_id==='benchmark') return `${suiteNumber(r.summary?.success)} / ${suiteNumber(r.summary?.attempted)} 请求成功`;
  const counts={};let total=0;
  for(const table of r.source_tables||[]) {total+=table.rows;for(const [k,v] of Object.entries(table.result_counts||{}))counts[k]=(counts[k]||0)+v;}
  return total?`${total} 条记录 · 通过 ${(counts.pass||0)+(counts.passed||0)} · 未通过 ${(counts.fail||0)+(counts.failed||0)}`:'尚无用例断言';
}
async function suiteOpenReport(id) { state.tab='reports'; history.replaceState(null,'','#reports'); renderNav(); await refreshData(); await renderSuiteReports(); await suiteShowReport(id); }
async function suiteShowReport(id) {
  try {
    const r=await api('/suites/runs/'+id);
    suiteReportExpanded=true;
    const html=r.files.find(f=>f.path==='report.html'||f.path.endsWith('/report.html'));
    const fileUrl=p=>'/api/suites/runs/'+id+'/files/'+p.split('/').map(encodeURIComponent).join('/');
    $('#suiteReport').innerHTML=`<div class="suite-section"><div class="suite-toolbar"><h2>#${r.id} · ${esc(r.name)} ${r.demo?'<span class="tag warn">模拟数据</span>':''}</h2><button class="btn primary" onclick="reportFromRuns([${r.id}])">自定义报告</button><button class="btn" onclick="reportFromJob('${esc(r.job_id)}')">任务汇总报告</button><button id="suiteReportToggle" class="btn" onclick="suiteToggleReport()" aria-expanded="true">收起报告</button></div><div class="suite-status">${esc(r.model)} · ${suiteStatus(r.status)} · ${esc(r.verdict)}</div>
      ${r.suite_id!=='benchmark'?suiteSourceMetrics(r):''}
      ${r.suite_id==='benchmark'?'<p class="suite-note">TTFT：首个正文或思考片段；TPM：全程折算；百分位：线性插值。未知 Usage 显示为空，不按 0 计算。</p>':''}
      <div id="suiteReportContent" class="suite-report-content"><div id="suiteFullReport"></div>
      ${html?`<p><a class="btn small" href="${fileUrl(html.path)}" target="_blank" rel="noopener">源脚本 HTML 报告</a></p>`:''}
      <details><summary>配置与全部证据（${r.files.length}）</summary><pre class="mono">${esc(JSON.stringify(r.config,null,2))}</pre><div class="suite-links">${r.files.map(f=>`<a href="${fileUrl(f.path)}" target="_blank" rel="noopener">${esc(f.path)} (${suiteNumber(f.bytes)} B)</a>`).join('')}</div></details></div></div>`;
    if(r.suite_id==='benchmark') await workspaceBenchmark($('#suiteFullReport'),r.id,false);
    else if(!['running','queued'].includes(r.status)) {
      const report=await reportApi('',{run_ids:[r.id]});
      $('#suiteFullReport').innerHTML=`<iframe class="suite-report" title="模型测试 HTML 报告" sandbox="allow-scripts allow-popups allow-downloads" src="/api/suite-reports/${report.id}/view"></iframe>`;
    } else $('#suiteFullReport').innerHTML='<p class="suite-note">测试执行中，结束后生成测试报告。</p>';
    $('#suiteReport').scrollIntoView({behavior:'smooth',block:'start'});
  } catch(e) {suiteError(e);}
}
function suiteToggleReport() {
  if(!$('#suiteReportContent')) return;
  suiteReportExpanded=!suiteReportExpanded;
  $('#suiteReportContent').hidden=!suiteReportExpanded;
  const button=$('#suiteReportToggle');
  if(button) {
    button.textContent=suiteReportExpanded?'收起报告':'查看报告';
    button.setAttribute('aria-expanded',String(suiteReportExpanded));
    button.title=suiteReportExpanded?'收起当前报告':'查看当前报告';
  }
}
function suiteSourceMetrics(r) {
  const counts={};let total=0;
  for(const table of r.source_tables||[]) {total+=table.rows;for(const [key,value] of Object.entries(table.result_counts||{}))counts[key]=(counts[key]||0)+value;}
  return `<div class="suite-metrics">${[['已归档记录',total],['源脚本通过',(counts.pass||0)+(counts.passed||0)],['源脚本未通过',(counts.fail||0)+(counts.failed||0)],['人工复核',counts.manual_review||0]].map(([label,value])=>`<div><b>${suiteNumber(value)}</b><span>${label}</span></div>`).join('')}</div>`;
}
async function suiteImport(id) {try {const r=await suitePost('/legacy-reports/'+id+'/import');await renderSuiteReports();await suiteShowReport(r.run_id);toast(r.imported?'已归档，原文件保持不变':'该版本已导入');} catch(e) {suiteError(e);} }
