import { Navigate, useLocation } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'
import { Loading } from './common'

/**
 * Gates a route on being signed in, and optionally on a role.
 *
 * Again: convenience, not security. It stops a customer landing on a page full
 * of 403s; it does not stop them calling the API. The server does that.
 */
export default function ProtectedRoute({ children, roles }) {
  const { isAuthenticated, loading, user } = useAuth()
  const location = useLocation()

  // While the stored token is being re-validated, render nothing decisive --
  // redirecting here would bounce a signed-in user to login on every refresh.
  if (loading) return <Loading label="Checking your session" />

  if (!isAuthenticated) {
    // Remember where they were headed so login can return them there.
    return <Navigate to="/login" state={{ from: location.pathname }} replace />
  }

  if (roles && !roles.includes(user?.role)) {
    return <Navigate to="/dashboard" replace />
  }

  return children
}
