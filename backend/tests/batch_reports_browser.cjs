/* Full workflow in an isolated database against a local OpenAI-compatible fixture. */
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const http=require('node:http');
const os=require('node:os');
const {spawn}=require('node:child_process');

(async()=>{
  const backend=path.resolve(__dirname,'..');
  const output=process.env.SUITE_UI_OUTPUT||path.resolve(backend,'../outputs/batch-reports-ui');
  fs.mkdirSync(output,{recursive:true});
  const temp=fs.mkdtempSync(path.join(os.tmpdir(),'suite-batch-ui-'));
  fs.writeFileSync(path.join(temp,'test.png'),'offline-image-fixture');
  fs.writeFileSync(path.join(temp,'test.mp4'),'offline-video-fixture');
  const mock=http.createServer((req,res)=>{
    let text='';req.on('data',d=>text+=d);req.on('end',()=>{
      const body=JSON.parse(text);
      const usage={prompt_tokens:16,completion_tokens:2,total_tokens:18};
      if(body.stream){res.writeHead(200,{'Content-Type':'text/event-stream'});res.end('data: '+JSON.stringify({choices:[{delta:{content:'hello'},finish_reason:null}]})+'\n\ndata: '+JSON.stringify({choices:[{delta:{},finish_reason:'stop'}],usage})+'\n\ndata: [DONE]\n\n');}
      else{res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify({choices:[{message:{role:'assistant',content:'hello'},finish_reason:'stop'}],usage}));}
    });
  });
  await new Promise(resolve=>mock.listen(0,'127.0.0.1',resolve));
  const service=spawn(process.env.TEST_PYTHON||path.join(backend,'.venv/bin/python'),['-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8034','--log-level','warning'],{cwd:backend,env:{...process.env,MT_DATA_DIR:temp,MT_MEDIA_DIR:temp},stdio:['ignore','pipe','pipe']});
  let logs='';service.stderr.on('data',d=>logs+=d);service.stdout.on('data',d=>logs+=d);
  const base='http://127.0.0.1:8034';
  let browser;
  try{
    for(let n=0;n<100;n++){try{if((await fetch(base+'/api/health')).ok)break;}catch{}await new Promise(r=>setTimeout(r,100));}
    const post=async(p,body)=>{const r=await fetch(base+'/api'+p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});assert(r.ok,await r.clone().text());return r.json();};
    const catalog=await (await fetch(base+'/api/suites')).json();
    const p=await post('/providers',{name:'本地集成验证',base_url:`http://127.0.0.1:${mock.address().port}/v1`,protocol:'openai'});
    await post('/models',{provider_id:p.id,model_name:'local-fixture'});
    await post(`/providers/${p.id}/credentials`,{label:'本地假密钥',api_key:'fake-local-ui-test-key'});
    browser=await chromium.launch({headless:true});
    const page=await browser.newPage({viewport:{width:1600,height:1100}}), errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.goto(base+'/#suites');await page.waitForSelector('#sqSuite');
    assert.deepEqual(await page.locator('#sqScope option').allTextContents(),['单个项目','所有项目']);
    assert(await page.getByLabel('选择测试项目',{exact:true}).isVisible());
    await page.getByRole('button',{name:'一键测试全部项目',exact:true}).click();
    await page.waitForSelector('#suiteExecute');
    const allPlan=await page.evaluate(()=>structuredClone(suitePlan.plan));
    assert.equal(allPlan.project_count,catalog.suites.length);
    assert.equal(await page.locator('#suitePreview tbody tr').count(),allPlan.project_count);
    assert.equal(await page.locator('#suitePreview .suite-metrics b').first().textContent(),String(allPlan.stage_count));
    assert(await page.locator('#suiteProjectConfig').isHidden());
    await page.screenshot({path:path.join(output,'all-projects-plan.png'),fullPage:true});
    await page.selectOption('#sqScope','single');
    assert(await page.getByLabel('选择测试项目',{exact:true}).isVisible());
    assert(await page.locator('#suiteSelection').isHidden());
    await page.selectOption('#sqSuite','benchmark');
    await page.fill('#sp_total_requests','2');await page.fill('#sqConcurrency','1');await page.fill('#sqInput','16');await page.fill('#sqOutput','8');
    await page.selectOption('#sqSuite','basic');await page.fill('#sp_cases','stream');await page.fill('#sp_rounds','1');await page.fill('#sp_max_requests','1');await page.fill('#sp_timeout','5');
    await page.selectOption('#sqSuite','stream_metrics');await page.fill('#sp_requests','2');await page.fill('#sp_concurrency','1');await page.fill('#sp_input_tokens','16');await page.fill('#sp_max_output_tokens','8');await page.fill('#sp_timeout','5');await page.fill('#sqCooldown','0');
    await page.getByRole('button',{name:'预览计划',exact:true}).click();await page.waitForSelector('#suiteExecute');
    assert.equal(await page.evaluate(()=>suitePlan.request.suite_id),'stream_metrics');
    assert.equal(await page.evaluate(()=>suitePlan.plan.stage_count),1);
    await page.setViewportSize({width:390,height:844});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Single-project controls must fit mobile');
    await page.screenshot({path:path.join(output,'single-project-mobile.png'),fullPage:true});
    await page.setViewportSize({width:1600,height:1100});
    await page.selectOption('#sqScope','all');
    await page.getByRole('button',{name:'配置 基础功能',exact:true}).click();
    assert(await page.getByLabel('配置测试项目',{exact:true}).isVisible());
    const request=await page.evaluate(()=>suiteRequest());
    // The API retains explicit batches for automation; the UI exposes only single/all.
    request.suite_ids=['benchmark','basic','stream_metrics'];
    request.suite_parameters=Object.fromEntries(request.suite_ids.map(id=>[id,request.suite_parameters[id]]));
    const plan=await post('/suites/preview',request);
    const job=await post('/suites/execute',{...request,confirmed:true,plan_hash:plan.plan_hash});
    await page.evaluate(id=>localStorage.setItem('suiteJob',id),job.job_id);
    await page.reload();
    await page.getByRole('button',{name:'编辑汇总报告',exact:true}).waitFor({timeout:30000});
    assert.equal(await page.locator('#suiteJobStatus tbody tr').count(),3);
    await page.getByRole('button',{name:'编辑汇总报告',exact:true}).click();await page.waitForSelector('#reportTitle');
    await page.frameLocator('#reportLivePreview').locator('canvas').first().waitFor();
    await page.fill('#reportTitle','自定义验收 · 本地验证');await page.fill('#reportSummary','已复核脚本断言，保留原始证据。');
    await page.selectOption('#reportTemplate','compact');
    await page.locator('[data-column="note"]').uncheck();
    await page.getByLabel('第 1 行报告结论',{exact:true}).fill('人工复核完成');
    await page.getByLabel('第 1 行备注',{exact:true}).fill('编辑记录，仅影响报告版本');
    await page.getByRole('button',{name:'新增行',exact:true}).click();
    const draftBeforeSave=await page.evaluate(()=>structuredClone(reportDraft));
    assert(draftBeforeSave.rows.at(-1).id.startsWith('custom:'));
    const frame=page.frameLocator('#reportLivePreview');
    await frame.getByRole('heading',{name:'自定义验收 · 本地验证',exact:true}).waitFor();
    await frame.getByText('自定义项目',{exact:true}).waitFor();
    assert.equal(await frame.locator('.detail-table thead th').count(),4);
    await page.screenshot({path:path.join(output,'report-editor-desktop.png'),fullPage:true});
    const pixels=await frame.locator('canvas').first().evaluate(c=>{const data=c.getContext('2d').getImageData(0,0,c.width,c.height).data;let n=0;for(let i=0;i<data.length;i+=4)if(data[i+3]&&Math.max(...data.slice(i,i+3))-Math.min(...data.slice(i,i+3))>70)n++;return n;});
    assert(pixels>100,'Customized report chart must have visible series');
    await page.click('#reportSave');await page.waitForFunction(()=>document.querySelector('#reportSaveState')?.textContent==='已保存 v2');
    await page.reload();await page.getByRole('button',{name:'预览与编辑',exact:true}).first().click();await page.waitForSelector('#reportTitle');
    assert.equal(await page.inputValue('#reportTitle'),'自定义验收 · 本地验证');
    assert.equal(await page.inputValue('#reportSummary'),'已复核脚本断言，保留原始证据。');
    assert.equal(await page.locator('#reportVersions option').count(),2);
    for(const kind of ['html','csv','json']){
      const downloaded=page.waitForEvent('download');await page.click(`[data-report-export="${kind}"]`);const file=await downloaded;await file.saveAs(path.join(output,'custom-report.'+kind));
    }
    const json=JSON.parse(fs.readFileSync(path.join(output,'custom-report.json'),'utf8'));
    assert.equal(json.version,2);assert.equal(json.source.runs.length,3);assert.equal(json.source.rows[0].conclusion,'待复核');assert.equal(json.content.rows[0].conclusion,'人工复核完成');
    await page.selectOption('#reportVersions','1');await page.waitForFunction(()=>document.querySelector('#reportSaveState')?.textContent==='已保存 v1');
    assert.notEqual(await page.inputValue('#reportTitle'),'自定义验收 · 本地验证');
    await page.selectOption('#reportVersions','2');await page.waitForFunction(()=>document.querySelector('#reportSaveState')?.textContent==='已保存 v2');
    await page.setViewportSize({width:390,height:844});
    await page.frameLocator('#reportLivePreview').getByRole('heading',{name:'自定义验收 · 本地验证',exact:true}).waitFor();
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Mobile editor must fit');
    await page.locator('#reportLivePreview').scrollIntoViewIfNeeded();
    await page.locator('#reportLivePreview').screenshot({path:path.join(output,'report-preview-mobile.png')});
    await page.screenshot({path:path.join(output,'report-editor-mobile.png'),fullPage:true});
    assert.deepEqual(errors,[]);
    const verification={ok:true,scopeOptions:2,allProjects:allPlan.project_count,allStages:allPlan.stage_count,batchExecuted:3,savedVersion:2,curvePixels:pixels,exports:['html','csv','json'],errors};
    fs.writeFileSync(path.join(output,'verification.json'),JSON.stringify(verification,null,2));console.log(JSON.stringify(verification));
  }finally{
    if(browser)await browser.close();
    service.kill('SIGTERM');await new Promise(resolve=>service.on('exit',resolve));
    await new Promise(resolve=>mock.close(resolve));
    if(logs)console.log(logs.slice(-4000));
    fs.rmSync(temp,{recursive:true,force:true});
  }
})().catch(e=>{console.error(e);process.exitCode=1;});
