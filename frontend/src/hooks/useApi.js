/**
 * A small data-fetching hook.
 *
 * Deliberately not React Query: the app has a handful of screens, and pulling
 * in a caching layer would be more code to explain than it saves. What it does
 * provide is the part that is easy to get wrong by hand -- **not writing state
 * after the component has unmounted or the request is stale**, which otherwise
 * produces the classic "response from a page the user already left overwrites
 * the current page's data" bug.
 */
import { useCallback, useEffect, useRef, useState } from 'react'

export function useApi(fetcher, deps = [], { skip = false } = {}) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(!skip)

  // Incremented on every run; a response is applied only if it belongs to the
  // most recent one.
  const requestId = useRef(0)

  const run = useCallback(async () => {
    if (skip) {
      setLoading(false)
      return
    }
    const id = ++requestId.current
    setLoading(true)
    setError(null)
    try {
      const response = await fetcher()
      if (id === requestId.current) setData(response.data)
    } catch (err) {
      if (id === requestId.current) setError(err)
    } finally {
      if (id === requestId.current) setLoading(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [skip, ...deps])

  useEffect(() => {
    run()
    return () => {
      // Invalidate anything in flight when the effect re-runs or unmounts.
      requestId.current += 1
    }
  }, [run])

  return { data, error, loading, refetch: run, setData }
}

/**
 * For actions rather than reads: submit, track pending/error, surface result.
 */
export function useAction(action) {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState(null)

  const execute = useCallback(
    async (...args) => {
      setPending(true)
      setError(null)
      try {
        return await action(...args)
      } catch (err) {
        setError(err)
        throw err
      } finally {
        setPending(false)
      }
    },
    [action],
  )

  return { execute, pending, error, clearError: () => setError(null) }
}
