const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH, headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    const output = (filename, cache_key) => ({ filename, cache_key, format: 'glb' });
    const fixture = {
      id: 'fixture', status: 'completed', payload: { mode: 'single', texture: true },
      outputs: [output('white_mesh.glb', 'white1'), output('textured_mesh.glb', 'tex2')],
      texture_versions: [{ id: 'old', outputs: [output('texture_versions/old/textured_mesh.glb', 'old1')] }],
    };
    let job = structuredClone(fixture);
    let fail = false;
    let hold = false;
    let release;
    let posts = [];
    await page.route('**/*', async (route) => {
      const url = new URL(route.request().url());
      if (url.hostname !== 'lgo.test') return route.fulfill({ body: '', contentType: 'text/javascript' });
      const json = (value, status = 200) => route.fulfill({ status, json: value });
      if (['/', '/app.js', '/styles.css'].includes(url.pathname)) {
        const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
        return route.fulfill({ path: path.join(__dirname, '../web', name), contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html' });
      }
      if (url.pathname === '/api/jobs') return json({ jobs: [] });
      if (url.pathname.endsWith('/log')) return route.fulfill({ body: '', contentType: 'text/plain' });
      if (url.pathname.endsWith('/export')) {
        assert.equal(route.request().method(), 'POST');
        const source = url.searchParams.get('source');
        const format = url.searchParams.get('format');
        posts.push({ source, format });
        if (fail) return json({ error: 'Blender unavailable' }, 400);
        job.status = 'converting_outputs';
        job.export_request = { source, format, status: 'running' };
        if (hold) await new Promise((resolve) => { release = resolve; });
        return json(job, 202);
      }
      if (url.pathname === '/api/jobs/fixture') return json(job);
      return json({ error: 'Offline fixture' }, 404);
    });
    await page.goto('http://lgo.test');
    await page.waitForFunction(() => typeof renderJob === 'function');
    const objButton = page.getByRole('button', { name: 'Export OBJ', exact: true });
    const fbxButton = page.getByRole('button', { name: 'Export FBX', exact: true });
    assert.equal(await objButton.isDisabled(), true);
    await page.evaluate((data) => renderJob(data, { preferredScene: 'white' }), job);
    await objButton.click();
    await page.waitForFunction(() => currentJob?.status === 'converting_outputs');
    assert.deepEqual(posts[0], { source: 'white_mesh.glb', format: 'obj' });
    assert.equal(await fbxButton.isDisabled(), true);
    job.status = 'completed';
    job.export_request.status = 'completed';
    const entry = { source: 'white_mesh.glb', source_cache_key: 'white1', format: 'obj', filename: 'exports/one.zip', download_name: 'white_mesh_obj.zip', cache_key: 'export1' };
    job.exports = [entry, entry];
    await page.waitForFunction(() => currentJob?.status === 'completed');
    assert.equal(await page.evaluate(() => currentSceneFilename), 'white_mesh.glb');
    assert.equal(await page.locator('#exportLinks a').count(), 1);
    assert.equal(await page.locator('#exportLinks a').getAttribute('download'), 'white_mesh_obj.zip');
    await page.locator('#viewerChoices button').filter({ hasText: 'Textured mesh' }).click();
    assert.equal(await page.locator('#exportLinks a').count(), 0);
    await page.evaluate(() => loadHistoryTexture('fixture', 'old'));
    await fbxButton.click();
    await page.waitForFunction(() => currentJob?.status === 'converting_outputs');
    assert.deepEqual(posts[1], { source: 'texture_versions/old/textured_mesh.glb', format: 'fbx' });
    job.status = 'completed';
    job.export_request.status = 'completed';
    job.exports.push({ ...entry, source: posts[1].source, format: 'fbx', filename: 'exports/two.fbx', source_cache_key: 'old1', download_name: 'textured_mesh.fbx' });
    await page.waitForFunction(() => currentJob?.status === 'completed');
    assert.equal(await page.evaluate(() => currentTextureVersionId), 'old');
    assert.equal(await page.evaluate(() => currentSceneFilename), posts[1].source);
    assert.equal(await page.locator('#exportLinks a').innerText(), 'Download FBX');
    const shotDir = process.env.LGO_TEST_SCREENSHOTS;
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: 1000 });
      await page.locator('.result-export').scrollIntoViewIfNeeded();
      const boxes = await page.locator('.result-export').evaluate((el) => {
        const parent = el.getBoundingClientRect();
        return [...el.children].map((child) => {
          const rect = child.getBoundingClientRect();
          return rect.left >= parent.left - 1 && rect.right <= parent.right + 1;
        });
      });
      assert.ok(boxes.every(Boolean), `Export controls overflow at ${width}px`);
      if (shotDir) {
        fs.mkdirSync(shotDir, { recursive: true });
        await page.locator('.result-export').screenshot({ path: path.join(shotDir, `exports-${width}.png`) });
      }
    }
    fail = true;
    await objButton.click();
    await page.waitForFunction(() => document.querySelector('#exportStatus').textContent.includes('Blender unavailable'));
    assert.equal(await objButton.isDisabled(), false);
    fail = false;
    hold = true;
    await objButton.click();
    await page.waitForFunction(() => document.querySelector('#exportStatus').textContent === 'Starting export...');
    await page.evaluate((data) => renderJob({ ...data, id: 'other' }, { preferredScene: 'white' }), fixture);
    while (!release) await new Promise((resolve) => setTimeout(resolve, 10));
    release();
    await page.waitForFunction(() => !pendingExports.has('fixture'));
    assert.equal(await page.evaluate(() => currentJobId), 'other');
    assert.deepEqual(errors, []);
    console.log('PASS: selected white/texture version, polling, downloads, duplicate filtering, errors, history switch, desktop/mobile layout');
  } finally {
    await browser.close();
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
