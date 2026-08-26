import { useCallback, useState } from 'react'
import { Link } from 'react-router-dom'
import { orderApi } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import {
  DataTable,
  ErrorBanner,
  Loading,
  Money,
  PageHeader,
  Pagination,
  StatusPill,
} from '../components/common'
import { useAction, useApi } from '../hooks/useApi'

const ORDER_STATUSES = [
  'CREATED',
  'INVENTORY_RESERVED',
  'PAYMENT_CONFIRMED',
  'CONFIRMED',
  'FULFILMENT_STARTED',
  'SHIPPED',
  'DELIVERED',
  'PAYMENT_FAILED',
  'INVENTORY_RELEASED',
  'CANCELLED',
]

export default function Orders() {
  const { isStaff } = useAuth()
  const [page, setPage] = useState(1)
  const [status, setStatus] = useState('')
  // Staff can switch between their own orders and every order.
  const [scope, setScope] = useState(isStaff ? 'all' : 'mine')

  const orders = useApi(
    useCallback(() => {
      const params = { page, page_size: 20 }
      if (scope === 'all') return orderApi.all({ ...params, status: status || undefined })
      return orderApi.mine(params)
    }, [page, scope, status]),
    [page, scope, status],
  )

  const cart = useApi(useCallback(() => orderApi.cart(), []), [])

  const checkout = useAction(
    useCallback(async (address) => {
      await orderApi.create({ shipping_address: address })
      await Promise.all([orders.refetch(), cart.refetch()])
    }, [orders, cart]),
  )

  const columns = [
    {
      key: 'order_id',
      label: 'Order',
      render: (row) => <Link to={`/orders/${row.order_id}`}>{row.order_id.slice(0, 8)}</Link>,
    },
    { key: 'items', label: 'Items', numeric: true, render: (row) => row.items.length },
    {
      key: 'total_amount',
      label: 'Total',
      numeric: true,
      render: (row) => <Money amount={row.total_amount} currency={row.currency} />,
    },
    { key: 'status', label: 'Status', render: (row) => <StatusPill status={row.status} /> },
    {
      key: 'created_at',
      label: 'Placed',
      render: (row) => new Date(row.created_at).toLocaleDateString(),
    },
  ]

  return (
    <>
      <PageHeader
        title="Orders"
        subtitle={scope === 'all' ? 'All customer orders' : 'Your orders'}
        actions={
          isStaff ? (
            <div className="segmented">
              <button
                type="button"
                className={scope === 'all' ? 'active' : undefined}
                onClick={() => {
                  setScope('all')
                  setPage(1)
                }}
              >
                All
              </button>
              <button
                type="button"
                className={scope === 'mine' ? 'active' : undefined}
                onClick={() => {
                  setScope('mine')
                  setPage(1)
                }}
              >
                Mine
              </button>
            </div>
          ) : null
        }
      />

      <CartPanel cart={cart} checkout={checkout} />

      {scope === 'all' ? (
        <div className="filters">
          <select
            value={status}
            onChange={(event) => {
              setStatus(event.target.value)
              setPage(1)
            }}
          >
            <option value="">All statuses</option>
            {ORDER_STATUSES.map((value) => (
              <option key={value} value={value}>
                {value.replaceAll('_', ' ')}
              </option>
            ))}
          </select>
        </div>
      ) : null}

      <ErrorBanner error={orders.error} onRetry={orders.refetch} />

      {orders.loading ? (
        <Loading label="Loading orders" />
      ) : (
        <>
          <DataTable
            columns={columns}
            rows={orders.data?.items}
            keyField="order_id"
            empty="No orders yet."
          />
          <Pagination
            page={orders.data?.page || 1}
            totalPages={orders.data?.total_pages || 0}
            onChange={setPage}
          />
        </>
      )}
    </>
  )
}

function CartPanel({ cart, checkout }) {
  const [address, setAddress] = useState('')

  if (cart.loading) return null
  const items = cart.data?.items || []
  if (!items.length) return null

  return (
    <section className="panel panel--accent">
      <h2>Your cart</h2>
      <DataTable
        columns={[
          { key: 'sku', label: 'SKU' },
          { key: 'product_name', label: 'Product' },
          { key: 'quantity', label: 'Qty', numeric: true },
          {
            key: 'subtotal',
            label: 'Subtotal',
            numeric: true,
            render: (row) => <Money amount={row.subtotal} />,
          },
          {
            key: 'is_orderable',
            label: '',
            /* A product can go out of catalog while it sits in a basket. The
               cart says so rather than failing at checkout with no
               explanation. */
            render: (row) => (row.is_orderable ? null : <StatusPill status="CANCELLED" />),
          },
        ]}
        rows={items}
        keyField="cart_item_id"
      />
      <p className="cart-total">
        Total: <Money amount={cart.data.total_amount} currency={cart.data.currency} />
      </p>

      <div className="checkout">
        <label>
          Shipping address
          <input
            type="text"
            value={address}
            onChange={(event) => setAddress(event.target.value)}
            minLength={10}
            placeholder="42 MG Road, Bangalore, Karnataka 560001"
          />
        </label>
        <button
          type="button"
          disabled={checkout.pending || address.trim().length < 10}
          onClick={() => checkout.execute(address)}
        >
          {checkout.pending ? 'Placing order...' : 'Place order'}
        </button>
      </div>
      <ErrorBanner error={checkout.error} />
    </section>
  )
}
