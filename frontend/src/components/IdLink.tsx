import { Link } from 'react-router-dom'
import { routeForId } from '../utils/idRoutes'

interface IdLinkProps {
  id: string
  label?: string
}

export function IdLink({ id, label }: IdLinkProps) {
  const route = routeForId(id)
  if (!route) return <span>{label ?? id}</span>
  return <Link to={route}>{label ?? id}</Link>
}

interface IdLinkListProps {
  ids: string[] | undefined
}

export function IdLinkList({ ids }: IdLinkListProps) {
  if (!ids || ids.length === 0) return <span>—</span>
  return (
    <span className="id-link-list">
      {ids.map((id, index) => (
        <span key={id}>
          <IdLink id={id} />
          {index < ids.length - 1 && ', '}
        </span>
      ))}
    </span>
  )
}
