import type { AskResponse } from "@/lib/api";

/**
 * P7.4 — a successful, cited answer. Renders `source_as_of` in the footer
 * unconditionally: PS §8.3 treats that date as the one thing an answer must
 * never be shown without.
 *
 * Departure from the code.html reference: that mock invents a per-fact
 * heading ("Expense Ratio") the API does not return (`AskResponse` carries
 * only free text + citation + date, no `doc_type` label) — inventing one
 * here would risk a heading that disagrees with the sentence beneath it.
 * The check-circle treatment is kept; the heading is a generic "Answer".
 *
 * P7.4/7.5b's stale variant: same card, a distinct amber-toned badge and
 * left-border color instead of a second component, since the content is a
 * real, dated answer that is merely ageing — not a refusal.
 */
export function AnswerCard({ response }: { response: AskResponse }) {
  const { text, citation_url, source_as_of, stale } = response;

  return (
    <div
      className={`rounded-xl border border-outline-variant border-l-4 bg-surface-container-lowest p-lg card-shadow ${
        stale ? "border-l-tertiary" : "border-l-primary"
      }`}
    >
      <div className="mb-sm flex items-start gap-sm">
        <span
          aria-hidden
          className={`material-symbols-outlined rounded-full p-base ${
            stale ? "bg-tertiary-container/20 text-tertiary" : "bg-primary-container/10 text-primary"
          }`}
        >
          {stale ? "schedule" : "check_circle"}
        </span>
        <div className="flex flex-1 items-center justify-between gap-sm">
          <h3 className="text-headline-md font-headline-md text-on-surface">Answer</h3>
          {stale && (
            <span className="rounded-full bg-tertiary-container/20 px-sm py-base text-label-caps text-tertiary">
              May be outdated
            </span>
          )}
        </div>
      </div>

      <p className="mb-md pl-10 text-body-md text-on-surface">{text}</p>

      <div className="flex flex-col items-start justify-between gap-xs border-t border-surface-variant pl-10 pt-sm sm:flex-row sm:items-center">
        {citation_url ? (
          <a
            href={citation_url}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-base text-label-md text-primary hover:underline"
          >
            Source: Groww scheme page
            <span aria-hidden className="material-symbols-outlined" style={{ fontSize: 14 }}>
              open_in_new
            </span>
          </a>
        ) : (
          <span />
        )}
        {source_as_of && (
          <span className="text-label-caps text-on-surface-variant">
            As of {formatDate(source_as_of)}
          </span>
        )}
      </div>
    </div>
  );
}

const MONTHS = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
];

/** Formats a plain `YYYY-MM-DD` date string without going through `Date`
 * parsing, which applies the browser's local timezone and can shift a
 * date-only value onto the wrong calendar day. */
function formatDate(iso: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  if (!match) return iso;
  const [, year, month, day] = match;
  const monthName = MONTHS[Number(month) - 1];
  if (!monthName) return iso;
  return `${monthName} ${Number(day)}, ${year}`;
}
