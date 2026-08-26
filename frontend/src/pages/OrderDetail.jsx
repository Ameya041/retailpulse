import { useCallback, useState } from 'react'
import { useParams } from 'react-router-dom'
import { orderApi } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import {
  BackLink,
  DataTable,
  ErrorBanner,
  Loading,
  Money,
  PageHeader,
  StatusPill,
} from '../components/common'
import { useAction, useApi } from '../hooks/useApi'

// Statuses a customer may cancel from. Mirrors the server's rule; the server
// is what enforces it.
const CANCELLABLE = new Set(['CREATED', 'INVENTORY_RESERVED', 'CONFIRMED'])

export default function OrderDetail() {
  const { orderId } = useParams()
  const { isStaff } = useAuth()
  const [reason, setReason] = useState('CHANGED_MIND')

  const order = useApi(useCallback(() => orderApi.get(orderId), [orderId]), [orderId])

  const cancel = useAction(
    useCallback(async () => {
      await orderApi.cancel(orderId, reason)
      await order.refetch()
    }, [orderId, reason, order]),
  )

  if (order.loading) return <Loading label="Loading order" />
  if (order.error) return <ErrorBanner error={order.error} onRetry={order.refetch} />
  if (!order.data) return null

  const data = order.data
  const canCancel = CANCELLABLE.has(data.status)

  return (
    <>
      <BackLink to="/orders">Back to orders</BackLink>
      <PageHeader
        title={`Order ${data.order_id.slice(0, 8)}`}
        subtitle={`Placed ${new Date(data.created_at).toLocaleString()}`}
        actions={<StatusPill status={data.status} />}
      />

      <div className="two-col">
        <section className="panel">
          <h2>Items</h2>
          <DataTable
            columns={[
              { key: 'sku', label: 'SKU' },
              { key: 'product_name', label: 'Product' },
              { key: 'quantity', label: 'Qty', numeric: true },
              {
                key: 'unit_price',
                label: 'Unit price',
                numeric: true,
                render: (row) => <Money amount={row.unit_price} />,
              },
              {
                key: 'subtotal',
                label: 'Subtotal',
                numeric: true,
                render: (row) => <Money amount={row.subtotal} />,
              },
            ]}
            rows={data.items}
            keyField="order_item_id"
          />
          <p className="cart-total">
            Total: <Money amount={data.total_amount} currency={data.currency} />
          </p>
          <p className="muted small">
            Prices are those agreed when the order was placed. A later catalog change does not
            alter them.
          </p>
        </section>

        <section className="panel">
          <h2>Progress</h2>
          {/* The transition history is the story of the saga across six
              services, which is exactly what is hard to see otherwise. */}
          <ol className="timeline">
            {data.transitions.map((step, index) => (
              <li key={`${step.to_status}-${index}`}>
                <StatusPill status={step.to_status} />
                <span className="muted small">
                  {new Date(step.created_at).toLocaleString()} &middot; {step.actor}
                  {step.reason ? ` · ${step.reason}` : ''}
                </span>
              </li>
            ))}
          </ol>

          {data.allowed_next_statuses?.length ? (
            <p className="muted small">
              Next possible: {data.allowed_next_statuses.join(', ').replaceAll('_', ' ')}
            </p>
          ) : (
            <p className="muted small">This order has reached a final state.</p>
          )}

          <h3>Shipping to</h3>
          <p>{data.shipping_address}</p>
          {data.cancellation_reason ? (
            <p className="muted">Cancelled: {data.cancellation_reason}</p>
          ) : null}
        </section>
      </div>

      {canCancel && !isStaff ? (
        <section className="panel">
          <h2>Cancel this order</h2>
          <p className="muted small">
            Orders can only be cancelled before fulfilment begins. Once a parcel is with a
            carrier, cancelling becomes a return.
          </p>
          <div className="checkout">
            <label>
              Reason
              <input
                type="text"
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                minLength={3}
              />
            </label>
            <button
              type="button"
              className="danger"
              disabled={cancel.pending}
              onClick={() => cancel.execute()}
            >
              {cancel.pending ? 'Cancelling...' : 'Cancel order'}
            </button>
          </div>
          <ErrorBanner error={cancel.error} />
        </section>
      ) : null}
    </>
  )
}
