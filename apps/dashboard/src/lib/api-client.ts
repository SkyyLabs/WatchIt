export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL || "http://127.0.0.1:4849";

export type ApiError = Error & {
  status?: number;
  detail?: string;
};

// The selected household scopes every guardian request via X-Household-Id.
// Held module-level so callers don't thread it through each apiFetch; the
// dashboard data provider updates it when the guardian switches households.
let activeHouseholdId: string | null = null;
export function setActiveHousehold(id: string | null) {
  activeHouseholdId = id;
}

export async function apiFetch<T>(
  path: string,
  token: string | null,
  init: RequestInit = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  if (token) headers.set("authorization", `Bearer ${token}`);
  if (activeHouseholdId) headers.set("X-Household-Id", activeHouseholdId);
  if (init.body && !headers.has("content-type")) {
    headers.set("content-type", "application/json");
  }

  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers,
  });
  const payload = await response.json().catch(() => null);

  if (!response.ok) {
    const error = new Error(payload?.detail || `Request failed with ${response.status}`) as ApiError;
    error.status = response.status;
    error.detail = payload?.detail;
    throw error;
  }

  return payload as T;
}

export function decisionStreamUrl(token: string | null): string {
  const params = new URLSearchParams();
  if (token) params.set("token", token);
  if (activeHouseholdId) params.set("household", activeHouseholdId);
  const qs = params.toString();
  return `${API_BASE_URL}/v1/stream/decisions${qs ? `?${qs}` : ""}`;
}
