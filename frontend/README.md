# Mutual Fund FAQ Assistant — Frontend

Next.js (App Router) + TypeScript + Tailwind CSS UI for `POST /ask` on the
FastAPI backend. See `../docs/implementation-plan.md` Phase 7 for the task
breakdown and `../stitch_motilal_oswal_faq_assistant/` for the visual design
reference this UI is built from.

## Setup

```bash
npm install
cp .env.local.example .env.local   # point NEXT_PUBLIC_API_BASE_URL at the backend
npm run dev
```

The backend must be running separately (from the repo root):

```bash
uvicorn mf_faq.api.main:app --reload
```

By default the backend's CORS config (`Settings.cors_allow_origins`) allows
`http://localhost:3000`, which matches `npm run dev`'s default port.

## Structure

- `app/page.tsx` — the single page; owns the `idle | loading | answered | error` state machine
- `components/` — `TopAppBar`, `WelcomeSection`, `QuestionInput`, `AnswerCard`,
  `RefusalCard`, `ErrorState`, `LoadingSkeleton`, `AnswerFeed`
- `lib/api.ts` — typed client for `/ask` and `/health`, mirroring the backend's
  `AskResponse` / `HealthResponse` Pydantic models field-for-field
- `app/globals.css` — design tokens (`@theme`) ported from `DESIGN.md`

## Notes

- No multi-turn memory: each submission replaces the feed rather than
  appending to it (matches ARCH §15.2).
- No client-side retry: a failed request renders the error state immediately;
  the backend already retries/backs off internally before giving up.
- No PII-inviting fields: the question input is free text only, no
  autocomplete hints, no separate fields for phone/PAN/account number.
