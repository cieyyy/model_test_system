/* Explicit online smoke test. Creates one clearly marked simulated run and report. */
const {chromium,request}=require('playwright');
const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');

(async()=>{
  const base=process.env.PLATFORM_URL;
  assert(base&&process.env.LOGIN_FILE,'Set PLATFORM_URL and LOGIN_FILE');
  const login=fs.readFileSync(process.env.LOGIN_FILE,'utf8');
  const username=login.match(/^Username: (.+)$/m)?.[1];
  const password=login.match(/^Password: (.+)$/m)?.[1];
  assert(username&&password,'Invalid login file');
  const output=process.env.SMOKE_OUTPUT||path.resolve('outputs/deployed-ui');
  fs.mkdirSync(output,{recursive:true});
  const anonymous=await request.newContext();
  assert.equal((await anonymous.get(base+'/api/health')).status(),401);
  await anonymous.dispose();
  const browser=await chromium.launch({headless:true});
  try{
    const context=await browser.newContext({httpCredentials:{username,password},viewport:{width:1600,height:1100}});
    const api=async(url,data)=>{
      const r=data===undefined?await context.request.get(base+url):await context.request.post(base+url,{data});
      assert(r.ok(),`${url}: ${r.status()} ${await r.text()}`);
      return r.json();
    };
    const catalog=await api('/api/suites');
    assert.equal(catalog.suites.filter(s=>s.available).length,22);
    const req={suite_id:'benchmark',demo:true,parameters:{total_requests:4},concurrency_levels:[1,2],input_token_levels:[32],output_token_levels:[16]};
    const plan=await api('/api/suites/preview',req);
    const started=process.env.SMOKE_JOB_ID?{job_id:process.env.SMOKE_JOB_ID}:await api('/api/suites/execute',{...req,confirmed:true,plan_hash:plan.plan_hash});
    let job;
    for(let i=0;i<120;i++){
      job=await api('/api/suites/jobs/'+started.job_id);
      if(job.status!=='running')break;
      await new Promise(r=>setTimeout(r,500));
    }
    assert.equal(job.status,'completed');
    assert(job.report_id);
    const page=await context.newPage(),errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.goto(base+'/#suites');
    await page.waitForSelector('#sqScope');
    assert.deepEqual(await page.locator('#sqScope option').allTextContents(),['单个项目','所有项目']);
    await page.screenshot({path:path.join(output,'suites-desktop.png'),fullPage:true});
    await page.goto(base+'/#reports');
    await page.waitForSelector('#reportEditor',{state:'attached'});
    await page.evaluate(id=>reportOpen(id),job.report_id);
    await page.waitForSelector('#reportTitle');
    const frame=page.frameLocator('#reportLivePreview');
    await frame.locator('canvas').first().waitFor();
    const curvePixels=await frame.locator('canvas').first().evaluate(c=>{
      const pixels=c.getContext('2d').getImageData(0,0,c.width,c.height).data;
      let count=0;for(let i=0;i<pixels.length;i+=4)if(pixels[i+3]&&Math.max(...pixels.slice(i,i+3))-Math.min(...pixels.slice(i,i+3))>70)count++;
      return count;
    });
    assert(curvePixels>100);
    await page.fill('#reportTitle','服务器部署验证 · 模拟数据');
    await page.fill('#reportSummary','仅使用平台本地模拟数据验证部署和曲线，不代表模型实测结论。');
    await page.click('#reportSave');
    await page.waitForFunction(()=>document.querySelector('#reportSaveState')?.textContent==='已保存 v2');
    await page.screenshot({path:path.join(output,'report-desktop.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.screenshot({path:path.join(output,'report-mobile.png'),fullPage:true});
    assert.deepEqual(errors,[]);
    const result={ok:true,tlsVerified:true,anonymousStatus:401,projects:22,jobId:started.job_id,runIds:job.runs.map(r=>r.id),reportId:job.report_id,version:2,curvePixels,errors};
    fs.writeFileSync(path.join(output,'verification.json'),JSON.stringify(result,null,2));
    console.log(JSON.stringify(result));
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
