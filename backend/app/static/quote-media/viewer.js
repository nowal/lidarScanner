'use strict';
const base = location.pathname.replace(/\/$/, '');
const viewer = document.getElementById('viewer');
const select = document.getElementById('models');
const statusText = document.getElementById('status');
const original = document.getElementById('usdz');
const glb = document.getElementById('glb');
let snapshot, selected = 0, rendered = false, attempts = 0;
function showModel() {
  const model = snapshot.models[selected];
  if (!model) { statusText.textContent = 'No textured model was available for this scope. The saved scan photos are below.'; select.hidden = true; return; }
  original.hidden = false; original.href = `${base}/models/${selected}/usdz`;
  glb.hidden = model.status !== 'ready'; glb.href = `${base}/models/${selected}/glb`;
  if (model.status === 'ready') {
    const url = `${base}/models/${selected}/glb`;
    if (viewer.getAttribute('src') !== url) {
      statusText.textContent = 'Loading the textured model…'; viewer.hidden = false;
      viewer.setAttribute('src', url); viewer.setAttribute('alt', `Textured scan: ${model.label}`);
    }
  } else {
    viewer.hidden = true; viewer.removeAttribute('src');
    statusText.textContent = model.status === 'failed'
      ? 'The desktop preview could not be prepared. The original USDZ and scan photos are still available. Try again later.'
      : 'Preparing a desktop version of this scan. Large homes can take a few minutes. You can view photos or download the original USDZ now.';
  }
}
select.addEventListener('change', () => {selected = Number(select.value); showModel();});
viewer.addEventListener('load', () => {statusText.textContent = 'Model ready. Orbit, zoom, or pan to inspect the scan.';});
viewer.addEventListener('error', () => {statusText.textContent = 'This browser could not display the model. Try a current desktop browser, or download the GLB or original USDZ.';});
async function refresh() {
  try {
    const response = await fetch(`${base}/status`, {cache: 'no-store', referrerPolicy: 'no-referrer'});
    if (!response.ok) {
      statusText.textContent = response.status === 410 ? 'This private link has expired. Ask TakeShape for a new email.' : 'This scan is unavailable. Please request a new link from TakeShape.';
      document.getElementById('revision').textContent = ''; return;
    }
    snapshot = await response.json();
    if (!rendered) {
      rendered = true;
      document.getElementById('revision').textContent = `Saved scan ${snapshot.revision} · Link expires ${new Date(snapshot.expiresAt * 1000).toLocaleDateString()}`;
      for (const m of snapshot.models) {const option = document.createElement('option'); option.value = m.index; option.textContent = m.label; select.appendChild(option);}
      const photos = document.getElementById('photos');
      for (const p of snapshot.photos) {
        const a = document.createElement('a'); a.href = `${base}/photos/${p.index}/full`; a.target = '_blank'; a.rel = 'noopener noreferrer';
        const img = document.createElement('img'); img.src = `${base}/photos/${p.index}/full`; img.alt = p.label; img.loading = 'lazy';
        const label = document.createElement('span'); label.textContent = p.label; a.append(img, label); photos.appendChild(a);
      }
      if (!snapshot.photos.length) photos.textContent = 'No scan photos were available for this scope.';
    }
    showModel(); attempts = 0;
    if (snapshot.models.some(m => m.status === 'preparing')) setTimeout(refresh, 5000);
  } catch (_) {
    statusText.textContent = 'Connection interrupted. Reconnecting…';
    if (++attempts < 12) setTimeout(refresh, 5000);
    else statusText.textContent = 'Unable to reconnect. Reload this page to try again.';
  }
}
refresh();
