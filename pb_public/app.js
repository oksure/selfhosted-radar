// Self-Hosted Radar — no build step. Everything renders through textContent
// or escaped markup so upstream descriptions can never inject HTML.

const API = '/api/collections';
const PER = 30;
const $ = (s, r = document) => r.querySelector(s);
const POST = q => fetch(API + '/' + q).then(r => r.ok ? r.json() : Promise.reject(new Error(q + ' -> ' + r.status)));

const state = {
  view: 'explore',
  q: '',
  preset: '',
  source: '',        // awesome | selfhst | both
  tags: [],
  lang: '',
  license: '',
  activity: '',      // '' (all) | active | slow | stale
  hideArchived: true,
  stars: '',         // '' | 0-500 | 500-5000 | 5000+
  sort: '-selfhst_trending',
  page: 1,
  layout: 'cards',
  slug: '',          // detail route
  auth: null,
};

// ---------- helpers ----------
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = n => (n == null ? '—' : Number(n).toLocaleString('en-US'));
const safeUrl = u => {
  if (!u) return '';
  try {
    const p = new URL(u);
    return (p.protocol === 'http:' || p.protocol === 'https:') ? p.href : '';
  } catch { return ''; }
};
const arr = v => (Array.isArray(v) ? v : (typeof v === 'string' && v ? (() => { try { const x = JSON.parse(v); return Array.isArray(x) ? x : []; } catch { return []; } })() : []));

// ---------- URL state ----------
function readUrl() {
  const u = new URLSearchParams(location.search);
  state.q = u.get('q') || '';
  state.preset = u.get('preset') || '';
  state.source = u.get('source') || '';
  state.tags = (u.get('tag') || '').split(',').filter(Boolean);
  state.lang = u.get('lang') || '';
  state.license = u.get('license') || '';
  state.activity = u.get('activity') || '';
  state.stars = u.get('stars') || '';
  state.hideArchived = u.get('arch') !== '1' ? true : false;
  state.sort = u.get('sort') || '-selfhst_trending';
  state.page = Math.max(1, parseInt(u.get('page') || '1', 10) || 1);
  if (state.preset === 'both' && !state.source) state.source = 'both';
  state.layout = u.get('layout') === 'table' ? 'table' : 'cards';
  state.view = location.pathname === '/radar' ? 'radar' : 'explore';
  const m = location.pathname.match(/^\/apps\/([^/]+)$/);
  state.slug = m ? decodeURIComponent(m[1]) : (u.get('app') || '');
}

function writeUrl(replace = false) {
  const u = new URLSearchParams();
  if (state.q) u.set('q', state.q);
  if (state.preset) u.set('preset', state.preset);
  if (state.source) u.set('source', state.source);
  if (state.tags.length) u.set('tag', state.tags.join(','));
  if (state.lang) u.set('lang', state.lang);
  if (state.license) u.set('license', state.license);
  if (state.activity) u.set('activity', state.activity);
  if (state.stars) u.set('stars', state.stars);
  if (!state.hideArchived) u.set('arch', '1');
  if (state.sort !== '-selfhst_trending') u.set('sort', state.sort);
  if (state.page > 1) u.set('page', String(state.page));
  if (state.layout === 'table') u.set('layout', 'table');
  const base = state.view === 'radar' ? '/radar' : '/';
  const qs = u.toString();
  const url = base + (qs ? '?' + qs : '');
  if ((location.pathname + location.search) === url) return;
  if (replace) history.replaceState({}, '', url);
  else history.pushState({}, '', url);
}

// ---------- filter building ----------
const q_ = v => '"' + String(v).replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"';

function filterString() {
  const parts = [];
  if (state.source === 'both') {
    parts.push('(sources ~ "awesome" && sources ~ "selfhst")');
  } else if (state.source) {
    parts.push('sources ~ ' + q_(state.source));
  }
  if (state.q.trim()) {
    const sub = state.q.trim();
    parts.push('(name ~ ' + q_(sub) + ' || description ~ ' + q_(sub) + ' || repo_full_name ~ ' + q_(sub) + ' || unified_tags ~ ' + q_(sub) + ')');
  }
  for (const t of state.tags) parts.push('unified_tags ~ ' + q_(t));
  if (state.lang) parts.push('language = ' + q_(state.lang));
  if (state.license) parts.push('license = ' + q_(state.license));
  if (state.activity) parts.push('activity_status = ' + q_(state.activity));
  if (state.hideArchived && !state.activity) parts.push('archived = false');
  if (state.stars === 'lt500') parts.push('stars < 500');
  else if (state.stars === '500-5000') parts.push('stars >= 500 && stars < 5000');
  else if (state.stars === 'gt5000') parts.push('stars >= 5000');
  if (state.preset === 'surge') parts.push('stars_7d_delta >= 20');  // history builds from day 1
  if (state.preset === 'release' && !state.q) parts.push('latest_release_at != ""');
  if (state.preset === 'new') {
    const d = new Date(Date.now() - 30 * 864e5).toISOString().slice(0, 10);
    parts.push('first_seen_at >= ' + q_(d));
  }
  return parts.join(' && ');
}

// ---------- renderers ----------
function badgeFor(p) {
  const srcs = arr(p.sources);
  const out = [];
  if (srcs.includes('awesome') && srcs.includes('selfhst')) out.push('<span class="badge both">both</span>');
  else if (srcs.includes('awesome')) out.push('<span class="badge a">awesome</span>');
  else if (srcs.includes('selfhst')) out.push('<span class="badge s">selfh.st</span>');
  if (p.activity_status === 'archived') out.push('<span class="badge arch">archived</span>');
  else if (p.activity_status === 'stale') out.push('<span class="badge arch">stale</span>');
  else if (p.activity_status === 'slow') out.push('<span class="badge slow">slow</span>');
  return out.join('');
}

function card(p) {
  const icon = safeUrl(p.icon_url);
  const tags = arr(p.unified_tags).slice(0, 4).map(t => `<span class="tag">${esc(t)}</span>`).join('');
  const d7 = Number(p.stars_7d_delta || 0);
  return `<article class="card" data-slug="${esc(p.slug)}" tabindex="0" role="button">
    <div class="card-top">
      ${icon ? `<img class="icon" src="${esc(icon)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.style.visibility='hidden'">` : '<div class="icon"></div>'}
      <div style="min-width:0">
        <h3>${esc(p.name)}</h3>
        <div class="badges">${badgeFor(p)}</div>
      </div>
    </div>
    <p class="desc">${esc(p.description)}</p>
    <div class="nums">
      <span>★ <b>${fmt(p.stars)}</b>${d7 ? ` <span class="up">+${fmt(d7)}/7d</span>` : ''}</span>
      <span>⇄ <b>${fmt(p.forks)}</b></span>
      ${p.selfhst_trending ? `<span>trend <b>${fmt(p.selfhst_trending)}</b></span>` : ''}
    </div>
    <div class="meta">${tags}${p.language ? `<span class="tag">${esc(p.language)}</span>` : ''}</div>
  </article>`;
}

function table(rows) {
  const body = rows.map(p => `<tr data-slug="${esc(p.slug)}">
    <td><b>${esc(p.name)}</b><div class="meta">${badgeFor(p)}</div></td>
    <td class="num">${fmt(p.stars)}</td>
    <td class="num">${p.stars_7d_delta ? '+' + fmt(p.stars_7d_delta) : '—'}</td>
    <td class="num">${p.selfhst_trending ? fmt(p.selfhst_trending) : '—'}</td>
    <td>${esc(p.language || '—')}</td>
    <td>${esc(p.license || '—')}</td>
    <td>${esc(p.last_commit_at || '—')}</td>
    <td>${esc(p.latest_release_at || '—')}</td>
  </tr>`).join('');
  return `<table class="tbl"><thead><tr>
    <th>App</th><th>Stars</th><th>7d</th><th>Trend</th><th>Language</th><th>License</th><th>Commit</th><th>Release</th>
  </tr></thead><tbody>${body}</tbody></table>`;
}

// ---------- explore ----------
let reqToken = 0;
async function loadExplore() {
  const token = ++reqToken;
  const list = $('#list');
  list.className = state.layout === 'table' ? '' : 'cards';
  list.innerHTML = '<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>';
  const flt = filterString();
  const params = { sort: state.sort, page: state.page, perPage: PER };
  if (flt) params.filter = flt;
  const qs = new URLSearchParams(params).toString();
  try {
    const d = await POST('projects/records?' + qs);
    if (token !== reqToken) return;
    const items = d.items || [];
    $('#count').textContent = `${fmt(d.totalItems)} app${d.totalItems === 1 ? '' : 's'}`;
    if (!items.length) {
      list.className = '';
      list.innerHTML = '<p class="empty">No applications match these filters.</p>';
    } else if (state.layout === 'table') {
      list.innerHTML = table(items);
    } else {
      list.innerHTML = items.map(card).join('');
    }
    renderPager(d);
    writeUrl(true);
  } catch (e) {
    if (token !== reqToken) return;
    list.className = '';
    list.innerHTML = `<p class="empty">Could not load applications. ${esc(e.message)}</p>`;
  }
}

function renderPager(d) {
  const pages = d.totalPages || 1;
  const p = d.page || 1;
  $('#pager').innerHTML = pages <= 1 ? '' :
    `<button data-p="${p - 1}" ${p <= 1 ? 'disabled' : ''}>← Prev</button>
     <span class="count">Page ${p} of ${fmt(pages)}</span>
     <button data-p="${p + 1}" ${p >= pages ? 'disabled' : ''}>Next →</button>`;
}

async function loadFacets() {
  try {
    const s = await fetch('/api/radar/summary').then(r => r.json());
    const stat = [
      `<span><b>${fmt(s.total)}</b> apps</span>`,
      `<span><b>${fmt(s.inBoth)}</b> in both directories</span>`,
      `<span>awesome <b>${fmt(s.bySource.awesome)}</b></span>`,
      `<span>selfh.st <b>${fmt(s.bySource.selfhst)}</b></span>`,
      s.lastSyncedAt ? `<span>synced <b>${esc(s.lastSyncedAt)}</b></span>` : '',
    ].filter(Boolean).join('');
    $('#statline').innerHTML = stat;

    const box = (title, name, opts, sel) => `<div class="facet"><h3>${title}</h3>` + opts.map(([v, label, n]) =>
      `<label><input type="checkbox" data-facet="${name}" value="${esc(v)}" ${sel.includes(v) ? 'checked' : ''}>${esc(label)}${n != null ? `<span class="n">${fmt(n)}</span>` : ''}</label>`).join('') + '</div>';

    const src = [['awesome', 'awesome-selfhosted'], ['selfhst', 'selfh.st'], ['both', 'In both']];
    let html = `<div class="facet"><h3>Source</h3>` + src.map(([v, l]) =>
      `<label><input type="radio" name="source" data-facet="source" value="${v}" ${state.source === v ? 'checked' : ''}>${l}</label>`).join('') +
      `<label><input type="radio" name="source" data-facet="source" value="" ${!state.source ? 'checked' : ''}>Any</label></div>`;
    const actOrder = ['active', 'slow', 'stale', 'archived'];
    html += box('Activity', 'activity',
      actOrder.filter(k => k in (s.activity || {})).map(k => [k, k, s.activity[k]]), [state.activity]);
    html += box('Stars', 'stars', [['lt500', '< 500'], ['500-5000', '500 – 5,000'], ['gt5000', '5,000+']], [state.stars]);
    html += box('Language', 'lang', (s.topLanguages || []).slice(0, 12).map(x => [x.value, x.value, x.count]), [state.lang]);
    html += box('License', 'license', (s.topLicenses || []).slice(0, 10).map(x => [x.value, x.value, x.count]), [state.license]);
    html += box('Tags', 'tag', (s.topTags || []).map(x => [x.value, x.value, x.count]), state.tags);
    $('#facet-body').innerHTML = html;

  } catch (e) {
    $('#statline').textContent = 'Summary unavailable.';
  }
}

// ---------- detail ----------
async function openDetail(slug, push = true) {
  const dlg = $('#detail');
  const body = $('#detail-body');
  body.innerHTML = '<p class="muted">Loading…</p>';
  if (!dlg.open) dlg.showModal();
  state.slug = slug;
  if (push) {
    history.pushState({ app: slug }, '', '/apps/' + encodeURIComponent(slug));
  }
  try {
    const r = await POST('projects/records?perPage=1&filter=' + encodeURIComponent('slug = ' + q_(slug)));
    const p = (r.items || [])[0];
    if (!p) { body.innerHTML = '<p class="empty">App not found.</p>'; return; }
    const snaps = await POST('repo_snapshots/records?perPage=120&sort=captured_date&filter=' + encodeURIComponent('project = ' + q_(p.id)));
    const links = ['homepage_url:Home', 'repo_url:Repository', 'demo_url:Demo']
      .filter(([k]) => safeUrl(p[k]))
      .map(([k, l]) => `<a href="${esc(safeUrl(p[k]))}" target="_blank" rel="noopener noreferrer">${l} ↗</a>`).join('');
    const alts = arr(p.unified_tags).length
      ? arr(p.unified_tags).map(t => `<a class="tag" href="/?tag=${encodeURIComponent(t)}" data-close>${esc(t)}</a>`).join('')
      : '';
    body.innerHTML = `
      <h2>${esc(p.name)}</h2>
      <div class="badges">${badgeFor(p)}</div>
      <p class="muted">${esc(p.description)}</p>
      <div class="links">${links}</div>
      <section><h4>Numbers</h4><div class="nums">
        <span>★ <b>${fmt(p.stars)}</b></span>
        <span>⇄ <b>${fmt(p.forks)}</b></span>
        <span>1d <b>${fmt(p.stars_1d_delta)}</b></span>
        <span>7d <b>${fmt(p.stars_7d_delta)}</b></span>
        <span>30d <b>${fmt(p.stars_30d_delta)}</b></span>
        <span>trend <b>${fmt(p.selfhst_trending)}</b></span>
      </div></section>
      <section><h4>Star history</h4>${spark(snaps.items || [])}</section>
      <section><h4>Facts</h4><div class="meta">
        ${p.repo_host ? `<span class="tag">${esc(p.repo_host)}</span>` : ''}
        ${arr(p.platforms).map(x => `<span class="tag">${esc(x)}</span>`).join('')}
        ${p.language ? `<span class="tag">${esc(p.language)}</span>` : ''}
        ${p.license ? `<span class="tag">${esc(p.license)}</span>` : ''}
        ${p.last_commit_at ? `<span class="tag">commit ${esc(p.last_commit_at)}</span>` : ''}
        ${p.latest_release_tag ? `<span class="tag">${esc(p.latest_release_tag)}</span>` : ''}
        ${p.first_seen_at ? `<span class="tag">first seen ${esc(p.first_seen_at)}</span>` : ''}
      </div></section>
      ${alts ? `<section><h4>Tags</h4><div class="altlist">${alts}</div></section>` : ''}
      <section><h4>My curation</h4><div id="curate"></div></section>
    `;
    renderCuration(p.id);
  } catch (e) {
    body.innerHTML = `<p class="empty">Could not load this app. ${esc(e.message)}</p>`;
  }
}

function spark(items) {
  const pts = items.map(r => ({ d: r.captured_date, v: Number(r.stars || 0) })).sort((a, b) => a.d < b.d ? -1 : 1);
  if (pts.length < 2) return '<p class="muted">Not enough snapshots yet — the chart fills in as daily runs accumulate.</p>';
  const w = 640, h = 90, pad = 4;
  const vs = pts.map(p => p.v);
  const lo = Math.min(...vs), hi = Math.max(...vs);
  const span = hi - lo || 1;
  const x = i => pad + i * (w - pad * 2) / (pts.length - 1);
  const y = v => h - pad - ((v - lo) / span) * (h - pad * 2);
  const line = pts.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p.v).toFixed(1)}`).join(' ');
  const area = `${line} L${x(pts.length - 1).toFixed(1)},${h - pad} L${x(0).toFixed(1)},${h - pad} Z`;
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="Star count over ${pts.length} snapshots">
    <path d="${area}" fill="color-mix(in srgb, var(--accent) 18%, transparent)"></path>
    <path d="${line}" fill="none" stroke="var(--accent)" stroke-width="2"></path>
  </svg><p class="muted">${esc(pts[0].d)} → ${esc(pts[pts.length - 1].d)} · ${fmt(lo)} → ${fmt(hi)} stars</div>`;
}

async function renderCuration(projectId) {
  const el = $('#curate');
  if (!state.auth) {
    el.innerHTML = '<p class="muted">Sign in to keep a private list of what you are watching, trying or running.</p>';
    return;
  }
  let rec = null;
  try {
    const r = await POST('curation/records?perPage=1&filter=' + encodeURIComponent('project = ' + q_(projectId)));
    rec = (r.items || [])[0] || null;
  } catch { /* falls through to an empty form */ }
  const statuses = ['inbox', 'watching', 'trying', 'deployed', 'dismissed'];
  el.innerHTML = `
    <div class="curate-row">
      <select id="cur-status">${statuses.map(s => `<option ${rec && rec.status === s ? 'selected' : ''}>${s}</option>`).join('')}</select>
      <input id="cur-priority" type="number" min="0" max="5" placeholder="priority" value="${rec && rec.priority != null ? esc(rec.priority) : ''}">
    </div>
    <p></p>
    <textarea id="cur-memo" placeholder="Notes…">${esc(rec && rec.memo ? rec.memo : '')}</textarea>
    <div class="row-end"><button id="cur-save" class="primary">Save</button></div>
    <p id="cur-msg" class="muted"></p>`;
  $('#cur-save').onclick = async () => {
    const body = {
      project: projectId,
      status: $('#cur-status').value,
      priority: Number($('#cur-priority').value || 0),
      memo: $('#cur-memo').value,
      owner: state.auth.email,
    };
    try {
      const url = rec ? `${API}/curation/records/${rec.id}` : `${API}/curation/records`;
      const res = await fetch(url, {
        method: rec ? 'PATCH' : 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: state.auth.token },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error((await res.json()).message || res.status);
      $('#cur-msg').textContent = 'Saved.';
    } catch (e) {
      $('#cur-msg').textContent = 'Save failed: ' + e.message;
    }
  };
}

// ---------- changes feed ----------
async function loadRadar() {
  const list = $('#list');
  list.className = 'radar-list';
  $('#pager').innerHTML = '';
  $('#count').textContent = 'Recent changes';
  list.innerHTML = '<div class="skeleton"></div><div class="skeleton"></div>';
  const flt = state.preset ? 'event_type = ' + q_(state.preset) : '';
  try {
    const d = await POST('surveillance_events/records?perPage=60&sort=-occurred_at&expand=project' +
      (flt ? '&filter=' + encodeURIComponent(flt) : ''));
    const items = d.items || [];
    $('#count').textContent = `${fmt(d.totalItems)} events · RSS at /radar.xml`;
    list.innerHTML = items.length ? items.map(e => {
      const p = e.expand && e.expand.project;
      return `<div class="ev" ${p ? `data-slug="${esc(p.slug)}"` : ''}>
        <time datetime="${esc(e.occurred_at)}">${esc(e.occurred_at)}</time>
        <span class="etype">${esc(e.event_type.replace(/_/g, ' '))}</span>
        <span class="t">${esc(e.title)}</span>
      </div>`;
    }).join('') : '<p class="empty">No events yet.</p>';
  } catch (e) {
    list.innerHTML = `<p class="empty">Could not load the change feed. ${esc(e.message)}</p>`;
  }
}

// ---------- auth ----------
function saveAuth(a) {
  state.auth = a;
  if (a) localStorage.setItem('radar_auth', JSON.stringify(a));
  else localStorage.removeItem('radar_auth');
  $('#login').textContent = a ? (a.email.split('@')[0] + ' · sign out') : 'Sign in';
}
function restoreAuth() {
  try {
    const a = JSON.parse(localStorage.getItem('radar_auth') || 'null');
    if (a && a.token) saveAuth(a);
  } catch { /* ignore */ }
}

// ---------- wiring ----------
function render() {
  if (state.view === 'explore') {
    document.querySelectorAll('#presets [data-preset]').forEach(c =>
      c.classList.toggle('is-on', c.dataset.preset === state.preset));
  }
  if (state.view === 'radar') {
    $('#presets').innerHTML = ['', 'new_in_awesome', 'new_in_selfhst', 'new_release', 'star_surge', 'archived_or_stale']
      .map(v => `<button class="chip ${state.preset === v ? 'is-on' : ''}" data-rpreset="${v}">${v ? v.replace(/_/g, ' ') : 'All'}</button>`).join('');
    loadRadar();
  } else {
    loadExplore();
  }
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('is-on', t.dataset.view === state.view));
}

function bind() {
  $('#q').addEventListener('input', e => {
    state.q = e.target.value;
    state.page = 1;
    clearTimeout(bind.t);
    bind.t = setTimeout(render, 220);
  });
  document.querySelectorAll('.tab').forEach(t => t.addEventListener('click', () => {
    state.view = t.dataset.view;
    state.preset = '';
    const url = state.view === 'radar' ? '/radar' : '/';
    history.pushState({}, '', url);
    state.page = 1;
    render();
  }));
  $('#presets').addEventListener('click', e => {
    const b = e.target.closest('[data-preset],[data-rpreset]');
    if (!b) return;
    if (state.view === 'radar') { state.preset = b.dataset.rpreset; render(); return; }
    state.preset = b.dataset.preset;
    // "in both directories" is a source facet, everything else is its own filter
    state.source = state.preset === 'both' ? 'both' : '';
    document.querySelectorAll('#presets .chip').forEach(c => c.classList.toggle('is-on', c === b));
    state.page = 1;
    render();
  });
  $('#sort').addEventListener('change', e => { state.sort = e.target.value; state.page = 1; render(); });
  $('#view-cards').addEventListener('click', () => {
    state.layout = 'cards';
    $('#view-cards').classList.add('is-on'); $('#view-table').classList.remove('is-on');
    render();
  });
  $('#view-table').addEventListener('click', () => {
    state.layout = 'table';
    $('#view-table').classList.add('is-on'); $('#view-cards').classList.remove('is-on');
    render();
  });
  $('#facet-body').addEventListener('change', e => {
    const f = e.target.dataset.facet;
    if (!f) return;
    if (f === 'source') { state.source = e.target.value; }
    else if (f === 'tag') {
      state.tags = [...document.querySelectorAll('[data-facet="tag"]:checked')].map(i => i.value);
    } else if (f === 'activity' || f === 'stars') {
      state[f] = e.target.checked ? e.target.value : '';
      document.querySelectorAll(`[data-facet="${f}"]`).forEach(i => { if (i !== e.target) i.checked = false; });
    } else {
      state[f] = e.target.checked ? e.target.value : '';
    }
    state.page = 1;
    render();
  });
  $('#facet-toggle').addEventListener('click', () => {
    const on = $('#facets').classList.toggle('open');
    $('#facet-toggle').setAttribute('aria-expanded', on ? 'true' : 'false');
  });
  $('#clear').addEventListener('click', () => {
    Object.assign(state, { q: '', preset: '', source: '', tags: [], lang: '', license: '', activity: '', stars: '', page: 1 });
    $('#q').value = '';
    loadFacets();
    render();
  });
  $('#pager').addEventListener('click', e => {
    const b = e.target.closest('button[data-p]');
    if (!b || b.disabled) return;
    state.page = Number(b.dataset.p);
    render();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  });
  $('#list').addEventListener('click', e => {
    const row = e.target.closest('[data-slug]');
    if (!row) return;
    if (e.target.closest('a')) return;
    openDetail(row.dataset.slug);
  });
  $('#list').addEventListener('keydown', e => {
    if (e.key !== 'Enter') return;
    const row = e.target.closest('[data-slug]');
    if (row) openDetail(row.dataset.slug);
  });
  $('#detail').addEventListener('click', e => {
    if (e.target.id === 'detail-close' || e.target.closest('[data-close]')) {
      $('#detail').close();
      if (location.pathname.startsWith('/apps/')) history.pushState({}, '', '/');
    }
  });
  $('#theme').addEventListener('click', () => {
    const cur = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
    document.documentElement.dataset.theme = cur;
    localStorage.setItem('radar_theme', cur);
  });
  $('#login').addEventListener('click', () => {
    if (state.auth) { saveAuth(null); $('#detail').close(); return; }
    $('#authdlg').showModal();
  });
  $('#auth-cancel').addEventListener('click', () => $('#authdlg').close());
  $('#authform').addEventListener('submit', async e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    $('#auth-err').textContent = '';
    try {
      const r = await fetch(API + '/users/auth-with-password', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ identity: fd.get('email'), password: fd.get('password') }),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.message || 'Sign in failed');
      saveAuth({ token: j.token, email: j.record ? j.record.email : fd.get('email') });
      $('#authdlg').close();
      if (state.slug) openDetail(state.slug, false);
    } catch (err) {
      $('#auth-err').textContent = err.message;
    }
  });
  window.addEventListener('popstate', () => {
    readUrl();
    if (state.slug) openDetail(state.slug, false);
    else if ($('#detail').open) $('#detail').close();
    render();
  });
}

function boot() {
  const theme = localStorage.getItem('radar_theme');
  document.documentElement.dataset.theme = theme || 'dark';
  restoreAuth();
  readUrl();
  $('#q').value = state.q;
  $('#sort').value = state.sort;
  if (state.layout === 'table') { $('#view-table').classList.add('is-on'); $('#view-cards').classList.remove('is-on'); }
  loadFacets();
  bind();
  render();
  if (state.slug) openDetail(state.slug, false);
}

boot();
