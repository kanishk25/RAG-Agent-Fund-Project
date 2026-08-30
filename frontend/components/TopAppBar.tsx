/**
 * P7.2 — brand mark + the disclaimer that must be visible without scrolling
 * on every viewport (exit criterion). Unlike the code.html reference, which
 * hides the disclaimer pill below `md:`, it stays visible at every
 * breakpoint here — a deliberate departure to satisfy that criterion on
 * mobile too, just rendered more compactly.
 */
export function TopAppBar() {
  return (
    <header className="sticky top-0 z-50 w-full border-b border-outline-variant bg-surface">
      <div className="mx-auto flex h-16 w-full max-w-container-max items-center justify-between gap-sm px-gutter">
        <h1 className="text-headline-md font-headline-md text-primary truncate">
          Motilal Oswal FAQ
        </h1>
        <span className="flex shrink-0 items-center gap-base rounded-full bg-surface-variant px-sm py-base text-label-caps text-on-surface-variant">
          <span aria-hidden className="material-symbols-outlined" style={{ fontSize: 14 }}>
            info
          </span>
          <span className="hidden sm:inline">Facts-only. No investment advice.</span>
          <span className="sm:hidden">Facts-only.</span>
        </span>
      </div>
    </header>
  );
}
