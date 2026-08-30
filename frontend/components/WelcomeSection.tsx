/** P7.3 — headline + a one-line scope statement (design ref: code.html:136-139). */
export function WelcomeSection() {
  return (
    <section className="mb-xl text-center md:text-left">
      <h2 className="mb-xs text-headline-lg-mobile md:text-headline-lg font-semibold text-on-surface">
        How can I help you today?
      </h2>
      <p className="max-w-2xl text-body-lg text-on-surface-variant">
        Ask about NAV, expense ratio, exit load, minimum SIP, lock-in, benchmark, or holdings for
        5 Motilal Oswal schemes.
      </p>
    </section>
  );
}
