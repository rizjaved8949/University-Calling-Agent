import { useEffect, useRef, useState } from "react";
import { createFileRoute } from "@tanstack/react-router";
import { useMutation } from "@tanstack/react-query";
import { Copy, Send, Sparkles } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ApiError, api } from "@/lib/api";
import { hasUrdu } from "@/lib/format";
import { useI18n } from "@/lib/i18n";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/ask")({
  head: () => ({
    meta: [
      { title: "Ask Ayesha · Admissions Voice Agent" },
      {
        name: "description",
        content:
          "Chat-test the same admissions knowledge base Ayesha uses on calls — fees, scholarships, hostel and deadlines.",
      },
      { property: "og:title", content: "Ask Ayesha · Admissions Voice Agent" },
      {
        property: "og:description",
        content: "A safe place to test what the admissions agent knows, without placing a call.",
      },
    ],
  }),
  component: AskPage,
});

const SUGGESTIONS = [
  "Admission kab start ho rahe hain?",
  "BS Computer Science ki fee kitni hai?",
  "Scholarship ke liye kya requirements hain?",
  "Hostel available hai?",
];

type Msg = { id: string; role: "user" | "agent"; text: string; sources?: string[] };

function AskPage() {
  const { t } = useI18n();
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const sessionId = useRef(`console-${Math.random().toString(36).slice(2, 10)}`);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [messages]);

  const send = useMutation({
    mutationFn: (text: string) => api.chat(text, sessionId.current),
    onSuccess: (res) => {
      const text = res.answer ?? res.reply ?? res.message ?? res.response ?? "";
      // Backend sources are {section, title, page, score} — show the section and
      // page so staff can look the answer up in the prospectus themselves.
      const sources = (res.sources ?? [])
        .map((s) => {
          if (typeof s === "string") return s;
          const label = s.section ?? s.title ?? s.text ?? "";
          return label && s.page ? `${label} (p. ${s.page})` : label;
        })
        .filter(Boolean);
      setMessages((prev) => [
        ...prev,
        {
          id: `a-${prev.length}`,
          role: "agent",
          text: text || "I don't have an answer for that yet.",
          ...(sources.length ? { sources } : {}),
        },
      ]);
    },
    onError: (err) => {
      const e = err as ApiError;
      toast.error(e.friendly ?? "Couldn't reach Ayesha.", {
        description: e.hint ?? "Check your connection, then send the question again.",
      });
    },
  });

  const submit = (text: string) => {
    const trimmed = text.trim();
    if (!trimmed || send.isPending) return;
    setMessages((prev) => [...prev, { id: `u-${prev.length}`, role: "user", text: trimmed }]);
    setInput("");
    send.mutate(trimmed);
  };

  return (
    <div className="mx-auto flex max-w-2xl flex-col gap-4">
      <div>
        <h1 className="text-lg font-semibold text-foreground">{t("askAyesha")}</h1>
        <p className="mt-1 rounded-xl bg-secondary px-3 py-2 text-sm text-secondary-foreground">
          {t("askBanner")}
        </p>
      </div>

      <div className="surface-panel flex min-h-[50vh] flex-col p-4">
        <div className="flex-1 space-y-3 overflow-y-auto" aria-live="polite">
          {messages.length === 0 ? (
            <div className="flex flex-col items-center px-4 py-10 text-center">
              <span className="mb-4 grid size-16 place-items-center rounded-2xl bg-primary/10">
                <Sparkles className="size-7 text-primary" aria-hidden="true" />
              </span>
              <h2 className="font-semibold text-foreground">Try a question below</h2>
              <p className="mt-1 max-w-sm text-sm text-muted-foreground">
                Ayesha answers with the same admissions knowledge she uses on live calls.
              </p>
            </div>
          ) : null}

          {messages.map((m) => {
            const agent = m.role === "agent";
            return (
              <div key={m.id} className={cn("flex", agent ? "justify-start" : "justify-end")}>
                <div
                  className={cn(
                    "max-w-[85%] rounded-2xl px-4 py-2.5 text-sm break-words shadow-soft",
                    agent
                      ? "rounded-tl-sm bg-secondary text-secondary-foreground"
                      : "rounded-tr-sm bg-primary text-primary-foreground",
                  )}
                >
                  <p
                    className={cn(hasUrdu(m.text) && "font-urdu text-right")}
                    dir={hasUrdu(m.text) ? "rtl" : "ltr"}
                  >
                    {m.text}
                  </p>
                  {m.sources?.length ? (
                    <details className="mt-2 text-xs opacity-80">
                      <summary className="cursor-pointer">{t("sources")}</summary>
                      <ul className="mt-1 list-disc ps-4">
                        {m.sources.map((s) => (
                          <li key={s}>{s}</li>
                        ))}
                      </ul>
                    </details>
                  ) : null}
                  {agent ? (
                    <button
                      type="button"
                      className="mt-2 inline-flex items-center gap-1 text-xs opacity-70 hover:opacity-100"
                      onClick={async () => {
                        await navigator.clipboard.writeText(m.text);
                        toast.success(t("copied"));
                      }}
                    >
                      <Copy className="size-3" aria-hidden="true" />
                      {t("copy")}
                    </button>
                  ) : null}
                </div>
              </div>
            );
          })}

          {send.isPending ? (
            <div className="flex justify-start">
              <div className="flex gap-1 rounded-2xl bg-secondary px-4 py-3">
                {[0, 1, 2].map((i) => (
                  <span
                    key={i}
                    className="size-2 animate-bounce rounded-full bg-muted-foreground/60"
                    style={{ animationDelay: `${i * 120}ms` }}
                  />
                ))}
              </div>
            </div>
          ) : null}
          <div ref={endRef} />
        </div>

        <div className="mt-4 flex flex-wrap gap-2">
          {SUGGESTIONS.map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => submit(s)}
              className="rounded-full border border-border bg-card px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
            >
              {s}
            </button>
          ))}
        </div>

        <form
          className="mt-3 flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            submit(input);
          }}
        >
          <Input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={t("askPlaceholder")}
            aria-label={t("askPlaceholder")}
          />
          <Button type="submit" disabled={!input.trim() || send.isPending} className="gap-2">
            <Send className="size-4" aria-hidden="true" />
            <span className="hidden sm:inline">{t("send")}</span>
          </Button>
        </form>
      </div>
    </div>
  );
}
