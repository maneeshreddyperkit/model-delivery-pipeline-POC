/*
 * Model viewer.
 *
 * Loads the GLB the pipeline actually published, renders it, and resolves
 * clicks back to catalog rows. The point is to prove the delivery is real:
 * the file opens, components are individually selectable, and the tags in
 * the geometry line up with the engineering attributes in SQL.
 *
 * Everything is served from this machine - three.js is vendored locally and
 * the model comes from the delivery share.
 */

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';

const host = document.getElementById('canvas-host');
const statusEl = document.getElementById('status');
const hudEl = document.getElementById('hud');
const treeEl = document.getElementById('tree');
const selectionEl = document.getElementById('selection');
const metaEl = document.getElementById('model-meta');
const legendEl = document.getElementById('legend-box');
const modelSelect = document.getElementById('model-select');
const lodSelect = document.getElementById('lod-select');
const filterInput = document.getElementById('tag-filter');
const isolateBtn = document.getElementById('isolate');
const resetBtn = document.getElementById('reset-view');

const HIGHLIGHT = new THREE.Color(0xffc82e);

let renderer, scene, camera, controls, root;
let currentModel = modelSelect.value;
let components = [];
let nodesByTag = new Map();
let selectedTag = null;
let selectedRestore = [];
let isolated = false;
let modelInfo = null;
let lastLoadMs = 0;
let lastDownloadBytes = 0;

// ---------------------------------------------------------------- scene

function initScene() {
  renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(host.clientWidth, host.clientHeight);
  renderer.setClearColor(0x0a0d12);
  host.appendChild(renderer.domElement);

  scene = new THREE.Scene();
  scene.fog = new THREE.Fog(0x0a0d12, 60000, 260000);

  camera = new THREE.PerspectiveCamera(50, host.clientWidth / host.clientHeight, 10, 800000);
  camera.position.set(40000, 30000, 40000);

  controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.screenSpacePanning = false;

  scene.add(new THREE.HemisphereLight(0xbfd4ff, 0x30363d, 2.1));
  const key = new THREE.DirectionalLight(0xffffff, 2.0);
  key.position.set(1, 2, 1.4);
  scene.add(key);
  const fill = new THREE.DirectionalLight(0x9db4d0, 0.8);
  fill.position.set(-1.2, 0.6, -1);
  scene.add(fill);

  window.addEventListener('resize', onResize);
  renderer.domElement.addEventListener('pointerdown', onPointerDown);
  animate();
}

function onResize() {
  if (!renderer) return;
  camera.aspect = host.clientWidth / host.clientHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(host.clientWidth, host.clientHeight);
}

let hudFrame = 0;

function animate() {
  requestAnimationFrame(animate);
  controls.update();
  renderer.render(scene, camera);

  // renderer.info counters are populated by the render call and reset on
  // the next one, so the HUD has to be read after a frame has actually
  // been drawn rather than straight after the model finishes loading.
  if (++hudFrame % 15 === 0) renderHud();
}

// ---------------------------------------------------------------- loading

async function loadModel(modelPath, lodName) {
  statusEl.style.display = 'flex';
  statusEl.textContent = 'Loading model...';

  const info = await (await fetch(`/api/model/${modelPath}`)).json();
  if (info.error) {
    statusEl.textContent = info.error;
    return;
  }
  modelInfo = info;

  // Populate the LOD picker from the published manifest the first time.
  if (!lodName) {
    lodSelect.innerHTML = '';
    for (const lod of info.lods) {
      const opt = document.createElement('option');
      opt.value = lod.file.replace(/\.glb$/, '');
      opt.textContent = `${lod.name} \u2014 ${lod.rendered_triangles.toLocaleString()} triangles`;
      lodSelect.appendChild(opt);
    }
    lodName = info.lods.length ? info.lods[0].file.replace(/\.glb$/, '') : 'lod0';
    lodSelect.value = lodName;
  }

  renderMeta(info);

  const tree = await (await fetch(`/api/model/${modelPath}/tree`)).json();
  components = tree.components || [];
  renderTree('');

  if (root) {
    scene.remove(root);
    disposeTree(root);
    root = null;
  }

  const url = `/api/model/${modelPath}/geometry/${lodName}.glb`;
  const started = performance.now();

  new GLTFLoader().load(url, (gltf) => {
    root = gltf.scene;
    scene.add(root);
    indexNodes();
    frameModel();
    statusEl.style.display = 'none';
    lastLoadMs = performance.now() - started;
    renderLegend();
    if (selectedTag) highlight(selectedTag, false);
  }, (progress) => {
    lastDownloadBytes = progress.loaded || lastDownloadBytes;
    if (progress.total) {
      statusEl.textContent =
        `Loading model... ${Math.round(100 * progress.loaded / progress.total)}%`;
    }
  }, (err) => {
    statusEl.textContent = 'Failed to load geometry: ' + err;
  });
}

function disposeTree(object) {
  object.traverse((child) => {
    if (child.isMesh) {
      child.geometry?.dispose();
      if (Array.isArray(child.material)) child.material.forEach(m => m.dispose());
      else child.material?.dispose();
    }
  });
}

function indexNodes() {
  nodesByTag = new Map();
  root.traverse((child) => {
    if (!child.isMesh || !child.name) return;
    if (!nodesByTag.has(child.name)) nodesByTag.set(child.name, []);
    nodesByTag.get(child.name).push(child);
  });
}

function frameModel() {
  const box = new THREE.Box3().setFromObject(root);
  if (box.isEmpty()) return;
  const size = box.getSize(new THREE.Vector3());
  const centre = box.getCenter(new THREE.Vector3());
  const radius = Math.max(size.x, size.y, size.z) || 1000;

  controls.target.copy(centre);
  camera.position.set(centre.x + radius * 0.9, centre.y + radius * 0.75, centre.z + radius * 0.9);
  camera.near = radius / 500;
  camera.far = radius * 60;
  camera.updateProjectionMatrix();
  scene.fog.near = radius * 2.0;
  scene.fog.far = radius * 9.0;
  controls.update();
}

// ---------------------------------------------------------------- panels

function renderMeta(info) {
  const saved = info.naive_bytes
    ? (100 * (info.naive_bytes - info.published_bytes) / info.naive_bytes).toFixed(1)
    : '0';
  metaEl.innerHTML = `
    <table class="attr-table">
      <tr><td>Model</td><td>${escapeHtml(info.model_name || '')}</td></tr>
      <tr><td>Discipline</td><td>${escapeHtml(info.discipline || '-')}</td></tr>
      <tr><td>Revision</td><td><span class="badge b-published">${escapeHtml(info.revision_label)}</span></td></tr>
      <tr><td>Published</td><td class="mono">${escapeHtml(info.published_at || '')}</td></tr>
      <tr><td>Components</td><td class="mono">${(info.component_count || 0).toLocaleString()}</td></tr>
      <tr><td>Triangles</td><td class="mono">${(info.triangle_count || 0).toLocaleString()}</td></tr>
      <tr><td>Delivered</td><td class="mono">${fmtBytes(info.published_bytes)} (${saved}% under naive)</td></tr>
      <tr><td>Over the wire</td><td class="mono">${fmtBytes(info.wire_bytes)}</td></tr>
      <tr><td>QA</td><td>${info.qa?.errors ? `<span class="badge b-error">${info.qa.errors} error</span>` : ''}
        ${info.qa?.warnings ? `<span class="badge b-warn">${info.qa.warnings} warning</span>` : ''}
        ${!info.qa?.errors && !info.qa?.warnings ? '<span class="badge b-ok">clean</span>' : ''}</td></tr>
    </table>`;
}

function renderTree(filter) {
  const needle = filter.trim().toLowerCase();
  const physical = components.filter(c => c.has_geometry);
  const matched = needle
    ? physical.filter(c => c.tag.toLowerCase().includes(needle) ||
                           (c.category || '').toLowerCase().includes(needle))
    : physical;

  const byCategory = new Map();
  for (const c of matched) {
    const key = c.category || 'Uncategorised';
    if (!byCategory.has(key)) byCategory.set(key, []);
    byCategory.get(key).push(c);
  }

  const parts = [];
  const shown = 400;
  let count = 0;
  for (const [category, items] of [...byCategory.entries()].sort()) {
    parts.push(`<div class="tree-group">${escapeHtml(category)} (${items.length})</div>`);
    for (const c of items) {
      if (count++ > shown) break;
      parts.push(
        `<div class="tree-node" data-tag="${escapeHtml(c.tag)}" title="${escapeHtml(c.tag)}">` +
        `${escapeHtml(c.tag)}</div>`);
    }
  }
  if (count > shown) {
    parts.push(`<div class="msg" style="padding:8px 6px">` +
      `Showing first ${shown} of ${matched.length.toLocaleString()}. Refine the filter.</div>`);
  }
  if (!matched.length) parts.push('<div class="msg" style="padding:8px 6px">No matches.</div>');

  treeEl.innerHTML = parts.join('');
  treeEl.querySelectorAll('.tree-node').forEach(node => {
    node.addEventListener('click', () => select(node.dataset.tag, true));
  });
}

function renderHud() {
  const info = renderer.info;
  hudEl.innerHTML =
    `draw calls   ${info.render.calls.toLocaleString()}<br>` +
    `triangles    ${info.render.triangles.toLocaleString()}<br>` +
    `geometries   ${info.memory.geometries.toLocaleString()}<br>` +
    `downloaded   ${fmtBytes(lastDownloadBytes)}<br>` +
    `loaded in    ${Math.round(lastLoadMs)} ms`;
}

function renderLegend() {
  const categories = new Map();
  root.traverse((child) => {
    if (child.isMesh && child.material?.name) {
      categories.set(child.material.name, child.material.color.getHexString());
    }
  });
  if (!categories.size) { legendEl.innerHTML = ''; return; }
  legendEl.innerHTML = '<h2>Categories</h2><div class="legend">' +
    [...categories.entries()].sort().map(([name, hex]) =>
      `<span><i style="background:#${hex}"></i>${escapeHtml(name)}</span>`).join('') +
    '</div>';
}

// ---------------------------------------------------------------- selection

function clearHighlight() {
  for (const { mesh, material } of selectedRestore) mesh.material = material;
  selectedRestore = [];
}

function highlight(tag, moveCamera) {
  clearHighlight();
  const meshes = nodesByTag.get(tag);
  if (!meshes || !meshes.length) return;

  for (const mesh of meshes) {
    selectedRestore.push({ mesh, material: mesh.material });
    const hot = mesh.material.clone();
    hot.color = HIGHLIGHT.clone();
    hot.emissive = new THREE.Color(0x6a4d00);
    mesh.material = hot;
  }

  if (moveCamera) {
    const box = new THREE.Box3();
    for (const mesh of meshes) box.expandByObject(mesh);
    if (!box.isEmpty()) {
      const centre = box.getCenter(new THREE.Vector3());
      const size = box.getSize(new THREE.Vector3());
      const radius = Math.max(size.x, size.y, size.z, 500);
      const direction = camera.position.clone().sub(controls.target).normalize();
      controls.target.copy(centre);
      camera.position.copy(centre).add(direction.multiplyScalar(radius * 5));
      controls.update();
    }
  }
}

async function select(tag, moveCamera) {
  selectedTag = tag;
  highlight(tag, moveCamera);
  isolateBtn.disabled = false;

  treeEl.querySelectorAll('.tree-node').forEach(n =>
    n.classList.toggle('sel', n.dataset.tag === tag));

  selectionEl.innerHTML = '<p class="msg">Loading attributes...</p>';
  const res = await fetch(`/api/model/${currentModel}/component/${encodeURIComponent(tag)}`);
  const data = await res.json();
  if (data.error) {
    selectionEl.innerHTML = `<p class="msg err-text">${escapeHtml(data.error)}</p>`;
    return;
  }

  const c = data.component;
  const dup = data.duplicate_tag_count > 1
    ? `<div class="note" style="margin:10px 0"><strong>Duplicate tag.</strong>
       ${data.duplicate_tag_count} components share this tag, so attribute lookups
       against it are ambiguous.</div>` : '';

  selectionEl.innerHTML = `
    <div class="tag-pill" style="margin-bottom:10px">${escapeHtml(c.tag)}</div>
    ${dup}
    <table class="attr-table">
      <tr><td>Category</td><td>${escapeHtml(c.category || '-')}</td></tr>
      <tr><td>Discipline</td><td>${escapeHtml(c.discipline || '-')}</td></tr>
      <tr><td>Parent</td><td class="mono">${escapeHtml(c.parent_tag || '-')}</td></tr>
      <tr><td>Triangles</td><td class="mono">${(c.triangle_count || 0).toLocaleString()}</td></tr>
    </table>
    <h2 style="margin-top:16px">Engineering attributes</h2>
    <p class="msg" style="margin-bottom:8px">Read from the catalog, not the geometry file.</p>
    <table class="attr-table">
      ${data.attributes.map(a =>
        `<tr><td>${escapeHtml(a.name)}</td><td>${escapeHtml(a.value)}</td></tr>`).join('')}
    </table>
    <h2 style="margin-top:16px">Hierarchy path</h2>
    <div class="msg mono" style="word-break:break-all">${escapeHtml(c.path || '')}</div>`;
}

function onPointerDown(event) {
  if (!root || event.button !== 0) return;
  const rect = renderer.domElement.getBoundingClientRect();
  const pointer = new THREE.Vector2(
    ((event.clientX - rect.left) / rect.width) * 2 - 1,
    -((event.clientY - rect.top) / rect.height) * 2 + 1);

  const raycaster = new THREE.Raycaster();
  raycaster.setFromCamera(pointer, camera);
  const hits = raycaster.intersectObject(root, true);
  const hit = hits.find(h => h.object.isMesh && h.object.visible && h.object.name);
  if (hit) select(hit.object.name, false);
}

// ---------------------------------------------------------------- controls

isolateBtn.addEventListener('click', () => {
  if (!root || !selectedTag) return;
  isolated = !isolated;
  const keep = new Set(nodesByTag.get(selectedTag) || []);
  root.traverse((child) => {
    if (child.isMesh) child.visible = !isolated || keep.has(child);
  });
  isolateBtn.textContent = isolated ? 'Show all' : 'Isolate';
});

resetBtn.addEventListener('click', () => {
  isolated = false;
  isolateBtn.textContent = 'Isolate';
  if (root) root.traverse((c) => { if (c.isMesh) c.visible = true; });
  frameModel();
});

modelSelect.addEventListener('change', () => {
  currentModel = modelSelect.value;
  selectedTag = null;
  selectedRestore = [];
  history.replaceState({}, '', `/viewer/${currentModel}`);
  loadModel(currentModel, null);
});

lodSelect.addEventListener('change', () => loadModel(currentModel, lodSelect.value));
filterInput.addEventListener('input', () => renderTree(filterInput.value));

// ---------------------------------------------------------------- helpers

function escapeHtml(v) {
  return String(v ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function fmtBytes(n) {
  n = Number(n || 0);
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${i === 0 ? n.toFixed(0) : n.toFixed(1)} ${units[i]}`;
}

initScene();
loadModel(currentModel, null);
