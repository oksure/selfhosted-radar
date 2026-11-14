/// <reference path="../pb_data/types.d.ts" />
// Change feed and HP's private curation list.

migrate((app) => {
  const projects = app.findCollectionByNameOrId('projects')

  const events = new Collection({
    name: 'surveillance_events',
    type: 'base',
    listRule: '',
    viewRule: '',
    createRule: null,
    updateRule: null,
    deleteRule: null,
    fields: [
      { name: 'project', type: 'relation', required: true, maxSelect: 1, collectionId: projects.id, cascadeDelete: true },
      {
        name: 'event_type',
        type: 'select',
        required: true,
        maxSelect: 1,
        values: ['new_in_awesome', 'new_in_selfhst', 'new_release', 'star_surge', 'archived_or_stale'],
      },
      { name: 'source', type: 'select', maxSelect: 1, values: ['awesome', 'selfhst', 'gh_trending', 'gh_popular'] },
      { name: 'title', type: 'text', required: true, max: 300 },
      { name: 'detail', type: 'json', maxSize: 20000 },
      { name: 'occurred_at', type: 'text', required: true, max: 10 },
      { name: 'is_read', type: 'bool' },
    ],
    indexes: [
      'CREATE INDEX idx_events_occurred ON surveillance_events (occurred_at DESC)',
      'CREATE INDEX idx_events_type ON surveillance_events (event_type)',
      'CREATE UNIQUE INDEX idx_events_unique ON surveillance_events (project, event_type, occurred_at)',
    ],
  })
  app.save(events)

  const curation = new Collection({
    name: 'curation',
    type: 'base',
    // Signed-in accounts only: the list is HP's own, never public.
    listRule: '@request.auth.id != ""',
    viewRule: '@request.auth.id != ""',
    createRule: '@request.auth.id != ""',
    updateRule: '@request.auth.id != ""',
    deleteRule: '@request.auth.id != ""',
    fields: [
      { name: 'project', type: 'relation', required: true, maxSelect: 1, collectionId: projects.id, cascadeDelete: true },
      {
        name: 'status',
        type: 'select',
        maxSelect: 1,
        values: ['inbox', 'watching', 'trying', 'deployed', 'dismissed'],
      },
      { name: 'priority', type: 'number' },
      { name: 'memo', type: 'text', max: 5000 },
      { name: 'personal_tags', type: 'json', maxSize: 5000 },
      { name: 'owner', type: 'text', max: 100 },
    ],
    indexes: [
      'CREATE UNIQUE INDEX idx_curation_project ON curation (project)',
      'CREATE INDEX idx_curation_status ON curation (status)',
    ],
  })
  app.save(curation)
}, (app) => {
  for (const name of ['curation', 'surveillance_events']) {
    try {
      app.delete(app.findCollectionByNameOrId(name))
    } catch (_) {
    }
  }
})
