import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'

/**
 * Navigation is filtered by role for usability, not for security. Every link
 * hidden here points at an endpoint the server would refuse anyway.
 */
const NAV = [
  { to: '/dashboard', label: 'Dashboard', staffOnly: false },
  { to: '/products', label: 'Products', staffOnly: false },
  { to: '/orders', label: 'Orders', staffOnly: false },
  { to: '/inventory', label: 'Inventory', staffOnly: true },
  { to: '/analytics', label: 'Analytics', staffOnly: true },
  { to: '/forecast', label: 'Forecast', staffOnly: true },
  { to: '/admin', label: 'Admin', adminOnly: true },
]

export default function Layout() {
  const { user, logout, isAdmin, isStaff } = useAuth()
  const navigate = useNavigate()

  const visible = NAV.filter((item) => {
    if (item.adminOnly) return isAdmin
    if (item.staffOnly) return isStaff
    return true
  })

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand__mark">RP</span>
          <span>RetailPulse</span>
        </div>
        <nav>
          {visible.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) => (isActive ? 'nav-link nav-link--active' : 'nav-link')}
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="sidebar__footer">
          <div className="user">
            <span className="user__name">{user?.full_name}</span>
            <span className="user__role">{user?.role?.replaceAll('_', ' ')}</span>
          </div>
          <button
            type="button"
            className="secondary"
            onClick={() => {
              logout()
              navigate('/login', { replace: true })
            }}
          >
            Sign out
          </button>
        </div>
      </aside>
      <main className="content">
        <Outlet />
      </main>
    </div>
  )
}
