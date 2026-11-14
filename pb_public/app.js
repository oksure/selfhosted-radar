// Self-Hosted Radar — no build step. Everything renders through textContent
// or escaped markup so upstream descriptions can never inject HTML.

const API = '/api/collections';
const DEFAULT_PER = 30;
const PER_OPTIONS = [30, 60, 100, 200];
const $ = (s, r = document) => r.querySelector(s);
const POST = q => fetch(API + '/' + q).then(r => r.ok ? r.json() : Promise.reject(new Error(q + ' -> ' + r.status)));

const state = {
  view: 'explore',
  q: '',
  preset: '',
  source: '',        // awesome | selfhst | both
  inUse: false,
  tags: [],
  showAllTags: false,
  lang: '',
  license: '',
  activity: '',      // '' (all) | active | slow | stale
  hideArchived: true,
  stars: '',         // '' | 0-500 | 500-5000 | 5000+
  sort: '-selfhst_trending',
  perPage: DEFAULT_PER,
  page: 1,
  layout: 'cards',
  slug: '',          // detail route
  auth: null,
  curation: {},      // projectId -> curation record
  markFilter: '',    // '' | marked | watching | trying | deployed | dismissed
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
  state.inUse = u.get('inuse') === '1' || state.preset === 'in-use';
  state.tags = (u.get('tag') || '').split(',').filter(Boolean);
  state.lang = u.get('lang') || '';
  state.license = u.get('license') || '';
  state.activity = u.get('activity') || '';
  state.stars = u.get('stars') || '';
  state.hideArchived = u.get('arch') !== '1' ? true : false;
  state.sort = u.get('sort') || '-selfhst_trending';
  const rawPer = parseInt(u.get('per') || '', 10);
  state.perPage = PER_OPTIONS.includes(rawPer) ? rawPer : DEFAULT_PER;
  state.page = Math.max(1, parseInt(u.get('page') || '1', 10) || 1);
  if ((state.preset === 'both' || state.preset === 'gh_trending' || state.preset === 'gh_popular') && !state.source) state.source = state.preset;
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
  if (state.inUse && state.preset !== 'in-use') u.set('inuse', '1');
  if (state.tags.length) u.set('tag', state.tags.join(','));
  if (state.lang) u.set('lang', state.lang);
  if (state.license) u.set('license', state.license);
  if (state.activity) u.set('activity', state.activity);
  if (state.stars) u.set('stars', state.stars);
  if (!state.hideArchived) u.set('arch', '1');
  if (state.sort !== '-selfhst_trending') u.set('sort', state.sort);
  if (state.perPage !== DEFAULT_PER) u.set('per', String(state.perPage));
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
  if (state.inUse || state.preset === 'in-use') parts.push('unified_tags ~ "in-use"');
  if (state.preset === 'surge') parts.push('stars_7d_delta >= 20');  // history builds from day 1
  if (state.preset === 'release' && !state.q) parts.push('latest_release_at != ""');
  if (state.preset === 'new') {
    const d = new Date(Date.now() - 30 * 864e5).toISOString().slice(0, 10);
    parts.push('first_seen_at >= ' + q_(d));
  }
  const wantStarred = state.preset === 'starred' || state.markFilter === 'starred' || state.markFilter === 'marked';
  if (wantStarred) {
    if (!state.auth) return 'id = ""';
    const ids = Object.values(state.curation)
      .filter(c => c.status && c.status !== 'dismissed')
      .map(c => c.project);
    if (!ids.length) return 'id = ""';
    parts.push('(' + ids.slice(0, 150).map(id => 'id = ' + q_(id)).join(' || ') + ')');
  } else if (state.auth && state.markFilter) {
    const ids = Object.values(state.curation)
      .filter(c => c.status === state.markFilter)
      .map(c => c.project);
    if (!ids.length) return 'id = ""';
    parts.push('(' + ids.slice(0, 150).map(id => 'id = ' + q_(id)).join(' || ') + ')');
  }
  return parts.join(' && ');
}

const isStarred = cur => Boolean(cur && cur.status && cur.status !== 'dismissed');

function quickMarkBar(p) {
  if (!state.auth) return '';
  const on = isStarred(state.curation[p.id]);
  return `<div class="qmarks" data-pid="${esc(p.id)}"><button type="button" class="qmark-btn ${on ? 'is-on is-starred' : ''}" data-mark="watching" title="Starred">${on ? '★ Starred' : '☆ Star'}</button></div>`;
}

function cardLinks(p) {
  const hp = safeUrl(p.homepage_url);
  const rp = safeUrl(p.repo_url);
  const out = [];
  if (hp) out.push(`<a class="card-link" href="${esc(hp)}" target="_blank" rel="noopener noreferrer">Home ↗</a>`);
  if (rp) out.push(`<a class="card-link" href="${esc(rp)}" target="_blank" rel="noopener noreferrer">${rp.includes('github.com') ? 'GitHub ↗' : 'Repo ↗'}</a>`);
  return out.length ? `<div class="card-links">${out.join('')}</div>` : '<div></div>';
}

// ---------- renderers ----------
function badgeFor(p) {
  const srcs = arr(p.sources);
  const out = [];
  if (arr(p.unified_tags).includes('in-use')) out.push('<span class="badge use">in use</span>');
  if (srcs.includes('awesome') && srcs.includes('selfhst')) out.push('<span class="badge both">both</span>');
  else if (srcs.includes('awesome')) out.push('<span class="badge a">awesome</span>');
  else if (srcs.includes('selfhst')) out.push('<span class="badge s">selfh.st</span>');
  if (srcs.includes('gh_trending')) out.push('<span class="badge gt">GH trending</span>');
  if (srcs.includes('gh_popular')) out.push('<span class="badge gp">GH popular</span>');
  if (p.activity_status === 'archived') out.push('<span class="badge arch">archived</span>');
  else if (p.activity_status === 'stale') out.push('<span class="badge arch">stale</span>');
  else if (p.activity_status === 'slow') out.push('<span class="badge slow">slow</span>');
  return out.join('');
}

// ---------- icons ----------
function normalizeIconUrl(raw) {
  const s = String(raw || '').trim();
  if (!s) return '';
  const u = safeUrl(s);
  if (u) return u;
  if (/^[a-z0-9._-]+$/i.test(s)) return `https://cdn.jsdelivr.net/gh/selfhst/icons/png/${encodeURIComponent(s)}.png`;
  return '';
}

function githubAvatarUrl(repoUrl) {
  const m = String(repoUrl || '').match(/^https?:\/\/(?:www\.)?github\.com\/([^/]+)\/[^/]+/i);
  return m ? `https://github.com/${encodeURIComponent(m[1])}.png?size=64` : '';
}

function iconFallbackHtml(name) {
  const clean = String(name || '?').trim();
  const words = clean.split(/[\s\-_/]+/).filter(Boolean);
  const initials = words.length >= 2
    ? (words[0][0] + words[1][0]).toUpperCase()
    : clean.slice(0, 2).toUpperCase();
  let h = 0;
  for (let i = 0; i < clean.length; i++) h = (h * 31 + clean.charCodeAt(i)) % 360;
  return `<div class="icon icon-fallback" style="--ic-h:${h}" aria-hidden="true">${esc(initials || '?')}</div>`;
}

window._radarIconErr = img => {
  const fb = img.dataset.fb || '';
  if (fb) {
    img.dataset.fb = '';
    img.src = fb;
    return;
  }
  const tpl = document.createElement('template');
  tpl.innerHTML = iconFallbackHtml(img.dataset.name || '');
  img.replaceWith(tpl.content.firstChild);
};

function iconHtml(p) {
  const primary = normalizeIconUrl(p.icon_url);
  const gh = githubAvatarUrl(p.repo_url);
  const src = primary || gh;
  const fb = primary && gh && primary !== gh ? gh : '';
  if (!src) return iconFallbackHtml(p.name);
  return `<img class="icon" src="${esc(src)}" data-fb="${esc(fb)}" data-name="${esc(p.name)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="window._radarIconErr(this)">`;
}

function card(p) {
  const tags = arr(p.unified_tags)
    .filter(t => t !== 'in-use')
    .slice(0, 4)
    .map(t => `<button type="button" class="tag tag-btn${state.tags.includes(t) ? ' is-on' : ''}" data-tag="${esc(t)}">${esc(t)}</button>`)
    .join('');
  const d7 = Number(p.stars_7d_delta || 0);
  const cur = state.auth ? state.curation[p.id] : null;
  const stCls = isStarred(cur) ? ' is-mark-watching' : '';
  return `<article class="card${stCls}" data-slug="${esc(p.slug)}" tabindex="0" role="button">
    <div class="card-top">
      ${iconHtml(p)}
      <div style="min-width:0;flex:1">
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
    <div class="card-foot">${cardLinks(p)}${quickMarkBar(p)}</div>
  </article>`;
}

function table(rows) {
  const body = rows.map(p => {
    const cur = state.auth ? state.curation[p.id] : null;
    const stCls = isStarred(cur) ? ' is-mark-watching' : '';
    return `<tr class="${stCls}" data-slug="${esc(p.slug)}">
    <td><b>${esc(p.name)}</b><div class="meta">${badgeFor(p)}</div><div class="card-foot">${cardLinks(p)}${quickMarkBar(p)}</div></td>
    <td class="num">${fmt(p.stars)}</td>
    <td class="num">${p.stars_7d_delta ? '+' + fmt(p.stars_7d_delta) : '—'}</td>
    <td class="num">${p.selfhst_trending ? fmt(p.selfhst_trending) : '—'}</td>
    <td>${esc(p.language || '—')}</td>
    <td>${esc(p.license || '—')}</td>
    <td>${esc(p.last_commit_at || '—')}</td>
    <td>${esc(p.latest_release_at || '—')}</td>
  </tr>`;
  }).join('');
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
  const params = { sort: state.sort, page: state.page, perPage: state.perPage };
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
    if (flt !== lastFacetFilter) loadFacets(flt);
  } catch (e) {
    if (token !== reqToken) return;
    list.className = '';
    list.innerHTML = `<p class="empty">Could not load applications. ${esc(e.message)}</p>`;
  }
}

function renderPager(d) {
  const pages = d.totalPages || 1;
  const p = d.page || 1;
  if (pages <= 1) { $('#pager').innerHTML = ''; return; }
  const nums = new Set([1, pages]);
  for (let i = Math.max(1, p - 2); i <= Math.min(pages, p + 2); i++) nums.add(i);
  const sorted = [...nums].sort((a, b) => a - b);
  let btns = `<button data-p="${p - 1}" ${p <= 1 ? 'disabled' : ''}>← Prev</button>`;
  let prev = 0;
  for (const n of sorted) {
    if (n - prev > 1) btns += '<span class="pager-dots">…</span>';
    btns += `<button data-p="${n}" class="${n === p ? 'is-on' : ''}" ${n === p ? 'aria-current="page"' : ''}>${fmt(n)}</button>`;
    prev = n;
  }
  btns += `<button data-p="${p + 1}" ${p >= pages ? 'disabled' : ''}>Next →</button>`;
  btns += `<form class="pager-jump" data-pages="${pages}"><span>Go to</span><input type="number" min="1" max="${pages}" value="${p}" aria-label="Jump to page"><span>/ ${fmt(pages)}</span><button type="submit">Go</button></form>`;
  $('#pager').innerHTML = btns;
}

let lastSummary = null;
let lastFacetFilter = null;
let facetToken = 0;

function ensureSelected(opts, selValues) {
  const seen = new Set(opts.map(x => x.value));
  const out = [...opts];
  for (const v of selValues) {
    if (v && !seen.has(v)) out.unshift({ value: v, count: 0 });
  }
  return out;
}

function renderFacets(s) {
  if (!s) return;
  const box = (title, name, opts, sel, extra = '') => `<div class="facet"><h3>${title}</h3>` + opts.map(([v, label, n]) =>
    `<label title="${esc(label)}"><input type="checkbox" data-facet="${name}" value="${esc(v)}" ${sel.includes(v) ? 'checked' : ''}><span class="facet-lbl">${esc(label)}</span>${n != null ? `<span class="n">${fmt(n)}</span>` : ''}</label>`).join('') + extra + '</div>';

  const starredTotal = s.starred != null
    ? s.starred
    : (state.auth ? Object.values(state.curation).filter(isStarred).length : null);
  const inUseOn = state.inUse || state.preset === 'in-use';
  const starredOn = state.markFilter === 'starred' || state.preset === 'starred';
  let html = `<div class="facet"><h3>My List</h3>` +
    `<label title="In Use"><input type="checkbox" data-facet="inuse" value="1" ${inUseOn ? 'checked' : ''}><span class="facet-lbl">In Use</span>${s.inUse != null ? `<span class="n">${fmt(s.inUse)}</span>` : ''}</label>` +
    `<label title="Starred"><input type="checkbox" data-facet="mark" value="starred" ${starredOn ? 'checked' : ''}><span class="facet-lbl">Starred</span><span class="n">${starredTotal != null ? fmt(starredTotal) : 'sign in'}</span></label>` +
    `</div>`;

  const bs = s.bySource || {};
  const src = [
    ['awesome', 'awesome-selfhosted', bs.awesome],
    ['selfhst', 'selfh.st', bs.selfhst],
    ['gh_trending', 'GitHub Trending', bs.gh_trending ?? 0],
    ['gh_popular', 'GitHub Popular', bs.gh_popular ?? 0],
    ['both', 'In both dirs', bs.both ?? s.inBoth],
  ];
  html += `<div class="facet"><h3>Source</h3>` + src.map(([v, l, n]) =>
    `<label title="${esc(l)}"><input type="radio" name="source" data-facet="source" value="${v}" ${state.source === v ? 'checked' : ''}><span class="facet-lbl">${l}</span>${n != null ? `<span class="n">${fmt(n)}</span>` : ''}</label>`).join('') +
    `<label title="Any"><input type="radio" name="source" data-facet="source" value="" ${!state.source ? 'checked' : ''}><span class="facet-lbl">Any</span><span class="n">${fmt(bs.any ?? s.total)}</span></label></div>`;

  const actOrder = ['active', 'slow', 'stale', 'archived'];
  html += box('Activity', 'activity',
    actOrder.map(k => [k, k, (s.activity || {})[k] ?? 0]), [state.activity]);

  const st = s.stars || {};
  html += box('Stars', 'stars', [
    ['lt500', '< 500', st.lt500 ?? 0],
    ['500-5000', '500 – 5,000', st['500-5000'] ?? 0],
    ['gt5000', '5,000+', st.gt5000 ?? 0],
  ], [state.stars]);

  const langs = ensureSelected((s.topLanguages || []).slice(0, 12), state.lang ? [state.lang] : []);
  html += box('Language', 'lang', langs.map(x => [x.value, x.value, x.count]), [state.lang]);

  const lics = ensureSelected((s.topLicenses || []).slice(0, 10), state.license ? [state.license] : []);
  html += box('License', 'license', lics.map(x => [x.value, x.value, x.count]), [state.license]);

  const allTags = ensureSelected((s.topTags || []).filter(x => x.value !== 'in-use'), state.tags);
  const shownTags = state.showAllTags ? allTags : allTags.slice(0, 12);
  const moreBtn = allTags.length > 12
    ? `<button type="button" id="tags-more" class="link more">${state.showAllTags ? 'Show fewer' : `Show all (${allTags.length})`}</button>`
    : '';
  html += box('Tags', 'tag', shownTags.map(x => [x.value, x.value, x.count]), state.tags, moreBtn);
  $('#facet-body').innerHTML = html;
}

async function loadFacets(flt = '') {
  const token = ++facetToken;
  lastFacetFilter = flt;
  try {
    const url = '/api/radar/summary' + (flt ? '?filter=' + encodeURIComponent(flt) : '');
    const opts = state.auth ? { headers: { Authorization: state.auth.token } } : {};
    const s = await fetch(url, opts).then(r => r.json());
    if (!flt) {
      const stat = [
        `<span><b>${fmt(s.total)}</b> apps</span>`,
        `<span><b>${fmt(s.inBoth)}</b> in both directories</span>`,
        `<span>awesome <b>${fmt(s.bySource.awesome)}</b></span>`,
        `<span>selfh.st <b>${fmt(s.bySource.selfhst)}</b></span>`,
        s.bySource.gh_trending ? `<span>GH trending <b>${fmt(s.bySource.gh_trending)}</b></span>` : '',
        s.bySource.gh_popular ? `<span>GH popular <b>${fmt(s.bySource.gh_popular)}</b></span>` : '',
        s.lastSyncedAt ? `<span>synced <b>${esc(s.lastSyncedAt)}</b></span>` : '',
      ].filter(Boolean).join('');
      $('#statline').innerHTML = stat;
    }
    if (token !== facetToken) return;
    lastSummary = s;
    renderFacets(s);
  } catch (e) {
    if (!flt) $('#statline').textContent = 'Summary unavailable.';
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
      <div class="card-top" style="margin-bottom:8px">
        ${iconHtml(p)}
        <div style="min-width:0;flex:1">
          <h2>${esc(p.name)}</h2>
          <div class="badges">${badgeFor(p)}</div>
        </div>
      </div>
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

async function loadCurationMap() {
  state.curation = {};
  if (!state.auth) return;
  try {
    const r = await fetch(`${API}/curation/records?perPage=500`, {
      headers: { Authorization: state.auth.token },
    });
    if (!r.ok) {
      if (r.status === 401 || r.status === 403) saveAuth(null);
      return;
    }
    const j = await r.json();
    for (const item of (j.items || [])) {
      state.curation[item.project] = item;
    }
  } catch { /* ignore */ }
}

async function toggleQuickMark(projectId, nextStatus, wrapEl) {
  if (!state.auth) return;
  const cur = state.curation[projectId] || null;
  const clearing = isStarred(cur);
  wrapEl.classList.add('is-busy');
  try {
    if (clearing) {
      const res = await fetch(`${API}/curation/records/${cur.id}`, {
        method: 'DELETE',
        headers: { Authorization: state.auth.token },
      });
      if (!res.ok) throw new Error(res.status);
      delete state.curation[projectId];
    } else if (cur) {
      const res = await fetch(`${API}/curation/records/${cur.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', Authorization: state.auth.token },
        body: JSON.stringify({ status: nextStatus, owner: state.auth.email }),
      });
      if (!res.ok) throw new Error(res.status);
      state.curation[projectId] = await res.json();
    } else {
      const res = await fetch(`${API}/curation/records`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: state.auth.token },
        body: JSON.stringify({ project: projectId, status: nextStatus, priority: 0, memo: '', owner: state.auth.email }),
      });
      if (!res.ok) throw new Error(res.status);
      state.curation[projectId] = await res.json();
    }
    const now = state.curation[projectId];
    const on = isStarred(now);
    const cardEl = wrapEl.closest('[data-slug]');
    if (cardEl) {
      cardEl.classList.toggle('is-mark-watching', on);
    }
    wrapEl.querySelectorAll('.qmark-btn').forEach(b => {
      b.className = 'qmark-btn' + (on ? ' is-on is-starred' : '');
      b.textContent = on ? '★ Starred' : '☆ Star';
    });
    loadFacets(filterString());
  } catch (e) {
    console.error('Star toggle failed', e);
  } finally {
    wrapEl.classList.remove('is-busy');
  }
}

async function renderCuration(projectId) {
  const el = $('#curate');
  if (!state.auth) {
    el.innerHTML = '<p class="muted">Sign in to star apps and filter your saved list.</p>';
    return;
  }
  const on = isStarred(state.curation[projectId]);
  el.innerHTML = `<div class="qmarks" data-pid="${esc(projectId)}"><button type="button" class="qmark-btn ${on ? 'is-on is-starred' : ''}" data-mark="watching">${on ? '★ Starred' : '☆ Star'}</button></div>`;
  el.querySelector('.qmark-btn').onclick = async () => {
    await toggleQuickMark(projectId, 'watching', el.querySelector('.qmarks'));
    if (state.view === 'explore') loadExplore();
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
  else {
    localStorage.removeItem('radar_auth');
    state.curation = {};
    state.markFilter = '';
  }
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
  if ($('#per-page')) $('#per-page').value = String(state.perPage);
  if ($('#sort')) $('#sort').value = state.sort;
  if (state.view === 'explore') {
    const activeChip = state.preset || (state.inUse ? 'in-use' : state.markFilter === 'starred' ? 'starred' : (state.source === 'both' || state.source === 'gh_trending' || state.source === 'gh_popular') ? state.source : '');
    document.querySelectorAll('#presets [data-preset]').forEach(c =>
      c.classList.toggle('is-on', c.dataset.preset === activeChip));
  }
  if (state.view === 'radar') {
    $('#presets').innerHTML = ['', 'new_in_awesome', 'new_in_selfhst', 'new_in_gh_trending', 'new_in_gh_popular', 'new_release', 'star_surge', 'archived_or_stale']
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
    // source presets map directly to the source facet
    state.source = (state.preset === 'both' || state.preset === 'gh_trending' || state.preset === 'gh_popular') ? state.preset : '';
    state.inUse = state.preset === 'in-use';
    state.markFilter = state.preset === 'starred' ? 'starred' : '';
    document.querySelectorAll('#presets .chip').forEach(c => c.classList.toggle('is-on', c === b));
    renderFacets(lastSummary);
    state.page = 1;
    render();
  });
  $('#sort').addEventListener('change', e => { state.sort = e.target.value; state.page = 1; render(); });
  $('#per-page').addEventListener('change', e => { state.perPage = Number(e.target.value) || DEFAULT_PER; state.page = 1; render(); });
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
  $('#facet-body').addEventListener('click', e => {
    if (e.target.id === 'tags-more') {
      state.showAllTags = !state.showAllTags;
      renderFacets(lastSummary);
    }
  });
  $('#facet-body').addEventListener('change', e => {
    const f = e.target.dataset.facet;
    if (!f) return;
    if (f === 'source') {
      state.source = e.target.value;
      if ((state.preset === 'both' || state.preset === 'gh_trending' || state.preset === 'gh_popular') && state.source !== state.preset) state.preset = '';
    } else if (f === 'inuse') {
      state.inUse = e.target.checked;
      if (!state.inUse && state.preset === 'in-use') state.preset = '';
    } else if (f === 'tag') {
      state.tags = [...document.querySelectorAll('[data-facet="tag"]:checked')].map(i => i.value);
    } else if (f === 'mark') {
      if (e.target.checked && !state.auth) {
        e.target.checked = false;
        $('#authdlg').showModal();
        return;
      }
      state.markFilter = e.target.checked ? e.target.value : '';
      if (!state.markFilter && state.preset === 'starred') state.preset = '';
      document.querySelectorAll(`[data-facet="mark"]`).forEach(i => { if (i !== e.target) i.checked = false; });
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
    Object.assign(state, { q: '', preset: '', source: '', inUse: false, tags: [], lang: '', license: '', activity: '', stars: '', markFilter: '', page: 1 });
    $('#q').value = '';
    renderFacets(lastSummary);
    render();
  });
  $('#pager').addEventListener('click', e => {
    const b = e.target.closest('button[data-p]');
    if (!b || b.disabled) return;
    state.page = Number(b.dataset.p);
    render();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  });
  $('#pager').addEventListener('submit', e => {
    const f = e.target.closest('.pager-jump');
    if (!f) return;
    e.preventDefault();
    const maxP = Number(f.dataset.pages) || 1;
    const inp = f.querySelector('input');
    const n = Math.min(maxP, Math.max(1, parseInt(inp.value || '1', 10) || 1));
    if (n === state.page) return;
    state.page = n;
    render();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  });
  $('#list').addEventListener('click', e => {
    const tbtn = e.target.closest('.tag-btn[data-tag]');
    if (tbtn) {
      e.stopPropagation();
      const t = tbtn.dataset.tag;
      if (state.tags.includes(t)) state.tags = state.tags.filter(x => x !== t);
      else state.tags = [...state.tags, t];
      state.page = 1;
      loadFacets(filterString());
      render();
      return;
    }
    const qbtn = e.target.closest('.qmark-btn');
    if (qbtn) {
      e.stopPropagation();
      const wrap = qbtn.closest('.qmarks');
      if (wrap) toggleQuickMark(wrap.dataset.pid, qbtn.dataset.mark, wrap);
      return;
    }
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
    if (state.auth) {
      saveAuth(null);
      $('#detail').close();
      loadFacets(filterString());
      render();
      return;
    }
    $('#authdlg').showModal();
  });
  $('#auth-cancel').addEventListener('click', () => $('#authdlg').close());
  $('#authform').addEventListener('submit', async e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    $('#auth-err').textContent = '';
    try {
      const r = await fetch(API + '/_superusers/auth-with-password', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ identity: fd.get('email'), password: fd.get('password') }),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.message || 'Sign in failed');
      saveAuth({ token: j.token, email: j.record ? j.record.email : fd.get('email') });
      await loadCurationMap();
      $('#authdlg').close();
      loadFacets(filterString());
      render();
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

async function boot() {
  const theme = localStorage.getItem('radar_theme');
  document.documentElement.dataset.theme = theme || 'dark';
  restoreAuth();
  readUrl();
  $('#q').value = state.q;
  $('#sort').value = state.sort;
  if (state.layout === 'table') { $('#view-table').classList.add('is-on'); $('#view-cards').classList.remove('is-on'); }
  await loadCurationMap();
  loadFacets();
  bind();
  render();
  if (state.slug) openDetail(state.slug, false);
}

boot();
