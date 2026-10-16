const { chromium } = require(process.env.BENCHMARK_PLAYWRIGHT_MODULE || 'playwright');
const path = require('path');
const fs = require('fs');
const assert = require('assert');

(async () => {
  const root = path.resolve(__dirname, '..');
  const output = path.join(root, 'results', 'ui-validation');
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  const errors = [];
  const page = await browser.newPage({ viewport: { width: 1600, height: 1160 }, deviceScaleFactor: 1 });
  page.on('pageerror', error => errors.push(error.message));
  try {
    await page.goto(process.env.BENCHMARK_URL || 'http://127.0.0.1:8765');
    await page.waitForSelector('#history-count');
    await page.click('#new-run');
    await page.click('#demo-run');
    await page.waitForFunction(() => document.querySelector('#status').textContent === '已完成');
    await page.waitForFunction(() => document.querySelector('#row-count').textContent === '共 160 条');
    assert.equal(await page.locator('.metric').count(), 25);
    assert.equal(await page.locator('#requests tbody tr').count(), 50);
    assert.equal(await page.locator('#demo-badge').isVisible(), true);
    const chartPixels = await page.evaluate(() => {
      const c = document.querySelector('canvas');
      const data = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
      let colors = 0;
      for (let i = 0; i < data.length; i += 4) if (data[i+3] && Math.max(data[i],data[i+1],data[i+2]) - Math.min(data[i],data[i+1],data[i+2]) > 80) colors++;
      return colors;
    });
    assert(chartPixels > 500, 'Chart must contain plotted color series');
    await page.click('#next-page');
    assert.equal(await page.locator('#requests tbody tr:first-child td:first-child').textContent(), '51');
    await page.click('#prev-page');
    await page.selectOption('#status-filter', 'failed');
    assert.equal(await page.locator('#requests tbody tr').count(), 0);
    await page.selectOption('#status-filter', 'all');
    const download = page.waitForEvent('download');
    await page.click('#export-csv');
    const file = await download;
    await file.saveAs(path.join(output, 'browser-export.csv'));
    const csv = fs.readFileSync(path.join(output, 'browser-export.csv'), 'utf8');
    assert.equal(csv.trim().split('\n').length, 161);
    const report = await page.evaluate(async () => {
      const reports = await (await fetch('/api/runs')).json();
      return await (await fetch('/api/runs/' + reports[0].id)).json();
    });
    const htmlPath = path.join(root, 'results', report.id, 'report.html');
    await page.screenshot({ path: path.join(output, 'desktop.png'), fullPage: true });
    await page.emulateMedia({ media: 'print' });
    await page.evaluate(() => window.dispatchEvent(new Event('beforeprint')));
    assert.equal(await page.locator('#requests tbody tr').count(), 160);
    await page.pdf({ path: path.join(output, 'demo-report.pdf'), format: 'A4', landscape: true, printBackground: true });
    await page.evaluate(() => window.dispatchEvent(new Event('afterprint')));
    await page.emulateMedia({ media: 'screen' });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: path.join(output, 'mobile.png'), fullPage: true });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), 'Mobile page must not overflow horizontally');
    const overlap = await page.evaluate(() => [...document.querySelectorAll('.metric-value')].some(el => el.scrollWidth > el.clientWidth));
    assert.equal(overlap, false, 'KPI text must fit');
    await page.goto('file://' + htmlPath);
    await page.waitForSelector('.metric-value');
    assert.equal(await page.locator('#sidebar').isVisible(), false);
    assert.equal(await page.locator('#requests tbody tr').count(), 50);
    assert.equal(await page.locator('#demo-badge').isVisible(), true);
    const offlineDownload = page.waitForEvent('download');
    await page.click('#export-json');
    await (await offlineDownload).saveAs(path.join(output, 'offline-export.json'));
    assert.equal(JSON.parse(fs.readFileSync(path.join(output, 'offline-export.json'), 'utf8')).requests.length, 160);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ ok: true, requestCount: report.requests.length, chartPixels, report: htmlPath, artifacts: output }));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
