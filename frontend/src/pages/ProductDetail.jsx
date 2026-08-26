import { useCallback, useState } from 'react'
import { useParams } from 'react-router-dom'
import { inventoryApi, orderApi, productApi } from '../api/client'
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

export default function ProductDetail() {
  const { productId } = useParams()
  const { isAuthenticated } = useAuth()
  const [quantity, setQuantity] = useState(1)
  const [added, setAdded] = useState(false)

  const product = useApi(useCallback(() => productApi.get(productId), [productId]), [productId])
  const inventory = useApi(
    useCallback(() => inventoryApi.forProduct(productId), [productId]),
    [productId],
  )

  const addToCart = useAction(
    useCallback(async () => {
      await orderApi.addToCart(productId, quantity)
      setAdded(true)
      setTimeout(() => setAdded(false), 2500)
    }, [productId, quantity]),
  )

  if (product.loading) return <Loading label="Loading product" />
  if (product.error) return <ErrorBanner error={product.error} onRetry={product.refetch} />
  if (!product.data) return null

  const item = product.data
  const isOrderable = item.status === 'ACTIVE'

  return (
    <>
      <BackLink to="/products">Back to products</BackLink>
      <PageHeader
        title={item.name}
        subtitle={`${item.sku} · ${item.category}${item.brand ? ` · ${item.brand}` : ''}`}
        actions={<StatusPill status={item.status} />}
      />

      <div className="two-col">
        <section className="panel">
          <h2>Details</h2>
          <dl className="detail-list">
            <div>
              <dt>Price</dt>
              <dd>
                <Money amount={item.price} currency={item.currency} />
              </dd>
            </div>
            <div>
              <dt>Weight</dt>
              <dd>{item.weight_grams ? `${item.weight_grams} g` : '--'}</dd>
            </div>
            <div>
              <dt>Description</dt>
              <dd>{item.description || '--'}</dd>
            </div>
          </dl>

          {isAuthenticated ? (
            <div className="add-to-cart">
              <label>
                Quantity
                <input
                  type="number"
                  min={1}
                  max={100}
                  value={quantity}
                  onChange={(event) => setQuantity(Number(event.target.value))}
                />
              </label>
              <button
                type="button"
                onClick={() => addToCart.execute()}
                disabled={addToCart.pending || !isOrderable}
                title={isOrderable ? undefined : 'This product is no longer available'}
              >
                {addToCart.pending ? 'Adding...' : added ? 'Added to cart' : 'Add to cart'}
              </button>
            </div>
          ) : (
            <p className="muted">Sign in to add this product to your cart.</p>
          )}
          <ErrorBanner error={addToCart.error} />
        </section>

        <section className="panel">
          <h2>Stock by location</h2>
          {inventory.loading ? (
            <Loading />
          ) : inventory.error ? (
            /* Inventory is a different service. If it is unavailable the
               product page still renders -- only this panel degrades. */
            <p className="muted">Stock information is unavailable right now.</p>
          ) : (
            <>
              <p className="muted small">
                {inventory.data?.total_available ?? 0} available across{' '}
                {inventory.data?.locations_in_stock ?? 0} location(s)
              </p>
              <DataTable
                columns={[
                  { key: 'location_code', label: 'Location' },
                  { key: 'available_quantity', label: 'Available', numeric: true },
                  { key: 'reserved_quantity', label: 'Reserved', numeric: true },
                  {
                    key: 'is_low',
                    label: 'Status',
                    render: (row) => (row.is_low ? <StatusPill status="HIGH" /> : 'OK'),
                  },
                ]}
                rows={inventory.data?.locations}
                keyField="inventory_id"
                empty="This product is not stocked anywhere yet."
              />
            </>
          )}
        </section>
      </div>
    </>
  )
}
