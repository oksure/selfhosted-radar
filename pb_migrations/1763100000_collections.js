/// <reference path="../pb_data/types.d.ts" />
// Unified self-hosted app directory: projects, per-source entries and daily snapshots.

migrate((app) => {
  const projects = new Collection({
    name: 'projects',
    type: 'base',
    listRule: '',
    viewRule: '',
    createRule: null,
    updateRule: null,
    deleteRule: null,
    fields: [
      { name: 'canonical_key', type: 'text', required: true, max: 300 },
      { name: 'slug', type: 'text', required: true, max: 200 },
      { name: 'name', type: 'text', required: true, max: 200 },
      { name: 'repo_full_name', type: 'text', max: 200 },
      { name: 'repo_host', type: 'select', maxSelect: 1, values: ['github', 'gitlab', 'codeberg', 'other'] },
      { name: 'homepage_url', type: 'url' },
      { name: 'repo_url', type: 'url' },
      { name: 'demo_url', type: 'url' },
      { name: 'icon_url', type: 'url' },
      { name: 'description', type: 'text', max: 2000 },
      { name: 'license', type: 'text', max: 200 },
      { name: 'language', type: 'text', max: 100 },
      { name: 'platforms', type: 'json', maxSize: 20000 },
      { name: 'unified_tags', type: 'json', maxSize: 20000 },
      { name: 'sources', type: 'json', maxSize: 2000 },
      { name: 'stars', type: 'number' },
      { name: 'forks', type: 'number' },
      { name: 'stars_1d_delta', type: 'number' },
      { name: 'stars_7d_delta', type: 'number' },
      { name: 'stars_30d_delta', type: 'number' },
      { name: 'selfhst_trending', type: 'number' },
      { name: 'last_commit_at', type: 'text', max: 40 },
      { name: 'latest_release_tag', type: 'text', max: 200 },
      { name: 'latest_release_at', type: 'text', max: 40 },
      { name: 'archived', type: 'bool' },
      {
        name: 'activity_status',
        type: 'select',
        maxSelect: 1,
        values: ['active', 'slow', 'stale', 'archived'],
      },
      { name: 'first_seen_at', type: 'text', max: 40 },
      { name: 'last_synced_at', type: 'text', max: 40 },
    ],
    indexes: [
      'CREATE UNIQUE INDEX idx_projects_canonical_key ON projects (canonical_key)',
      'CREATE UNIQUE INDEX idx_projects_slug ON projects (slug)',
      'CREATE INDEX idx_projects_stars ON projects (stars DESC)',
      'CREATE INDEX idx_projects_trending ON projects (selfhst_trending DESC)',
      'CREATE INDEX idx_projects_7d ON projects (stars_7d_delta DESC)',
      'CREATE INDEX idx_projects_last_commit ON projects (last_commit_at)',
      'CREATE INDEX idx_projects_release ON projects (latest_release_at)',
      'CREATE INDEX idx_projects_first_seen ON projects (first_seen_at)',
      'CREATE INDEX idx_projects_activity ON projects (activity_status)',
    ],
  })
  app.save(projects)

  const entries = new Collection({
    name: 'source_entries',
    type: 'base',
    listRule: '',
    viewRule: '',
    createRule: null,
    updateRule: null,
    deleteRule: null,
    fields: [
      { name: 'project', type: 'relation', required: true, maxSelect: 1, collectionId: projects.id, cascadeDelete: true },
      { name: 'source', type: 'select', required: true, maxSelect: 1, values: ['awesome', 'selfhst'] },
      { name: 'external_id', type: 'text', required: true, max: 200 },
      { name: 'source_url', type: 'text', max: 500 },
      { name: 'raw_tags', type: 'json', maxSize: 5000 },
      { name: 'alternatives_refs', type: 'json', maxSize: 5000 },
      { name: 'raw_payload', type: 'json', maxSize: 200000 },
      { name: 'last_seen_at', type: 'text', max: 40 },
    ],
    indexes: [
      'CREATE UNIQUE INDEX idx_entries_source_ext ON source_entries (source, external_id)',
      'CREATE INDEX idx_entries_project ON source_entries (project)',
    ],
  })
  app.save(entries)

  const snaps = new Collection({
    name: 'repo_snapshots',
    type: 'base',
    listRule: '',
    viewRule: '',
    createRule: null,
    updateRule: null,
    deleteRule: null,
    fields: [
      { name: 'project', type: 'relation', required: true, maxSelect: 1, collectionId: projects.id, cascadeDelete: true },
      { name: 'captured_date', type: 'text', required: true, max: 10 },
      { name: 'stars', type: 'number' },
      { name: 'forks', type: 'number' },
      { name: 'selfhst_trending', type: 'number' },
      { name: 'release_tag', type: 'text', max: 200 },
    ],
    indexes: [
      'CREATE UNIQUE INDEX idx_snap_project_date ON repo_snapshots (project, captured_date)',
      'CREATE INDEX idx_snap_date ON repo_snapshots (captured_date)',
    ],
  })
  app.save(snaps)
}, (app) => {
  for (const name of ['repo_snapshots', 'source_entries', 'projects']) {
    try {
      app.delete(app.findCollectionByNameOrId(name))
    } catch (_) {
    }
  }
})
