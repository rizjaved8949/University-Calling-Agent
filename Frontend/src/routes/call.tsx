import { useEffect, useMemo, useState } from "react";
import { createFileRoute } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, ClipboardPaste, Delete, Phone, RotateCcw, Trash2 } from "lucide-react";
import { toast } from "sonner";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { LiveCallPanel } from "@/components/LiveCallPanel";
import { RecordingPlayer } from "@/components/RecordingPlayer";
import { ApiError, api, queryKeys } from "@/lib/api";
import type { Call } from "@/lib/types";
import {
  digitsOnly,
  formatDuration,
  formatE164,
  formatNational,
  isActiveCall,
  prettyNumber,
  validatePkNumber,
} from "@/lib/format";
import { useI18n } from "@/lib/i18n";
import { useRealtime } from "@/lib/realtime";

export const Route = createFileRoute("/call")({
  head: () => ({
    meta: [
      { title: "Place a call · Admissions Voice Agent" },
      {
        name: "description",
        content:
          "Dial a prospective student and let Ayesha handle the admissions conversation in Urdu or English.",
      },
      { property: "og:title", content: "Place a call · Admissions Voice Agent" },
      {
        property: "og:description",
        content: "A thumb-friendly dialer for the university admissions office.",
      },
    ],
  }),
  component: CallPage,
});

const KEYS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "*", "0", "#"];
const RECENTS_KEY = "recent-numbers";

function CallPage() {
  const { t } = useI18n();
  const qc = useQueryClient();
  const { activeCallId, setActiveCallId, clearBubbles } = useRealtime();

  const [national, setNational] = useState("");
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [recents, setRecents] = useState<string[]>([]);
  const [finished, setFinished] = useState<{ id: string; seconds: number; number: string } | null>(
    null,
  );
  /**
   * The row returned by POST /outbound, held until the calls list catches up.
   * Without it the panel does not render on the first frame after dialling —
   * `liveCall` is derived from the list query, which has not refetched yet — so
   * pressing Call appeared to do nothing until the next poll.
   */
  const [placedCall, setPlacedCall] = useState<Call | null>(null);

  useEffect(() => {
    const raw = window.localStorage.getItem(RECENTS_KEY);
    if (raw) {
      try {
        setRecents(JSON.parse(raw) as string[]);
      } catch {
        /* ignore */
      }
    }
  }, []);

  const calls = useQuery({
    queryKey: queryKeys.calls(),
    queryFn: () => api.calls(),
    refetchInterval: activeCallId ? 5_000 : 30_000,
    retry: 1,
  });

  const liveCall = useMemo(() => {
    const list = calls.data?.calls ?? [];
    return (
      list.find((c) => c.id === activeCallId) ??
      (placedCall && placedCall.id === activeCallId ? placedCall : null) ??
      list.find(isActiveCall) ??
      null
    );
  }, [calls.data, activeCallId, placedCall]);

  useEffect(() => {
    if (liveCall && !activeCallId) setActiveCallId(liveCall.id);
  }, [liveCall, activeCallId, setActiveCallId]);

  /**
   * When the timer starts ticking: pickup, as reported by the backend, not the
   * moment this browser pressed Call. Null while the phone is still ringing.
   */
  const answeredAt = useMemo(() => {
    const iso = liveCall?.answered_at;
    if (!iso) return null;
    const ms = new Date(iso).getTime();
    return Number.isNaN(ms) ? null : ms;
  }, [liveCall]);

  // The server says the call is over: show the outcome card.
  useEffect(() => {
    if (!activeCallId) return;
    const list = calls.data?.calls ?? [];
    const current = list.find((c) => c.id === activeCallId);
    if (current && !isActiveCall(current)) {
      setFinished({
        id: current.id,
        // The backend's duration is talk time from pickup, which is what the
        // recording contains — trust it over anything measured in the browser.
        seconds: current.duration_seconds ?? 0,
        number: current.phone_number || current.to_number || "",
      });
      setPlacedCall(null);
      setActiveCallId(null);
    }
  }, [calls.data, activeCallId, setActiveCallId]);

  const error = validatePkNumber(national);
  const e164 = formatE164("+92", national);

  const place = useMutation({
    mutationFn: () => api.placeCall(e164),
    onSuccess: (call) => {
      clearBubbles();
      setFinished(null);
      setPlacedCall(call);
      setActiveCallId(call.id);
      const next = [e164, ...recents.filter((r) => r !== e164)].slice(0, 6);
      setRecents(next);
      window.localStorage.setItem(RECENTS_KEY, JSON.stringify(next));
      void qc.invalidateQueries({ queryKey: queryKeys.calls() });
      toast.success("Calling now — Ayesha will greet the student.");
    },
    onError: (err) => {
      const e = err as ApiError;
      toast.error(e.friendly ?? "Couldn't place the call.", {
        description: e.hint ?? "Check your connection, then try again.",
      });
    },
  });

  const end = useMutation({
    mutationFn: () => api.endCall(activeCallId as string),
    onSuccess: (closed) => {
      // Close it here rather than waiting for the status to come back round.
      // The backend has already hung up both legs by the time this resolves,
      // and the old flow left the live panel — orb, timer, End button — on
      // screen until the next poll, so the call looked like it was still running.
      const id = activeCallId;
      const current = id ? (calls.data?.calls ?? []).find((c) => c.id === id) : undefined;
      if (id) {
        setFinished({
          id,
          // The settled row from the response first: the cached list still holds
          // the duration as it was while the call was running.
          seconds: closed?.duration_seconds ?? current?.duration_seconds ?? 0,
          number:
            closed?.phone_number ||
            current?.phone_number ||
            current?.to_number ||
            liveCall?.phone_number ||
            "",
        });
      }
      setPlacedCall(null);
      setActiveCallId(null);
      toast.success("Call ended.");
      void qc.invalidateQueries({ queryKey: queryKeys.calls() });
    },
    onError: (err) => {
      const e = err as ApiError;
      toast.error(e.friendly ?? "Couldn't end the call.", {
        description: e.hint ?? "Try again, or wait for the student to hang up.",
      });
    },
  });

  if (activeCallId && liveCall) {
    return (
      <LiveCallPanel
        {...(liveCall.phone_number || liveCall.to_number
          ? { phoneNumber: liveCall.phone_number || liveCall.to_number }
          : {})}
        direction={liveCall.direction ?? "OUTBOUND"}
        status={liveCall.status ?? liveCall.state ?? "connected"}
        startedAt={answeredAt}
        ending={end.isPending}
        onEnd={() => end.mutate()}
      />
    );
  }

  return (
    <div className="mx-auto max-w-md space-y-6">
      {finished ? (
        <section className="surface-panel space-y-3 p-5">
          <div className="flex items-center gap-2 text-success">
            <CheckCircle2 className="size-5" aria-hidden="true" />
            <p className="font-semibold">Call finished</p>
          </div>
          <p className="text-sm text-muted-foreground">
            {prettyNumber(finished.number)} · {formatDuration(finished.seconds)}
          </p>
          {/* Always mounted. It used to render only after a recording.ready
              frame arrived, so a recording that finished while this tab was on
              another page — or before the socket reconnected — was unreachable,
              with no player and no download link anywhere on this screen. */}
          <RecordingPlayer callId={finished.id} />
          <Button
            variant="secondary"
            className="gap-2"
            onClick={() => {
              setNational(digitsOnly(finished.number).replace(/^92/, ""));
              setFinished(null);
            }}
          >
            <RotateCcw className="size-4" aria-hidden="true" />
            {t("callAgain")}
          </Button>
        </section>
      ) : null}

      <section className="surface-panel p-5 sm:p-6">
        <h1 className="text-lg font-semibold text-foreground">{t("callStudent")}</h1>
        <p className="mt-1 text-sm text-muted-foreground">{t("realCallNote")}</p>

        <div className="mt-5">
          <label htmlFor="phone" className="text-sm font-medium text-foreground">
            {t("phoneNumber")}
          </label>
          <div className="mt-2 flex items-center gap-2 rounded-xl border border-input bg-card px-3 py-2 focus-within:ring-2 focus-within:ring-ring">
            <span className="shrink-0 text-base font-medium text-muted-foreground">🇵🇰 +92</span>
            <input
              id="phone"
              inputMode="numeric"
              autoComplete="tel-national"
              value={formatNational(national)}
              onChange={(e) => setNational(digitsOnly(e.target.value).slice(0, 10))}
              placeholder="300 1234567"
              aria-describedby="phone-help"
              className="min-w-0 flex-1 bg-transparent py-1.5 font-mono text-xl tracking-wide outline-none"
            />
          </div>
          <p
            id="phone-help"
            className={`mt-2 text-sm ${error && national ? "text-destructive" : "text-muted-foreground"}`}
          >
            {error ?? "Looks good — you can place the call."}
          </p>
        </div>

        <div className="mt-5 grid grid-cols-3 gap-2.5">
          {KEYS.map((k) => (
            <button
              key={k}
              type="button"
              onClick={() => setNational((v) => digitsOnly(v + k).slice(0, 10))}
              className="h-14 rounded-xl border border-border bg-secondary text-xl font-medium text-secondary-foreground transition-transform duration-150 hover:bg-accent active:scale-95"
            >
              {k}
            </button>
          ))}
        </div>

        <div className="mt-3 flex gap-2">
          <Button
            variant="ghost"
            size="sm"
            className="flex-1"
            onClick={async () => {
              try {
                const text = await navigator.clipboard.readText();
                setNational(digitsOnly(text).replace(/^92/, "").slice(0, 10));
              } catch {
                toast.error("We couldn't read your clipboard.", {
                  description: "Type the number instead, or allow clipboard access.",
                });
              }
            }}
          >
            <ClipboardPaste className="size-4" aria-hidden="true" />
            {t("paste")}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            className="flex-1"
            onClick={() => setNational((v) => v.slice(0, -1))}
            aria-label="Backspace"
          >
            <Delete className="size-4" aria-hidden="true" />
          </Button>
          <Button variant="ghost" size="sm" className="flex-1" onClick={() => setNational("")}>
            <Trash2 className="size-4" aria-hidden="true" />
            {t("clear")}
          </Button>
        </div>

        <Button
          size="lg"
          className="mt-5 h-14 w-full gap-2 text-base font-semibold"
          disabled={Boolean(error) || place.isPending}
          onClick={() => setConfirmOpen(true)}
        >
          <Phone className="size-5" aria-hidden="true" />
          {t("placeCall")}
        </Button>

        {recents.length ? (
          <div className="mt-6">
            <p className="mb-2 text-sm font-medium text-muted-foreground">{t("recentNumbers")}</p>
            <div className="flex flex-wrap gap-2">
              {recents.map((r) => (
                <button
                  key={r}
                  type="button"
                  onClick={() => setNational(digitsOnly(r).replace(/^92/, "").slice(0, 10))}
                  className="rounded-full border border-border bg-secondary px-3 py-1.5 font-mono text-xs text-secondary-foreground transition-colors hover:bg-accent"
                >
                  {prettyNumber(r)}
                </button>
              ))}
            </div>
          </div>
        ) : null}
      </section>

      <AlertDialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("confirmCall")}</AlertDialogTitle>
            <AlertDialogDescription>
              {prettyNumber(e164)} — {t("realCallNote")}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t("cancel")}</AlertDialogCancel>
            <AlertDialogAction onClick={() => place.mutate()}>{t("placeCall")}</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
