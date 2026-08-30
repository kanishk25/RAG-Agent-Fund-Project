/** P7.7 — shown between submit and response; matches AnswerCard's shape
 * (icon circle + heading + three text lines + footer row) so the layout
 * doesn't jump when the real card replaces it. */
export function LoadingSkeleton() {
  return (
    <div
      aria-hidden
      className="rounded-xl border border-outline-variant bg-surface-container-lowest p-lg card-shadow opacity-70"
    >
      <div className="mb-sm flex items-center gap-sm">
        <div className="h-8 w-8 rounded-full skeleton-shimmer" />
        <div className="h-4 w-48 rounded skeleton-shimmer" />
      </div>
      <div className="space-y-sm pl-10">
        <div className="h-3 w-full rounded skeleton-shimmer" />
        <div className="h-3 w-5/6 rounded skeleton-shimmer" />
        <div className="h-3 w-4/6 rounded skeleton-shimmer" />
      </div>
    </div>
  );
}
