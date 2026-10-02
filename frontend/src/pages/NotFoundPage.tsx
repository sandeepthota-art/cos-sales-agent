import { Link } from 'react-router-dom'

export function NotFoundPage() {
  return (
    <div className="empty-state">
      <p className="empty-state__title">Page not found</p>
      <p className="empty-state__description">
        <Link to="/">Return to the overview</Link>
      </p>
    </div>
  )
}
