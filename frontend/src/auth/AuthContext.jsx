/**
 * Authentication state.
 *
 * The role held here drives what the UI *shows*. It is not a security
 * boundary: every permission is enforced server-side, and a user who edits
 * this state in devtools gains nothing but a menu item that 403s. Saying that
 * plainly matters, because "the button was hidden" is a common and wrong
 * answer to "how is this protected?".
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import { authApi, setUnauthorizedHandler, tokenStore } from '../api/client'

const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [user, setUser] = useState(() => tokenStore.getUser())
  const [loading, setLoading] = useState(Boolean(tokenStore.get()))

  const logout = useCallback(() => {
    tokenStore.clear()
    setUser(null)
  }, [])

  useEffect(() => {
    // The client calls this when any request comes back 401, so an expired
    // token drops the session everywhere at once rather than in whichever
    // component happened to notice.
    setUnauthorizedHandler(() => {
      setUser(null)
    })
  }, [])

  useEffect(() => {
    if (!tokenStore.get()) {
      setLoading(false)
      return
    }
    // Re-validate against the server on load. The stored role could be stale
    // -- an admin may have changed it since the token was issued -- and
    // /users/me reads the live record rather than trusting the token claims.
    let cancelled = false
    authApi
      .me()
      .then(({ data }) => {
        if (cancelled) return
        tokenStore.setUser(data)
        setUser(data)
      })
      .catch(() => {
        if (!cancelled) logout()
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [logout])

  const login = useCallback(async (email, password) => {
    const { data } = await authApi.login(email, password)
    tokenStore.set(data.access_token)
    tokenStore.setUser(data.user)
    setUser(data.user)
    return data.user
  }, [])

  const value = useMemo(
    () => ({
      user,
      loading,
      login,
      logout,
      isAuthenticated: Boolean(user),
      isAdmin: user?.role === 'ADMIN',
      isStaff: user?.role === 'ADMIN' || user?.role === 'WAREHOUSE_OPERATOR',
    }),
    [user, loading, login, logout],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const context = useContext(AuthContext)
  if (!context) {
    throw new Error('useAuth must be used inside an AuthProvider')
  }
  return context
}
