// Run against serve_quote_media_preview.py, never a real request.
// PLAYWRIGHT_MODULE can point to a separately installed Playwright package.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const url = process.env.PROVIDER_PREVIEW_URL || fs.readFileSync('/tmp/takeshape-provider-preview/url.txt', 'utf8');
assert.equal(new URL(url).hostname, '127.0.0.1');
(async () => {
  const browser = await chromium.launch({headless: true, channel: 'chrome'});
  try {
    const page = await browser.newPage({viewport: {width: 1280, height: 1100}, hasTouch: true});
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.goto(url);
    await page.waitForFunction(() => document.getElementById('viewer').loaded);
    await page.locator('#measure').click();
    assert.equal(await page.locator('.camera-pin').first().isVisible(), false);
    await page.evaluate(async () => {
      const v = document.getElementById('viewer');
      v.cameraOrbit = '0deg 90deg 12m';
      v.cameraTarget = '0m 1.3m -2m';
      await v.updateComplete; v.jumpCameraToGoal();
      await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
    });
    // Project known metre positions on the fixture wall to real mouse positions.
    async function screen(p) {
      return page.evaluate(async p => {
        const v = document.getElementById('viewer');
        const n = document.createElement('span');
        n.slot = 'hotspot-test'; n.style.opacity = '0';
        n.dataset.position = p.map(x => x + 'm').join(' '); v.append(n);
        await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
        const q = v.queryHotspot(n.slot).canvasPosition;
        const rect = v.getBoundingClientRect(); n.remove();
        return {x: q.x + rect.left, y: q.y + rect.top};
      }, p);
    }
    const a = await screen([-1, 1, -2]), b = await screen([1, 1, -2]);
    await page.mouse.click(a.x, a.y); await page.mouse.click(b.x, b.y);
    await page.waitForFunction(() => document.querySelector('#measurement-list')?.textContent.includes('6 ft 7 in'));
    assert.equal(await page.locator('.measurement-endpoint').count(), 2);
    assert.equal(await page.locator('#measurement-lines line').count(), 1);
    // Dragging updates model-space distance without orbiting the view.
    const orbit = await page.locator('#viewer').evaluate(v => JSON.stringify(v.getCameraOrbit()));
    await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))));
    const end = await page.locator('.measurement-endpoint').first().boundingBox();
    const c = await screen([-2, 1, -2]);
    await page.mouse.move(end.x + end.width / 2, end.y + end.height / 2);
    await page.mouse.down(); await page.mouse.move(c.x, c.y, {steps: 12}); await page.mouse.up();
    assert.match(await page.locator('#measurement-list').textContent(), /9 ft 10 in/);
    assert.equal(await page.locator('#viewer').evaluate(v => JSON.stringify(v.getCameraOrbit())), orbit);
    const d = await screen([0, 2, -2]), e = await screen([1, 2, -2]);
    await page.mouse.click(d.x, d.y); await page.mouse.click(e.x, e.y);
    assert.equal(await page.locator('#measurement-list button').count(), 2);
    assert.match(await page.locator('#measurement-list button').last().textContent(), /3 ft 3 in/);
    // A sky click creates no endpoint; an orbit drag creates no measurement.
    const bounds = await page.locator('#viewer').boundingBox();
    await page.mouse.click(bounds.x + 10, bounds.y + 10);
    assert.equal(await page.locator('.measurement-endpoint').count(), 4);
    await page.mouse.move(bounds.x + 80, bounds.y + 80); await page.mouse.down();
    await page.mouse.move(bounds.x + 140, bounds.y + 110, {steps: 10}); await page.mouse.up();
    assert.equal(await page.locator('.measurement-endpoint').count(), 4);
    if (process.env.PROVIDER_SCREENSHOT) await page.screenshot({path: process.env.PROVIDER_SCREENSHOT, fullPage: true});
    await page.locator('#measurement-list button').first().click();
    await page.locator('#delete-measurement').click();
    assert.equal(await page.locator('#measurement-list button').count(), 1);
    await page.locator('#measure').click();
    assert.equal(await page.locator('.camera-pin').first().isVisible(), true);
    await page.locator('.camera-pin').first().click();
    await page.locator('#photo-dialog').waitFor({state: 'visible'});
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('.measurement-endpoint').count(), 2);
    await page.locator('#models').selectOption('1');
    await page.waitForFunction(() => document.getElementById('viewer').loaded);
    assert.equal(await page.locator('#measurement-list button').count(), 0);
    await page.locator('#models').selectOption('0');
    await page.waitForFunction(() => document.getElementById('viewer').loaded);
    assert.equal(await page.locator('#measurement-list button').count(), 1);
    await page.locator('#clear-measurements').click();
    assert.equal(await page.locator('.measurement-endpoint').count(), 0);
    await page.locator('#measure').click();
    const point = await screen([0, 1, -2]);
    await page.mouse.click(point.x, point.y);
    await page.locator('#cancel-point').click();
    assert.equal(await page.locator('.measurement-endpoint').count(), 0);
    await page.setViewportSize({width: 390, height: 844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.locator('#viewer').scrollIntoViewIfNeeded();
    await page.evaluate(async () => {
      const v = document.getElementById('viewer'); v.cameraOrbit = '0deg 90deg 12m';
      v.cameraTarget = '0m 1.3m -2m'; await v.updateComplete; v.jumpCameraToGoal();
      await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
    });
    const ta = await screen([-1, 1, -2]), tb = await screen([1, 1, -2]);
    await page.touchscreen.tap(ta.x, ta.y); await page.touchscreen.tap(tb.x, tb.y);
    assert.match(await page.locator('#measurement-list').textContent(), /6 ft 7 in/);
    assert.deepEqual(errors, []);
    console.log('PASS: metre-based distances, endpoint drag, multiple measurements, sky rejection, orbit gestures, selection/delete, camera photos, area restoration, clear/cancel, narrow layout and touch input.');
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exit(1); });
