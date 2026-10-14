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
    await page.locator('.photo-card').first().click();
    assert.equal(await page.locator('#photo-dialog').isVisible(), true);
    await page.locator('#next-photo').click();
    assert.match(await page.locator('#photo-title').textContent(), /Photo 2/);
    await page.locator('#close-photo').click();
    assert.equal(await page.locator('#viewer').isVisible(), true);
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
    await page.goto(new URL('/fixtures/email.html', url).href);
    await page.screenshot({path: '/tmp/takeshape-provider-email-mobile.png', fullPage: true});
    await page.setViewportSize({width: 1000, height: 1000});
    await page.screenshot({path: '/tmp/takeshape-provider-email.png', fullPage: true});
    assert.deepEqual(errors, []);
    console.log('Provider form, photo navigation, required price, PDF receipt, and desktop/mobile layout passed.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
