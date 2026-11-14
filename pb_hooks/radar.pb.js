/// <reference path="../pb_data/types.d.ts" />
// Read-side helpers for the explorer: one cached summary call, one RSS feed,
// and a slug route that serves the app shell with per-app metadata.

routerAdd('GET', '/api/radar/summary', (e) => {
  const rawFlt = String((e.requestInfo().query && e.requestInfo().query['filter']) || '').trim()
  const now = Date.now()

  const starredForRows = (rowIdSet) => {
    if (!e.auth) return null
    try {
      const cur = $app.findRecordsByFilter('curation', 'status != "dismissed"', '', 2000, 0)
      if (!rowIdSet) return cur.length
      let n = 0
      for (const c of cur) {
        if (rowIdSet[c.getString('project')]) n++
      }
      return n
    } catch (_) {
      return 0
    }
  }

  if (!rawFlt) {
    const cache = $app.store().get('radar_summary')
    if (cache && now - cache.at < 300000) {
      const out = Object.assign({}, cache.body)
      const st = starredForRows(null)
      if (st !== null) out.starred = st
      return e.json(200, out)
    }
  }

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

  const tags = {}
  const langs = {}
  const lics = {}
  const bump = (cnt, key) => {
    if (!key) return
    cnt[key] = (cnt[key] || 0) + 1
  }

  let rows = []
  try {
    rows = $app.findRecordsByFilter('projects', rawFlt || '1=1', '', 4000, 0)
  } catch (_) {
    rows = []
  }

  let lastSync = ''
  let inUse = 0
  let inBoth = 0
  let srcAwesome = 0
  let srcSelfhst = 0
  const act = { active: 0, slow: 0, stale: 0, archived: 0 }
  const stars = { lt500: 0, '500-5000': 0, gt5000: 0 }
  const rowIds = e.auth ? {} : null

  for (const r of rows) {
    if (rowIds) rowIds[r.id] = true
    let ts = []
    try {
      ts = JSON.parse(r.getString('unified_tags') || '[]')
    } catch (_) {
      ts = []
    }
    for (const t of ts) {
      const s = String(t)
      if (s === 'in-use') inUse++
      else bump(tags, s)
    }
    let srcs = []
    try {
      srcs = JSON.parse(r.getString('sources') || '[]')
    } catch (_) {
      srcs = []
    }
    const hasA = srcs.includes('awesome')
    const hasS = srcs.includes('selfhst')
    if (hasA) srcAwesome++
    if (hasS) srcSelfhst++
    if (hasA && hasS) inBoth++

    const st = r.getString('activity_status')
    if (st in act) act[st]++

    const sc = Number(r.getInt('stars') || 0)
    if (sc < 500) stars.lt500++
    else if (sc < 5000) stars['500-5000']++
    else stars.gt5000++

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
    total: rows.length,
    bySource: {
      awesome: srcAwesome,
      selfhst: srcSelfhst,
      both: inBoth,
      any: rows.length,
    },
    inBoth: inBoth,
    inUse: inUse,
    lastSyncedAt: lastSync,
    topTags: top(tags, 40),
    topLanguages: top(langs, 25),
    topLicenses: top(lics, 20),
    activity: act,
    stars: stars,
    generatedAt: new Date().toISOString(),
  }

  if (!rawFlt) {
    const seA = countOf('source_entries', 'source = "awesome"')
    const seS = countOf('source_entries', 'source = "selfhst"')
    body.entryCount = seA + seS
    body.events = countOf('surveillance_events', '1=1')
    $app.store().set('radar_summary', { at: now, body: body })
  }

  const out = Object.assign({}, body)
  const stCount = starredForRows(rawFlt ? rowIds : null)
  if (stCount !== null) out.starred = stCount
  return e.json(200, out)
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
