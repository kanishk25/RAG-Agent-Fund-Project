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

/**
 * `?? "http://localhost:8000"` alone is not enough here: an *empty string*
 * env var (unset in one Vercel environment, cleared during editing, etc.)
 * passes `??` unchanged, and `${""}/ask` is a same-origin relative URL —
 * which "succeeds" at the network level while quietly hitting the frontend's
 * own domain instead of the backend, producing a confusing 404 with no clue
 * why. Blank is therefore folded in with unset. In dev, falling back to the
 * local API is a convenience; in any other build, an unset base URL is a
 * deploy misconfiguration and should fail loudly rather than guess.
 */
function resolveApiBaseUrl(): string {
  const configured = process.env.NEXT_PUBLIC_API_BASE_URL?.trim();
  if (configured) return configured.replace(/\/$/, "");
  if (process.env.NODE_ENV === "development") return "http://localhost:8000";
  return "";
}

const API_BASE_URL = resolveApiBaseUrl();

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

function requireApiBaseUrl(): string {
  if (!API_BASE_URL) {
    // Logged, never shown to the user — ErrorState always renders the same
    // generic "temporarily unavailable" copy regardless of cause (P7.6).
    // This is what a developer sees in the console instead of a mystifying
    // 404 to the frontend's own domain.
    console.error(
      "NEXT_PUBLIC_API_BASE_URL is not set. Set it in the deployment platform's " +
        "environment variables to the backend's URL and redeploy (see docs/deployment.md).",
    );
    throw new ApiError("API base URL is not configured.");
  }
  return API_BASE_URL;
}

export async function askQuestion(question: string, signal?: AbortSignal): Promise<AskResponse> {
  const baseUrl = requireApiBaseUrl();
  let response: Response;
  try {
    response = await fetch(`${baseUrl}/ask`, {
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
  const baseUrl = requireApiBaseUrl();
  const response = await fetch(`${baseUrl}/health`, { signal });
  if (!response.ok) {
    throw new ApiError(`Request failed with status ${response.status}`);
  }
  return (await response.json()) as HealthResponse;
}
