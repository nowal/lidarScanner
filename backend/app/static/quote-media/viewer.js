'use strict';
const base = location.pathname.replace(/\/$/, '');
const viewer = document.getElementById('viewer');
const select = document.getElementById('models');
const statusText = document.getElementById('status');
const cameras = document.getElementById('cameras');
const directions = document.getElementById('directions');
const dialog = document.getElementById('photo-dialog');
const photoImage = document.getElementById('photo-image');
const form = document.getElementById('quote-form');
const uploadStatus = document.getElementById('upload-status');
const measurements = new ModelMeasurements(viewer);
let snapshot, selected = 0, rendered = false, attempts = 0, activePhoto, photoOpener;
let submissionId = crypto.randomUUID(), submitting = false;
const vector = p => p.map(n => `${n}m`).join(' ');
function modelPhotos() {
  const model = snapshot.models[selected];
  return snapshot.photos.filter(p => !model || model.key === 'home' || p.roomKey === model.key);
}
function showPhoto(photo, opener) {
  activePhoto = photo;
  photoOpener = opener || photoOpener;
  document.getElementById('photo-title').textContent = `Photo ${photo.index + 1} · ${photo.label}`;
  document.getElementById('photo-error').hidden = true;
  photoImage.alt = photo.label;
  photoImage.src = `${base}/photos/${photo.index}/full`;
  const list = modelPhotos();
  document.getElementById('previous-photo').disabled = list.length < 2;
  document.getElementById('next-photo').disabled = list.length < 2;
  for (const button of viewer.querySelectorAll('.camera-pin')) button.classList.toggle('selected', Number(button.dataset.index) === photo.index);
  if (!dialog.open) dialog.showModal();
}
function stepPhoto(step) {
  const list = modelPhotos();
  const index = list.findIndex(p => p.index === activePhoto.index);
  showPhoto(list[(index + step + list.length) % list.length]);
}
document.getElementById('close-photo').addEventListener('click', () => dialog.close());
document.getElementById('previous-photo').addEventListener('click', () => stepPhoto(-1));
document.getElementById('next-photo').addEventListener('click', () => stepPhoto(1));
dialog.addEventListener('close', () => { photoImage.removeAttribute('src'); photoOpener?.focus(); });
dialog.addEventListener('keydown', e => {
  if (e.key === 'ArrowLeft') { e.preventDefault(); stepPhoto(-1); }
  if (e.key === 'ArrowRight') { e.preventDefault(); stepPhoto(1); }
});
photoImage.addEventListener('error', () => { document.getElementById('photo-error').hidden = false; });
function renderPhotos() {
  viewer.querySelectorAll('.camera-pin, .direction-point').forEach(el => el.remove());
  directions.querySelectorAll('line').forEach(el => el.remove());
  const gallery = document.getElementById('photos');
  gallery.replaceChildren();
  let count = 0;
  for (const photo of modelPhotos()) {
    const button = document.createElement('button'); button.type = 'button'; button.className = 'photo-card';
    const img = document.createElement('img'); img.src = `${base}/photos/${photo.index}/thumb`; img.alt = photo.label; img.loading = 'lazy';
    const label = document.createElement('span'); label.textContent = `${photo.index + 1} · ${photo.label}`;
    button.append(img, label); button.addEventListener('click', () => showPhoto(photo, button)); gallery.append(button);
    if (!photo.camera) continue;
    count++;
    const pin = document.createElement('button'); pin.type = 'button'; pin.className = 'camera-pin';
    pin.slot = `hotspot-camera-${photo.index}`; pin.dataset.index = photo.index;
    pin.dataset.position = vector(photo.camera.position);
    pin.textContent = `📷 ${photo.index + 1}`;
    pin.title = `Open photo ${photo.index + 1}: ${photo.label}`; pin.setAttribute('aria-label', pin.title);
    pin.addEventListener('click', e => { e.stopPropagation(); showPhoto(photo, pin); });
    viewer.append(pin);
    const tip = document.createElement('span'); tip.slot = `hotspot-direction-${photo.index}`; tip.className = 'direction-point';
    tip.dataset.position = vector(photo.camera.position.map((v, i) => v + photo.camera.direction[i] * 0.6));
    viewer.append(tip);
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'line'); line.dataset.index = photo.index;
    line.setAttribute('marker-end', 'url(#arrow)'); directions.append(line);
  }
  cameras.disabled = count === 0;
  if (!gallery.children.length) gallery.textContent = 'No scan photos were available for this area.';
  toggleCameras();
}
function updateDirections() {
  if (!viewer.loaded || !viewer.queryHotspot) return;
  for (const line of directions.querySelectorAll('line')) {
    const n = line.dataset.index;
    const start = viewer.queryHotspot(`hotspot-camera-${n}`)?.canvasPosition;
    const end = viewer.queryHotspot(`hotspot-direction-${n}`)?.canvasPosition;
    if (!start || !end) continue;
    line.setAttribute('x1', start.x); line.setAttribute('y1', start.y);
    line.setAttribute('x2', end.x); line.setAttribute('y2', end.y);
  }
}
function toggleCameras() {
  viewer.classList.toggle('hide-cameras', !cameras.checked);
  directions.toggleAttribute('hidden', !cameras.checked || viewer.hidden || measurements.enabled);
  updateDirections();
}
cameras.addEventListener('change', toggleCameras);
viewer.addEventListener('measurement-mode-change', toggleCameras);
viewer.addEventListener('camera-change', updateDirections);
new ResizeObserver(updateDirections).observe(viewer);
function showModel() {
  const model = snapshot.models[selected];
  measurements.setModel(model?.key, model?.status === 'ready');
  if (!model) { statusText.textContent = 'No textured model was available for this scope. The saved scan photos are below.'; select.hidden = true; return; }
  if (model.status === 'ready') {
    const url = `${base}/models/${selected}/glb`;
    if (viewer.getAttribute('src') !== url) {
      statusText.textContent = 'Loading the textured model…'; viewer.hidden = false;
      viewer.setAttribute('src', url); viewer.setAttribute('alt', `Textured scan: ${model.label}`);
    }
  } else {
    viewer.hidden = true; viewer.removeAttribute('src');
    statusText.textContent = model.status === 'failed'
      ? 'The desktop preview could not be prepared. The scan photos are still available. Try again later.'
      : 'Preparing a desktop version of this scan. Large homes can take a few minutes. You can view the photos below.';
  }
  toggleCameras();
}
select.addEventListener('change', () => { selected = Number(select.value); showModel(); renderPhotos(); });
viewer.addEventListener('load', () => { statusText.textContent = 'Model ready.'; updateDirections(); });
viewer.addEventListener('error', () => { statusText.textContent = 'This browser could not display the model. Try reloading in a current desktop browser.'; });
form.addEventListener('input', () => { if (!submitting) submissionId = crypto.randomUUID(); });
form.addEventListener('submit', async e => {
  e.preventDefault();
  if (submitting || !snapshot?.canRespond) return;
  const file = document.getElementById('quote-pdf').files[0];
  if (!file || !/\.pdf$/i.test(file.name)) { uploadStatus.textContent = 'Choose a PDF quote.'; return; }
  if (file.size > snapshot.maxPdfBytes) { uploadStatus.textContent = 'The PDF must be 25 MB or smaller.'; return; }
  const name = document.getElementById('provider-name').value.trim();
  if (!name) { uploadStatus.textContent = 'Enter your business name.'; return; }
  const params = new URLSearchParams({submission_id: submissionId, provider_name: name, file_name: file.name});
  const price = document.getElementById('quote-price').value;
  if (!price.trim() || !Number.isFinite(Number(price)) || Number(price) < 0) { uploadStatus.textContent = 'Enter the total price in USD.'; return; }
  params.set('price_usd', price);
  submitting = true; document.getElementById('quote-fields').disabled = true;
  uploadStatus.textContent = 'Uploading your quote…';
  try {
    const response = await fetch(`${base}/responses?${params}`, {method: 'POST', headers: {'Content-Type': 'application/pdf', 'X-Upload-Token': snapshot.uploadToken}, body: file, referrerPolicy: 'no-referrer'});
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Your quote could not be sent. Check the form and try again.');
    uploadStatus.textContent = 'Quote received. Your PDF is now available in the homeowner’s dashboard.';
    form.reset(); submissionId = crypto.randomUUID();
  } catch (error) { uploadStatus.textContent = error.message || 'Connection interrupted. Retry to finish the same submission.'; }
  finally { submitting = false; document.getElementById('quote-fields').disabled = !snapshot.canRespond; }
});
async function refresh() {
  try {
    const response = await fetch(`${base}/status`, {cache: 'no-store', referrerPolicy: 'no-referrer'});
    if (!response.ok) {
      statusText.textContent = response.status === 410 ? 'This private link has expired. Ask TakeShape for a new email.' : 'This scan is unavailable. Please request a new link from TakeShape.';
      document.getElementById('quote-fields').disabled = true;
      document.getElementById('revision').textContent = ''; return;
    }
    snapshot = await response.json();
    if (!submitting) document.getElementById('quote-fields').disabled = !snapshot.canRespond;
    if (!snapshot.canRespond) uploadStatus.textContent = 'This quote request is closed.';
    if (!rendered) {
      rendered = true;
      document.getElementById('revision').textContent = `Link expires ${new Date(snapshot.expiresAt * 1000).toLocaleDateString()}`;
      for (const m of snapshot.models) { const option = document.createElement('option'); option.value = m.index; option.textContent = m.label; select.appendChild(option); }
      renderPhotos();
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
