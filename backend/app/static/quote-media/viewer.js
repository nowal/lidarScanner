'use strict';
const base = location.pathname.replace(/\/$/, '');
const viewer = document.getElementById('viewer');
const select = document.getElementById('models');
const statusText = document.getElementById('status');
const cameras = document.getElementById('cameras');
const gallery = document.getElementById('photo-gallery');
let cameraFrustums = [], directionFrame = 0;
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
  photoOpener = opener || photoOpener;
  document.getElementById('photo-title').textContent = `Photo ${photo.index + 1} · ${photo.label}`;
  document.getElementById('photo-error').hidden = true;
  photoImage.alt = photo.label;
  photoImage.src = `${base}/photos/${photo.index}/full`;
  const list = snapshot.photos;
  document.getElementById('previous-photo').disabled = list.length < 2;
  document.getElementById('next-photo').disabled = list.length < 2;
  for (const button of document.querySelectorAll('.camera-pin, .photo-card')) button.classList.toggle('selected', Number(button.dataset.index) === photo.index);
  if (!dialog.open) dialog.showModal();
}
function stepPhoto(step) {
  const list = snapshot.photos;
  const index = list.findIndex(p => p.index === activePhoto.index);
  showPhoto(list[(index + step + list.length) % list.length]);
}
document.getElementById('close-photo').addEventListener('click', () => dialog.close());
document.getElementById('previous-photo').addEventListener('click', () => stepPhoto(-1));
document.getElementById('next-photo').addEventListener('click', () => stepPhoto(1));
dialog.addEventListener('close', () => {
  photoImage.removeAttribute('src');
  (photoOpener?.isConnected && photoOpener.getClientRects().length ? photoOpener : gallery.querySelector('.photo-card'))?.focus();
});
dialog.addEventListener('keydown', e => {
  if (e.key === 'ArrowLeft') { e.preventDefault(); stepPhoto(-1); }
  if (e.key === 'ArrowRight') { e.preventDefault(); stepPhoto(1); }
});
photoImage.addEventListener('error', () => { document.getElementById('photo-error').hidden = false; });
function validCamera(photo) {
  return photo.camera && ['position', 'direction'].every(key =>
    Array.isArray(photo.camera[key]) && photo.camera[key].length === 3 && photo.camera[key].every(Number.isFinite)) &&
    Math.hypot(...photo.camera.direction) > 1e-6;
}
// Deriving a private thumbnail can require reading the capture archive. Request
// only nearby cards, one at a time, instead of flooding the on-demand worker.
const pendingThumbnails = new Set();
let thumbnailLoading = false;
function loadNextThumbnail() {
  if (thumbnailLoading || !pendingThumbnails.size) return;
  const card = pendingThumbnails.values().next().value;
  pendingThumbnails.delete(card);
  thumbnailLoading = true;
  const img = card.querySelector('img');
  const finish = failed => {
    card.classList.add(failed ? 'preview-failed' : 'preview-ready');
    card.querySelector('.photo-preview-note').textContent = failed ? 'Preview unavailable · Open photo' : '';
    thumbnailLoading = false;
    loadNextThumbnail();
  };
  img.addEventListener('load', () => finish(false), {once: true});
  img.addEventListener('error', () => finish(true), {once: true});
  img.src = `${base}/photos/${card.dataset.index}/thumb`;
}
const thumbnailObserver = new IntersectionObserver(entries => {
  for (const entry of entries) {
    const card = entry.target;
    if (entry.isIntersecting && !card.querySelector('img').hasAttribute('src')) pendingThumbnails.add(card);
    else pendingThumbnails.delete(card);
  }
  loadNextThumbnail();
}, {rootMargin: '200px'});
function renderGallery() {
  const cards = document.createDocumentFragment();
  for (const photo of snapshot.photos) {
    const item = document.createElement('li');
    const card = document.createElement('button'); card.type = 'button'; card.className = 'photo-card'; card.dataset.index = photo.index;
    card.setAttribute('aria-label', `Open photo ${photo.index + 1}: ${photo.label}`);
    const preview = document.createElement('span'); preview.className = 'photo-preview';
    const img = document.createElement('img'); img.alt = ''; img.decoding = 'async'; img.referrerPolicy = 'no-referrer';
    const note = document.createElement('span'); note.className = 'photo-preview-note'; note.textContent = 'Loading photo…'; note.setAttribute('aria-hidden', 'true');
    preview.append(img, note);
    const caption = document.createElement('span'); caption.className = 'photo-caption';
    const number = document.createElement('span'); number.className = 'photo-number'; number.textContent = String(photo.index + 1).padStart(2, '0');
    const label = document.createElement('span'); label.className = 'photo-label'; label.textContent = photo.label;
    if (photo.supplemental) {
      const kind = document.createElement('span'); kind.className = 'photo-kind'; kind.textContent = 'Homeowner photo'; label.append(kind);
    }
    caption.append(number, label); card.append(preview, caption); item.append(card); cards.append(item);
    card.addEventListener('click', () => showPhoto(photo, card));
    thumbnailObserver.observe(card);
  }
  gallery.replaceChildren(cards);
  document.getElementById('photo-count').textContent = `${snapshot.photos.length} photos`;
  document.getElementById('gallery-status').textContent = snapshot.photos.length ? 'Photos in scan order. Select a photo to open it.' : 'No photos are available for this scan.';
}
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const normalize = v => v.map(n => n / Math.hypot(...v));
const svgElement = tag => document.createElementNS('http://www.w3.org/2000/svg', tag);
function renderHotspots() {
  viewer.querySelectorAll('.camera-pin, .frustum-anchor').forEach(el => el.remove());
  cameraFrustums = [];
  const pins = document.createDocumentFragment();
  for (const photo of modelPhotos()) {
    if (!validCamera(photo)) continue;
    const pin = document.createElement('button'); pin.type = 'button'; pin.className = 'camera-pin';
    pin.slot = `hotspot-camera-${photo.index}`; pin.dataset.index = photo.index;
    pin.dataset.position = vector(photo.camera.position);
    const badge = document.createElement('span'); badge.className = 'pin-label'; badge.textContent = photo.index + 1;
    const icon = svgElement('svg'); icon.classList.add('camera-frustum'); icon.setAttribute('viewBox', '-22 -22 44 44'); icon.setAttribute('aria-hidden', 'true');
    const face = svgElement('path'); face.classList.add('frustum-face');
    const edges = svgElement('path'); edges.classList.add('frustum-edges');
    icon.append(face, edges); pin.append(icon, badge);
    pin.title = `Open photo ${photo.index + 1}: ${photo.label}`; pin.setAttribute('aria-label', pin.title);
    pin.addEventListener('click', e => { e.stopPropagation(); showPhoto(photo, pin); });
    pin.classList.toggle('selected', activePhoto?.index === photo.index);
    pins.append(pin);
    // An illustrative perspective camera: apex at the saved camera position,
    // rectangular image plane facing along its normalized viewing direction.
    // Project all four corners through model-viewer's public hotspot API so
    // orbit, pan, perspective, and vertical camera directions stay aligned.
    const forward = normalize(photo.camera.direction);
    const right = normalize(cross(forward, Math.abs(forward[1]) > .98 ? [0, 0, 1] : [0, 1, 0]));
    const up = cross(right, forward);
    const corners = [[-1, -1], [1, -1], [1, 1], [-1, 1]].map(([x, y], i) => {
      const corner = document.createElement('span'); corner.className = 'frustum-anchor'; corner.slot = `hotspot-frustum-${photo.index}-${i}`;
      corner.dataset.position = vector(photo.camera.position.map((v, axis) => v + .28 * (forward[axis] + x * .65 * right[axis] + y * .45 * up[axis])));
      pins.append(corner); return corner.slot;
    });
    cameraFrustums.push({pin, corners, face, edges});
  }
  viewer.append(pins);
  cameras.disabled = cameraFrustums.length === 0;
  toggleCameras();
}
function scheduleDirections() {
  if (!directionFrame && cameras.checked && !viewer.hidden && !measurements.enabled) directionFrame = requestAnimationFrame(() => {
    directionFrame = 0; updateDirections();
  });
}
function updateDirections() {
  if (!cameras.checked || viewer.hidden || measurements.enabled || !viewer.loaded || !viewer.queryHotspot) return;
  for (const {pin, corners, face, edges} of cameraFrustums) {
    const apex = viewer.queryHotspot(pin.slot)?.canvasPosition;
    const points = corners.map(slot => viewer.queryHotspot(slot)?.canvasPosition);
    const valid = [apex, ...points].every(p => p && Number.isFinite(p.x) && Number.isFinite(p.y));
    pin.hidden = !valid;
    if (!valid) continue;
    const offsets = points.map(p => [p.x - apex.x, p.y - apex.y]);
    const extent = Math.max(...offsets.flat().map(Math.abs));
    // A readable 22–38px glyph inside a stable 44px pointer/touch target.
    // Only the glyph is scaled; its apex stays at the actual camera location.
    const scale = Math.min(19, Math.max(11, extent)) / Math.max(extent, .001);
    const coords = offsets.map(p => p.map(n => (n * scale).toFixed(2)).join(','));
    face.setAttribute('d', `M${coords.join(' L')} Z`);
    edges.setAttribute('d', coords.map(p => `M0,0 L${p}`).join(' '));
  }
}
function toggleCameras() {
  viewer.classList.toggle('hide-cameras', !cameras.checked);
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
    statusText.textContent = 'No textured model was available for this scope. The scan photos are available below the quote form.';
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
      : 'Preparing a desktop version of this scan. Large homes can take a few minutes. The scan photos are available below the quote form.';
  }
  toggleCameras();
}
select.addEventListener('change', () => { selected = Number(select.value); showModel(); renderHotspots(); });
viewer.addEventListener('load', () => { statusText.textContent = 'Model ready.'; scheduleDirections(); });
viewer.addEventListener('error', () => { statusText.textContent = 'This browser could not display the model. The scan photos are available below the quote form, or reload to try again.'; });
form.addEventListener('input', () => { if (!submitting) submissionId = crypto.randomUUID(); });
form.addEventListener('submit', async e => {
  e.preventDefault();
  if (submitting || !snapshot?.canRespond) return;
  const file = document.getElementById('quote-pdf').files[0];
  if (!file || !/\.pdf$/i.test(file.name)) { uploadStatus.textContent = 'Choose a PDF quote.'; return; }
  if (file.size > snapshot.maxPdfBytes) { uploadStatus.textContent = 'The PDF must be 25 MB or smaller.'; return; }
  const name = document.getElementById('provider-name').value.trim();
  if (!name) { uploadStatus.textContent = 'Enter your business name.'; return; }
  const email = document.getElementById('provider-email');
  if (!email.value.trim() || !email.checkValidity()) { uploadStatus.textContent = 'Enter your business email address.'; return; }
  const params = new URLSearchParams({submission_id: submissionId, provider_name: name, provider_email: email.value.trim(), file_name: file.name});
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
      document.getElementById('revision').textContent = '';
      document.getElementById('gallery-status').textContent = 'Photos are unavailable. Please request a new link.'; return;
    }
    snapshot = await response.json();
    if (!submitting) document.getElementById('quote-fields').disabled = !snapshot.canRespond;
    if (!snapshot.canRespond) uploadStatus.textContent = 'This quote request is closed.';
    if (!rendered) {
      rendered = true;
      document.getElementById('revision').textContent = `Link expires ${new Date(snapshot.expiresAt * 1000).toLocaleDateString()}`;
      for (const m of snapshot.models) { const option = document.createElement('option'); option.value = m.index; option.textContent = m.label; select.appendChild(option); }
      renderGallery(); renderHotspots();
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
