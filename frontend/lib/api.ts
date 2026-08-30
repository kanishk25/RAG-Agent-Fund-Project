/**
 * Typed client for the FastAPI backend (P7.1).
 *
 * Types mirror `mf_faq/generation/render.py::AskResponse` and
 * `mf_faq/api/main.py::HealthResponse` field-for-field — if the backend
 * response shape changes, update both sides together.
 *
 * Deliberately no client-side retry loop: the backend already retries and
 * backs off internally (P4.5b) and renders any exhausted failure as an
 * `answered: false` / `service_unavailable` response, not an HTTP error. A
 * non-2xx or network failure here means something is actually down, and maps
 * straight to the UI's error state (components/ErrorState.tsx) — retrying is
 * a decision the person reading that state makes, not this client.
 */

const API_BASE_URL = (process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000").replace(
  /\/$/,
  "",
);

export interface LinkTarget {
  url: string;
  label: string;
}

export interface AskResponse {
  answered: boolean;
  text: string;
  citation_url: string | null;
  source_as_of: string | null;
  stale: boolean;
  refusal_reason: string | null;
  link: LinkTarget | null;
  disclaimer: string;
}

export interface HealthResponse {
  status: string;
  index_sha: string;
  index_committed_at: string | null;
  documents: number;
  schemes: number;
  disclaimer: string;
}

export class ApiError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ApiError";
  }
}

export async function askQuestion(question: string, signal?: AbortSignal): Promise<AskResponse> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}/ask`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
      signal,
    });
  } catch {
    throw new ApiError("Could not reach the assistant.");
  }

  if (!response.ok) {
    throw new ApiError(`Request failed with status ${response.status}`);
  }

  return (await response.json()) as AskResponse;
}

export async function getHealth(signal?: AbortSignal): Promise<HealthResponse> {
  const response = await fetch(`${API_BASE_URL}/health`, { signal });
  if (!response.ok) {
    throw new ApiError(`Request failed with status ${response.status}`);
  }
  return (await response.json()) as HealthResponse;
}
