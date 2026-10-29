'use strict';
const base = location.pathname.replace(/\/$/, '');
const viewer = document.getElementById('viewer');
const select = document.getElementById('models');
const statusText = document.getElementById('status');
const cameras = document.getElementById('cameras');
const directions = document.getElementById('directions');
const photoPicker = document.getElementById('photo-picker');
const openPhoto = document.getElementById('open-photo');
let directionLines = [], directionFrame = 0, directionPhoto = null;
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
  return snapshot.photos.filter(p => p.supplemental || !p.roomKey || !model || model.key === 'home' || p.roomKey === model.key);
}
function showPhoto(photo, opener) {
  if (!photo) return;
  activePhoto = photo;
  directionPhoto = photo.index;
  scheduleDirections();
  photoPicker.value = photo.index;
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
dialog.addEventListener('close', () => {
  photoImage.removeAttribute('src');
  (photoOpener?.isConnected && photoOpener.getClientRects().length ? photoOpener : openPhoto).focus();
});
openPhoto.addEventListener('click', () => showPhoto(modelPhotos().find(p => p.index === Number(photoPicker.value)), openPhoto));
dialog.addEventListener('keydown', e => {
  if (e.key === 'ArrowLeft') { e.preventDefault(); stepPhoto(-1); }
  if (e.key === 'ArrowRight') { e.preventDefault(); stepPhoto(1); }
});
photoImage.addEventListener('error', () => { document.getElementById('photo-error').hidden = false; });
function validCamera(photo) {
  return photo.camera && ['position', 'direction'].every(key =>
    Array.isArray(photo.camera[key]) && photo.camera[key].length === 3 && photo.camera[key].every(Number.isFinite));
}
function renderPhotoControls() {
  photoPicker.replaceChildren();
  const photos = modelPhotos();
  for (const photo of photos) {
    const option = document.createElement('option'); option.value = photo.index;
    const kind = photo.supplemental ? 'Homeowner photo' : !validCamera(photo) ? 'Unpositioned photo' : 'Scan photo';
    option.textContent = `${photo.index + 1} · ${kind}: ${photo.label}`;
    photoPicker.append(option);
  }
  photoPicker.disabled = openPhoto.disabled = photos.length === 0;
  if (!photos.length) {
    const option = document.createElement('option'); option.textContent = 'No photos available'; photoPicker.append(option);
  }
}
function renderHotspots() {
  viewer.querySelectorAll('.camera-pin, .direction-point').forEach(el => el.remove());
  directionLines.forEach(el => el.remove()); directionLines = [];
  const pins = document.createDocumentFragment(), arrows = document.createDocumentFragment();
  for (const photo of modelPhotos()) {
    if (!validCamera(photo)) continue;
    const pin = document.createElement('button'); pin.type = 'button'; pin.className = 'camera-pin';
    pin.slot = `hotspot-camera-${photo.index}`; pin.dataset.index = photo.index;
    pin.dataset.position = vector(photo.camera.position);
    const badge = document.createElement('span'); badge.className = 'pin-label'; badge.textContent = photo.index + 1;
    pin.append(badge);
    pin.title = `Open photo ${photo.index + 1}: ${photo.label}`; pin.setAttribute('aria-label', pin.title);
    pin.addEventListener('click', e => { e.stopPropagation(); showPhoto(photo, pin); });
    for (const event of ['pointerenter', 'focus']) pin.addEventListener(event, () => {
      directionPhoto = photo.index; scheduleDirections();
    });
    for (const event of ['pointerleave', 'blur']) pin.addEventListener(event, () => {
      directionPhoto = activePhoto?.index ?? null; scheduleDirections();
    });
    pins.append(pin);
    const tip = document.createElement('span'); tip.slot = `hotspot-direction-${photo.index}`; tip.className = 'direction-point';
    tip.dataset.position = vector(photo.camera.position.map((v, i) => v + photo.camera.direction[i] * 0.6));
    pins.append(tip);
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'line'); line.dataset.index = photo.index;
    line.setAttribute('marker-end', 'url(#arrow)'); arrows.append(line); directionLines.push(line);
  }
  viewer.append(pins); directions.append(arrows);
  cameras.disabled = directionLines.length === 0;
  toggleCameras();
}
function scheduleDirections() {
  if (!directionFrame && !directions.hidden) directionFrame = requestAnimationFrame(() => {
    directionFrame = 0; updateDirections();
  });
}
function updateDirections() {
  if (directions.hidden || !viewer.loaded || !viewer.queryHotspot) return;
  for (const line of directionLines) {
    const n = line.dataset.index;
    line.style.display = Number(n) === directionPhoto ? '' : 'none';
    if (Number(n) !== directionPhoto) continue;
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
  scheduleDirections();
}
cameras.addEventListener('change', toggleCameras);
viewer.addEventListener('measurement-mode-change', toggleCameras);
viewer.addEventListener('camera-change', scheduleDirections);
new ResizeObserver(scheduleDirections).observe(viewer);
function showModel() {
  const model = snapshot.models[selected];
  measurements.setModel(model?.key, model?.status === 'ready');
  if (!model) {
    viewer.hidden = true; viewer.removeAttribute('src'); select.disabled = true;
    statusText.textContent = 'No textured model was available for this scope. Use Photos to view the saved images.';
    toggleCameras(); return;
  }
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
      : 'Preparing a desktop version of this scan. Large homes can take a few minutes. Use Photos to view the saved images.';
  }
  toggleCameras();
}
select.addEventListener('change', () => { selected = Number(select.value); showModel(); renderPhotoControls(); renderHotspots(); });
viewer.addEventListener('load', () => { statusText.textContent = 'Model ready.'; scheduleDirections(); });
viewer.addEventListener('error', () => { statusText.textContent = 'This browser could not display the model. Use Photos to view the saved images, or reload to try again.'; });
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
      renderPhotoControls(); renderHotspots();
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
