import { useCallback, useState } from 'react'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { analyticsApi, orderApi } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { ErrorBanner, Loading, Money, PageHeader, StatCard, StatusPill } from '../components/common'
import { useApi } from '../hooks/useApi'

const WINDOWS = [7, 30, 90]

export default function Dashboard() {
  const { isStaff, user } = useAuth()
  const [days, setDays] = useState(30)

  if (isStaff) return <StaffDashboard days={days} setDays={setDays} />
  return <CustomerDashboard user={user} />
}

function StaffDashboard({ days, setDays }) {
  const metrics = useApi(useCallback(() => analyticsApi.dashboard(days), [days]), [days])
  const timeline = useApi(useCallback(() => analyticsApi.salesOverTime(days), [days]), [days])
  const categories = useApi(useCallback(() => analyticsApi.byCategory(days), [days]), [days])

  return (
    <>
      <PageHeader
        title="Dashboard"
        subtitle={`Trading summary for the last ${days} days`}
        actions={
          <div className="segmented">
            {WINDOWS.map((option) => (
              <button
                key={option}
                type="button"
                className={option === days ? 'active' : undefined}
                onClick={() => setDays(option)}
              >
                {option}d
              </button>
            ))}
          </div>
        }
      />

      <ErrorBanner error={metrics.error} onRetry={metrics.refetch} />

      {metrics.loading ? (
        <Loading label="Loading metrics" />
      ) : metrics.data ? (
        <div className="stat-grid">
          <StatCard label="Orders" value={metrics.data.total_orders} hint={`${days} days`} />
          <StatCard label="Orders today" value={metrics.data.orders_today} />
          <StatCard label="Revenue" value={<Money amount={metrics.data.total_revenue} />} />
          <StatCard
            label="Average order"
            value={<Money amount={metrics.data.average_order_value} />}
          />
          <StatCard label="Units sold" value={metrics.data.total_units_sold} />
          <StatCard
            label="Fulfilment rate"
            value={`${metrics.data.fulfilment_rate_pct}%`}
            hint="of completed orders"
          />
          <StatCard
            label="Cancellation rate"
            value={`${metrics.data.cancellation_rate_pct}%`}
            hint="of completed orders"
          />
          <StatCard
            label="Low stock"
            value={metrics.data.low_stock_products}
            /* Null means the inventory service did not answer. Showing a dash
               rather than 0 keeps an outage from reading as "all good". */
            unavailable={metrics.data.low_stock_products === null}
            hint="products below threshold"
          />
        </div>
      ) : null}

      <section className="panel">
        <h2>Sales over time</h2>
        {timeline.loading ? (
          <Loading />
        ) : (
          <ResponsiveContainer width="100%" height={260}>
            <LineChart data={timeline.data || []}>
              <CartesianGrid strokeDasharray="3 3" stroke="#eee" />
              <XAxis dataKey="date" tick={{ fontSize: 11 }} />
              <YAxis tick={{ fontSize: 11 }} />
              <Tooltip />
              <Line
                type="monotone"
                dataKey="units_sold"
                stroke="#2563eb"
                strokeWidth={2}
                dot={false}
                name="Units"
              />
            </LineChart>
          </ResponsiveContainer>
        )}
      </section>

      <section className="panel">
        <h2>Revenue by category</h2>
        {categories.loading ? (
          <Loading />
        ) : (
          <ResponsiveContainer width="100%" height={260}>
            <BarChart data={categories.data || []}>
              <CartesianGrid strokeDasharray="3 3" stroke="#eee" />
              <XAxis dataKey="category" tick={{ fontSize: 11 }} />
              <YAxis tick={{ fontSize: 11 }} />
              <Tooltip />
              <Bar dataKey="revenue" fill="#2563eb" name="Revenue" />
            </BarChart>
          </ResponsiveContainer>
        )}
      </section>
    </>
  )
}

function CustomerDashboard({ user }) {
  const orders = useApi(useCallback(() => orderApi.mine({ page: 1, page_size: 5 }), []), [])

  return (
    <>
      <PageHeader title={`Welcome, ${user?.full_name}`} subtitle="Your recent orders" />
      <ErrorBanner error={orders.error} onRetry={orders.refetch} />
      {orders.loading ? (
        <Loading />
      ) : (
        <section className="panel">
          <h2>Recent orders</h2>
          {orders.data?.items?.length ? (
            <ul className="order-list">
              {orders.data.items.map((order) => (
                <li key={order.order_id}>
                  <div>
                    <strong>{order.order_id.slice(0, 8)}</strong>
                    <span className="muted"> &middot; {order.items.length} item(s)</span>
                  </div>
                  <div className="order-list__right">
                    <Money amount={order.total_amount} currency={order.currency} />
                    <StatusPill status={order.status} />
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted">You have not placed any orders yet.</p>
          )}
        </section>
      )}
    </>
  )
}
