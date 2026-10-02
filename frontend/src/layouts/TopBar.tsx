import { useNavigate } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'

export function TopBar() {
  const { status, logout } = useAuth()
  const navigate = useNavigate()

  async function handleLogout() {
    await logout()
    navigate('/login', { replace: true })
  }

  return (
    <header className="topbar">
      <div />
      {status === 'authenticated' && (
        <button type="button" className="button button--secondary" onClick={handleLogout}>
          Log out
        </button>
      )}
    </header>
  )
}
