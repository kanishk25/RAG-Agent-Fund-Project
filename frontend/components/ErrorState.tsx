/**
 * P7.6 — the `service_unavailable` outcome (429 exhausted, empty index, or
 * any uncaught exception at the FastAPI boundary — see api/main.py's
 * outermost `except Exception`). Never shows the raw error or a stack trace;
 * `Retry` resubmits the exact question that failed.
 */
export function ErrorState({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="flex flex-col items-center justify-between gap-sm rounded-xl border border-outline-variant bg-surface-container-high p-lg card-shadow sm:flex-row">
      <div className="flex items-center gap-sm text-tertiary">
        <span aria-hidden className="material-symbols-outlined">
          wifi_off
        </span>
        <span className="text-body-md">
          The assistant is temporarily unavailable. Please try again in a moment.
        </span>
      </div>
      <button
        type="button"
        onClick={onRetry}
        className="shrink-0 rounded-lg bg-on-surface px-md py-xs text-label-md text-on-primary transition-colors hover:bg-on-surface-variant"
      >
        Retry
      </button>
    </div>
  );
}
