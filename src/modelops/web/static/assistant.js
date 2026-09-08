/*
 * The assistant panel.
 *
 * Bottom-right launcher, per the usual convention. Two things make it worth
 * having over the pages it sits on:
 *
 *   1. It shows its working. Every answer lists the tools it called, so a
 *      figure it quotes can be traced to the query that produced it.
 *   2. It drives the viewer. When a tool returns viewer state, the panel
 *      applies it to the live scene if you are on the viewer, and offers a
 *      link if you are not. A chat box that only emits paragraphs is a worse
 *      interface than a table.
 *
 * Local like everything else: the request goes to this Flask app, which talks
 * to Ollama on 127.0.0.1. Nothing leaves the machine.
 */

(function () {
  const launcher = document.getElementById('assistant-launcher');
  const panel = document.getElementById('assistant-panel');
  if (!launcher || !panel) return;

  const log = document.getElementById('assistant-log');
  const form = document.getElementById('assistant-form');
  const input = document.getElementById('assistant-input');
  const badge = document.getElementById('assistant-engine');
  const closeBtn = document.getElementById('assistant-close');

  // Trimmed to the last few exchanges before sending. Context is the main
  // cost at this model size and older turns rarely change the answer.
  const history = [];
  let busy = false;

  const SUGGESTIONS = [
    'Which packages are blocked only by missing model data?',
    'Show me IWP-BOP-200-STL-01-001 in 3D',
    'Colour the model by install status',
    'How complete are the handover attributes?',
    'Why was NI1-HVAC-U10 not published?',
    'What is this pipeline for?',
  ];

  function open() {
    panel.hidden = false;
    launcher.hidden = true;
    if (window.modelopsMotion) window.modelopsMotion.popIn(panel);
    input.focus();
    if (!log.childElementCount) greet();
  }

  function close() {
    panel.hidden = true;
    launcher.hidden = false;
  }

  launcher.addEventListener('click', open);
  closeBtn.addEventListener('click', close);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !panel.hidden) close();
  });

  async function greet() {
    bubble('assistant',
      'I answer questions about this catalog by querying it, and I can change ' +
      'what the 3D viewer is showing. I cannot change any data.');
    const chips = document.createElement('div');
    chips.className = 'assistant-chips';
    for (const s of SUGGESTIONS) {
      const chip = document.createElement('button');
      chip.className = 'ghost';
      chip.textContent = s;
      chip.addEventListener('click', () => { input.value = s; send(); });
      chips.appendChild(chip);
    }
    log.appendChild(chips);
    scroll();

    try {
      const health = await (await fetch('/api/agent/health')).json();
      setEngine(health.engine, health.model, health.detail);
      if (health.engine === 'rules') {
        // Said out loud rather than hidden in a tooltip. Letting someone
        // assume there is a language model here and discover otherwise is
        // the one thing that would undermine everything else on screen.
        const note = document.createElement('div');
        note.className = 'assistant-note';
        note.innerHTML =
          'No language model is installed on this machine, so questions are ' +
          'routed to the tools by pattern matching rather than understood. ' +
          'The tools, the numbers and the 3D control are identical either way; ' +
          'what is missing is tolerance for how you phrase things. ' +
          '<a href="/tools">See the tool layer</a>.';
        log.appendChild(note);
        scroll();
      }
    } catch { setEngine('rules', null, 'Could not reach the assistant service.'); }
  }

  function setEngine(engine, model, detail) {
    const isModel = engine === 'model';
    badge.textContent = isModel ? model : 'rule-based';
    badge.className = 'badge ' + (isModel ? 'b-ok' : 'b-warn');
    badge.title = detail || (isModel
      ? `Local model via Ollama. Nothing leaves this machine.`
      : 'No local model installed, so questions are matched to the same tools by pattern.');
  }

  function bubble(role, text) {
    const el = document.createElement('div');
    el.className = `assistant-msg ${role}`;
    el.textContent = text;
    log.appendChild(el);
    scroll();
    return el;
  }

  function scroll() { log.scrollTop = log.scrollHeight; }

  function renderCalls(calls) {
    if (!calls || !calls.length) return;
    const el = document.createElement('details');
    el.className = 'assistant-calls';
    el.innerHTML =
      `<summary>${calls.length} tool call${calls.length > 1 ? 's' : ''}</summary>` +
      calls.map(c =>
        `<div><span class="mono">${escapeHtml(c.name)}(${escapeHtml(
          Object.entries(c.arguments || {})
            .map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(', '))})</span>` +
        `<div class="hint">${escapeHtml(c.summary || '')}</div></div>`).join('');
    log.appendChild(el);
    scroll();
  }

  function renderRows(rows, columns) {
    if (!rows || !rows.length || !columns || !columns.length) return;
    const shown = rows.slice(0, 12);
    const el = document.createElement('div');
    el.className = 'assistant-table';
    el.innerHTML =
      '<table><thead><tr>' +
      columns.map(c => `<th>${escapeHtml(c.replace(/_/g, ' '))}</th>`).join('') +
      '</tr></thead><tbody>' +
      shown.map(r => '<tr>' + columns.map(c =>
        `<td>${escapeHtml(r[c] ?? '')}</td>`).join('') + '</tr>').join('') +
      '</tbody></table>' +
      (rows.length > shown.length
        ? `<div class="hint">and ${rows.length - shown.length} more</div>` : '');
    log.appendChild(el);
    scroll();
  }

  function applyViewer(state) {
    if (!state) return;
    // On the viewer, change the live scene. Anywhere else, offer the link
    // rather than navigating out from under someone mid-conversation.
    if (window.modelopsViewer) {
      const applied = window.modelopsViewer.apply(state);
      if (applied) return;
    }
    const model = state.model || (window.modelopsViewer
      && window.modelopsViewer.currentModel());
    if (!model) return;
    const q = new URLSearchParams();
    if (state.colour) q.set('colour', state.colour);
    if (state.iwp) q.set('iwp', state.iwp);
    const link = document.createElement('a');
    link.className = 'btn assistant-jump';
    link.href = `/viewer/${model}${q.toString() ? '?' + q : ''}`;
    link.textContent = state.iwp ? `Open ${state.iwp} in the viewer`
                                 : 'Open this view in the viewer';
    log.appendChild(link);
    scroll();
  }

  async function send() {
    const question = input.value.trim();
    if (!question || busy) return;
    busy = true;
    input.value = '';
    document.querySelector('.assistant-chips')?.remove();
    bubble('user', question);
    const pending = bubble('assistant pending', 'Thinking...');

    try {
      const response = await fetch('/api/agent/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question, history: history.slice(-6) }),
      });
      const turn = await response.json();
      pending.remove();

      if (turn.error) { bubble('assistant err', turn.error); return; }

      setEngine(turn.engine, turn.model);
      bubble('assistant', turn.reply);
      renderCalls(turn.calls);
      renderRows(turn.rows, turn.columns);
      applyViewer(turn.viewer);

      history.push({ role: 'user', content: question });
      history.push({ role: 'assistant', content: turn.reply });
    } catch (err) {
      pending.remove();
      bubble('assistant err', 'The assistant did not respond: ' + err);
    } finally {
      busy = false;
      input.focus();
    }
  }

  form.addEventListener('submit', (e) => { e.preventDefault(); send(); });

  // #ask opens the panel; #ask=<question> also asks it. Pre-baked links are
  // worth more than they look during a live demo: typing a long package id
  // into a chat box while someone watches is a good way to typo it.
  const hash = decodeURIComponent(location.hash.replace(/^#/, ''));
  if (hash === 'ask' || hash.startsWith('ask=')) {
    open();
    const question = hash.slice(4);
    if (question) {
      input.value = question;
      // After greet() has resolved the engine badge, so the answer is not
      // labelled before we know what produced it.
      setTimeout(send, 150);
    }
  }

  function escapeHtml(v) {
    return String(v ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
})();
