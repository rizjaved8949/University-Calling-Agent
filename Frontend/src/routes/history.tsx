import { useEffect, useMemo, useState } from "react";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Archive,
  ArrowDownLeft,
  ArrowUpRight,
  Download,
  FileSpreadsheet,
  Loader2,
  Search,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";
import { CallDetailSheet } from "@/components/CallDetailSheet";
import { EmptyState } from "@/components/EmptyState";
import { RecordingPlayer } from "@/components/RecordingPlayer";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { api, queryKeys } from "@/lib/api";
import {
  callDuration,
  callEndTime,
  callNumber,
  callTime,
  digitsOnly,
  formatDuration,
  isActiveCall,
  liveDuration,
  maskNumber,
  relativeTime,
  stampTime,
  toCsv,
} from "@/lib/format";
import { useI18n } from "@/lib/i18n";
import { describeFailure, describeStatus } from "@/lib/normalize";
import type { Call } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * Who actually did the talking.
 *
 * `mode` is absent on every call placed before the manual modes existed, and
 * those were all Ayesha's — so anything not explicitly MANUAL_* is hers.
 */
type Handler = "AI_IN" | "AI_OUT" | "HUMAN";

function handlerOf(call: Call): Handler {
  if ((call.mode ?? "").startsWith("MANUAL")) return "HUMAN";
  return call.direction === "INBOUND" ? "AI_IN" : "AI_OUT";
}

const TABS: { id: "ALL" | Handler; label: string }[] = [
  { id: "ALL", label: "All calls" },
  { id: "AI_IN", label: "Ayesha answered" },
  { id: "AI_OUT", label: "Ayesha called" },
  { id: "HUMAN", label: "You called" },
];

export const Route = createFileRoute("/history")({
  head: () => ({
    meta: [
      { title: "Call history · Admissions Voice Agent" },
      {
        name: "description",
        content:
          "Search, replay and export every admissions call Ayesha has handled, with full transcripts.",
      },
      { property: "og:title", content: "Call history · Admissions Voice Agent" },
      {
        property: "og:description",
        content: "Every admissions call, searchable with recordings and transcripts.",
      },
    ],
  }),
  component: HistoryPage,
});

function HistoryPage() {
  const { t } = useI18n();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [query, setQuery] = useState("");
  const [tab, setTab] = useState<"ALL" | Handler>("ALL");
  const [openCall, setOpenCall] = useState<string | null>(null);

  // One unfiltered fetch, filtered here. The tabs need counts for every tab at
  // once, and a single cache key is also what lets a live call event refresh
  // this page no matter which tab is open.
  const calls = useQuery({
    queryKey: queryKeys.calls(),
    queryFn: () => api.calls(),
    retry: 1,
  });

  // Whether the server can build the workbook at all. An older deployment
  // cannot, and a button that always fails is worse than no button.
  const config = useQuery({
    queryKey: queryKeys.config,
    queryFn: () => api.config(),
    staleTime: 5 * 60_000,
  });

  const downloadExcel = useMutation({
    mutationFn: () => api.downloadExcel(),
    onSuccess: (filename) =>
      toast.success("Excel report downloaded.", {
        description: `${filename} — every call recorded so far.`,
      }),
    onError: (err: Error) =>
      toast.error("The Excel report could not be downloaded.", {
        description: err.message,
      }),
  });

  const remove = useMutation({
    mutationFn: (id: string) => api.deleteCall(id),
    // Drop the row the moment it is asked for. Waiting for the refetch left the
    // deleted row on screen with a live delete button under it, and a second
    // click on the same row is what produced the 404 beside the success toast.
    onMutate: (id: string) => {
      void qc.cancelQueries({ queryKey: queryKeys.calls() });
      const previous = qc.getQueryData<{ calls: Call[] }>(queryKeys.calls());
      if (previous) {
        qc.setQueryData(queryKeys.calls(), {
          ...previous,
          calls: previous.calls.filter((c) => c.id !== id),
        });
      }
      return { previous };
    },
    // A real failure puts the row back, so the list never claims a call is gone
    // when the server still has it.
    onError: (err: Error, _id, context) => {
      if (context?.previous) qc.setQueryData(queryKeys.calls(), context.previous);
      toast.error("That call could not be deleted.", { description: err.message });
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: queryKeys.stats });
      toast.success("Call deleted.");
    },
    onSettled: () => void qc.invalidateQueries({ queryKey: queryKeys.calls() }),
  });

  // Memoised so the empty-list fallback is not a fresh array on every render,
  // which would re-run both filters below for nothing.
  const all = useMemo(() => calls.data?.calls ?? [], [calls.data]);
  const counts = useMemo(() => {
    const tally: Record<string, number> = { ALL: all.length };
    for (const c of all) {
      const h = handlerOf(c);
      tally[h] = (tally[h] ?? 0) + 1;
    }
    return tally;
  }, [all]);

  const filtered = useMemo(() => {
    const q = digitsOnly(query);
    return all.filter((c) => {
      if (tab !== "ALL" && handlerOf(c) !== tab) return false;
      return !q || digitsOnly(callNumber(c) ?? "").includes(q);
    });
  }, [all, query, tab]);

  const exportCsv = () => {
    const csv = toCsv(
      filtered.map((c) => ({
        number: callNumber(c) ?? "",
        direction: c.direction ?? "",
        started: callTime(c) ?? "",
        ended: callEndTime(c) ?? "",
        duration_seconds: callDuration(c),
        outcome: c.outcome ?? c.status ?? c.state ?? "",
        recording: c.recording_state ?? "",
      })),
    );
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = "calls.csv";
    a.click();
    URL.revokeObjectURL(url);
  };

  const filtersActive = query !== "" || tab !== "ALL";

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="mr-auto text-lg font-semibold text-foreground">{t("history")}</h1>
        <Button variant="secondary" size="sm" onClick={exportCsv} disabled={!filtered.length}>
          <Download className="size-4" aria-hidden="true" />
          {t("exportCsv")}
        </Button>
        {/*
          The master workbook. Unlike the CSV beside it, this ignores the search
          box and the tabs entirely — it is the complete history from the server,
          every call ever taken, with the student details and the outstanding
          follow-ups on their own sheets. Enabled even when the filtered list is
          empty, because what it downloads is not this list.
        */}
        {config.data?.excel_ready !== false && (
          <Button
            size="sm"
            onClick={() => downloadExcel.mutate()}
            disabled={downloadExcel.isPending}
          >
            {downloadExcel.isPending ? (
              <Loader2 className="size-4 animate-spin" aria-hidden="true" />
            ) : (
              <FileSpreadsheet className="size-4" aria-hidden="true" />
            )}
            {downloadExcel.isPending ? "Preparing…" : t("downloadExcel")}
          </Button>
        )}
      </div>

      <div className="flex flex-col gap-3 sm:flex-row">
        <div className="relative flex-1">
          <Search
            className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground"
            aria-hidden="true"
          />
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t("search")}
            aria-label={t("search")}
            className="pl-9"
          />
        </div>
      </div>

      {/* Who handled the call, not which way it went: the useful split here is
          Ayesha's calls versus the ones a counselor took themselves. */}
      <div className="flex flex-wrap gap-1 border-b border-border" role="tablist">
        {TABS.map((option) => (
          <button
            key={option.id}
            type="button"
            role="tab"
            aria-selected={tab === option.id}
            onClick={() => setTab(option.id)}
            className={cn(
              "-mb-px border-b-2 px-3 py-2 text-sm font-medium transition-colors",
              tab === option.id
                ? "border-primary text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            {option.label}
            <span className="ml-1.5 text-xs tabular-nums opacity-60">{counts[option.id] ?? 0}</span>
          </button>
        ))}
      </div>

      {calls.isLoading ? (
        <div className="space-y-2">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-16 rounded-xl" />
          ))}
        </div>
      ) : all.length === 0 ? (
        <div className="surface-panel">
          <EmptyState
            icon={Archive}
            title={t("noCallsYet")}
            body={t("noCallsBody")}
            actionLabel={t("placeCall")}
            onAction={() => void navigate({ to: "/call" })}
          />
        </div>
      ) : filtered.length === 0 ? (
        <div className="surface-panel">
          <EmptyState
            icon={Search}
            title={t("noResults")}
            body="Try a different number, or clear the filters to see every call."
            actionLabel={t("clearFilters")}
            onAction={() => {
              setQuery("");
              setTab("ALL");
            }}
          />
        </div>
      ) : (
        <>
          {/* Desktop table */}
          <div className="surface-panel hidden overflow-hidden md:block">
            <table className="w-full text-sm">
              <thead className="bg-secondary/60 text-left text-xs text-muted-foreground uppercase">
                <tr>
                  <th className="px-4 py-3 font-medium">{t("number")}</th>
                  <th className="px-4 py-3 font-medium">{t("direction")}</th>
                  <th className="px-4 py-3 font-medium">Started</th>
                  <th className="px-4 py-3 font-medium">Ended</th>
                  <th className="px-4 py-3 font-medium">{t("duration")}</th>
                  <th className="px-4 py-3 font-medium">{t("recording")}</th>
                  <th className="px-4 py-3">
                    <span className="sr-only">Delete</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((c) => (
                  <tr
                    key={c.id}
                    tabIndex={0}
                    onClick={() => setOpenCall(c.id)}
                    onKeyDown={(e) => e.key === "Enter" && setOpenCall(c.id)}
                    className="cursor-pointer border-t border-border transition-colors hover:bg-accent"
                  >
                    <td className="px-4 py-3 font-medium text-foreground">
                      {maskNumber(callNumber(c))}
                    </td>
                    <td className="px-4 py-3">
                      <span className="inline-flex items-center gap-1.5 text-muted-foreground capitalize">
                        {c.direction === "OUTBOUND" ? (
                          <ArrowUpRight className="size-4" aria-hidden="true" />
                        ) : (
                          <ArrowDownLeft className="size-4" aria-hidden="true" />
                        )}
                        {(c.direction ?? "").toLowerCase()}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-muted-foreground">
                      <span className="block tabular-nums text-foreground">
                        {stampTime(callTime(c))}
                      </span>
                      <span className="block text-xs">{relativeTime(callTime(c))}</span>
                    </td>
                    <td className="px-4 py-3 text-muted-foreground">
                      {isActiveCall(c) ? <LiveBadge /> : <EndedAt call={c} />}
                    </td>
                    <td className="px-4 py-3 tabular-nums text-muted-foreground">
                      <CallDuration call={c} />
                    </td>
                    <td className="w-64 px-4 py-3" onClick={(e) => e.stopPropagation()}>
                      <RecordingPlayer callId={c.id} state={c.recording_state} compact />
                    </td>
                    <td className="px-2 py-3" onClick={(e) => e.stopPropagation()}>
                      <DeleteCallButton
                        number={maskNumber(callNumber(c))}
                        live={isActiveCall(c)}
                        pending={remove.isPending && remove.variables === c.id}
                        onConfirm={() => remove.mutate(c.id)}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Mobile cards */}
          <ul className="space-y-3 md:hidden">
            {filtered.map((c) => (
              <li key={c.id} className="surface-panel p-4">
                <button
                  type="button"
                  onClick={() => setOpenCall(c.id)}
                  className="flex w-full items-center gap-3 text-left"
                >
                  <span
                    className={cn(
                      "grid size-10 shrink-0 place-items-center rounded-full",
                      c.direction === "OUTBOUND"
                        ? "bg-gold/15 text-gold-foreground"
                        : "bg-secondary text-primary",
                    )}
                  >
                    {c.direction === "OUTBOUND" ? (
                      <ArrowUpRight className="size-4.5" aria-hidden="true" />
                    ) : (
                      <ArrowDownLeft className="size-4.5" aria-hidden="true" />
                    )}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-medium text-foreground">
                      {maskNumber(callNumber(c))}
                    </span>
                    <span className="block text-xs text-muted-foreground">
                      {stampTime(callTime(c))} · <CallDuration call={c} />
                    </span>
                    <span className="block text-xs text-muted-foreground">
                      {isActiveCall(c) ? <LiveBadge /> : <>Ended {stampTime(callEndTime(c))}</>}
                    </span>
                  </span>
                </button>
                <div className="mt-3 flex items-center gap-2">
                  <div className="min-w-0 flex-1">
                    <RecordingPlayer callId={c.id} state={c.recording_state} />
                  </div>
                  <DeleteCallButton
                    number={maskNumber(callNumber(c))}
                    live={isActiveCall(c)}
                    pending={remove.isPending && remove.variables === c.id}
                    onConfirm={() => remove.mutate(c.id)}
                  />
                </div>
              </li>
            ))}
          </ul>
        </>
      )}

      {filtersActive && filtered.length > 0 ? (
        <Button
          variant="ghost"
          size="sm"
          onClick={() => {
            setQuery("");
            setTab("ALL");
          }}
        >
          {t("clearFilters")}
        </Button>
      ) : null}

      <CallDetailSheet callId={openCall} onOpenChange={(o) => !o && setOpenCall(null)} />
    </div>
  );
}

/**
 * Duration that keeps counting while the call is up.
 *
 * A finished call's duration is a fact and is printed as one. A live call's is
 * not: the row is only refreshed when an event arrives, so a quiet call would
 * show the same number for minutes and then jump. Counting locally from pickup
 * matches the clock the person on the call is watching.
 */
function CallDuration({ call }: { call: Call }) {
  const live = isActiveCall(call);
  const [seconds, setSeconds] = useState(() => (live ? liveDuration(call) : callDuration(call)));

  useEffect(() => {
    if (!live) {
      setSeconds(callDuration(call));
      return;
    }
    setSeconds(liveDuration(call));
    const timer = window.setInterval(() => setSeconds(liveDuration(call)), 1000);
    return () => window.clearInterval(timer);
  }, [call, live]);

  return (
    <span className={cn("tabular-nums", live && "text-foreground")}>{formatDuration(seconds)}</span>
  );
}

function LiveBadge() {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs font-medium text-foreground">
      <span className="relative flex size-2">
        <span className="absolute inline-flex size-full animate-ping rounded-full bg-primary opacity-75" />
        <span className="relative inline-flex size-2 rounded-full bg-primary" />
      </span>
      In progress
    </span>
  );
}

/**
 * When the call ended. Falls back to the outcome for calls that finished
 * without one — a row that just says "—" leaves the reader wondering whether
 * the call is still up.
 */
function EndedAt({ call }: { call: Call }) {
  const { t } = useI18n();
  const ended = callEndTime(call);
  if (ended) {
    return <span className="tabular-nums text-foreground">{stampTime(ended)}</span>;
  }
  // No end stamp. The carrier's reason is the better explanation when there is
  // one — "nobody answered" says more than "failed" — and the status is the
  // fallback for a call that finished without one.
  return (
    <span className="text-xs">
      {call.outcome ? describeFailure(call.outcome) : describeStatus(call.status, t)}
    </span>
  );
}

/**
 * Delete, with the confirmation built into the button.
 *
 * Deleting takes the recording with it and there is no undo, so a single click
 * must not be enough — but a modal for one row is heavier than the action
 * deserves. The button asks, and forgets the question if you look away.
 */
function DeleteCallButton({
  number,
  live = false,
  pending,
  onConfirm,
}: {
  number: string;
  /** Deleting this one drops the line as well, so the button says so. */
  live?: boolean;
  pending: boolean;
  onConfirm: () => void;
}) {
  const [armed, setArmed] = useState(false);

  if (armed) {
    return (
      <span className="flex items-center gap-1">
        <Button
          variant="destructive"
          size="sm"
          disabled={pending}
          onClick={() => {
            setArmed(false);
            onConfirm();
          }}
        >
          {pending ? "Deleting…" : live ? "Hang up & delete" : "Delete"}
        </Button>
        <Button variant="ghost" size="sm" onClick={() => setArmed(false)}>
          Cancel
        </Button>
      </span>
    );
  }

  return (
    <Button
      variant="ghost"
      size="sm"
      className="text-muted-foreground hover:text-destructive"
      onClick={() => setArmed(true)}
      onBlur={() => setArmed(false)}
      aria-label={
        live ? `Hang up and delete the call with ${number}` : `Delete the call with ${number}`
      }
    >
      <Trash2 className="size-4" aria-hidden="true" />
    </Button>
  );
}
