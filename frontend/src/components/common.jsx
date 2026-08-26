/**
 * Shared presentational pieces.
 *
 * Loading and error states are components rather than ad-hoc JSX because every
 * page needs all three states (loading / failed / empty), and a page that
 * silently renders nothing while a request is in flight looks broken.
 */
import { Link } from 'react-router-dom'

export function Loading({ label = 'Loading' }) {
  return (
    <div className="state state--loading" role="status" aria-live="polite">
      {label}...
    </div>
  )
}

export function ErrorBanner({ error, onRetry }) {
  if (!error) return null

  // Rate limiting gets its own message because "try again" is actively wrong
  // advice -- retrying immediately makes it worse.
  const isRateLimited = error.status === 429

  return (
    <div className="state state--error" role="alert">
      <strong>{isRateLimited ? 'Slow down' : 'Something went wrong'}</strong>
      <p>{error.message}</p>
      {isRateLimited && error.retryAfterSeconds ? (
        <p className="muted">Try again in {error.retryAfterSeconds} seconds.</p>
      ) : null}
      {error.status === 403 ? (
        <p className="muted">Your account does not have permission for this.</p>
      ) : null}
      {onRetry && !isRateLimited ? (
        <button type="button" onClick={onRetry}>
          Retry
        </button>
      ) : null}
    </div>
  )
}

export function Empty({ message, action }) {
  return (
    <div className="state state--empty">
      <p>{message}</p>
      {action}
    </div>
  )
}

export function StatCard({ label, value, hint, unavailable }) {
  return (
    <div className="stat-card">
      <span className="stat-card__label">{label}</span>
      <span className={`stat-card__value${unavailable ? ' stat-card__value--muted' : ''}`}>
        {/* An unavailable figure is shown as a dash, never as zero. A
            dashboard reporting zero stock during an outage gets acted on. */}
        {unavailable ? '--' : value}
      </span>
      {hint ? <span className="stat-card__hint">{unavailable ? 'Unavailable' : hint}</span> : null}
    </div>
  )
}

export function Money({ amount, currency = 'INR' }) {
  if (amount === null || amount === undefined) return <span className="muted">--</span>
  const symbol = currency === 'INR' ? '₹' : ''
  // Amounts arrive as strings so decimal precision survives JSON. Formatting
  // for display is the only place they become numbers.
  const value = Number(amount)
  return (
    <span>
      {symbol}
      {value.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
    </span>
  )
}

export function StatusPill({ status }) {
  const tone = {
    DELIVERED: 'good',
    CONFIRMED: 'good',
    PAYMENT_CONFIRMED: 'good',
    SHIPPED: 'info',
    FULFILMENT_STARTED: 'info',
    INVENTORY_RESERVED: 'info',
    CREATED: 'neutral',
    CANCELLED: 'bad',
    PAYMENT_FAILED: 'bad',
    INVENTORY_RELEASED: 'warn',
    CRITICAL: 'bad',
    HIGH: 'warn',
    MEDIUM: 'info',
    NONE: 'neutral',
  }[status] || 'neutral'

  return <span className={`pill pill--${tone}`}>{String(status).replaceAll('_', ' ')}</span>
}

export function DataTable({ columns, rows, keyField, empty = 'Nothing to show.' }) {
  if (!rows?.length) return <Empty message={empty} />

  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column.key} className={column.numeric ? 'numeric' : undefined}>
                {column.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={keyField ? row[keyField] : index}>
              {columns.map((column) => (
                <td key={column.key} className={column.numeric ? 'numeric' : undefined}>
                  {column.render ? column.render(row) : row[column.key]}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function PageHeader({ title, subtitle, actions }) {
  return (
    <header className="page-header">
      <div>
        <h1>{title}</h1>
        {subtitle ? <p className="muted">{subtitle}</p> : null}
      </div>
      {actions ? <div className="page-header__actions">{actions}</div> : null}
    </header>
  )
}

export function Pagination({ page, totalPages, onChange }) {
  if (!totalPages || totalPages <= 1) return null
  return (
    <nav className="pagination" aria-label="Pagination">
      <button type="button" disabled={page <= 1} onClick={() => onChange(page - 1)}>
        Previous
      </button>
      <span className="muted">
        Page {page} of {totalPages}
      </span>
      <button type="button" disabled={page >= totalPages} onClick={() => onChange(page + 1)}>
        Next
      </button>
    </nav>
  )
}

export function BackLink({ to, children }) {
  return (
    <Link className="back-link" to={to}>
      &larr; {children}
    </Link>
  )
}
