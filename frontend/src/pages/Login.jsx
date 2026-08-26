import { useState } from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router-dom'
import { authApi } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { ErrorBanner } from '../components/common'

export default function Login() {
  const { login, isAuthenticated, loading } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()

  const [mode, setMode] = useState('login')
  const [form, setForm] = useState({ email: '', password: '', full_name: '' })
  const [error, setError] = useState(null)
  const [pending, setPending] = useState(false)

  if (loading) return null
  if (isAuthenticated) return <Navigate to={location.state?.from || '/dashboard'} replace />

  const update = (field) => (event) => setForm({ ...form, [field]: event.target.value })

  async function handleSubmit(event) {
    event.preventDefault()
    setError(null)
    setPending(true)
    try {
      if (mode === 'register') {
        await authApi.register(form)
      }
      await login(form.email, form.password)
      navigate(location.state?.from || '/dashboard', { replace: true })
    } catch (err) {
      setError(err)
    } finally {
      setPending(false)
    }
  }

  return (
    <div className="auth">
      <form className="auth__card" onSubmit={handleSubmit}>
        <div className="brand brand--large">
          <span className="brand__mark">RP</span>
          <span>RetailPulse</span>
        </div>
        <p className="muted">
          {mode === 'login' ? 'Sign in to continue.' : 'Create a customer account.'}
        </p>

        <ErrorBanner error={error} />

        {mode === 'register' ? (
          <label>
            Full name
            <input
              type="text"
              value={form.full_name}
              onChange={update('full_name')}
              required
              minLength={2}
              autoComplete="name"
            />
          </label>
        ) : null}

        <label>
          Email
          <input
            type="email"
            value={form.email}
            onChange={update('email')}
            required
            autoComplete="email"
          />
        </label>

        <label>
          Password
          <input
            type="password"
            value={form.password}
            onChange={update('password')}
            required
            /* Matches the server's minimum. Enforcing it here is a courtesy so
               the user is not told "too short" only after a round trip; the
               server still validates it. */
            minLength={mode === 'register' ? 8 : 1}
            autoComplete={mode === 'register' ? 'new-password' : 'current-password'}
          />
        </label>

        <button type="submit" disabled={pending}>
          {pending ? 'Please wait...' : mode === 'login' ? 'Sign in' : 'Create account'}
        </button>

        <button
          type="button"
          className="link"
          onClick={() => {
            setMode(mode === 'login' ? 'register' : 'login')
            setError(null)
          }}
        >
          {mode === 'login' ? 'Need an account? Register' : 'Already have an account? Sign in'}
        </button>

        <p className="muted small">
          New accounts are always customers. Staff roles are granted by an administrator --
          the registration endpoint ignores any role sent with it.
        </p>
      </form>
    </div>
  )
}
