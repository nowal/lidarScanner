const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const root = process.env.WALKTHROUGH_PREVIEW_DIR;
const url = fs.readFileSync(root + '/url.txt','utf8');
assert.equal(new URL(url).hostname,'127.0.0.1');
(async () => {
 const browser = await chromium.launch({headless:true, channel:'chrome'});
 try {
  const page = await browser.newPage({viewport:{width:1100,height:1000}});
  const errors=[]; page.on('pageerror',e=>errors.push(e.message));
  await page.goto(url);
  await page.waitForFunction(()=>document.querySelector('#walkthrough-video').readyState>=1);
  assert.equal(await page.locator('#model-section').isVisible(),false);
  assert.equal(await page.locator('#walkthrough-video').isVisible(),true);
  assert.equal(await page.locator('#quote-fields').isEnabled(),true);
  assert.match(await page.locator('body').innerText(),/No measured dimensions/);
  await page.locator('#walkthrough-video').evaluate(async v=>{v.muted=true;await v.play()});
  await page.waitForFunction(()=>document.querySelector('#walkthrough-video').currentTime>0);
  await page.locator('#walkthrough-video').evaluate(v=>{v.pause();v.currentTime=2});
  await page.waitForFunction(()=>Math.abs(document.querySelector('#walkthrough-video').currentTime-2)<0.2);
  await page.screenshot({path:root+'/provider-video-desktop.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
  await page.locator('#walkthrough-section').scrollIntoViewIfNeeded();
  await page.screenshot({path:root+'/provider-video-mobile.png'});
  assert.deepEqual(errors,[]);
  console.log('PASS: real private viewer redirect, video playback/seek, no model measurements, enabled quote form and mobile layout');
 } finally {await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
