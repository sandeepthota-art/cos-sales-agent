// Canonical id prefix -> detail route, per the project's canonical id scheme
// (EML-/THR-/PER-/ORG-/PRJ-/OPP-/CMT-/FUP-/MTG-). Used to render a plain id
// string as a clickable link without an extra API lookup (avoids N+1 fetches
// just to resolve a relationship -- the link IS the relationship).
const PREFIX_ROUTES: Record<string, string> = {
  EML: '/emails',
  THR: '/threads',
  PER: '/people',
  ORG: '/organizations',
  PRJ: '/projects',
  OPP: '/opportunities',
  MTG: '/meetings',
}

export function routeForId(id: string): string | null {
  const prefix = id.split('-')[0]
  const base = PREFIX_ROUTES[prefix]
  return base ? `${base}/${encodeURIComponent(id)}` : null
}
