"use client";

import { AnswerCard } from "@/components/AnswerCard";
import { ErrorState } from "@/components/ErrorState";
import { LoadingSkeleton } from "@/components/LoadingSkeleton";
import { RefusalCard } from "@/components/RefusalCard";
import type { AskResponse } from "@/lib/api";

/**
 * P7.8 — one query in flight at a time; a new submission replaces the feed
 * rather than appending to it (no multi-turn memory, ARCH §15.2). `stale` is
 * folded into the `answered` case rather than a fifth top-level status: the
 * backend already represents it as `AskResponse.stale` on a normal answered
 * response, and giving it a separate state here would mean deriving the same
 * boolean twice.
 */
export type FeedState =
  | { status: "idle" }
  | { status: "loading"; question: string }
  | { status: "answered"; response: AskResponse }
  | { status: "error"; question: string };

interface AnswerFeedProps {
  state: FeedState;
  onRetry: (question: string) => void;
}

export function AnswerFeed({ state, onRetry }: AnswerFeedProps) {
  return (
    <section aria-live="polite" aria-atomic="true" className="flex flex-col gap-lg">
      {state.status === "loading" && <LoadingSkeleton />}
      {state.status === "answered" &&
        (state.response.answered ? (
          <AnswerCard response={state.response} />
        ) : (
          <RefusalCard response={state.response} />
        ))}
      {state.status === "error" && <ErrorState onRetry={() => onRetry(state.question)} />}
    </section>
  );
}
