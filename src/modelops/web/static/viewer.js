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
const colourSelect = document.getElementById('colour-select');
const colourNote = document.getElementById('colour-note');
const timelineEl = document.getElementById('timeline');
const tlRange = document.getElementById('tl-range');
const tlDate = document.getElementById('tl-date');
const tlCounts = document.getElementById('tl-counts');
const tlPlay = document.getElementById('tl-play');
const tlOn = document.getElementById('tl-on');
const tlOff = document.getElementById('tl-off');
const focusEl = document.getElementById('focus-banner');

const HIGHLIGHT = new THREE.Color(0xffc82e);

let renderer, scene, camera, controls, root;
let currentModel = modelSelect.value;
let components = [];
let byTag = new Map();          // tag -> catalog row, for colouring
let packages = {};              // IWP -> { verdict, summary }
let nodesByTag = new Map();
let selectedTag = null;
let selectedRestore = [];
let isolated = false;
let modelInfo = null;
let lastLoadMs = 0;
let lastDownloadBytes = 0;

// Viewer state is expressed in the query string, so any view can be linked
// to: the work packages page hands off a package this way, and it is the
// same handle anything else driving the viewer would use.
const params = new URLSearchParams(location.search);

// Colouring state. Original materials are kept so Category mode restores
// exactly what the pipeline published rather than an approximation.
let originalMaterials = new Map();   // mesh -> material
// Validated against the dropdown rather than a second list, so the URL can
// only ever ask for a mode that actually exists.
let colourMode = 'category';
{
  const wanted = params.get('colour');
  if (wanted && [...colourSelect.options].some(o => o.value === wanted)) {
    colourMode = wanted;
    colourSelect.value = wanted;
  }
}
let sharedMaterials = new Map();     // colour hex -> MeshStandardMaterial
let firstLoad = true;                // query-string state applies once
let timeline = null;                 // { min, max } when the model has dates
let timelineActive = false;
let playHandle = null;
// Set when the Work Packages page hands off a package to look at. The rest
// of the plant stays on screen but ghosted, because a package floating in
// black tells you nothing about where in the unit it sits.
let focusedPackage = params.get('iwp');

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
let hudDirty = false;

function animate() {
  requestAnimationFrame(animate);
  controls.update();
  renderer.render(scene, camera);

  // renderer.info counters are populated by the render call and reset on
  // the next one, so the HUD has to be read after a frame has actually
  // been drawn rather than straight after the model finishes loading.
  // Loading sets hudDirty so the figures appear on the first frame that
  // follows rather than whenever the fifteen-frame cycle next comes round.
  if (hudDirty || ++hudFrame % 15 === 0) {
    renderHud();
    hudDirty = false;
  }
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
  packages = tree.packages || {};
  byTag = new Map(components.map(c => [c.tag, c]));
  renderTree('');
  buildTimeline();
  renderFocusBanner();
  firstLoad = false;

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
    hudDirty = true;
    applyColours();
    if (focusedPackage) framePackage();
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
  originalMaterials = new Map();
  root.traverse((child) => {
    if (!child.isMesh || !child.name) return;
    if (!nodesByTag.has(child.name)) nodesByTag.set(child.name, []);
    nodesByTag.get(child.name).push(child);
    originalMaterials.set(child, child.material);
  });
}

// ---------------------------------------------------------------- colouring
//
// Recolouring reassigns a small set of shared materials rather than cloning
// one per mesh. With several thousand components the difference is the
// difference between a viewer that stays interactive and one that does not,
// and sharing materials also lets three.js keep batching draw calls.

function sharedMaterial(hex, { ghost = false } = {}) {
  const key = `${hex}${ghost ? ':ghost' : ''}`;
  let material = sharedMaterials.get(key);
  if (!material) {
    material = new THREE.MeshStandardMaterial({
      color: new THREE.Color(hex),
      metalness: 0.1,
      roughness: 0.75,
      transparent: ghost,
      opacity: ghost ? 0.06 : 1,
      depthWrite: !ghost,
    });
    sharedMaterials.set(key, material);
  }
  return material;
}

const NO_DATA = '#3a4049';

// Grey through amber to green: the further along the chain, the warmer and
// then the greener. Ordered so a half-built model reads as a gradient
// rather than as noise.
const LIFECYCLE_COLOURS = {
  'Designed': '#454c57', 'IFC': '#5c6675', 'Procured': '#7d6f4a',
  'Fabricated': '#a58c33', 'Delivered': '#d8b528', 'Installed': '#74ad35',
  'Tested': '#45a45e', 'Commissioned': '#2f9e73',
};
const LIFECYCLE_ORDER = Object.keys(LIFECYCLE_COLOURS);

const DISCIPLINE_COLOURS = {
  'PIPING': '#4da3ff', 'STRUCTURAL': '#f0803c', 'ELECTRICAL': '#c77dff',
  'HVAC': '#4ecdc4', 'EQUIPMENT': '#ffd166', 'CIVIL': '#9aa5b1',
};

// Yellow is the attention colour everywhere else in this app, so blocked
// packages get it here too.
const READINESS_COLOURS = {
  'READY': '#43b581', 'BLOCKED': '#ffcd23', 'COMPLETE': '#3b82f6',
};

const CATEGORICAL = [
  '#4da3ff', '#f0803c', '#43b581', '#c77dff', '#4ecdc4', '#ffd166',
  '#ff6b9d', '#8fbf5f', '#b48ead', '#e07a5f', '#5fa8d3', '#d4a373',
];

function categorical(value) {
  let hash = 0;
  for (let i = 0; i < value.length; i++) hash = (hash * 31 + value.charCodeAt(i)) | 0;
  return CATEGORICAL[Math.abs(hash) % CATEGORICAL.length];
}

const COLOUR_MODES = {
  category: {
    note: 'The colours the model was published with, one per component category.',
  },
  lifecycle: {
    note: 'Where each component sits in the eight-state chain from Designed ' +
          'to Commissioned. Read from the catalog, so it is the same status ' +
          'the work package pages count.',
    key: (row) => row.lifecycle_status,
    colour: (value) => LIFECYCLE_COLOURS[value] || NO_DATA,
    order: LIFECYCLE_ORDER,
  },
  readiness: {
    note: 'Each component painted with the verdict of the package it belongs ' +
          'to. Yellow is a package a crew cannot start.',
    key: (row) => packages[row.iwp]?.verdict,
    colour: (value) => READINESS_COLOURS[value] || NO_DATA,
    order: ['READY', 'BLOCKED', 'COMPLETE'],
  },
  discipline: {
    note: 'Discipline as recorded in the catalog, which is how the model was ' +
          'split for delivery.',
    key: (row) => row.discipline,
    colour: (value) => DISCIPLINE_COLOURS[value] || categorical(value || ''),
  },
  package: {
    note: 'One colour per installation work package, so the boundaries a crew ' +
          'would actually work to are visible in the geometry.',
    key: (row) => row.iwp,
    colour: (value) => categorical(value || ''),
    collapse: 24,
  },
  commissioning: {
    note: 'Grouped by commissioning system rather than by how the model was ' +
          'drawn. This is the split that turnover cares about.',
    key: (row) => row.commissioning_system,
    colour: (value) => categorical(value || ''),
    collapse: 24,
  },
};

function applyColours() {
  if (!root) return;
  const wasSelected = selectedTag;
  clearHighlight();

  const mode = COLOUR_MODES[colourMode];
  colourNote.textContent = mode.note;

  if (colourMode === 'category' && !timelineActive && !focusedPackage) {
    for (const [mesh, material] of originalMaterials) mesh.material = material;
    renderLegend();
    if (wasSelected) highlight(wasSelected, false);
    return;
  }

  const cursor = timelineActive ? timelineCursor() : null;
  const counts = new Map();
  let planned = 0, installed = 0, slipping = 0;

  for (const [mesh, original] of originalMaterials) {
    const row = byTag.get(mesh.name);

    // Everything outside the focused package drops back to context.
    if (focusedPackage && row?.iwp !== focusedPackage) {
      mesh.material = sharedMaterial(NO_DATA, { ghost: true });
      continue;
    }

    // 4D takes priority over the palette: anything not yet due is ghosted,
    // whatever else it would have been coloured.
    if (cursor) {
      const due = row?.planned_date ? Date.parse(row.planned_date) : null;
      if (due === null || due > cursor) {
        mesh.material = sharedMaterial(NO_DATA, { ghost: true });
        continue;
      }
      planned++;
      const done = LIFECYCLE_ORDER.indexOf(row.lifecycle_status) >=
                   LIFECYCLE_ORDER.indexOf('Installed');
      if (done) installed++; else slipping++;
      // Planned by now and not installed is the only thing worth looking
      // at on a 4D playback, so it gets the attention colour.
      mesh.material = sharedMaterial(done ? '#43b581' : '#ffcd23');
      continue;
    }

    // Category mode has no palette of its own: it is whatever the pipeline
    // published, which is the point of showing it.
    if (!mode.key) { mesh.material = original; continue; }

    const value = mode.key(row || {}) || null;
    const label = value || 'Not populated';
    counts.set(label, (counts.get(label) || 0) + 1);
    mesh.material = value ? sharedMaterial(mode.colour(value))
                          : sharedMaterial(NO_DATA);
  }

  if (cursor) {
    renderTimelineLegend(planned, installed, slipping);
    tlCounts.textContent =
      `${installed.toLocaleString()} installed, ${slipping.toLocaleString()} behind`;
  } else {
    renderLegend(mode.key ? mode : null, mode.key ? counts : null);
  }
  if (wasSelected) highlight(wasSelected, false);
}

// ---------------------------------------------------------------- package focus

function renderFocusBanner() {
  if (!focusedPackage) { focusEl.hidden = true; return; }
  const pkg = packages[focusedPackage];
  const n = components.filter(c => c.iwp === focusedPackage && c.has_geometry).length;
  focusEl.hidden = false;
  focusEl.innerHTML =
    `<span class="badge ${pkg?.verdict === 'READY' ? 'b-ready'
      : pkg?.verdict === 'COMPLETE' ? 'b-complete' : 'b-blocked'}">` +
    `${escapeHtml(pkg?.verdict || 'PACKAGE')}</span>` +
    `<div><strong class="mono">${escapeHtml(focusedPackage)}</strong>` +
    `<div class="hint">${n.toLocaleString()} components` +
    `${pkg?.summary ? ' &middot; ' + escapeHtml(pkg.summary) : ''}</div></div>` +
    `<a class="btn ghost" href="/packages/${encodeURIComponent(focusedPackage)}">Package detail</a>` +
    '<button class="ghost" id="focus-clear">Show whole model</button>';
  document.getElementById('focus-clear').addEventListener('click', () => {
    focusedPackage = null;
    syncUrl();
    renderFocusBanner();
    applyColours();
    frameModel();
  });
}

function framePackage() {
  const tags = new Set(components
    .filter(c => c.iwp === focusedPackage && c.has_geometry).map(c => c.tag));
  const box = new THREE.Box3();
  for (const [tag, meshes] of nodesByTag) {
    if (tags.has(tag)) for (const mesh of meshes) box.expandByObject(mesh);
  }
  if (box.isEmpty()) return;
  const centre = box.getCenter(new THREE.Vector3());
  const size = box.getSize(new THREE.Vector3());
  const radius = Math.max(size.x, size.y, size.z, 1000);
  controls.target.copy(centre);
  camera.position.set(centre.x + radius * 1.3, centre.y + radius * 1.0,
                      centre.z + radius * 1.3);
  controls.update();
}

// ---------------------------------------------------------------- 4D

function buildTimeline() {
  const dates = components
    .filter(c => c.has_geometry && c.planned_date)
    .map(c => Date.parse(c.planned_date))
    .filter(t => !Number.isNaN(t));
  timeline = dates.length
    ? { min: Math.min(...dates), max: Math.max(...dates) } : null;
  // Switching models resets the scrubber. Carrying a cursor across two
  // models with different schedules would show a number that means nothing.
  stopPlayback();
  timelineActive = false;
  timelineEl.hidden = true;
  tlRange.value = tlRange.max;
  tlOn.hidden = !timeline;
  if (!timeline) return;

  // ?t=0..100 opens straight onto a point in the sequence. Only honoured on
  // the first model, since the position means a different date on each one.
  const at = params.get('t');
  if (at !== null && firstLoad) {
    tlRange.value = Math.min(Math.max(Number(at) || 0, 0), Number(tlRange.max));
    timelineActive = true;
    timelineEl.hidden = false;
    tlOn.hidden = true;
  }
  updateTimelineLabel();
}

function timelineCursor() {
  if (!timeline) return null;
  const t = Number(tlRange.value) / Number(tlRange.max);
  return timeline.min + t * (timeline.max - timeline.min);
}

function setTimeline(on) {
  timelineActive = on && !!timeline;
  timelineEl.hidden = !timelineActive;
  tlOn.hidden = timelineActive || !timeline;
  if (!timelineActive) stopPlayback();
  if (timelineActive) updateTimelineLabel();
  applyColours();
}

function updateTimelineLabel() {
  const cursor = timelineCursor();
  if (cursor === null) return;
  tlDate.textContent = new Date(cursor).toISOString().slice(0, 10);
}

const icon = (name) => '<svg class="i"><use href="#i-' + name + '"/></svg>';

function stopPlayback() {
  if (playHandle) { clearInterval(playHandle); playHandle = null; }
  tlPlay.innerHTML = icon('play');
  tlPlay.title = 'Play the sequence';
}

function togglePlayback() {
  if (playHandle) { stopPlayback(); return; }
  if (Number(tlRange.value) >= Number(tlRange.max)) tlRange.value = 0;
  tlPlay.innerHTML = icon('pause');
  tlPlay.title = 'Pause';
  playHandle = setInterval(() => {
    const next = Number(tlRange.value) + 1;
    tlRange.value = Math.min(next, Number(tlRange.max));
    updateTimelineLabel();
    applyColours();
    if (next >= Number(tlRange.max)) stopPlayback();
  }, 90);
}

function renderTimelineLegend(planned, installed, slipping) {
  legendEl.innerHTML =
    '<h2>4D sequence</h2>' +
    '<p class="msg" style="margin-bottom:8px">Planned install dates played ' +
    'against the status each component actually reports.</p>' +
    '<div class="legend">' +
    `<span><i style="background:#43b581"></i>Installed (${installed.toLocaleString()})</span>` +
    `<span><i style="background:#ffcd23"></i>Due, not installed (${slipping.toLocaleString()})</span>` +
    `<span><i style="background:${NO_DATA};opacity:.4"></i>Not yet due</span>` +
    '</div>' +
    (slipping
      ? `<div class="note" style="margin-top:10px">${slipping.toLocaleString()} of the
         ${planned.toLocaleString()} components due by this date are not installed.
         The same slip shows up on the work packages page as a blocked
         predecessor, because both read the one status field.</div>`
      : '');
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

function renderLegend(mode, counts) {
  // Category mode reads its colours back off the published materials, which
  // is the honest thing to show: those are the colours in the file.
  if (!mode || !counts) {
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
    return;
  }

  // Fixed-order palettes list every state, including ones with no
  // components, because an empty Commissioned bucket is information.
  let entries = mode.order
    ? mode.order.map(label => [label, counts.get(label) || 0])
    : [...counts.entries()].sort((a, b) => b[1] - a[1]);
  if (!mode.order) {
    const missing = entries.find(([label]) => label === 'Not populated');
    entries = entries.filter(([label]) => label !== 'Not populated');
    if (mode.collapse && entries.length > mode.collapse) {
      entries = entries.slice(0, mode.collapse);
    }
    if (missing) entries.push(missing);
  } else if (counts.has('Not populated')) {
    entries.push(['Not populated', counts.get('Not populated')]);
  }

  const total = [...counts.values()].reduce((a, b) => a + b, 0);
  const hidden = total - entries.reduce((a, [, n]) => a + n, 0);

  legendEl.innerHTML =
    `<h2>${escapeHtml(colourSelect.selectedOptions[0].textContent)}</h2>` +
    '<div class="legend">' +
    entries.map(([label, n]) => {
      const hex = label === 'Not populated' ? NO_DATA : mode.colour(label);
      return `<span><i style="background:${hex}"></i>${escapeHtml(label)}` +
             `<b>${n.toLocaleString()}</b></span>`;
    }).join('') +
    '</div>' +
    (hidden > 0
      ? `<p class="msg" style="margin-top:8px">and ${hidden.toLocaleString()} in
         further groups, not listed.</p>` : '') +
    (counts.get('Not populated')
      ? `<div class="note" style="margin-top:10px">${counts.get('Not populated').toLocaleString()}
         components have nothing recorded here. Grey is not a status, it is a
         gap, and it is what the handover feed reports.</div>` : '');
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
  applyColours();
});

modelSelect.addEventListener('change', () => {
  currentModel = modelSelect.value;
  selectedTag = null;
  selectedRestore = [];
  // Packages are scoped to a model, so a focus carried over from the
  // previous one would match nothing and ghost the entire plant.
  focusedPackage = null;
  syncUrl();
  loadModel(currentModel, null);
});

lodSelect.addEventListener('change', () => loadModel(currentModel, lodSelect.value));
filterInput.addEventListener('input', () => renderTree(filterInput.value));

colourSelect.addEventListener('change', () => {
  colourMode = colourSelect.value;
  syncUrl();
  applyColours();
});

function syncUrl() {
  const q = new URLSearchParams();
  if (colourMode !== 'category') q.set('colour', colourMode);
  if (focusedPackage) q.set('iwp', focusedPackage);
  const qs = q.toString();
  history.replaceState({}, '', `/viewer/${currentModel}${qs ? '?' + qs : ''}`);
}

tlOn.addEventListener('click', () => setTimeline(true));
tlOff.addEventListener('click', () => setTimeline(false));
tlPlay.addEventListener('click', togglePlayback);
tlRange.addEventListener('input', () => {
  stopPlayback();
  updateTimelineLabel();
  applyColours();
});

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

// ---------------------------------------------------------------- external control
//
// The assistant drives the viewer through this. It is the same state the
// query string carries, so anything the assistant can do can also be typed
// into the address bar or sent in a link, and the viewer stays testable
// without a language model in the loop.

window.modelopsViewer = {
  currentModel: () => currentModel,

  apply(state) {
    if (!state) return false;

    // A different model means a reload, and the rest of the state has to be
    // applied after the geometry arrives rather than against the old scene.
    if (state.model && state.model !== currentModel) {
      currentModel = state.model;
      modelSelect.value = state.model;
      if (modelSelect.value !== state.model) return false;   // unknown model
      selectedTag = null;
      selectedRestore = [];
      focusedPackage = state.iwp || null;
      if (state.colour) { colourMode = state.colour; colourSelect.value = state.colour; }
      syncUrl();
      loadModel(currentModel, null);
      return true;
    }

    if (state.colour && state.colour !== colourMode) {
      colourMode = state.colour;
      colourSelect.value = state.colour;
    }
    if (state.iwp !== undefined) focusedPackage = state.iwp || null;

    syncUrl();
    renderFocusBanner();
    applyColours();
    if (focusedPackage) framePackage();
    return true;
  },
};

initScene();
loadModel(currentModel, null);
