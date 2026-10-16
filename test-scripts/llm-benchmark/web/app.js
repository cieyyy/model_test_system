(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const offline = Boolean(window.BENCHMARK_DATA);
  const statuses = {queued:'准备中',running:'运行中',completed:'已完成',cancelled:'已取消',error:'执行错误',interrupted:'已中断',success:'成功',failed:'失败'};
  const number = (value, digits = 1) => value == null ? '—' : Number(value).toLocaleString('zh-CN', {maximumFractionDigits:digits});
  const ms = value => value == null ? '—' : number(value) + ' ms';
  const date = value => value ? new Date(value).toLocaleString('zh-CN', {hour12:false,timeZone:'Asia/Shanghai'}) : '—';
  let current = null, events = null, page = 0, selection = 0, printing = false;
  const pageSize = 50;

  async function api(path, options = {}) {
    const response = await fetch(path, options);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '请求失败');
    return data;
  }
  function message(text = '') { $('message').textContent = text; $('message').hidden = !text; }
  function post(path, data) { return api(path, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}); }
  function busy(value) { $('start-run').disabled = value; $('demo-run').disabled = value; }
  function active(report) { return ['queued','running'].includes(report.status); }

  async function history() {
    if (offline) return;
    const data = await api('/api/runs');
    $('history-count').textContent = data.length;
    $('history').replaceChildren();
    for (const report of data) {
      const button = document.createElement('button');
      button.className = 'history-item' + (current?.id === report.id ? ' active' : '');
      button.dataset.runId = report.id;
      const title = document.createElement('strong');
      title.textContent = report.config.name;
      const sub = document.createElement('small');
      sub.textContent = date(report.created_at) + ' · ' + statuses[report.status];
      button.append(title, sub);
      button.onclick = () => select(report.id).catch(e => message(e.message));
      $('history').append(button);
    }
    return data;
  }

  async function select(id) {
    const ticket = ++selection;
    events?.close();
    page = 0;
    const report = await api('/api/runs/' + id);
    if (ticket !== selection) return;
    render(report);
    $('configuration').open = false;
    await history();
    if (ticket !== selection || !active(report)) return;
    events = new EventSource('/api/runs/' + id + '/events');
    events.addEventListener('progress', event => { if (ticket === selection) render(JSON.parse(event.data)); });
    events.addEventListener('complete', async event => {
      events?.close();
      if (ticket !== selection) return;
      render(JSON.parse(event.data));
      try {
        const final = await api('/api/runs/' + id);
        if (ticket !== selection) return;
        render(final);
        await history();
      } catch (e) { message(e.message); }
    });
    events.onerror = () => message('实时连接中断，正在尝试重新连接。');
    events.onopen = () => message();
  }

  function metric(label, value, help = '') {
    const cell = document.createElement('div'); cell.className = 'metric';
    const caption = document.createElement('div'); caption.className = 'metric-label';
    const text = document.createElement('div'); text.textContent = label;
    const mark = document.createElement('span'); mark.textContent = '∿'; mark.setAttribute('aria-hidden','true');
    caption.append(text,mark);
    const result = document.createElement('div'); result.className = 'metric-value'; result.textContent = value;
    if (help) cell.title = help;
    cell.append(caption,result); return cell;
  }

  function render(report) {
    current = report;
    const c = report.config, s = report.summary, running = active(report);
    $('report').hidden = false; $('empty-state').hidden = true;
    $('report-title').textContent = '测试报告 · ' + c.name;
    $('report-subtitle').textContent = `${c.model || 'demo-model'} · ${date(report.started_at || report.created_at)}`;
    $('report-subtitle').title = c.endpoint || '';
    $('demo-badge').hidden = !c.demo;
    $('status').textContent = statuses[report.status] || report.status;
    $('status').className = 'status ' + report.status;
    $('run-meta').textContent = `并发 ${c.concurrency}　请求 ${c.total_requests}　限制时长 ${c.duration_seconds ? c.duration_seconds + ' s' : '不限'}　代理 ${c.proxy ? '已配置' : '直连'}　${c.prompt_mode === 'generated' ? '自动生成' : '自定义'} ${number(report.local_prompt_tokens,0)} Token`;
    $('cancel-run').hidden = offline || !running;
    $('progress-track').hidden = !running;
    $('progress-bar').style.width = Math.min(100, s.attempted / c.total_requests * 100) + '%';
    $('report-error').hidden = !report.error; $('report-error').textContent = report.error;
    busy(running);
    const measures = [
      ['成功率',s.success_rate == null ? '—' : number(s.success_rate,2)+'%','成功 / 已发出请求；取消请求计入分母'],
      ['缓存命中率',s.cache_hit_rate == null ? '—' : number(s.cache_hit_rate,2)+'%','成功请求的缓存输入 Token / 输入 Token；无 Usage 时不可用'],
      ['请求数',number(s.attempted,0)+' / '+number(c.total_requests,0)],
      ['成功 / 失败',number(s.success,0)+' / '+number(s.failed,0),'取消 '+s.cancelled+' 条'],
      ['测试时长',number(s.elapsed_seconds,2)+' s'],
      ['RPM',number(s.rpm)],['Input TPM',number(s.input_tokens_tpm,0)],['Output TPM',number(s.output_tokens_tpm,0)],
      ['Cached TPM',number(s.cached_input_tokens_tpm,0)],['Total TPM',number(s.total_tokens_tpm,0)],
      ['Req TPS（成功）',number(s.req_tps,2),'成功请求数 / 整个测试窗口'],
      ['Req TPS（全部）',number(s.elapsed_seconds > 0 ? s.attempted / s.elapsed_seconds : 0,2),'所有已完成请求数 / 整个测试窗口'],
      ['系统 Gen TPS',number(s.system_gen_tps),'成功请求输出 Token 总数 / 整个测试窗口'],
      ['总 Token',number(s.total_tokens,0)],['缓存 Token',number(s.cached_input_tokens,0)],
      ['Latency Avg',ms(s.latency_ms.avg)],['Latency P50',ms(s.latency_ms.p50)],['Latency P90',ms(s.latency_ms.p90)],
      ['TTFT Avg',ms(s.ttft_ms.avg)],['TTFT P50',ms(s.ttft_ms.p50)],['TTFT P90',ms(s.ttft_ms.p90)],
      ['TPOT Avg',ms(s.tpot_ms.avg)],['TPOT P50',ms(s.tpot_ms.p50)],['TPOT P90',ms(s.tpot_ms.p90)]
    ];
    $('metrics').replaceChildren(...measures.map(m => metric(...m)));
    const filler = document.createElement('div'); filler.className = 'metric filler'; filler.setAttribute('aria-hidden','true'); $('metrics').append(filler);
    const body = $('percentiles').querySelector('tbody'); body.replaceChildren();
    for (const [name,key] of [['总延迟','latency_ms'],['TTFT','ttft_ms'],['TPOT','tpot_ms']]) {
      const row = document.createElement('tr');
      for (const value of [name,...['avg','p50','p90','p95'].map(k => ms(s[key][k]))]) { const cell = document.createElement('td'); cell.textContent = value; row.append(cell); }
      row.title = '有效样本 '+s[key].samples+' 条'; body.append(row);
    }
    $('failure-count').textContent = `失败 ${s.failed} 条${s.cancelled ? ' · 取消 '+s.cancelled+' 条' : ''}`;
    renderRows(); drawChart();
    for (const button of document.querySelectorAll('.history-item')) button.classList.toggle('active',button.dataset.runId === report.id);
  }

  function filteredRows() { return (current?.requests || []).filter(r => $('status-filter').value === 'all' || r.status === $('status-filter').value); }
  function renderRows() {
    if (!current) return;
    const rows = filteredRows();
    const pages = Math.max(1,Math.ceil(rows.length/pageSize));
    page = Math.max(0,Math.min(page,pages-1));
    const visible = printing ? rows : rows.slice(page*pageSize,(page+1)*pageSize);
    const body = $('requests').querySelector('tbody'); body.replaceChildren();
    const fragment = document.createDocumentFragment();
    for (const r of visible) {
      const row = document.createElement('tr');
      const values = [r.sequence,statuses[r.status],r.error || r.prompt_preview,ms(r.latency_ms),ms(r.ttft_ms),ms(r.tpot_ms),number(r.input_tokens,0)+' / '+number(r.output_tokens,0),number(r.cached_input_tokens,0),r.http_status ?? '—'];
      values.forEach((value,i) => {
        const cell = document.createElement('td'); cell.textContent = value;
        if (i===1) { const badge = document.createElement('span'); badge.className = 'row-status '+r.status; badge.textContent = value; cell.replaceChildren(badge); }
        if (i===2) { cell.className = 'prompt-cell'; cell.title = r.error || r.output_preview || r.prompt_preview; }
        row.append(cell);
      }); fragment.append(row);
    }
    body.append(fragment);
    $('row-count').textContent = `共 ${rows.length} 条${active(current) && current.summary.attempted > 500 ? '（实时显示最近 500 条）' : ''}`;
    $('page-label').textContent = `${page+1} / ${pages}`;
    $('prev-page').disabled = page===0; $('next-page').disabled = page===pages-1;
  }

  const series = [['tpot_ms','#0c9b71','TPOT'],['ttft_ms','#336cfb','TTFT'],['latency_ms','#ea414a','总延迟']];
  let chartGeometry = null;
  function drawChart() {
    if (!current) return;
    const canvas = $('latency-chart'), rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(rect.width*dpr); canvas.height = Math.round(rect.height*dpr);
    const ctx = canvas.getContext('2d'); ctx.scale(dpr,dpr);
    const rows = current.requests || [], left=58, top=12, width=Math.max(1,rect.width-left-12), height=rect.height-40;
    const enabled = series.filter(([key]) => document.querySelector(`[data-series="${key}"]`).checked);
    let max = 0;
    for (const row of rows) for (const [key] of enabled) if (row[key]!=null) max=Math.max(max,row[key]);
    const ceiling = Math.max(100,Math.ceil(max/4/100)*100*4);
    const x = i => left + (rows.length>1 ? i/(rows.length-1)*width : width/2);
    const y = value => top + height - value/ceiling*height;
    ctx.font = '11px -apple-system, sans-serif'; ctx.fillStyle='#798797'; ctx.textAlign='right';
    for(let i=0;i<=4;i++) {const value=ceiling*i/4, yy=y(value);ctx.setLineDash([4,4]);ctx.strokeStyle='#dce2e9';ctx.beginPath();ctx.moveTo(left,yy);ctx.lineTo(left+width,yy);ctx.stroke();ctx.fillText(number(value,0)+'ms',left-8,yy+4);}
    ctx.setLineDash([]); ctx.textAlign='center';
    const ticks = Math.min(7,rows.length);
    for(let i=0;i<ticks;i++) {const index=ticks===1 ? 0 : Math.round(i/(ticks-1)*(rows.length-1));ctx.fillText(String(rows[index].sequence),x(index),top+height+20);}
    for(const [key,color] of enabled) {ctx.strokeStyle=color;ctx.lineWidth=1.7;ctx.beginPath();let pen=false;for(let i=0;i<rows.length;i++){const v=rows[i][key];if(v==null){pen=false;continue;}if(pen)ctx.lineTo(x(i),y(v));else ctx.moveTo(x(i),y(v));pen=true;}ctx.stroke();if(rows.length===1&&rows[0][key]!=null){ctx.beginPath();ctx.arc(x(0),y(rows[0][key]),2.5,0,Math.PI*2);ctx.fillStyle=color;ctx.fill();}}
    chartGeometry={left,width,rows};
  }
  $('latency-chart').addEventListener('mousemove', event => {
    if(!chartGeometry?.rows.length)return;
    const rect=event.currentTarget.getBoundingClientRect();
    const {left,width,rows}=chartGeometry;
    const index=Math.max(0,Math.min(rows.length-1,Math.round((event.clientX-rect.left-left)/width*(rows.length-1))));
    const row=rows[index], tip=$('chart-tooltip');
    tip.textContent=`请求 #${row.sequence}\n总延迟 ${ms(row.latency_ms)}\nTTFT ${ms(row.ttft_ms)}\nTPOT ${ms(row.tpot_ms)}`;
    tip.hidden=false;tip.style.left=Math.max(0,Math.min(event.clientX-rect.left+12,rect.width-150))+'px';tip.style.top='15px';
  });
  $('latency-chart').addEventListener('mouseleave',()=>{$('chart-tooltip').hidden=true;});
  new ResizeObserver(drawChart).observe($('latency-chart'));
  document.querySelectorAll('[data-series]').forEach(el=>el.addEventListener('change',drawChart));

  function download(text,type,name) {
    const url=URL.createObjectURL(new Blob([text],{type}));const link=document.createElement('a');link.href=url;link.download=name;link.click();setTimeout(()=>URL.revokeObjectURL(url),2000);
  }
  function csv() {
    const fields=['sequence','request_id','status','http_status','latency_ms','ttft_ms','tpot_ms','input_tokens','output_tokens','cached_input_tokens','total_tokens','finish_reason','error'];
    const escape=v=>{let s=String(v??'');if(/^[\s]*[=+\-@]/.test(s))s="'"+s;return '"'+s.replaceAll('"','""')+'"';};
    return '\ufeff'+[fields.join(','),...current.requests.map(r=>fields.map(k=>escape(r[k])).join(','))].join('\r\n');
  }
  $('export-csv').onclick=()=>offline ? download(csv(),'text/csv',current.id+'.csv') : location.assign('/api/runs/'+current.id+'/csv');
  $('export-json').onclick=()=>download(JSON.stringify(current,null,2),'application/json',current.id+'.json');
  $('export-html').onclick=()=>location.assign('/api/runs/'+current.id+'/html');
  $('export-pdf').onclick=()=>window.print();
  window.addEventListener('beforeprint',()=>{printing=true;renderRows();drawChart();});
  window.addEventListener('afterprint',()=>{printing=false;renderRows();drawChart();});
  $('status-filter').onchange=()=>{page=0;renderRows();};
  $('prev-page').onclick=()=>{page--;renderRows();};$('next-page').onclick=()=>{page++;renderRows();};
  $('new-run').onclick=()=>{$('configuration').open=true;$('configuration').scrollIntoView({behavior:'smooth'});};
  const form=$('config-form');
  form.elements.prompt_mode.onchange=()=>{$('custom-prompt').hidden=form.elements.prompt_mode.value!=='custom';};
  async function start(demo) {
    if(!form.reportValidity())return;
    message();busy(true);
    const data=Object.fromEntries(new FormData(form));
    const apiKey=data.api_key;delete data.api_key;
    for(const key of ['concurrency','total_requests','timeout_seconds','duration_seconds','prompt_tokens','max_tokens'])data[key]=Number(data[key]);
    data.temperature=data.temperature===''?null:Number(data.temperature);data.include_usage=form.elements.include_usage.checked;data.demo=demo;
    if(demo){data.name='DeepSeek · 本地模拟';data.model='demo-model';data.endpoint='';data.proxy='';}
    try{const result=await post('/api/runs',{config:data,api_key:demo?'':apiKey});form.elements.api_key.value='';await select(result.id);}
    catch(e){message(e.message);busy(false);}
  }
  form.onsubmit=event=>{event.preventDefault();start(false);};
  $('demo-run').onclick=()=>start(true);
  $('cancel-run').onclick=async()=>{try{await post('/api/runs/'+current.id+'/cancel',{});}catch(e){message(e.message);}};
  if(offline){document.body.classList.add('offline');render(window.BENCHMARK_DATA);}
  else history().then(reports=>{if(reports.length)return select(reports[0].id);}).catch(e=>message(e.message));
  if (window.BENCHMARK_EMBED && window.parent !== window) {
    let heightFrame = 0;
    const resize = () => {
      cancelAnimationFrame(heightFrame);
      heightFrame = requestAnimationFrame(() => window.parent.postMessage({type:'benchmark:resize',height:Math.ceil(document.body.getBoundingClientRect().height)}, '*'));
    };
    window.addEventListener('message', event => {
      if(event.source !== window.parent || event.data?.type !== 'benchmark:update') return;
      const report = event.data.report;
      if(!report?.config || !report.summary || !Array.isArray(report.requests)) return;
      render(report);
      resize();
    });
    new ResizeObserver(resize).observe(document.body);
    window.parent.postMessage({type:'benchmark:ready'}, '*');
    resize();
  }
})();
