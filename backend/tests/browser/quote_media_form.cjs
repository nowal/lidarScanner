// Isolated fixture only: provider form, privacy copy, photos, and PDF round trip.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const url = process.env.PROVIDER_PREVIEW_URL || fs.readFileSync('/tmp/takeshape-provider-preview/url.txt', 'utf8');
assert.equal(new URL(url).hostname, '127.0.0.1');
(async () => {
  const browser = await chromium.launch({headless: true, channel: 'chrome'});
  try {
    const page = await browser.newPage({viewport: {width: 1280, height: 1000}});
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    const photoRequests = [];
    page.on('request', r => { if (/\/photos\/\d+\//.test(r.url())) photoRequests.push(r.url()); });
    await page.goto(url);
    await page.waitForFunction(() => document.getElementById('viewer').loaded);
    const content = await page.locator('body').innerText();
    for (const gone of ['Provider workspace', 'Saved scan', 'Download original', 'Download GLB', 'Drag to orbit', 'Select a camera', 'Earliest available']) {
      assert.ok(!content.includes(gone), gone);
    }
    assert.match(content, /Link expires/);
    assert.equal(await page.locator('#quote-date').count(), 0);
    assert.equal(await page.locator('#quote-price, #photo-picker, #open-photo, #directions').count(), 0);
    assert.match(content, /up to 25 MB/);
    assert.equal(await page.locator('.photo-card').count(), 242);
    assert.deepEqual(await page.locator('.photo-card').evaluateAll(cards => cards.map(c => Number(c.dataset.index))), Array.from({length: 242}, (_, i) => i));
    assert.ok(await page.locator('#photo-gallery').evaluate(el => el.getBoundingClientRect().top > document.getElementById('quote-form').getBoundingClientRect().bottom));
    assert.equal(await page.locator('.camera-pin').count(), 240);
    await page.waitForFunction(() => [...document.querySelectorAll('.frustum-face')].every(p => p.getAttribute('d')?.endsWith('Z')));
    assert.ok(await page.locator('.camera-frustum path').evaluateAll(paths => paths.every(p => !/NaN|Infinity/.test(p.getAttribute('d')))));
    const firstDirection = await page.locator('.frustum-face').first().getAttribute('d');
    assert.equal(photoRequests.length, 0, 'No thumbnail or full photo is fetched on load');
    await page.locator('.camera-pin').first().focus();
    await page.keyboard.press('Enter');
    assert.equal(await page.locator('#photo-dialog').isVisible(), true);
    await page.waitForFunction(() => document.getElementById('photo-image').naturalWidth > 0);
    await page.locator('#next-photo').click();
    assert.match(await page.locator('#photo-title').textContent(), /Photo 2/);
    await page.locator('#previous-photo').click();
    assert.match(await page.locator('#photo-title').textContent(), /Photo 1 /);
    await page.locator('#close-photo').click();
    assert.equal(await page.locator('#viewer').isVisible(), true);
    assert.equal(await page.locator('.camera-pin').first().evaluate(el => el === document.activeElement), true);
    await page.locator('#cameras').uncheck();
    assert.equal(await page.locator('.camera-pin').first().isVisible(), false);
    await page.locator('#models').selectOption('1');
    await page.waitForFunction(() => document.getElementById('viewer').loaded);
    assert.equal(await page.locator('.camera-pin').count(), 120);
    assert.equal(await page.locator('.photo-card').count(), 242, 'The gallery includes every area regardless of the selected model');
    assert.equal(await page.locator('#cameras').isChecked(), false);
    await page.locator('#measure').click(); await page.locator('#measure').click();
    assert.equal(await page.locator('#cameras').isChecked(), false);
    assert.equal(await page.locator('.camera-pin').first().isVisible(), false);
    await page.locator('#cameras').check();
    await page.locator('.photo-card[data-index="240"]').click();
    assert.match(await page.locator('#photo-title').textContent(), /Homeowner detail/);
    await page.keyboard.press('ArrowRight');
    assert.match(await page.locator('#photo-title').textContent(), /Older unposed/);
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('.photo-card[data-index="240"]').evaluate(el => el === document.activeElement), true);
    await page.locator('#viewer').scrollIntoViewIfNeeded();
    // Pointer activation of a visible marker on the dense model.
    const hit = await page.locator('#viewer').evaluate(v => {
      for (const pin of v.querySelectorAll('.camera-pin')) {
        const r = pin.getBoundingClientRect(), x = r.x + r.width / 2, y = r.y + r.height / 2;
        if (pin.contains(document.elementFromPoint(x, y))) return {x, y};
      }
    });
    assert.ok(hit, 'At least one dense marker is directly clickable');
    await page.mouse.click(hit.x, hit.y);
    assert.equal(await page.locator('#photo-dialog').isVisible(), true);
    await page.locator('#close-photo').click();
    await page.locator('#models').selectOption('0');
    await page.waitForFunction(() => document.getElementById('viewer').loaded);
    // Coalesced projection work remains interactive with 240 markers.
    const projection = await page.evaluate(async () => {
      const v = document.getElementById('viewer'), start = performance.now();
      for (let n = 0; n < 60; n++) {
        v.cameraOrbit = `${n}deg 65deg auto`; v.jumpCameraToGoal();
        await new Promise(requestAnimationFrame);
      }
      return performance.now() - start;
    });
    assert.ok(projection < 10000, `60 dense projection frames took ${projection}ms`);
    assert.notEqual(await page.locator('.frustum-face').first().getAttribute('d'), firstDirection, 'Frustums project the saved direction as the model is orbited');
    assert.ok(await page.locator('.camera-frustum path').evaluateAll(paths => paths.every(p => [...p.getAttribute('d').matchAll(/-?\d+\.?\d*/g)].every(m => Math.abs(Number(m[0])) <= 19.01))), 'Frustum graphics stay within their compact touch targets');
    console.log(`240 markers: 60 orbit frames in ${Math.round(projection)}ms`);
    await page.locator('#provider-name').fill('Browser Fixture Painting');
    await page.locator('#quote-pdf').setInputFiles({name: 'quote.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.4\n% isolated fixture\n%%EOF')});
    const responsePromise = page.waitForResponse(r => r.url().includes('/responses?') && r.request().method() === 'POST');
    await page.locator('#quote-form button[type="submit"]').click();
    const response = await responsePromise;
    assert.equal(response.status(), 201);
    assert.equal(new URL(response.url()).searchParams.has('price_usd'), false);
    await page.waitForFunction(() => document.getElementById('upload-status').textContent.includes('Quote received'));
    const requestId = new URL(url).pathname.split('/')[4];
    const returned = await (await page.request.get(new URL(`/api/v1/ai/quote-requests/${requestId}`, url).href)).json();
    assert.ok(returned.quotes.some(q => q.providerName === 'Browser Fixture Painting' && q.priceUsd == null && q.document));
    assert.ok(returned.quotes.every(q => !('earliestAvailableDate' in q)));
    await page.locator('#photo-gallery').scrollIntoViewIfNeeded();
    await page.waitForFunction(() => document.querySelector('.photo-card.preview-ready'));
    assert.ok(photoRequests.filter(u => u.endsWith('/thumb')).length < 50, 'Off-screen thumbnails are not fetched eagerly');
    await page.screenshot({path: '/tmp/takeshape-provider-gallery-desktop.png'});
    await page.setViewportSize({width: 390, height: 844});
    await page.locator('#photo-gallery').scrollIntoViewIfNeeded();
    await page.screenshot({path: '/tmp/takeshape-provider-gallery-mobile.png'});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    // Photos remain usable while a model is pending, failed, or absent.
    for (const state of ['preparing', 'failed', 'absent']) {
      await page.route('**/status', async route => {
        const response = await route.fetch(); const data = await response.json();
        data.models = state === 'absent' ? [] : data.models.map(m => ({...m, status: state}));
        await route.fulfill({response, json: data});
      });
      await page.goto(url);
      await page.locator('.photo-card').first().waitFor({state: 'visible'});
      assert.equal(await page.locator('#viewer').isVisible(), false);
      await page.locator('.photo-card[data-index="240"]').click();
      assert.match(await page.locator('#photo-title').textContent(), /Homeowner detail/);
      await page.keyboard.press('Escape');
      await page.unroute('**/status');
    }
    // A failed thumbnail still opens the full photo, and empty snapshots explain themselves.
    await page.route('**/photos/0/thumb', route => route.fulfill({status: 503, body: 'Retry later'}));
    await page.goto(url);
    await page.locator('.photo-card').first().scrollIntoViewIfNeeded();
    await page.locator('.photo-card.preview-failed').first().waitFor();
    await page.locator('.photo-card').first().click();
    await page.waitForFunction(() => document.getElementById('photo-image').naturalWidth > 0);
    await page.keyboard.press('Escape');
    await page.unroute('**/photos/0/thumb');
    await page.route('**/status', async route => {
      const response = await route.fetch(); const data = await response.json(); data.photos = [];
      await route.fulfill({response, json: data});
    });
    await page.goto(url);
    await page.waitForFunction(() => document.getElementById('gallery-status').textContent.includes('No photos'));
    assert.equal(await page.locator('.photo-card').count(), 0);
    await page.unroute('**/status');
    await page.goto(new URL('/fixtures/email.html', url).href);
    await page.screenshot({path: '/tmp/takeshape-provider-email-mobile.png', fullPage: true});
    await page.setViewportSize({width: 1000, height: 1000});
    await page.screenshot({path: '/tmp/takeshape-provider-email.png', fullPage: true});
    assert.deepEqual(errors, []);
    console.log('Ordered gallery, camera frustums, photo navigation, PDF-only pricing, receipt, and desktop/mobile layout passed.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
