import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowDown, BookOpenCheck, PhoneOff, Zap } from "lucide-react";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { VoiceOrb } from "@/components/VoiceOrb";
import { useI18n } from "@/lib/i18n";
import { describeStatus } from "@/lib/normalize";
import { useRealtime } from "@/lib/realtime";
import { formatDuration, hasUrdu, prettyNumber } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * Talk time, counted from pickup.
 *
 * `startedAt` is null until the call is answered: the ringing phase is not
 * conversation, and a timer running through it disagreed with both the
 * recording and the phone bill. Ringing shows a waiting state instead.
 */
function Timer({ startedAt }: { startedAt: number | null }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (startedAt === null) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [startedAt]);

  if (startedAt === null) {
    return (
      <span className="font-mono text-2xl tabular-nums text-muted-foreground" aria-live="off">
        --:--
      </span>
    );
  }
  return (
    <span className="font-mono text-2xl tabular-nums text-foreground" aria-live="off">
      {formatDuration(Math.max(now - startedAt, 0) / 1000)}
    </span>
  );
}

export function LiveCallPanel({
  phoneNumber,
  direction,
  status,
  startedAt,
  onEnd,
  ending,
}: {
  phoneNumber?: string;
  direction: string;
  status: string;
  /** Pickup time in epoch ms, or null while the call is still ringing. */
  startedAt: number | null;
  onEnd: () => void;
  ending: boolean;
}) {
  const { t } = useI18n();
  const { bubbles: allBubbles, speaking, activeCallId } = useRealtime();
  const scroller = useRef<HTMLDivElement>(null);
  const [stuck, setStuck] = useState(true);

  /**
   * This call's turns only. Bubbles carry the id of the call they came from, and
   * showing the unfiltered list meant a second call opened with the previous
   * conversation still on screen. Frames that arrive without an id — the socket
   * does not tag every event — belong to the call in progress, since only one
   * can be live at a time.
   */
  const bubbles = useMemo(
    () => allBubbles.filter((b) => !b.callId || !activeCallId || b.callId === activeCallId),
    [allBubbles, activeCallId],
  );

  const orbState = useMemo(() => {
    if (speaking === "agent") return "agent" as const;
    if (speaking === "caller") return "caller" as const;
    if (speaking === "thinking") return "thinking" as const;
    return "idle" as const;
  }, [speaking]);

  useEffect(() => {
    if (stuck && scroller.current) {
      scroller.current.scrollTop = scroller.current.scrollHeight;
    }
  }, [bubbles, stuck]);

  const onScroll = () => {
    const el = scroller.current;
    if (!el) return;
    setStuck(el.scrollHeight - el.scrollTop - el.clientHeight < 48);
  };

  return (
    <section className="grid gap-6 lg:grid-cols-[minmax(0,360px)_minmax(0,1fr)]">
      <div className="surface-panel flex flex-col items-center gap-5 p-6">
        <VoiceOrb state={orbState} size={200} />
        <div className="text-center">
          <p className="text-lg font-semibold text-foreground">{prettyNumber(phoneNumber)}</p>
          <p className="mt-1 text-sm text-muted-foreground capitalize">
            {direction.toLowerCase()} · {describeStatus(status, t)}
          </p>
        </div>
        <Timer startedAt={startedAt} />

        <AlertDialog>
          <AlertDialogTrigger asChild>
            <Button
              variant="destructive"
              size="lg"
              className="h-14 w-full gap-2 text-base font-semibold"
              disabled={ending}
            >
              <PhoneOff className="size-5" aria-hidden="true" />
              {t("endCall")}
            </Button>
          </AlertDialogTrigger>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>{t("confirmEnd")}</AlertDialogTitle>
              <AlertDialogDescription>{t("confirmEndNote")}</AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>{t("cancel")}</AlertDialogCancel>
              <AlertDialogAction onClick={onEnd}>{t("endCall")}</AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </div>

      <div className="surface-panel relative flex min-h-[420px] flex-col p-4 sm:p-5">
        <h2 className="mb-3 text-sm font-semibold text-muted-foreground">{t("transcript")}</h2>
        <div
          ref={scroller}
          onScroll={onScroll}
          aria-live="polite"
          aria-relevant="additions text"
          className="flex-1 space-y-3 overflow-y-auto pr-1"
        >
          {bubbles.length === 0 ? (
            <p className="py-10 text-center text-sm text-muted-foreground">
              The conversation will appear here as soon as anyone speaks.
            </p>
          ) : null}
          {bubbles.map((b) => {
            if (b.kind === "rag") {
              return (
                <div key={b.id} className="flex justify-center">
                  <span className="inline-flex items-center gap-2 rounded-full bg-gold/15 px-3 py-1 text-xs font-medium text-gold-foreground">
                    <BookOpenCheck className="size-3.5" aria-hidden="true" />
                    {t("knowledgeChecked")}
                    {b.text ? `: ${b.text}` : ""}
                  </span>
                </div>
              );
            }
            if (b.kind === "interrupt") {
              return (
                <div key={b.id} className="flex justify-center">
                  <span className="inline-flex items-center gap-2 rounded-full bg-secondary px-3 py-1 text-xs font-medium text-muted-foreground">
                    <Zap className="size-3.5" aria-hidden="true" />
                    {t("interrupted")}
                  </span>
                </div>
              );
            }
            const agent = b.role === "agent";
            return (
              <div key={b.id} className={cn("flex", agent ? "justify-start" : "justify-end")}>
                <div
                  className={cn(
                    "max-w-[85%] rounded-2xl px-4 py-2.5 text-sm break-words shadow-soft sm:max-w-[75%]",
                    agent
                      ? "rounded-tl-sm bg-primary text-primary-foreground"
                      : "rounded-tr-sm bg-secondary text-secondary-foreground",
                  )}
                >
                  <p
                    className={cn(hasUrdu(b.text) && "font-urdu text-right")}
                    dir={hasUrdu(b.text) ? "rtl" : "ltr"}
                  >
                    {b.text}
                  </p>
                  <p className="mt-1 text-[11px] opacity-70">
                    {new Date(b.at).toLocaleTimeString([], {
                      hour: "2-digit",
                      minute: "2-digit",
                    })}
                  </p>
                </div>
              </div>
            );
          })}
        </div>

        {!stuck ? (
          <Button
            size="sm"
            variant="secondary"
            className="absolute bottom-5 left-1/2 -translate-x-1/2 gap-1.5 shadow-lift"
            onClick={() => setStuck(true)}
          >
            <ArrowDown className="size-4" aria-hidden="true" />
            {t("jumpToLatest")}
          </Button>
        ) : null}
      </div>
    </section>
  );
}
