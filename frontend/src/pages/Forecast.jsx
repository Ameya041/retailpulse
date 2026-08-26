import { useCallback, useEffect, useState } from 'react'
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { analyticsApi, forecastApi } from '../api/client'
import {
  DataTable,
  ErrorBanner,
  Loading,
  PageHeader,
  StatCard,
  StatusPill,
} from '../components/common'
import { useApi } from '../hooks/useApi'

export default function Forecast() {
  const [productId, setProductId] = useState('')
  const [storeId, setStoreId] = useState('')

  const series = useApi(useCallback(() => forecastApi.products(), []), [])
  const modelInfo = useApi(useCallback(() => forecastApi.modelInfo(), []), [])
  const replenishment = useApi(useCallback(() => analyticsApi.replenishment(), []), [])

  // Default to the first forecastable series once the list arrives.
  useEffect(() => {
    if (!productId && series.data?.length) {
      setProductId(series.data[0].product_id)
      setStoreId(series.data[0].stores[0] || '')
    }
  }, [series.data, productId])

  const forecast = useApi(
    useCallback(
      () => forecastApi.forProduct(productId, storeId || undefined, 7),
      [productId, storeId],
    ),
    [productId, storeId],
    { skip: !productId },
  )

  const selected = series.data?.find((entry) => entry.product_id === productId)
  const info = modelInfo.data

  return (
    <>
      <PageHeader
        title="Demand forecast"
        subtitle="Predicted demand and reorder recommendations"
      />

      {info ? (
        <div className="stat-grid">
          <StatCard label="Model" value={info.model_version} hint={info.model_name} />
          <StatCard
            label="MAE"
            value={info.metrics.mae.toFixed(2)}
            hint="units, held-out period"
          />
          <StatCard
            label="RMSE"
            value={info.metrics.rmse.toFixed(2)}
            hint="units, held-out period"
          />
          <StatCard
            label="vs naive baseline"
            value={`${info.mae_improvement_over_naive_pct}%`}
            /* Accuracy is meaningless without this comparison, so it sits
               beside the raw error rather than buried in a docs page. */
            hint="better than 'last 7 days repeated'"
          />
        </div>
      ) : null}

      <section className="panel">
        <h2>Forecast a product</h2>
        <ErrorBanner error={series.error} onRetry={series.refetch} />

        <div className="filters">
          <select
            value={productId}
            onChange={(event) => {
              const next = event.target.value
              setProductId(next)
              const entry = series.data?.find((item) => item.product_id === next)
              setStoreId(entry?.stores?.[0] || '')
            }}
          >
            {(series.data || []).map((entry) => (
              <option key={entry.product_id} value={entry.product_id}>
                {entry.product_id}
              </option>
            ))}
          </select>

          <select value={storeId} onChange={(event) => setStoreId(event.target.value)}>
            {(selected?.stores || []).map((store) => (
              <option key={store} value={store}>
                {store}
              </option>
            ))}
          </select>
        </div>

        <ErrorBanner error={forecast.error} onRetry={forecast.refetch} />

        {forecast.loading ? (
          <Loading label="Forecasting" />
        ) : forecast.data ? (
          <>
            <p className="muted small">
              <strong>{forecast.data.total_predicted_units} units</strong> predicted over the next{' '}
              {forecast.data.horizon_days} days, from {forecast.data.history_days_used} days of
              history.
            </p>
            <ResponsiveContainer width="100%" height={240}>
              <BarChart data={forecast.data.forecast}>
                <CartesianGrid strokeDasharray="3 3" stroke="#eee" />
                <XAxis dataKey="date" tick={{ fontSize: 11 }} />
                <YAxis tick={{ fontSize: 11 }} />
                <Tooltip />
                <Bar dataKey="predicted_units" fill="#7c3aed" name="Predicted units" />
              </BarChart>
            </ResponsiveContainer>
            <p className="muted small">
              The model predicts a 7-day total. The daily bars are that total allocated across
              days using this series&apos; own weekday pattern -- an allocation, not seven
              independent predictions.
            </p>
            {forecast.data.note ? <p className="muted small">{forecast.data.note}</p> : null}
          </>
        ) : null}
      </section>

      <section className="panel">
        <h2>Replenishment</h2>
        <ErrorBanner error={replenishment.error} onRetry={replenishment.refetch} />
        {replenishment.loading ? (
          <Loading />
        ) : (
          <>
            {replenishment.data?.degraded_reason ? (
              /* An empty list must never read as "nothing to reorder". */
              <p className="state state--warn">{replenishment.data.degraded_reason}</p>
            ) : null}
            <DataTable
              columns={[
                { key: 'product_id', label: 'Product' },
                { key: 'store_id', label: 'Store' },
                { key: 'current_stock', label: 'In stock', numeric: true },
                { key: 'predicted_demand_7d', label: 'Forecast 7d', numeric: true },
                {
                  key: 'recommended_order_quantity',
                  label: 'Order',
                  numeric: true,
                },
                {
                  key: 'urgency',
                  label: 'Urgency',
                  render: (row) => <StatusPill status={row.urgency} />,
                },
              ]}
              rows={replenishment.data?.items}
              keyField="product_id"
              empty="No replenishment recommendations available."
            />
          </>
        )}
      </section>
    </>
  )
}
