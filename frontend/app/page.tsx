"use client";

import { useCallback, useRef, useState } from "react";

import { AnswerFeed, type FeedState } from "@/components/AnswerFeed";
import { QuestionInput } from "@/components/QuestionInput";
import { TopAppBar } from "@/components/TopAppBar";
import { WelcomeSection } from "@/components/WelcomeSection";
import { askQuestion } from "@/lib/api";

export default function Home() {
  const [feed, setFeed] = useState<FeedState>({ status: "idle" });
  const requestId = useRef(0);

  const submit = useCallback((question: string) => {
    const id = ++requestId.current;
    setFeed({ status: "loading", question });

    askQuestion(question)
      .then((response) => {
        if (id !== requestId.current) return; // a newer question superseded this one
        setFeed({ status: "answered", response });
      })
      .catch(() => {
        if (id !== requestId.current) return;
        setFeed({ status: "error", question });
      });
  }, []);

  return (
    <>
      <TopAppBar />
      <main className="mx-auto w-full max-w-[800px] flex-grow px-gutter pb-xl pt-xl">
        <WelcomeSection />
        <QuestionInput onSubmit={submit} disabled={feed.status === "loading"} />
        <AnswerFeed state={feed} onRetry={submit} />
      </main>
      <Footer />
    </>
  );
}

function Footer() {
  return (
    <footer className="mt-auto w-full border-t border-outline-variant bg-surface-bright px-gutter py-lg">
      <div className="mx-auto flex w-full max-w-container-max flex-col items-center justify-between gap-sm text-label-caps text-on-surface-variant md:flex-row">
        <span>Facts-only. No investment advice.</span>
        <span>Data sourced from Groww scheme pages.</span>
      </div>
    </footer>
  );
}
