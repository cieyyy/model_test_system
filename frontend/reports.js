let reportData=null, reportDraft=null, reportPage=0, reportFilter='', reportTimer=null, reportRevision=0, reportDirty=false;
let reportRowsObserver=null;
const reportColumns={project:'测试项目',case:'用例 / 指标',observed:'实测 / 原始判定',conclusion:'报告结论',note:'备注'};
const reportSections={summary:'测试结论',results:'结果表格',curves:'指标曲线',evidence:'原始证据'};
const reportApi=(path,body)=>api('/suite-reports'+path,body===undefined?{}:{method:'POST',body:JSON.stringify(body)});

async function renderEditableReports() {
  const items=await reportApi('');
  if(!$('#editableReportList'))return;
  $('#editableReportList').innerHTML=items.length?`<div class="suite-report-versions"><div class="suite-preview"><table><thead><tr><th>测试报告</th><th>版本</th><th>创建时间</th><th></th></tr></thead><tbody>${items.map(r=>`<tr><td>${esc(r.title)}</td><td>v${r.version}</td><td>${esc(r.created_at.replace('T',' ').slice(0,19))}</td><td><button class="btn small" onclick="reportOpen(${r.id})">预览与编辑</button></td></tr>`).join('')}</tbody></table></div></div>`:'';
}
async function reportFromJob(job) {await reportCreate({job_id:job});}
async function reportFromRuns(ids) {await reportCreate({run_ids:ids});}
async function reportCreate(scope) {
  try {
    const r=await reportApi('',scope);
    if(state.tab!=='reports') {state.tab='reports';history.replaceState(null,'','#reports');renderNav();await refreshData();await renderSuiteReports();}
    await reportOpen(r.id);
  } catch(e){suiteError(e);}
}
async function reportOpen(id,version) {
  try {
    clearTimeout(reportTimer);reportRevision++;
    reportData=await reportApi('/'+id+(version?'?version='+version:''));
    if(!$('#reportEditor')) return;
    reportDraft=structuredClone(reportData.content);
    reportDirty=false;
    const saved=localStorage.getItem('reportDraft:'+id);
    if(saved&&!version) {
      try {const draft=JSON.parse(saved);if(draft.base_version===reportData.latest_version){reportDraft=draft.content;reportDirty=true;}}catch{}
    }
    reportPage=0;reportFilter='';
    reportRenderEditor();
    await reportPreview();
    $('#reportEditor').scrollIntoView({behavior:'smooth',block:'start'});
  } catch(e){suiteError(e);}
}
function reportRenderEditor() {
  const d=reportDraft;
  $('#reportEditor').innerHTML=`<div class="suite-section"><div class="suite-toolbar"><h2>报告编辑</h2><span id="reportSaveState" class="suite-status"></span><button class="btn" onclick="reportDiscardDraft()">恢复保存版本</button><button class="btn primary" id="reportSave" onclick="reportSave()">保存新版本</button></div>
    <div class="suite-form">${suiteField('reportTitle','报告标题',d.title)}${suiteField('reportSubtitle','副标题',d.subtitle)}
    <div class="field"><label for="reportTemplate">版式</label><select id="reportTemplate"><option value="model_test">模型测试报告（参考本机 HTML）</option><option value="standard">标准报告</option><option value="compact">紧凑报告</option><option value="acceptance">验收报告</option></select></div>
    <div class="field"><label for="reportAccent">主题色</label><input type="color" id="reportAccent" value="${esc(d.accent)}"></div>
    <div class="field"><label for="reportVersions">历史版本</label><select id="reportVersions" onchange="reportOpen(${reportData.id},Number(this.value))">${reportData.versions.map(v=>`<option value="${v.version}" ${v.version===reportData.version?'selected':''}>v${v.version} · ${esc(v.editor)} · ${esc(v.change_summary)}</option>`).join('')}</select></div>
    ${suiteField('reportEditorName','编辑人','本机用户')}${suiteField('reportChange','修改说明','更新报告结论与展示')}
    </div><div class="field report-summary"><label for="reportSummary">测试结论</label><textarea id="reportSummary" rows="3">${esc(d.summary)}</textarea></div>
    <div class="report-options"><strong>章节</strong>${Object.entries(reportSections).map(([k,label])=>`<label><input type="checkbox" data-section="${k}" ${d.sections.includes(k)?'checked':''}>${label}</label>`).join('')}</div>
    <div class="report-options"><strong>表格列</strong>${Object.entries(reportColumns).map(([k,label])=>`<label><input type="checkbox" data-column="${k}" ${d.columns.includes(k)?'checked':''}>${label}</label>`).join('')}</div>
    <div class="report-downloads">${['html','csv','json'].map(k=>`<a class="btn small" data-report-export="${k}">${k==='csv'?'Excel / CSV':k.toUpperCase()}</a>`).join('')}<span class="suite-note">导出已保存版本</span></div>
    <div id="reportEditError" role="alert" class="suite-error"></div>
    <div class="report-layout"><div class="report-table-panel"><div class="suite-toolbar"><h3>结果与备注</h3><input id="reportSearch" type="search" placeholder="筛选项目或用例" aria-label="筛选报告结果" oninput="reportFilter=this.value;reportPage=0;reportRenderRows()"><button class="btn small" onclick="reportAddRow()">新增行</button></div><div id="reportRows"></div><div id="reportPagination" class="suite-toolbar"></div><p class="suite-note">人工编辑另存版本。原始断言与完整明细保存在运行证据中。</p></div>
    <div class="report-preview-panel"><h3>实时预览</h3><iframe id="reportLivePreview" title="自定义报告实时预览" sandbox="allow-scripts allow-popups allow-downloads"></iframe></div></div></div>`;
  $('#reportTemplate').value=d.template;
  for(const id of ['reportTitle','reportSubtitle','reportTemplate','reportAccent','reportSummary']) $('#'+id).addEventListener('input',reportCollect);
  for(const el of document.querySelectorAll('[data-section],[data-column]')) el.addEventListener('change',reportCollect);
  reportRenderRows();reportSaveState();
}
function reportDiscardDraft() {localStorage.removeItem('reportDraft:'+reportData.id);reportOpen(reportData.id,reportData.version);}
function reportCollect() {
  Object.assign(reportDraft,{title:$('#reportTitle').value,subtitle:$('#reportSubtitle').value,summary:$('#reportSummary').value,template:$('#reportTemplate').value,accent:$('#reportAccent').value,
    sections:[...document.querySelectorAll('[data-section]:checked')].map(e=>e.dataset.section),columns:[...document.querySelectorAll('[data-column]:checked')].map(e=>e.dataset.column)});
  reportChanged();
}
function reportChanged() {
  reportRevision++;
  reportDirty=true;
  try {localStorage.setItem('reportDraft:'+reportData.id,JSON.stringify({base_version:reportData.latest_version,content:reportDraft}));} catch{}
  reportSaveState();
  clearTimeout(reportTimer);reportTimer=setTimeout(reportPreview,300);
}
function reportSaveState() {
  $('#reportSaveState').textContent=reportDirty?'未保存 · 已保留本机草稿':`已保存 v${reportData.version}`;
  document.querySelectorAll('[data-report-export]').forEach(el=>{
    el.href=`/api/suite-reports/${reportData.id}/export/${el.dataset.reportExport}?version=${reportData.version}`;
    el.setAttribute('aria-disabled',reportDirty?'true':'false');
    el.onclick=e=>{if(reportDirty){e.preventDefault();$('#reportEditError').textContent='请先保存新版本，再导出。';}};
  });
}
function reportRenderRows() {
  reportRowsObserver?.disconnect();
  const entries=reportDraft.rows.map((r,i)=>({r,i})).filter(({r})=>(r.project+' '+r.case).toLowerCase().includes(reportFilter.toLowerCase()));
  const pages=Math.max(1,Math.ceil(entries.length/15));reportPage=Math.min(reportPage,pages-1);
  $('#reportRows').innerHTML=`<div class="report-edit-table"><table><thead><tr><th>显示</th>${Object.values(reportColumns).map(v=>`<th>${v}</th>`).join('')}<th>操作</th></tr></thead><tbody>${entries.slice(reportPage*15,(reportPage+1)*15).map(({r,i})=>`<tr><td><input type="checkbox" aria-label="显示第 ${i+1} 行" ${r.visible?'checked':''} onchange="reportDraft.rows[${i}].visible=this.checked;reportChanged()"></td>${Object.entries(reportColumns).map(([k,label])=>`<td><textarea aria-label="第 ${i+1} 行${label}" rows="1" oninput="reportFitCell(this);reportDraft.rows[${i}]['${k}']=this.value;reportChanged()">${esc(r[k])}</textarea></td>`).join('')}<td><div class="report-row-actions"><button class="suite-config-icon" title="恢复原始行" aria-label="恢复第 ${i+1} 行" onclick="reportRestoreRow(${i})">↶</button><button class="suite-config-icon" title="上移" aria-label="上移第 ${i+1} 行" ${i===0?'disabled':''} onclick="reportMoveRow(${i},-1)">↑</button><button class="suite-config-icon" title="下移" aria-label="下移第 ${i+1} 行" ${i===reportDraft.rows.length-1?'disabled':''} onclick="reportMoveRow(${i},1)">↓</button></div></td></tr>`).join('')}</tbody></table></div>`;
  $('#reportPagination').innerHTML=`<span class="suite-status">${entries.length} 行 · ${reportPage+1} / ${pages} 页</span><div><button class="btn small" ${reportPage?'':'disabled'} onclick="reportPage--;reportRenderRows()">上一页</button> <button class="btn small" ${reportPage<pages-1?'':'disabled'} onclick="reportPage++;reportRenderRows()">下一页</button></div>`;
  const table=$('#reportRows table');
  const fitCells=()=>table.querySelectorAll('textarea').forEach(reportFitCell);
  fitCells();
  let width=table.getBoundingClientRect().width;
  reportRowsObserver=new ResizeObserver(()=>{
    const nextWidth=table.getBoundingClientRect().width;
    if(nextWidth!==width){width=nextWidth;fitCells();}
  });
  reportRowsObserver.observe(table);
}
function reportFitCell(input) {
  input.style.height='auto';
  input.style.height=input.scrollHeight+'px';
}
function reportRestoreRow(i) {
  const original=reportData.source.rows.find(r=>r.id===reportDraft.rows[i].id);
  if(original)reportDraft.rows[i]=structuredClone(original);else reportDraft.rows.splice(i,1);
  reportChanged();reportRenderRows();
}
function reportMoveRow(i,delta) {
  const j=i+delta;if(j<0||j>=reportDraft.rows.length)return;
  [reportDraft.rows[i],reportDraft.rows[j]]=[reportDraft.rows[j],reportDraft.rows[i]];reportChanged();reportRenderRows();
}
function reportAddRow() {
  reportDraft.rows.push({id:'custom:'+crypto.randomUUID(),project:'自定义项目',case:'补充结论',observed:'',conclusion:'待复核',note:'',visible:true});
  reportFilter='';$('#reportSearch').value='';reportPage=Math.floor((reportDraft.rows.length-1)/15);reportChanged();reportRenderRows();
}
async function reportPreview() {
  const rev=++reportRevision, id=reportData.id;
  try {
    const response=await fetch(`/api/suite-reports/${id}/preview`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(reportDraft)});
    if(rev!==reportRevision||!$('#reportLivePreview'))return;
    if(!response.ok) {const e=await response.json();throw Error(typeof e.detail==='string'?e.detail:'请检查标题、章节与表格列配置');}
    const html=await response.text();
    if(rev!==reportRevision||!$('#reportLivePreview'))return;
    $('#reportLivePreview').srcdoc=html;$('#reportEditError').textContent='';
  }catch(e){if(rev===reportRevision&&$('#reportEditError'))$('#reportEditError').textContent=e.message;}
}
async function reportSave() {
  try {
    $('#reportSave').disabled=true;
    const editor=$('#reportEditorName').value,change_summary=$('#reportChange').value;
    const result=await reportApi('/'+reportData.id+'/versions',{base_version:reportData.latest_version,content:reportDraft,editor,change_summary});
    localStorage.removeItem('reportDraft:'+reportData.id);
    await renderEditableReports();await reportOpen(result.id,result.version);toast(`已保存 v${result.version}`);
  }catch(e){$('#reportEditError').textContent=e.message;}finally{if($('#reportSave'))$('#reportSave').disabled=false;}
}
