/// <reference path="../pb_data/types.d.ts" />
// Stage 2: allow gh_trending and gh_popular in source_entries and surveillance_events.

migrate((app) => {
  const entries = app.findCollectionByNameOrId('source_entries')
  const srcField = entries.fields.getByName('source')
  if (srcField) {
    srcField.values = ['awesome', 'selfhst', 'gh_trending', 'gh_popular']
    app.save(entries)
  }

  const events = app.findCollectionByNameOrId('surveillance_events')
  const evField = events.fields.getByName('event_type')
  if (evField) {
    evField.values = [
      'new_in_awesome',
      'new_in_selfhst',
      'new_in_gh_trending',
      'new_in_gh_popular',
      'new_release',
      'star_surge',
      'archived_or_stale',
    ]
    app.save(events)
  }
}, (app) => {
  const entries = app.findCollectionByNameOrId('source_entries')
  const srcField = entries.fields.getByName('source')
  if (srcField) {
    srcField.values = ['awesome', 'selfhst']
    app.save(entries)
  }

  const events = app.findCollectionByNameOrId('surveillance_events')
  const evField = events.fields.getByName('event_type')
  if (evField) {
    evField.values = ['new_in_awesome', 'new_in_selfhst', 'new_release', 'star_surge', 'archived_or_stale']
    app.save(events)
  }
})
