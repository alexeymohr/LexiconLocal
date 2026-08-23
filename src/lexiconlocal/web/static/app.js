/* Lexicon web UI — vendored, no framework, no build step (D-2026-08-19-07).
 *
 * Three views behind one shell, routed on the URL so every state is a real
 * address you can bookmark or paste into a note. Rendering is client-side
 * because the API answers in single-digit milliseconds and a page round-trip
 * would spend more time on HTML than on the query — which is also why there
 * are no spinners for anything that normally beats 100 ms (a "Loading…" that
 * flashes for 40 ms is worse than nothing).
 *
 * Everything user- or corpus-supplied goes through `esc()` or `text()`. The
 * only place raw HTML is inserted is where the *server* rendered and scrubbed
 * it, and the page's CSP forbids inline script regardless.
 */
'use strict';

const view = document.getElementById('view');
const qbox = document.getElementById('q');
const form = document.getElementById('searchform');
const chip = document.getElementById('healthchip');

/* ---- helpers ---------------------------------------------------------- */

const esc = (s) => String(s == null ? '' : s)
  .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
  .replaceAll('"', '&quot;').replaceAll("'", '&#39;');

const n = (x) => (x == null ? '—' : Number(x).toLocaleString());

async function api(path, params) {
  const url = new URL(path, location.origin);
  for (const [k, v] of Object.entries(params || {})) {
    if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, v);
  }
  const r = await fetch(url, { headers: { Accept: 'application/json' } });
  const body = await r.json().catch(() => ({ error: 'unreadable response' }));
  if (!r.ok) throw Object.assign(new Error(body.error || r.statusText), { status: r.status });
  return body;
}

function go(path, replace) {
  if (replace) history.replaceState({}, '', path);
  else history.pushState({}, '', path);
  route();
}

/* Slow-only spinner: if the answer arrives quickly the user never sees a
 * flash of "Loading…", which is the difference between "instant" and "fast". */
function withPending(fn) {
  let done = false;
  const t = setTimeout(() => { if (!done) view.innerHTML = '<div class="loading">Loading…</div>'; }, 120);
  return fn().finally(() => { done = true; clearTimeout(t); });
}

function fail(e) {
  view.innerHTML = `<div class="card"><h1>${e.status === 404 ? 'Not found' : 'Error'}</h1>
    <p class="subtitle">${esc(e.message || 'something went wrong')}</p>
    <p><a href="/" data-nav>Back to Home</a></p></div>`;
}

/* ---- health chip ------------------------------------------------------ */

function paintChip(h) {
  if (!h || !h.ok) { chip.textContent = 'no index'; chip.className = 'health-chip red'; return; }
  chip.className = 'health-chip ' + h.state;
  const bits = [`${n(h.documents)} docs`];
  if (h.pending_embed) bits.push(`${n(h.pending_embed)} pending`);
  if (h.integrity && h.integrity.dangling_occurrences) bits.push('DAMAGED');
  chip.textContent = bits.join(' · ');
  chip.title = `index ${h.state} · ${n(h.chunks)} chunks · ${n(h.embedded)} embeddings`;
}

/* ---- home ------------------------------------------------------------- */

async function home() {
  const d = await api('/api/dashboard');
  paintChip(d.health);
  const h = d.health;

  const stats = h.ok ? `
    <div class="healthstrip">
      ${stat(n(h.documents), 'documents')}
      ${stat(n(h.chunks), 'chunks')}
      ${stat(n(h.embedded), 'embeddings')}
      ${stat(n(h.pending_embed), 'pending embed', h.pending_embed ? 'warn' : '')}
      ${stat(n(h.integrity.dangling_occurrences), 'unretrievable', h.integrity.dangling_occurrences ? 'bad' : '')}
      ${stat(h.last_run ? h.last_run.finished.slice(0, 16).replace('T', ' ') : '—', 'last index')}
    </div>` : `<div class="banner">${esc(h.detail || 'index unavailable')}</div>`;

  const untested = h.ok ? h.sources.filter((s) => s.status !== 'ok') : [];
  const warn = untested.length
    ? `<div class="banner">Sources needing attention: ${untested
        .map((s) => `<span class="pill ${esc(s.status)}">${esc(s.key)} — ${esc(s.status)}</span>`)
        .join(' ')}</div>`
    : '';

  view.innerHTML = `
    <h1>Lexicon</h1>
    <p class="subtitle">${esc(d.lexicon_root)} · ${d.projects.length} projects</p>
    ${warn}
    <div class="card">${stats}</div>

    <h2>Projects</h2>
    <div class="card rows">${d.projects.map(projectRow).join('') || empty('no curated projects yet')}</div>

    <div class="grid two">
      <div>
        <h2>Recent activity</h2>
        <div class="card rows">${d.recent_log.map(logRow).join('') || empty('no log entries')}</div>
      </div>
      <div>
        <h2>Recent decisions</h2>
        <div class="card rows">${d.recent_decisions.map(decRow).join('') || empty('no decisions')}</div>
      </div>
    </div>

    <h2>Open questions</h2>
    <div class="card rows">${d.open_questions.map(qRow).join('') || empty('nothing outstanding')}</div>

    <h2>Not yet distilled</h2>
    <p class="subtitle">Indexed and searchable, but with no curated notes — searchable, not yet learnable.</p>
    <div class="card rows">${(d.distillation_backlog || []).map(backlogRow).join('') || empty('every indexed project has notes')}</div>
  `;
}

const stat = (v, l, cls) => `<div class="stat ${cls || ''}"><span class="n">${esc(v)}</span><span class="l">${esc(l)}</span></div>`;
const empty = (m) => `<div class="empty">${esc(m)}</div>`;

const projectRow = (p) => `
  <div class="row">
    <span class="when">${esc(p.last_activity || '—')}</span>
    <span class="what"><a href="/project?name=${encodeURIComponent(p.name)}" data-nav>${esc(p.name)}</a>
      ${p.status ? `<div class="sub">${esc(p.status)}</div>` : ''}</span>
    <span class="who"><span class="pill">${p.log_entries} log</span>
      <span class="pill active">${p.active_decisions} active</span>
      ${p.open_questions ? `<span class="pill unknown">${p.open_questions} open</span>` : ''}</span>
  </div>`;

const backlogRow = (e) => `
  <div class="row">
    <span class="when">${esc(e.last_activity || '—')}</span>
    <span class="what">${esc(e.project)}
      <div class="sub">${e.documents.toLocaleString()} documents · ${e.chunks.toLocaleString()} chunks · raw only</div></span>
    <span class="who"><span class="pill unknown">not distilled</span></span>
  </div>`;

const logRow = (e) => `
  <div class="row">
    <span class="when">${esc(e.date)}</span>
    <span class="what"><a href="/project?name=${encodeURIComponent(e.project)}#log" data-nav>${esc(e.project)}</a>
      — ${esc(e.heading)}${e.agent ? ` <span class="pill">${esc(e.agent)}</span>` : ''}
      ${e.body ? `<div class="sub">${esc(e.body)}</div>` : ''}</span>
  </div>`;

/* Decision ids are unique within a project, not across the Lexicon: two
 * projects can mint the same D-YYYY-MM-DD-NN on the same day, and did. A
 * cross-project feed that shows the id alone reads as a duplicate or a
 * conflict, so the project is part of the identity here. */
const decRow = (d) => `
  <div class="row">
    <span class="when">${esc(d.date || '—')}</span>
    <span class="what"><a href="/project?name=${encodeURIComponent(d.project)}#${esc(d.id)}" data-nav>${esc(d.id)}</a>
      <span class="pill">${esc(d.project)}</span>
      — ${esc(d.title)} <span class="pill ${esc(d.status)}">${esc(d.status)}</span></span>
  </div>`;

const qRow = (q) => `
  <div class="row">
    <span class="who"><a href="/project?name=${encodeURIComponent(q.project)}" data-nav>${esc(q.project)}</a></span>
    <span class="what">${esc(q.question)}</span>
  </div>`;

/* ---- search ----------------------------------------------------------- */

const FILTERS = {
  source_type: ['lexicon', 'repo-doc', 'transcript', 'archive-doc',
                'claude-memory', 'claude-project', 'codex-memory'],
};
let selected = -1;

async function search(params) {
  qbox.value = params.q || '';
  const d = await api('/api/search', params);

  const pills = FILTERS.source_type.map((s) => {
    const on = params.source_type === s;
    return `<button type="button" class="pill" aria-pressed="${on}" data-filter="source_type" data-value="${esc(s)}">${esc(s)}</button>`;
  }).join('');
  const exactOn = params.exact === '1';

  view.innerHTML = `
    <h1>${esc(d.query)}</h1>
    <p class="subtitle">${d.count} result${d.count === 1 ? '' : 's'}${d.exact_mode ? ' · exact-match ranking' : ''}${d.vector_leg ? '' : ' · <strong>lexical only — Ollama unavailable</strong>'}</p>
    <div class="filters">
      <button type="button" class="pill" aria-pressed="${exactOn}" data-filter="exact" data-value="1">exact</button>
      ${pills}
      ${params.project ? `<button type="button" class="pill" aria-pressed="true" data-filter="project" data-value="${esc(params.project)}">project: ${esc(params.project)}</button>` : ''}
    </div>
    <div id="results">${d.results.map(resultCard).join('') || empty('nothing matched')}</div>`;

  selected = -1;
  view.querySelectorAll('[data-filter]').forEach((b) => {
    b.addEventListener('click', () => {
      const k = b.dataset.filter;
      const next = { ...params };
      if (next[k] === b.dataset.value) delete next[k];
      else next[k] = b.dataset.value;
      go(searchUrl(next));
    });
  });
  view.querySelectorAll('.result').forEach((el, i) => {
    el.addEventListener('click', () => open_(el.dataset.path, el.dataset.chunk));
    el.addEventListener('mouseenter', () => select(i));
  });
}

function searchUrl(p) {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(p)) if (v) u.set(k, v);
  return '/search?' + u.toString();
}

function resultCard(r) {
  const low = r.confidence < 0.45;
  const cc = r.confidence >= 0.75 ? 'hi' : (r.confidence < 0.45 ? 'lo' : '');
  return `
  <article class="result ${low ? 'low' : ''}" tabindex="0"
           data-path="${esc(r.path)}" data-chunk="${esc(r.chunk_ord)}">
    <div class="head">
      <span class="title">${esc(r.title || r.path.split('/').pop())}</span>
      <span class="pill">${esc(r.source_type)}${r.chunk_kind !== 'prose' ? '/' + esc(r.chunk_kind) : ''}</span>
      ${r.project ? `<span class="pill">${esc(r.project)}</span>` : ''}
      ${r.doc_date ? `<span class="pill">${esc(r.doc_date)}</span>` : ''}
      <span class="conf ${cc}">conf ${r.confidence.toFixed(2)}</span>
      <span class="conf">${esc(r.matched_by.join('+'))}</span>
    </div>
    <div class="excerpt">${esc(r.excerpt)}</div>
    <div class="locator">${esc(r.locator)}</div>
  </article>`;
}

function select(i) {
  const els = [...view.querySelectorAll('.result')];
  if (!els.length) return;
  selected = Math.max(0, Math.min(els.length - 1, i));
  els.forEach((e, k) => e.classList.toggle('sel', k === selected));
  els[selected].scrollIntoView({ block: 'nearest' });
}

function open_(path, chunk) {
  go('/doc?path=' + encodeURIComponent(path) + (chunk ? '#chunk-' + chunk : ''));
}

/* ---- project ---------------------------------------------------------- */

async function project(name) {
  const d = await api('/api/project/' + encodeURIComponent(name));
  const dec = d.decisions.map((x) => `
    <div class="row" id="${esc(x.id)}">
      <span class="when">${esc(x.date || '—')}</span>
      <span class="what"><strong>${esc(x.id)}</strong> — ${esc(x.title)}
        <span class="pill ${esc(x.status)}">${esc(x.status)}</span>
        ${x.superseded_by.length ? `<span class="sub">superseded by ${x.superseded_by.map(esc).join(', ')}</span>` : ''}
        ${x.supersedes.length ? `<span class="sub">supersedes ${x.supersedes.map(esc).join(', ')}</span>` : ''}
      </span>
    </div>`).join('');

  /* body_html is rendered and scrubbed server-side; a log entry is the most
   * valuable prose in the Lexicon and deserves its own formatting, not a
   * flattened grey caption. */
  const log = d.log.map((e) => `
    <div class="row entry">
      <span class="when">${esc(e.date)}</span>
      <span class="what"><strong>${esc(e.heading)}</strong>${e.agent ? ` <span class="pill">${esc(e.agent)}</span>` : ''}
        ${e.body_html ? `<div class="entrybody">${e.body_html}</div>` : ''}</span>
    </div>`).join('');

  view.innerHTML = `
    <div class="crumbs"><a href="/" data-nav>Home</a> / ${esc(d.name)}</div>
    <h1>${esc(d.name)}</h1>
    <p class="subtitle">${d.indexed_documents} indexed documents${d.resolved_from ? ` · reached via alias “${esc(d.resolved_from)}”` : ''}
      · <a href="${searchUrl({ q: d.name, project: d.name })}" data-nav>search this project</a></p>
    ${d.overview_path ? sourceLine(d.overview_path) : ''}
    ${d.overview_html ? `<div class="prose">${d.overview_html}</div>` : empty('no overview.md')}
    <h2>Decisions</h2>
    <div class="card rows" id="decisions">${dec || empty('no decisions recorded')}</div>
    <h2>Log</h2>
    <div class="card rows" id="log">${log || empty('no log entries')}</div>
    <h2>Files</h2>
    <div class="card rows">${d.files.map((f) => `
      <div class="row"><span class="what"><a href="/doc?path=${encodeURIComponent(f.path)}" data-nav>${esc(f.name)}</a></span>
      <span class="who"><span class="pill">${n(f.bytes)} B</span></span></div>`).join('')}</div>`;
  wireCopy();
  if (location.hash) {
    const el = document.getElementById(decodeURIComponent(location.hash.slice(1)));
    if (el) el.scrollIntoView({ block: 'center' });
  }
}

/* ---- document --------------------------------------------------------- */

async function doc(path, hash) {
  const d = await api('/api/doc', { path });
  const m = d.meta || {};
  view.innerHTML = `
    <div class="crumbs"><a href="/" data-nav>Home</a>${m.project ? ` / <a href="/project?name=${encodeURIComponent(m.project)}" data-nav>${esc(m.project)}</a>` : ''}</div>
    <h1>${esc(m.title || path.split('/').pop())}</h1>
    <div class="docmeta">
      ${m.source_type ? `<span class="pill">${esc(m.source_type)}</span>` : ''}
      ${m.doc_date ? `<span class="pill">${esc(m.doc_date)}</span>` : ''}
      ${d.kind === 'transcript' ? `<span class="pill">${d.chunk_count} chunks</span>` : ''}
      ${d.truncated ? '<span class="pill unknown">truncated</span>' : ''}
      ${d.indexed ? '' : '<span class="pill unknown">not indexed</span>'}
    </div>
    ${sourceLine(d.source_path)}
    ${d.truncated ? '<div class="banner">This file is larger than the view limit; the tail is not shown. Open it in an editor for the whole thing.</div>' : ''}
    <div class="${d.kind === 'transcript' ? 'card' : 'prose'}">${d.html}</div>`;
  wireCopy();
  const target = hash && document.getElementById(hash.slice(1));
  if (target) { target.classList.add('hit'); target.scrollIntoView({ block: 'center' }); }
}

/* The "open in editor" affordance: a copyable absolute path, which works with
 * every editor, plus an editor URL when one is configured. A path you can
 * paste is the lowest common denominator that never breaks. */
function sourceLine(p) {
  if (!p) return '';
  return `<div class="sourcepath"><span id="srcpath">${esc(p)}</span>
    <button type="button" class="copybtn" data-copy="${esc(p)}">copy path</button>
    <a class="copybtn" href="vscode://file${esc(p)}">open in editor</a></div>`;
}

function wireCopy() {
  view.querySelectorAll('[data-copy]').forEach((b) => {
    b.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(b.dataset.copy); b.textContent = 'copied'; }
      catch { b.textContent = 'copy failed'; }
      setTimeout(() => { b.textContent = 'copy path'; }, 1400);
    });
  });
}

/* ---- routing ---------------------------------------------------------- */

function route() {
  const u = new URL(location.href);
  const p = u.pathname;
  const params = Object.fromEntries(u.searchParams.entries());
  if (p !== '/search') qbox.value = '';
  const run =
    p === '/search' ? () => search(params)
    : p === '/project' ? () => project(params.name || '')
    : p === '/doc' ? () => doc(params.path || '', u.hash)
    : home;
  withPending(() => run().catch(fail));
}

document.addEventListener('click', (e) => {
  const a = e.target.closest('a[data-nav]');
  if (!a || a.target === '_blank' || e.metaKey || e.ctrlKey) return;
  e.preventDefault();
  go(a.getAttribute('href'));
});

window.addEventListener('popstate', route);

form.addEventListener('submit', (e) => {
  e.preventDefault();
  const q = qbox.value.trim();
  if (q) go(searchUrl({ q }));
});

/* Keyboard-first: `/` focuses search from anywhere, arrows walk results,
 * Enter opens the selected one, Escape leaves the box. */
document.addEventListener('keydown', (e) => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
  if (e.key === '/' && !typing) { e.preventDefault(); qbox.focus(); qbox.select(); return; }
  if (e.key === 'Escape' && typing) { qbox.blur(); return; }
  if (typing) return;
  const els = [...view.querySelectorAll('.result')];
  if (!els.length) return;
  if (e.key === 'ArrowDown' || e.key === 'j') { e.preventDefault(); select(selected + 1); }
  else if (e.key === 'ArrowUp' || e.key === 'k') { e.preventDefault(); select(selected - 1); }
  else if (e.key === 'Enter' && selected >= 0) {
    e.preventDefault();
    open_(els[selected].dataset.path, els[selected].dataset.chunk);
  }
});

/* The chip is painted by whatever loads first. Home already fetches the
 * dashboard, so asking for it a second time here would double the most
 * expensive call on the page. */
if (location.pathname !== '/') {
  api('/api/dashboard').then((d) => paintChip(d.health)).catch(() => {});
}
route();
