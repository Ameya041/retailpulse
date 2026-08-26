import { useCallback, useState } from 'react'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { analyticsApi } from '../api/client'
import { DataTable, ErrorBanner, Loading, Money, PageHeader } from '../components/common'
import { useApi } from '../hooks/useApi'

// A fixed, ordered palette so a category keeps the same colour between the
// pie chart and the bar chart. Colours that shuffle between views make two
// charts of the same data look like different data.
const PALETTE = ['#2563eb', '#7c3aed', '#059669', '#d97706', '#dc2626', '#0891b2']

export default function Analytics() {
  const [days, setDays] = useState(30)

  const byCategory = useApi(useCallback(() => analyticsApi.byCategory(days), [days]), [days])
  const byStore = useApi(useCallback(() => analyticsApi.byStore(days), [days]), [days])
  const topProducts = useApi(
    useCallback(() => analyticsApi.topProducts(days, 10), [days]),
    [days],
  )

  return (
    <>
      <PageHeader
        title="Analytics"
        subtitle={`Sales breakdown for the last ${days} days`}
        actions={
          <div className="segmented">
            {[7, 30, 90].map((option) => (
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

      <div className="two-col">
        <section className="panel">
          <h2>Revenue share by category</h2>
          <ErrorBanner error={byCategory.error} onRetry={byCategory.refetch} />
          {byCategory.loading ? (
            <Loading />
          ) : byCategory.data?.length ? (
            <ResponsiveContainer width="100%" height={280}>
              <PieChart>
                <Pie
                  data={byCategory.data}
                  dataKey="revenue"
                  nameKey="category"
                  outerRadius={95}
                  label={(entry) => `${entry.revenue_share_pct}%`}
                >
                  {byCategory.data.map((entry, index) => (
                    <Cell key={entry.category} fill={PALETTE[index % PALETTE.length]} />
                  ))}
                </Pie>
                <Legend />
                <Tooltip />
              </PieChart>
            </ResponsiveContainer>
          ) : (
            <p className="muted">No sales in this window.</p>
          )}
        </section>

        <section className="panel">
          <h2>Revenue by store</h2>
          {byStore.loading ? (
            <Loading />
          ) : (
            <ResponsiveContainer width="100%" height={280}>
              <BarChart data={byStore.data || []}>
                <CartesianGrid strokeDasharray="3 3" stroke="#eee" />
                <XAxis dataKey="store_id" tick={{ fontSize: 11 }} />
                <YAxis tick={{ fontSize: 11 }} />
                <Tooltip />
                <Bar dataKey="revenue" fill="#059669" name="Revenue" />
              </BarChart>
            </ResponsiveContainer>
          )}
        </section>
      </div>

      <section className="panel">
        <h2>Top products</h2>
        {topProducts.loading ? (
          <Loading />
        ) : (
          <DataTable
            columns={[
              { key: 'sku', label: 'SKU' },
              { key: 'product_name', label: 'Product' },
              { key: 'category', label: 'Category' },
              { key: 'units_sold', label: 'Units', numeric: true },
              {
                key: 'revenue',
                label: 'Revenue',
                numeric: true,
                render: (row) => <Money amount={row.revenue} />,
              },
            ]}
            rows={topProducts.data}
            keyField="sku"
            empty="No sales in this window."
          />
        )}
      </section>

      <section className="panel">
        <h2>Store performance</h2>
        {byStore.loading ? (
          <Loading />
        ) : (
          <DataTable
            columns={[
              { key: 'store_id', label: 'Store' },
              { key: 'order_count', label: 'Orders', numeric: true },
              { key: 'units_sold', label: 'Units', numeric: true },
              {
                key: 'revenue',
                label: 'Revenue',
                numeric: true,
                render: (row) => <Money amount={row.revenue} />,
              },
              {
                key: 'average_order_value',
                label: 'Avg order',
                numeric: true,
                render: (row) => <Money amount={row.average_order_value} />,
              },
            ]}
            rows={byStore.data}
            keyField="store_id"
          />
        )}
      </section>
    </>
  )
}
