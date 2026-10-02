// Fields confirmed (via app/entities/resolution.py) to be either never set at
// all, or always set to the same non-informative constant, by any current
// code path. MongoDB keeps them untouched -- nothing here is removed from
// storage -- this only matches, on the React side, the same hiding the
// Streamlit dashboard already does via its *_COLUMN_ORDER tuples
// (app/ui/column_descriptions.py), so a generic cross-collection renderer
// (RecordList/KeyValueList) never surfaces them either.
export const HIDDEN_FIELDS = {
  organizations: ['aliases', 'source'],
  projects: ['cluster', 'objective', 'target', 'collaborators', 'last_movement', 'note_link', 'source'],
  opportunities: ['description'],
  follow_ups: ['escalation_level', 'surfaced'],
  meetings: ['minutes_record', 'next_meeting_date', 'agenda_target', 'agenda_written'],
} as const
