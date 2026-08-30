import type { AskResponse } from "@/lib/api";

/**
 * P7.5 — a refusal, rendered as a correct outcome (P4's own framing), never
 * as an error: neutral surface, a policy icon rather than a warning icon,
 * and no red. `response.link` is rendered whenever present; several refusal
 * reasons (pii, ambiguous_scheme) legitimately carry none (render.py), and
 * the card omits the link row entirely rather than showing a dead button.
 */
export function RefusalCard({ response }: { response: AskResponse }) {
  return (
    <div className="rounded-xl border border-outline-variant bg-surface-variant p-lg card-shadow">
      <div className="flex items-start gap-sm">
        <span
          aria-hidden
          className="material-symbols-outlined rounded-full bg-surface-container/50 p-base text-secondary"
        >
          policy
        </span>
        <div>
          <p className="mb-xs text-body-md text-on-surface">{response.text}</p>
          {response.link && (
            <a
              href={response.link.url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-base rounded-lg border border-secondary px-sm py-base text-label-md text-secondary transition-colors hover:bg-secondary/10"
            >
              {response.link.label}
              <span aria-hidden className="material-symbols-outlined" style={{ fontSize: 14 }}>
                open_in_new
              </span>
            </a>
          )}
        </div>
      </div>
    </div>
  );
}
