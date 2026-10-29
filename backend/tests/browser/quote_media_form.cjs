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
    assert.equal(await page.locator('#quote-price').getAttribute('required'), '');
    assert.equal(await page.locator('#quote-price').getAttribute('placeholder'), 'See PDF for pricing');
    assert.match(content, /up to 25 MB/);
    assert.equal(await page.locator('.photo-card, #photos, section[aria-label="Scan photos"]').count(), 0);
    assert.equal(await page.locator('.camera-pin').count(), 240);
    assert.equal(await page.locator('#photo-picker option').count(), 242);
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
    assert.equal(await page.locator('#photo-picker option').count(), 122);
    assert.equal(await page.locator('#cameras').isChecked(), false);
    await page.locator('#measure').click(); await page.locator('#measure').click();
    assert.equal(await page.locator('#cameras').isChecked(), false);
    assert.equal(await page.locator('.camera-pin').first().isVisible(), false);
    await page.locator('#cameras').check();
    await page.locator('#photo-picker').selectOption('240');
    await page.locator('#open-photo').click();
    assert.match(await page.locator('#photo-title').textContent(), /Homeowner detail/);
    await page.keyboard.press('ArrowRight');
    assert.match(await page.locator('#photo-title').textContent(), /Older unposed/);
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#open-photo').evaluate(el => el === document.activeElement), true);
    // Pointer activation of a visible marker on the dense model.
    const hit = await page.locator('#viewer').evaluate(v => {
      for (const pin of v.querySelectorAll('.camera-pin')) {
        const r = pin.getBoundingClientRect(), x = r.x + r.width / 2, y = r.y + r.height / 2;
        if (document.elementFromPoint(x, y) === pin) return {x, y};
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
    console.log(`240 markers: 60 orbit frames in ${Math.round(projection)}ms`);
    await page.locator('#provider-name').fill('Browser Fixture Painting');
    await page.locator('#quote-pdf').setInputFiles({name: 'quote.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.4\n% isolated fixture\n%%EOF')});
    await page.locator('#quote-form button[type="submit"]').click();
    assert.equal(await page.locator('#quote-price').evaluate(el => el.validity.valueMissing), true);
    await page.locator('#quote-price').fill('2450');
    const responsePromise = page.waitForResponse(r => r.url().includes('/responses?') && r.request().method() === 'POST');
    await page.locator('#quote-form button[type="submit"]').click();
    const response = await responsePromise;
    assert.equal(response.status(), 201);
    await page.waitForFunction(() => document.getElementById('upload-status').textContent.includes('Quote received'));
    const requestId = new URL(url).pathname.split('/')[4];
    const returned = await (await page.request.get(new URL(`/api/v1/ai/quote-requests/${requestId}`, url).href)).json();
    assert.ok(returned.quotes.some(q => q.providerName === 'Browser Fixture Painting' && q.priceUsd === 2450));
    assert.ok(returned.quotes.every(q => !('earliestAvailableDate' in q)));
    await page.screenshot({path: '/tmp/takeshape-provider-viewer-desktop.png', fullPage: true});
    await page.setViewportSize({width: 390, height: 844});
    await page.screenshot({path: '/tmp/takeshape-provider-viewer-mobile.png', fullPage: true});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    // Photos remain usable while a model is pending, failed, or absent.
    for (const state of ['preparing', 'failed', 'absent']) {
      await page.route('**/status', async route => {
        const response = await route.fetch(); const data = await response.json();
        data.models = state === 'absent' ? [] : data.models.map(m => ({...m, status: state}));
        await route.fulfill({response, json: data});
      });
      await page.goto(url);
      await page.locator('#open-photo').waitFor({state: 'visible'});
      await page.waitForFunction(() => !document.getElementById('open-photo').disabled);
      assert.equal(await page.locator('#viewer').isVisible(), false);
      await page.locator('#photo-picker').selectOption('240');
      await page.locator('#open-photo').click();
      assert.match(await page.locator('#photo-title').textContent(), /Homeowner detail/);
      await page.keyboard.press('Escape');
      await page.unroute('**/status');
    }
    await page.goto(new URL('/fixtures/email.html', url).href);
    await page.screenshot({path: '/tmp/takeshape-provider-email-mobile.png', fullPage: true});
    await page.setViewportSize({width: 1000, height: 1000});
    await page.screenshot({path: '/tmp/takeshape-provider-email.png', fullPage: true});
    assert.deepEqual(errors, []);
    console.log('Provider form, photo navigation, required price, PDF receipt, and desktop/mobile layout passed.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
