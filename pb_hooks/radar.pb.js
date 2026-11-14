/// <reference path="../pb_data/types.d.ts" />
// Read-side helpers for the explorer: one cached summary call, one RSS feed,
// and a slug route that serves the app shell with per-app metadata.

routerAdd('GET', '/api/radar/summary', (e) => {
  const cache = $app.store().get('radar_summary')
  const now = Date.now()
  if (cache && now - cache.at < 300000) {
    return e.json(200, cache.body)
  }
  // $app.countRecords returns 0 on this build; count through the filter API.
  const countOf = (coll, flt) => {
    try {
      let n = 0
      for (let page = 0; page < 40; page++) {
        const batch = $app.findRecordsByFilter(coll, flt, '', 500, page * 500)
        n += batch.length
        if (batch.length < 500) break
      }
      return n
    } catch (_) {
      return 0
    }
  }
  const total = countOf('projects', '1=1')
  const bySource = {
    awesome: countOf('source_entries', 'source = "awesome"'),
    selfhst: countOf('source_entries', 'source = "selfhst"'),
  }

  // Facets: walk a bounded sample. 2k rows is cheap once per cache window.
  const tags = {}
  const langs = {}
  const lics = {}
  const bump = (cnt, key) => {
    if (!key) return
    cnt[key] = (cnt[key] || 0) + 1
  }
  let rows = []
  try {
    rows = $app.findRecordsByFilter('projects', '1=1', '', 4000, 0)
  } catch (_) {
    rows = []
  }
  let lastSync = ''
  for (const r of rows) {
    let ts = []
    try {
      ts = JSON.parse(r.getString('unified_tags') || '[]')
    } catch (_) {
      ts = []
    }
    for (const t of ts) bump(tags, String(t))
    bump(langs, r.getString('language'))
    bump(lics, r.getString('license'))
    const d = r.getString('last_synced_at')
    if (d > lastSync) lastSync = d
  }
  const top = (cnt, n) =>
    Object.entries(cnt)
      .sort((a, b) => b[1] - a[1])
      .slice(0, n)
      .map(([k, v]) => ({ value: k, count: v }))

  const body = {
    total: total,
    bySource: bySource,
    entryCount: bySource.awesome + bySource.selfhst,
    inBoth: countOf('projects', 'sources ~ "awesome" && sources ~ "selfhst"'),
    lastSyncedAt: lastSync,
    topTags: top(tags, 40),
    topLanguages: top(langs, 25),
    topLicenses: top(lics, 20),
    activity: {
      active: countOf('projects', 'activity_status = "active"'),
      slow: countOf('projects', 'activity_status = "slow"'),
      stale: countOf('projects', 'activity_status = "stale"'),
      archived: countOf('projects', 'activity_status = "archived"'),
    },
    events: countOf('surveillance_events', '1=1'),
    generatedAt: new Date().toISOString(),
  }
  $app.store().set('radar_summary', { at: now, body: body })
  return e.json(200, body)
})

routerAdd('GET', '/radar.xml', (e) => {
  const esc = (s) =>
    String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&apos;' })[c])
  let items = []
  try {
    items = $app.findRecordsByFilter('surveillance_events', '1=1', '-occurred_at', 50, 0)
  } catch (_) {
    items = []
  }
  const site = 'https://selfhosted.pockethost.io'
  let xml = '<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel>'
  xml += '<title>Self-Hosted Radar</title>'
  xml += '<link>' + site + '/</link>'
  xml += '<description>Changes in the self-hosted app directories</description>'
  xml += '<language>en</language>'
  for (const rec of items) {
    let slug = ''
    let pname = ''
    try {
      const pid = rec.getString('project')
      if (pid) {
        const p = $app.findRecordById('projects', pid)
        slug = p.getString('slug')
        pname = p.getString('name')
      }
    } catch (_) {
      slug = ''
    }
    if (!slug) {
      // keep the item; fall back to the event title alone
    }
    const occurred = rec.getString('occurred_at')
    const etype = rec.getString('event_type')
    const link = slug ? site + '/apps/' + encodeURIComponent(slug) : site + '/'
    xml += '<item>'
    xml += '<title>' + esc(rec.getString('title')) + '</title>'
    xml += '<link>' + esc(link) + '</link>'
    xml += '<guid isPermaLink="false">' + esc(etype + ':' + slug + ':' + occurred) + '</guid>'
    xml += '<category>' + esc(etype) + '</category>'
    xml += '<pubDate>' + esc(new Date(occurred + 'T00:00:00Z').toUTCString()) + '</pubDate>'
    xml += '<description>' + esc(pname + ' · ' + rec.getString('source')) + '</description>'
    xml += '</item>'
  }
  xml += '</channel></rss>'
  e.response.header().set('Content-Type', 'application/rss+xml; charset=utf-8')
  return e.string(200, xml)
})

// /apps/<slug> serves the same shell; the client reads the slug from the path.
routerAdd('GET', '/apps/{slug}', (e) => {
  return e.file($app.root + '/pb_public/index.html')
})
