/** Shared helpers for turning unknown thrown values into user-facing messages. */

/**
 * Extract a backend error message from an unknown thrown value.
 *
 * Axios rejects with an error whose `response.data.detail` carries the FastAPI
 * error string. Anything that doesn't match that shape (network errors, non-Axios
 * throws) falls back to the supplied message.
 */
export function apiError(e: unknown, fallback: string): string {
  if (typeof e === "object" && e !== null && "response" in e) {
    const detail = (e as { response?: { data?: { detail?: unknown } } })
      .response?.data?.detail;
    if (typeof detail === "string") return detail;
  }
  return fallback;
}
