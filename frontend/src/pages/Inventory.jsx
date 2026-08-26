import { useCallback, useState } from 'react'
import { inventoryApi } from '../api/client'
import {
  DataTable,
  ErrorBanner,
  Loading,
  PageHeader,
  StatusPill,
} from '../components/common'
import { useAction, useApi } from '../hooks/useApi'

export default function Inventory() {
  const lowStock = useApi(useCallback(() => inventoryApi.lowStock({ limit: 100 }), []), [])
  const locations = useApi(useCallback(() => inventoryApi.locations(), []), [])

  const [form, setForm] = useState({
    product_id: '',
    location_id: '',
    quantity: 10,
    reorder_threshold: 5,
  })
  const [done, setDone] = useState(false)

  const restock = useAction(
    useCallback(async () => {
      await inventoryApi.restock({
        product_id: form.product_id,
        location_id: form.location_id,
        quantity: Number(form.quantity),
        reorder_threshold: Number(form.reorder_threshold),
      })
      setDone(true)
      setTimeout(() => setDone(false), 2500)
      await lowStock.refetch()
    }, [form, lowStock]),
  )

  const update = (field) => (event) => setForm({ ...form, [field]: event.target.value })

  return (
    <>
      <PageHeader title="Inventory" subtitle="Stock levels and replenishment" />

      <section className="panel">
        <h2>Below reorder threshold</h2>
        <ErrorBanner error={lowStock.error} onRetry={lowStock.refetch} />
        {lowStock.loading ? (
          <Loading />
        ) : (
          <DataTable
            columns={[
              { key: 'location_code', label: 'Location' },
              { key: 'product_id', label: 'Product', render: (row) => row.product_id.slice(0, 8) },
              { key: 'available_quantity', label: 'Available', numeric: true },
              { key: 'reorder_threshold', label: 'Threshold', numeric: true },
              {
                key: 'shortfall',
                label: 'Shortfall',
                numeric: true,
                render: (row) =>
                  row.shortfall > 0 ? <StatusPill status="CRITICAL" /> : row.shortfall,
              },
            ]}
            rows={lowStock.data}
            keyField="product_id"
            empty="Nothing is below its reorder threshold."
          />
        )}
      </section>

      <section className="panel">
        <h2>Restock</h2>
        <p className="muted small">
          Adds units at a location. Creates the stock record on first delivery.
        </p>
        <div className="form-grid">
          <label>
            Product ID
            <input
              type="text"
              value={form.product_id}
              onChange={update('product_id')}
              placeholder="UUID"
            />
          </label>
          <label>
            Location
            <select value={form.location_id} onChange={update('location_id')}>
              <option value="">Select a location</option>
              {(locations.data || []).map((location) => (
                <option key={location.location_id} value={location.location_id}>
                  {location.code} -- {location.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            Quantity
            <input type="number" min={1} value={form.quantity} onChange={update('quantity')} />
          </label>
          <label>
            Reorder threshold
            <input
              type="number"
              min={0}
              value={form.reorder_threshold}
              onChange={update('reorder_threshold')}
            />
          </label>
        </div>
        <button
          type="button"
          disabled={restock.pending || !form.product_id || !form.location_id}
          onClick={() => restock.execute()}
        >
          {restock.pending ? 'Restocking...' : done ? 'Stock added' : 'Restock'}
        </button>
        <ErrorBanner error={restock.error} />
      </section>
    </>
  )
}
