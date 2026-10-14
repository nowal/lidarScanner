'use strict';

// GLB positions are in metres (including the USD stage's unit conversion).
// Keep each area's measurements separate; nothing is written to the scan.
class ModelMeasurements {
  constructor(viewer) {
    this.viewer = viewer;
    this.overlay = document.getElementById('measurement-lines');
    this.toggle = document.getElementById('measure');
    this.cancel = document.getElementById('cancel-point');
    this.remove = document.getElementById('delete-measurement');
    this.clear = document.getElementById('clear-measurements');
    this.hint = document.getElementById('measurement-hint');
    this.list = document.getElementById('measurement-list');
    this.areas = new Map();
    this.items = [];
    this.enabled = false;
    this.ready = false;
    this.nextID = 1;
    this.pointers = new Set();
    this.toggle.addEventListener('click', () => this.setEnabled(!this.enabled));
    this.cancel.addEventListener('click', () => this.cancelPoint());
    this.remove.addEventListener('click', () => {
      this.items.splice(this.items.findIndex(m => m.id === this.selected), 1);
      this.selected = null;
      this.render();
    });
    this.clear.addEventListener('click', () => {
      this.items.length = 0;
      this.selected = null;
      this.render();
    });
    viewer.addEventListener('pointerdown', e => this.pointerDown(e), true);
    viewer.addEventListener('pointermove', e => this.pointerMove(e), true);
    viewer.addEventListener('pointerup', e => this.pointerUp(e), true);
    viewer.addEventListener('pointercancel', e => this.pointerUp(e, true), true);
    viewer.addEventListener('lostpointercapture', () => { this.drag = null; this.tap = null; });
    viewer.addEventListener('camera-change', () => this.project());
    viewer.addEventListener('load', () => { this.ready = true; this.controls(); this.scheduleProjection(); });
    viewer.addEventListener('error', () => { this.ready = false; this.setEnabled(false); });
    viewer.addEventListener('keydown', e => {
      if (e.key === 'Escape' && this.enabled) {
        e.preventDefault();
        if (this.items.some(m => !m.b)) this.cancelPoint();
        else this.setEnabled(false);
      }
    });
    new ResizeObserver(() => this.scheduleProjection()).observe(viewer);
    this.controls();
  }

  setModel(key, available) {
    if (this.key !== key) {
      this.setEnabled(false);
      this.key = key;
      if (!this.areas.has(key)) this.areas.set(key, []);
      this.items = this.areas.get(key);
      this.selected = null;
      this.ready = false;
      this.render();
    }
    if (!available) { this.ready = false; this.setEnabled(false); }
    this.controls();
  }

  setEnabled(value) {
    this.enabled = value && this.ready;
    this.viewer.classList.toggle('measuring', this.enabled);
    this.viewer.toggleAttribute('disable-tap', this.enabled);
    this.viewer.touchAction = this.enabled ? 'none' : 'pan-y';
    this.tap = null;
    this.drag = null;
    this.pointers.clear();
    if (!this.enabled) this.cancelPoint();
    this.controls();
    this.viewer.dispatchEvent(new Event('measurement-mode-change'));
  }

  cancelPoint() {
    const pending = this.items.findIndex(m => !m.b);
    if (pending !== -1) this.items.splice(pending, 1);
    this.render();
  }

  controls(message) {
    this.toggle.disabled = !this.ready;
    this.toggle.setAttribute('aria-pressed', String(this.enabled));
    this.toggle.textContent = this.enabled ? 'Done measuring' : 'Measure';
    this.cancel.hidden = !this.items.some(m => !m.b);
    this.remove.disabled = !this.items.some(m => m.b && m.id === this.selected);
    this.clear.disabled = !this.items.length;
    this.hint.textContent = message || (this.enabled
      ? (this.items.some(m => !m.b) ? 'Select the second point on the model. Drag to orbit between points.'
        : 'Select two points on the model. Drag endpoints to adjust; drag elsewhere to orbit. Cameras are hidden while measuring.')
      : '');
    this.hint.hidden = !this.hint.textContent;
  }

  pick(e) {
    const rect = this.viewer.getBoundingClientRect();
    if (e.clientX < rect.left || e.clientX > rect.right || e.clientY < rect.top || e.clientY > rect.bottom) return null;
    // model-viewer 4.1.0 getNDC takes viewport client coordinates, including scroll.
    const hit = this.viewer.positionAndNormalFromPoint(e.clientX, e.clientY);
    const p = hit && [hit.position.x, hit.position.y, hit.position.z];
    return p?.every(Number.isFinite) ? p : null;
  }

  pointerDown(e) {
    if (!this.enabled || !this.ready || e.button !== 0) return;
    this.pointers.add(e.pointerId);
    if (this.pointers.size > 1) { this.tap = null; return; }
    const endpoint = e.composedPath().find(el => el.classList?.contains('measurement-endpoint'));
    if (endpoint) {
      e.preventDefault(); e.stopImmediatePropagation();
      this.selected = Number(endpoint.dataset.id);
      this.drag = {id: this.selected, end: endpoint.dataset.end, pointer: e.pointerId};
      this.viewer.setPointerCapture(e.pointerId);
      this.updateSelection();
      return;
    }
    if (e.composedPath().some(el => el instanceof HTMLButtonElement)) return;
    this.tap = {pointer: e.pointerId, x: e.clientX, y: e.clientY, moved: false};
  }

  pointerMove(e) {
    if (this.tap?.pointer === e.pointerId && Math.hypot(e.clientX - this.tap.x, e.clientY - this.tap.y) > 6) this.tap.moved = true;
    if (this.drag?.pointer !== e.pointerId) return;
    e.preventDefault(); e.stopImmediatePropagation();
    const m = this.items.find(m => m.id === this.drag.id);
    const p = this.pick(e);
    const other = this.drag.end === 'a' ? m?.b : m?.a;
    if (!p || (other && ModelMeasurements.distance(p, other) <= 0.01)) return;
    m[this.drag.end] = p;
    this.updatePositions(m);
    this.controls(m.b ? ModelMeasurements.length(m) : undefined);
    this.scheduleProjection();
  }

  pointerUp(e, cancelled = false) {
    this.pointers.delete(e.pointerId);
    if (this.drag?.pointer === e.pointerId) {
      e.preventDefault(); e.stopImmediatePropagation();
      this.drag = null;
      if (this.viewer.hasPointerCapture(e.pointerId)) this.viewer.releasePointerCapture(e.pointerId);
      this.controls();
      return;
    }
    const tap = this.tap;
    if (tap?.pointer !== e.pointerId) return;
    this.tap = null;
    if (cancelled || tap.moved || Math.hypot(e.clientX - tap.x, e.clientY - tap.y) > 6) return;
    const p = this.pick(e);
    if (!p) { this.controls('Select a visible surface on the model.'); return; }
    const pending = this.items.find(m => !m.b);
    if (pending) {
      if (ModelMeasurements.distance(pending.a, p) <= 0.01) { this.controls('Choose a second point farther from the first.'); return; }
      pending.b = p;
      this.selected = pending.id;
    } else {
      this.items.push({id: this.nextID++, a: p});
    }
    this.render();
  }

  static distance(a, b) { return Math.hypot(...a.map((n, i) => n - b[i])); }
  static length(m) {
    const inches = Math.round(ModelMeasurements.distance(m.a, m.b) / 0.0254);
    return `${Math.floor(inches / 12)} ft ${inches % 12} in`;
  }
  static position(p) { return p.map(n => `${n}m`).join(' '); }

  render() {
    this.viewer.querySelectorAll('.measurement-hotspot').forEach(el => el.remove());
    this.overlay.replaceChildren();
    this.list.replaceChildren();
    for (const m of this.items) {
      m.nodes = {};
      for (const end of ['a', 'b']) {
        if (!m[end]) continue;
        const node = document.createElement('button');
        node.type = 'button'; node.slot = `hotspot-measure-${m.id}-${end}`;
        node.className = 'measurement-hotspot measurement-endpoint';
        node.dataset.id = m.id; node.dataset.end = end;
        node.setAttribute('aria-label', `Measurement ${m.id}, ${end === 'a' ? 'first' : 'second'} endpoint. Drag to adjust.`);
        node.addEventListener('click', e => { e.stopPropagation(); this.selected = m.id; this.updateSelection(); });
        m.nodes[end] = node;
        node.dataset.position = ModelMeasurements.position(m[end]);
        this.viewer.append(node);
      }
      if (!m.b) continue;
      const label = document.createElement('button');
      label.type = 'button'; label.slot = `hotspot-measure-${m.id}-label`;
      label.className = 'measurement-hotspot measurement-label';
      label.addEventListener('click', e => { e.stopPropagation(); this.selected = m.id; this.updateSelection(); });
      m.nodes.label = label;
      this.viewer.append(label);
      const row = document.createElement('button'); row.type = 'button';
      row.addEventListener('click', () => { this.selected = m.id; this.updateSelection(); });
      m.row = row; this.list.append(row);
      m.line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      this.overlay.append(m.line);
      this.updatePositions(m);
    }
    this.updateSelection();
    this.scheduleProjection();
  }

  updatePositions(m) {
    for (const end of ['a', 'b', 'label']) {
      const node = m.nodes[end];
      if (!node) continue;
      const p = end === 'label' ? m.a.map((v, i) => (v + m.b[i]) / 2) : m[end];
      node.dataset.position = ModelMeasurements.position(p);
      this.viewer.updateHotspot?.({name: node.slot, position: node.dataset.position});
    }
    if (m.b) {
      m.nodes.label.textContent = `${m.id} · ${ModelMeasurements.length(m)}`;
      m.row.textContent = `Measurement ${m.id}: ${ModelMeasurements.length(m)}`;
    }
  }

  updateSelection() {
    for (const m of this.items) {
      const active = m.id === this.selected;
      Object.values(m.nodes || {}).forEach(n => n.classList.toggle('selected', active));
      m.line?.classList.toggle('selected', active);
      m.row?.setAttribute('aria-pressed', String(active));
    }
    this.controls();
  }

  scheduleProjection() {
    // Hotspot registration and the renderer's matrices settle on the next frame.
    cancelAnimationFrame(this.frame);
    this.frame = requestAnimationFrame(() => { this.frame = requestAnimationFrame(() => this.project()); });
  }

  project() {
    if (!this.viewer.loaded) return;
    for (const m of this.items) {
      for (const node of Object.values(m.nodes || {})) {
        const p = this.viewer.queryHotspot(node.slot)?.canvasPosition;
        node.hidden = !p || p.z < -1 || p.z > 1;
      }
      if (!m.b) continue;
      const a = this.viewer.queryHotspot(m.nodes.a.slot)?.canvasPosition;
      const b = this.viewer.queryHotspot(m.nodes.b.slot)?.canvasPosition;
      m.line.toggleAttribute('hidden', !a || !b || m.nodes.a.hidden || m.nodes.b.hidden);
      if (!a || !b) continue;
      for (const [key, value] of Object.entries({x1: a.x, y1: a.y, x2: b.x, y2: b.y})) m.line.setAttribute(key, value);
    }
  }
}
