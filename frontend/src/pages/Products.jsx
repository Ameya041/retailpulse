import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { productApi } from '../api/client'
import {
  DataTable,
  ErrorBanner,
  Loading,
  Money,
  PageHeader,
  Pagination,
  StatusPill,
} from '../components/common'
import { useApi } from '../hooks/useApi'

export default function Products() {
  const [page, setPage] = useState(1)
  const [query, setQuery] = useState('')
  const [debounced, setDebounced] = useState('')
  const [category, setCategory] = useState('')

  // Debounce the search box. Without it every keystroke is a request, which
  // both wastes the backend and will trip the gateway's rate limiter on a
  // moderately fast typist.
  useEffect(() => {
    const timer = setTimeout(() => {
      setDebounced(query.trim())
      setPage(1)
    }, 350)
    return () => clearTimeout(timer)
  }, [query])

  const categories = useApi(useCallback(() => productApi.categories(), []), [])

  const products = useApi(
    useCallback(() => {
      // The search endpoint requires at least two characters, so short input
      // falls back to the list endpoint rather than sending a request the
      // server will reject.
      if (debounced.length >= 2) {
        return productApi.search(debounced, { page, page_size: 20 })
      }
      return productApi.list({ page, page_size: 20, category: category || undefined })
    }, [debounced, page, category]),
    [debounced, page, category],
  )

  const columns = [
    {
      key: 'sku',
      label: 'SKU',
      render: (row) => <Link to={`/products/${row.product_id}`}>{row.sku}</Link>,
    },
    { key: 'name', label: 'Name' },
    { key: 'category', label: 'Category' },
    { key: 'brand', label: 'Brand', render: (row) => row.brand || '--' },
    {
      key: 'price',
      label: 'Price',
      numeric: true,
      render: (row) => <Money amount={row.price} currency={row.currency} />,
    },
    { key: 'status', label: 'Status', render: (row) => <StatusPill status={row.status} /> },
  ]

  return (
    <>
      <PageHeader title="Products" subtitle="Browse the catalog" />

      <div className="filters">
        <input
          type="search"
          placeholder="Search by name, SKU or brand"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <select
          value={category}
          onChange={(event) => {
            setCategory(event.target.value)
            setPage(1)
          }}
          disabled={debounced.length >= 2}
        >
          <option value="">All categories</option>
          {(categories.data || []).map((item) => (
            <option key={item.category_id} value={item.slug}>
              {item.name}
            </option>
          ))}
        </select>
      </div>

      <ErrorBanner error={products.error} onRetry={products.refetch} />

      {products.loading ? (
        <Loading label="Loading products" />
      ) : (
        <>
          <p className="muted small">
            {products.data?.total ?? 0} product(s)
            {debounced.length >= 2 ? ` matching "${debounced}"` : ''}
          </p>
          <DataTable
            columns={columns}
            rows={products.data?.items}
            keyField="product_id"
            empty="No products match this search."
          />
          <Pagination
            page={products.data?.page || 1}
            totalPages={products.data?.total_pages || 0}
            onChange={setPage}
          />
        </>
      )}
    </>
  )
}
