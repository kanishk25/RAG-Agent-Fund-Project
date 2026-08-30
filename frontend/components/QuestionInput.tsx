"use client";

import { useId, useState } from "react";

/**
 * P7.3 — search-style input + three clickable suggestion chips.
 *
 * The chips ask real, answerable questions against the actual corpus
 * (config/sources.yaml) rather than placeholder copy, so "three example
 * questions ... return real answers" (exit criterion) holds by construction.
 */
const SUGGESTIONS = [
  {
    icon: "trending_up",
    text: "What is the NAV of Motilal Oswal ELSS Tax Saver Fund?",
  },
  {
    icon: "payments",
    text: "What is the minimum SIP for the Nifty Next 50 Index Fund?",
  },
  {
    icon: "lock_clock",
    text: "What is the lock-in period for the ELSS fund?",
  },
] as const;

interface QuestionInputProps {
  onSubmit: (question: string) => void;
  disabled: boolean;
}

export function QuestionInput({ onSubmit, disabled }: QuestionInputProps) {
  const [value, setValue] = useState("");
  const inputId = useId();

  function submit(question: string) {
    const trimmed = question.trim();
    if (!trimmed || disabled) return;
    onSubmit(trimmed);
  }

  return (
    <section className="relative z-10 mb-xl rounded-xl border border-outline-variant bg-surface-container-lowest p-md card-shadow">
      <form
        className="relative flex items-center"
        onSubmit={(e) => {
          e.preventDefault();
          submit(value);
        }}
      >
        <label htmlFor={inputId} className="visually-hidden">
          Ask a question about a Motilal Oswal scheme
        </label>
        <span
          aria-hidden
          className="material-symbols-outlined absolute left-sm text-outline"
          style={{ fontSize: 20 }}
        >
          search
        </span>
        <input
          id={inputId}
          type="text"
          autoComplete="off"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          disabled={disabled}
          placeholder="e.g. What is the expense ratio of Motilal Oswal ELSS Tax Saver Fund?"
          className="w-full rounded-lg border border-outline-variant bg-surface py-sm pl-10 pr-24 text-body-lg text-on-surface transition-all focus:border-primary focus:ring-2 focus:ring-primary-fixed-dim/50 focus:outline-none disabled:opacity-60"
        />
        <button
          type="submit"
          disabled={disabled || !value.trim()}
          className="absolute right-sm flex items-center gap-base rounded-lg bg-primary px-sm py-base text-label-md text-on-primary transition-colors hover:bg-primary-container disabled:cursor-not-allowed disabled:opacity-50"
        >
          Ask
          <span aria-hidden className="material-symbols-outlined" style={{ fontSize: 16 }}>
            send
          </span>
        </button>
      </form>

      <div className="mt-sm flex flex-wrap gap-xs">
        {SUGGESTIONS.map((suggestion) => (
          <button
            key={suggestion.text}
            type="button"
            disabled={disabled}
            onClick={() => {
              setValue(suggestion.text);
              submit(suggestion.text);
            }}
            className="flex items-center gap-base rounded-full border border-outline-variant bg-surface px-sm py-base text-label-md text-on-surface-variant transition-colors hover:bg-surface-variant disabled:cursor-not-allowed disabled:opacity-50"
          >
            <span aria-hidden className="material-symbols-outlined" style={{ fontSize: 14 }}>
              {suggestion.icon}
            </span>
            {suggestion.text}
          </button>
        ))}
      </div>
    </section>
  );
}
