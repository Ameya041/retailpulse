import { useCallback, useState } from 'react'
import { productApi } from '../api/client'
import { ErrorBanner, PageHeader } from '../components/common'
import { useAction, useApi } from '../hooks/useApi'

const BLANK = {
  sku: '',
  name: '',
  description: '',
  category: '',
  brand: '',
  price: '',
  currency: 'INR',
  weight_grams: '',
}

export default function Admin() {
  const [form, setForm] = useState(BLANK)
  const [created, setCreated] = useState(null)

  const categories = useApi(useCallback(() => productApi.categories(), []), [])

  const create = useAction(
    useCallback(async () => {
      const payload = {
        ...form,
        // Empty optional fields are omitted rather than sent as empty strings,
        // which would fail validation on the server for no good reason.
        brand: form.brand || undefined,
        description: form.description || undefined,
        weight_grams: form.weight_grams ? Number(form.weight_grams) : undefined,
      }
      const { data } = await productApi.create(payload)
      setCreated(data)
      setForm(BLANK)
      await categories.refetch()
      return data
    }, [form, categories]),
  )

  const update = (field) => (event) => setForm({ ...form, [field]: event.target.value })

  return (
    <>
      <PageHeader title="Admin" subtitle="Catalog management" />

      <section className="panel">
        <h2>Create a product</h2>
        <p className="muted small">
          Requires the ADMIN role. This form is only convenience -- the server rejects the
          request outright if the caller is not an admin.
        </p>

        {created ? (
          <p className="state state--success">
            Created {created.sku} ({created.product_id.slice(0, 8)}).
          </p>
        ) : null}
        <ErrorBanner error={create.error} />

        <div className="form-grid">
          <label>
            SKU
            <input
              type="text"
              value={form.sku}
              onChange={update('sku')}
              placeholder="ELE-0001"
              required
            />
          </label>
          <label>
            Name
            <input type="text" value={form.name} onChange={update('name')} required />
          </label>
          <label>
            Category
            <input
              type="text"
              list="category-options"
              value={form.category}
              onChange={update('category')}
              required
            />
            <datalist id="category-options">
              {(categories.data || []).map((item) => (
                <option key={item.category_id} value={item.name} />
              ))}
            </datalist>
          </label>
          <label>
            Brand
            <input type="text" value={form.brand} onChange={update('brand')} />
          </label>
          <label>
            Price
            <input
              type="number"
              step="0.01"
              min="0.01"
              value={form.price}
              onChange={update('price')}
              required
            />
          </label>
          <label>
            Weight (g)
            <input
              type="number"
              min="0"
              value={form.weight_grams}
              onChange={update('weight_grams')}
            />
          </label>
        </div>

        <label className="full">
          Description
          <textarea rows={3} value={form.description} onChange={update('description')} />
        </label>

        <button
          type="button"
          disabled={create.pending || !form.sku || !form.name || !form.category || !form.price}
          onClick={() => create.execute()}
        >
          {create.pending ? 'Creating...' : 'Create product'}
        </button>
      </section>

      <section className="panel">
        <h2>Categories</h2>
        <ul className="chip-list">
          {(categories.data || []).map((item) => (
            <li key={item.category_id} className="chip">
              {item.name}
            </li>
          ))}
        </ul>
      </section>
    </>
  )
}
