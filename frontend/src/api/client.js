/**
 * The single HTTP client for the whole app.
 *
 * Every request goes through here so that three things are handled in one
 * place rather than repeated in every component:
 *
 *  1. Attaching the bearer token.
 *  2. Turning the backend's error envelope into a predictable shape.
 *  3. Reacting to 401 (session gone) and 429 (rate limited).
 *
 * ## Where the token lives
 *
 * In `localStorage`. That is a real trade-off and worth stating: localStorage
 * is readable by any script on the page, so a successful XSS can steal the
 * token. The alternative -- an httpOnly, SameSite cookie -- cannot be read by
 * JavaScript at all and is what a production deployment should use, but it
 * requires the API and the app to share a domain and adds CSRF handling.
 *
 * localStorage is chosen here because it keeps the auth flow legible, and the
 * mitigation that actually matters is already in place: **the server enforces
 * every permission**. A stolen token grants exactly what that user could do
 * anyway, and nothing the UI does can widen it.
 */
import axios from 'axios'

const TOKEN_KEY = 'retailpulse.token'
const USER_KEY = 'retailpulse.user'

export const tokenStore = {
  get: () => localStorage.getItem(TOKEN_KEY),
  set: (token) => localStorage.setItem(TOKEN_KEY, token),
  clear: () => {
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(USER_KEY)
  },
  getUser: () => {
    const raw = localStorage.getItem(USER_KEY)
    try {
      return raw ? JSON.parse(raw) : null
    } catch {
      // A corrupted entry must not wedge the app on every page load.
      localStorage.removeItem(USER_KEY)
      return null
    }
  },
  setUser: (user) => localStorage.setItem(USER_KEY, JSON.stringify(user)),
}

export const api = axios.create({
  baseURL: '/api',
  timeout: 15000,
  headers: { 'Content-Type': 'application/json' },
})

api.interceptors.request.use((config) => {
  const token = tokenStore.get()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

/**
 * Normalise every failure into { status, code, message, details }.
 *
 * Components should never have to know whether a failure came from the
 * backend's error envelope, a network timeout, or the browser being offline.
 */
export class ApiError extends Error {
  constructor({ status, code, message, details, retryAfterSeconds }) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.details = details || {}
    this.retryAfterSeconds = retryAfterSeconds
  }
}

let onUnauthorized = () => {}
export function setUnauthorizedHandler(handler) {
  onUnauthorized = handler
}

api.interceptors.response.use(
  (response) => response,
  (error) => {
    // No response at all: the request never reached a server.
    if (!error.response) {
      const timedOut = error.code === 'ECONNABORTED'
      return Promise.reject(
        new ApiError({
          status: 0,
          code: timedOut ? 'timeout' : 'network_error',
          message: timedOut
            ? 'The server took too long to respond. Please try again.'
            : 'Could not reach the server. Check your connection.',
        }),
      )
    }

    const { status, data, headers } = error.response
    const envelope = data?.error ?? {}

    if (status === 401) {
      // The session is gone. Clear it and let the app route to login rather
      // than leaving the user clicking a dead UI.
      tokenStore.clear()
      onUnauthorized()
    }

    return Promise.reject(
      new ApiError({
        status,
        code: envelope.code || `http_${status}`,
        message:
          envelope.message ||
          (status === 429
            ? 'Too many requests. Please slow down.'
            : 'Something went wrong. Please try again.'),
        details: envelope.details,
        retryAfterSeconds:
          Number(headers?.['retry-after']) || envelope.details?.retry_after_seconds,
      }),
    )
  },
)

// ---------------------------------------------------------------------------
// Endpoints, grouped by the service that owns them.
// ---------------------------------------------------------------------------
export const authApi = {
  login: (email, password) => api.post('/auth/login', { email, password }),
  register: (payload) => api.post('/auth/register', payload),
  me: () => api.get('/users/me'),
}

export const productApi = {
  list: (params) => api.get('/products', { params }),
  get: (id) => api.get(`/products/${id}`),
  search: (q, params) => api.get('/products/search', { params: { q, ...params } }),
  categories: () => api.get('/categories'),
  create: (payload) => api.post('/products', payload),
  update: (id, payload) => api.put(`/products/${id}`, payload),
  discontinue: (id) => api.delete(`/products/${id}`),
}

export const inventoryApi = {
  forProduct: (productId) => api.get(`/inventory/${productId}`),
  locations: () => api.get('/locations'),
  lowStock: (params) => api.get('/inventory/low-stock', { params }),
  restock: (payload) => api.post('/inventory/restock', payload),
}

export const orderApi = {
  mine: (params) => api.get('/orders', { params }),
  all: (params) => api.get('/orders/all', { params }),
  get: (id) => api.get(`/orders/${id}`),
  create: (payload) => api.post('/orders', payload),
  cancel: (id, reason) => api.post(`/orders/${id}/cancel`, { reason }),
  cart: () => api.get('/cart'),
  addToCart: (productId, quantity) =>
    api.post('/cart/items', { product_id: productId, quantity }),
  setCartQuantity: (productId, quantity) =>
    api.put(`/cart/items/${productId}`, { quantity }),
  clearCart: () => api.delete('/cart'),
}

export const analyticsApi = {
  dashboard: (days) => api.get('/analytics/dashboard', { params: { days } }),
  salesOverTime: (days) => api.get('/analytics/sales/over-time', { params: { days } }),
  byCategory: (days) => api.get('/analytics/sales/by-category', { params: { days } }),
  byStore: (days) => api.get('/analytics/sales/by-store', { params: { days } }),
  topProducts: (days, limit) =>
    api.get('/analytics/top-products', { params: { days, limit } }),
  replenishment: () => api.get('/analytics/replenishment'),
}

export const forecastApi = {
  products: () => api.get('/forecast/products'),
  forProduct: (productId, storeId, days) =>
    api.get(`/forecast/${productId}`, { params: { store_id: storeId, forecast_days: days } }),
  modelInfo: () => api.get('/model/info'),
}
