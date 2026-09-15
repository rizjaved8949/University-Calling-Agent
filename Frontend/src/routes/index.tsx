import { useState } from "react";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  ArrowDownLeft,
  ArrowUpRight,
  BookOpenCheck,
  Clock,
  Mic,
  PhoneCall,
  PhoneIncoming,
  PhoneOutgoing,
  Radio,
  Signal,
} from "lucide-react";
import campusImage from "@/assets/campus.jpg";
import { CallDetailSheet } from "@/components/CallDetailSheet";
import { EmptyState } from "@/components/EmptyState";
import { StatCard } from "@/components/StatCard";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { api, queryKeys } from "@/lib/api";
import {
  callDuration,
  callNumber,
  callTime,
  formatDuration,
  formatTalkTime,
  isActiveCall,
  maskNumber,
  relativeTime,
} from "@/lib/format";
import { useI18n } from "@/lib/i18n";
import { describeFailure, describeStatus } from "@/lib/normalize";
import { useRealtime } from "@/lib/realtime";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: "Dashboard · Admissions Voice Agent" },
      {
        name: "description",
        content:
          "Today's call volume, talk time and live activity for Ayesha, the university's AI admissions voice agent.",
      },
      { property: "og:title", content: "Dashboard · Admissions Voice Agent" },
      {
        property: "og:description",
        content: "Track calls, talk time and live admissions conversations in one calm console.",
      },
    ],
  }),
  component: Dashboard,
});

const EVENT_LABEL: Record<string, { label: string; icon: typeof Activity }> = {
  "call.created": { label: "Call started", icon: PhoneCall },
  "call.updated": { label: "Call updated", icon: Activity },
  transcript: { label: "New speech", icon: Mic },
  "rag.query": { label: "Knowledge checked", icon: BookOpenCheck },
  "agent.interrupted": { label: "Caller interrupted", icon: Signal },
  "recording.ready": { label: "Recording ready", icon: Radio },
  "agent.accepted": { label: "Agent answered", icon: PhoneIncoming },
  "agent.error": { label: "Agent problem", icon: Activity },
  "call.error": { label: "Call problem", icon: Activity },
  snapshot: { label: "Synced with server", icon: Activity },
  "agent.connected": { label: "Agent joined the call", icon: Radio },
  "agent.disconnected": { label: "Agent left the call", icon: Radio },
  "agent.wrapping_up": { label: "Wrapping up the call", icon: Activity },
  "calls.reaped": { label: "Cleared stale calls", icon: Activity },
};

function num(v: unknown, fallback = 0) {
  return typeof v === "number" ? v : fallback;
}

function HealthPill({ ok, label, value }: { ok: boolean; label: string; value: string }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-2 rounded-full border px-3 py-1.5 text-xs font-medium",
        ok
          ? "border-success/30 bg-success/10 text-success"
          : "border-warning/40 bg-warning/10 text-warning",
      )}
    >
      <span className={cn("size-1.5 rounded-full", ok ? "bg-success" : "bg-warning")} />
      {label}: {value}
    </span>
  );
}

function Dashboard() {
  const { t } = useI18n();
  const navigate = useNavigate();
  const { events, state } = useRealtime();
  const [openCall, setOpenCall] = useState<string | null>(null);

  const config = useQuery({ queryKey: queryKeys.config, queryFn: api.config, retry: 1 });
  const stats = useQuery({
    queryKey: queryKeys.stats,
    queryFn: api.stats,
    refetchInterval: 20_000,
    retry: 1,
  });
  const calls = useQuery({
    queryKey: queryKeys.calls(),
    queryFn: () => api.calls(),
    refetchInterval: 20_000,
    retry: 1,
  });

  const s = stats.data ?? {};
  const list = calls.data?.calls ?? [];
  const active = list.filter(isActiveCall);
  const activeCount = num(s["active_calls"], num(s["active"], active.length));
  const recordings = num(
    s["recordings"],
    list.filter((c) => c.recording_url || c.recording_ready).length,
  );

  const offline = state === "offline" && stats.isError;

  return (
    <div className="space-y-6">
      <section className="surface-panel relative overflow-hidden">
        <img
          src={campusImage}
          alt="Students walking through a university campus in Lahore at golden hour"
          width={1600}
          height={912}
          className="h-40 w-full object-cover object-center sm:h-48"
        />
        <div className="absolute inset-0 bg-gradient-to-r from-primary/90 via-primary/70 to-primary/20" />
        <div className="absolute inset-0 flex flex-col justify-center gap-2 px-5 sm:px-8">
          <p className="text-xs font-semibold tracking-wide text-primary-foreground/80 uppercase">
            {t("university")}
          </p>
          <h1 className="max-w-md text-2xl font-semibold text-balance-tight text-primary-foreground sm:text-3xl">
            {t("welcome")} — Ayesha is on duty
          </h1>
          <p className="hidden max-w-md text-sm text-primary-foreground/85 sm:block">
            {t("welcomeBody")}
          </p>
        </div>
      </section>

      <section aria-label={t("agentHealth")} className="flex flex-wrap items-center gap-2">
        <HealthPill
          ok={Boolean(config.data?.knowledge_base_ready)}
          label={t("knowledgeBase")}
          value={config.data?.knowledge_base_ready ? t("ready") : t("notReady")}
        />
        <HealthPill
          ok={Boolean(config.data?.telephony_ready)}
          label={t("phoneLine")}
          value={config.data?.telephony_ready ? t("connected") : t("notReady")}
        />
        <HealthPill
          ok={state === "live"}
          label="AI"
          value={state === "live" ? t("live") : t("offline")}
        />
      </section>

      {offline ? (
        <div className="surface-panel flex flex-col gap-3 p-5 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <p className="font-semibold text-foreground">{t("serverDown")}</p>
            <p className="text-sm text-muted-foreground">{t("serverDownBody")}</p>
          </div>
          <Button
            variant="secondary"
            onClick={() => {
              void stats.refetch();
              void calls.refetch();
              void config.refetch();
            }}
          >
            {t("retry")}
          </Button>
        </div>
      ) : null}

      {stats.isLoading ? (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-36 rounded-2xl" />
          ))}
        </div>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          <StatCard
            icon={PhoneCall}
            label={t("totalCalls")}
            value={String(num(s["total_calls"], list.length))}
            numeric={num(s["total_calls"], list.length)}
          />
          <StatCard
            icon={PhoneIncoming}
            label={t("inbound")}
            value={String(
              num(
                s["inbound_calls"],
                num(s["inbound"], list.filter((c) => c.direction === "INBOUND").length),
              ),
            )}
            numeric={num(
              s["inbound_calls"],
              num(s["inbound"], list.filter((c) => c.direction === "INBOUND").length),
            )}
          />
          <StatCard
            icon={PhoneOutgoing}
            label={t("outbound")}
            value={String(
              num(
                s["outbound_calls"],
                num(s["outbound"], list.filter((c) => c.direction === "OUTBOUND").length),
              ),
            )}
            numeric={num(
              s["outbound_calls"],
              num(s["outbound"], list.filter((c) => c.direction === "OUTBOUND").length),
            )}
          />
          <StatCard
            icon={Clock}
            label={t("talkTime")}
            value={formatTalkTime(
              num(
                s["total_duration"],
                num(
                  s["talk_time"],
                  list.reduce((a, c) => a + callDuration(c), 0),
                ),
              ),
            )}
            tone="gold"
          />
          <StatCard
            icon={Radio}
            label={t("activeNow")}
            value={String(activeCount)}
            numeric={activeCount}
            tone="live"
            highlight={activeCount > 0}
            {...(activeCount > 0 ? { onClick: () => void navigate({ to: "/call" }) } : {})}
          />
          <StatCard
            icon={Mic}
            label={t("recordings")}
            value={String(recordings)}
            numeric={recordings}
          />
        </div>
      )}

      <div className="grid gap-6 lg:grid-cols-2">
        <section className="surface-panel p-5">
          <h2 className="mb-4 text-sm font-semibold text-muted-foreground">{t("recentCalls")}</h2>
          {calls.isLoading ? (
            <div className="space-y-3">
              {Array.from({ length: 4 }).map((_, i) => (
                <Skeleton key={i} className="h-16 rounded-xl" />
              ))}
            </div>
          ) : list.length === 0 ? (
            <EmptyState
              icon={PhoneCall}
              title={t("noCallsYet")}
              body={t("noCallsBody")}
              actionLabel={t("placeCall")}
              onAction={() => void navigate({ to: "/call" })}
            />
          ) : (
            <ul className="space-y-2">
              {list.slice(0, 5).map((c) => {
                const outbound = c.direction === "OUTBOUND";
                return (
                  <li key={c.id}>
                    <button
                      type="button"
                      onClick={() => setOpenCall(c.id)}
                      className="flex w-full items-center gap-3 rounded-xl border border-transparent p-3 text-left transition-colors hover:border-border hover:bg-accent"
                    >
                      <span
                        className={cn(
                          "grid size-10 shrink-0 place-items-center rounded-full",
                          outbound
                            ? "bg-gold/15 text-gold-foreground"
                            : "bg-secondary text-primary",
                        )}
                      >
                        {outbound ? (
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
                          {relativeTime(callTime(c))} · {formatDuration(callDuration(c))}
                        </span>
                      </span>
                      <span className="shrink-0 rounded-full bg-secondary px-2.5 py-1 text-xs font-medium text-secondary-foreground capitalize">
                        {c.outcome
                          ? describeFailure(c.outcome)
                          : describeStatus(c.status ?? c.state, t)}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </section>

        <section className="surface-panel p-5">
          <h2 className="mb-4 text-sm font-semibold text-muted-foreground">{t("liveActivity")}</h2>
          {events.length === 0 ? (
            <EmptyState icon={Activity} title={t("waitingEvents")} body={t("waitingEventsBody")} />
          ) : (
            <ul className="max-h-96 space-y-3 overflow-y-auto pr-1" aria-live="polite">
              {events.map((e, i) => {
                const meta = EVENT_LABEL[e.type] ?? { label: e.type, icon: Activity };
                const Icon = meta.icon;
                return (
                  <li key={`${e.type}-${i}`} className="flex items-start gap-3">
                    <span className="mt-0.5 grid size-7 shrink-0 place-items-center rounded-lg bg-secondary text-primary">
                      <Icon className="size-3.5" aria-hidden="true" />
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block text-sm font-medium text-foreground">
                        {meta.label}
                      </span>
                      <span className="block text-xs text-muted-foreground">
                        {relativeTime(
                          typeof e["timestamp"] === "string" ? e["timestamp"] : undefined,
                        )}
                      </span>
                    </span>
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      </div>

      <CallDetailSheet callId={openCall} onOpenChange={(o) => !o && setOpenCall(null)} />
    </div>
  );
}
