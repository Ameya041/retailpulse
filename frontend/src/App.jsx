import { Suspense, lazy } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import { AuthProvider } from './auth/AuthContext'
import Layout from './components/Layout'
import ProtectedRoute from './components/ProtectedRoute'
import { Loading } from './components/common'
import Login from './pages/Login'
import OrderDetail from './pages/OrderDetail'
import Orders from './pages/Orders'
import ProductDetail from './pages/ProductDetail'
import Products from './pages/Products'

/**
 * Chart-heavy pages are loaded on demand.
 *
 * Recharts is by far the largest dependency, and only these three pages use
 * it -- all three staff-only. Bundling it into the main chunk meant every
 * customer downloaded a charting library to look at their order history.
 * Splitting here cut the initial bundle roughly in half.
 */
const Dashboard = lazy(() => import('./pages/Dashboard'))
const Analytics = lazy(() => import('./pages/Analytics'))
const Forecast = lazy(() => import('./pages/Forecast'))
const Inventory = lazy(() => import('./pages/Inventory'))
const Admin = lazy(() => import('./pages/Admin'))

const STAFF = ['ADMIN', 'WAREHOUSE_OPERATOR']

export default function App() {
  return (
    <AuthProvider>
      <Suspense fallback={<Loading label="Loading page" />}>
        <Routes>
          <Route path="/login" element={<Login />} />

          <Route
            element={
              <ProtectedRoute>
                <Layout />
              </ProtectedRoute>
            }
          >
            <Route path="/dashboard" element={<Dashboard />} />
            <Route path="/products" element={<Products />} />
            <Route path="/products/:productId" element={<ProductDetail />} />
            <Route path="/orders" element={<Orders />} />
            <Route path="/orders/:orderId" element={<OrderDetail />} />

            <Route
              path="/inventory"
              element={
                <ProtectedRoute roles={STAFF}>
                  <Inventory />
                </ProtectedRoute>
              }
            />
            <Route
              path="/analytics"
              element={
                <ProtectedRoute roles={STAFF}>
                  <Analytics />
                </ProtectedRoute>
              }
            />
            <Route
              path="/forecast"
              element={
                <ProtectedRoute roles={STAFF}>
                  <Forecast />
                </ProtectedRoute>
              }
            />
            <Route
              path="/admin"
              element={
                <ProtectedRoute roles={['ADMIN']}>
                  <Admin />
                </ProtectedRoute>
              }
            />
          </Route>

          <Route path="/" element={<Navigate to="/dashboard" replace />} />
          {/* Anything unrecognised goes to the dashboard rather than a blank
              screen, which is what an unmatched route otherwise renders. */}
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Routes>
      </Suspense>
    </AuthProvider>
  )
}
