import { useQuery } from "@tanstack/react-query";
import { Copy } from "lucide-react";
import { toast } from "sonner";
import { RecordingPlayer } from "@/components/RecordingPlayer";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { api, queryKeys } from "@/lib/api";
import {
  callDuration,
  callEndTime,
  callNumber,
  callTime,
  entryRole,
  entryText,
  formatDuration,
  hasUrdu,
  isActiveCall,
  prettyNumber,
  stampTime,
} from "@/lib/format";
import { useI18n } from "@/lib/i18n";
import { cn } from "@/lib/utils";

export function CallDetailSheet({
  callId,
  onOpenChange,
}: {
  callId: string | null;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useI18n();
  const { data, isLoading, isError } = useQuery({
    queryKey: queryKeys.call(callId ?? ""),
    queryFn: () => api.call(callId as string),
    enabled: Boolean(callId),
  });

  const transcript = data?.transcript ?? [];

  const copyTranscript = async () => {
    const text = transcript
      .map((e) => `${entryRole(e) === "agent" ? "Ayesha" : "Caller"}: ${entryText(e)}`)
      .join("\n");
    await navigator.clipboard.writeText(text);
    toast.success(t("copied"));
  };

  return (
    <Sheet open={Boolean(callId)} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full gap-0 overflow-y-auto sm:max-w-lg">
        <SheetHeader>
          <SheetTitle>{prettyNumber(data ? callNumber(data) : undefined)}</SheetTitle>
          <SheetDescription>
            {data
              ? `${(data.direction ?? "").toLowerCase() || "call"} · ${formatDuration(callDuration(data))} · ${
                  callTime(data) ? new Date(callTime(data) as string).toLocaleString() : "—"
                }`
              : "Loading call details…"}
          </SheetDescription>
        </SheetHeader>

        <div className="space-y-5 px-4 pb-8">
          {isLoading ? (
            <div className="space-y-3">
              <Skeleton className="h-16 w-full" />
              <Skeleton className="h-24 w-full" />
              <Skeleton className="h-24 w-full" />
            </div>
          ) : null}

          {isError ? (
            <p className="rounded-xl bg-secondary p-4 text-sm text-muted-foreground">
              We couldn't load this call. Check your connection, then close and open it again.
            </p>
          ) : null}

          {data ? (
            <>
              {/* Exact times, not "12 min ago". When a student says they were
                  called at half past four, this is the panel that answers it. */}
              <dl className="surface-panel grid grid-cols-3 gap-2 p-4 text-sm">
                <div>
                  <dt className="text-xs text-muted-foreground">Started</dt>
                  <dd className="font-medium tabular-nums text-foreground">
                    {stampTime(callTime(data))}
                  </dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">Ended</dt>
                  <dd className="font-medium tabular-nums text-foreground">
                    {isActiveCall(data) ? "in progress" : stampTime(callEndTime(data))}
                  </dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">{t("duration")}</dt>
                  <dd className="font-medium tabular-nums text-foreground">
                    {formatDuration(callDuration(data))}
                  </dd>
                </div>
              </dl>

              <div className="surface-panel p-4">
                <p className="mb-2 text-sm font-semibold text-foreground">{t("recording")}</p>
                {callId ? <RecordingPlayer callId={callId} state={data.recording_state} /> : null}
              </div>

              <div>
                <div className="mb-3 flex items-center justify-between">
                  <p className="text-sm font-semibold text-foreground">{t("transcript")}</p>
                  {transcript.length ? (
                    <Button variant="ghost" size="sm" onClick={copyTranscript}>
                      <Copy className="size-4" aria-hidden="true" />
                      {t("copyTranscript")}
                    </Button>
                  ) : null}
                </div>
                {transcript.length === 0 ? (
                  <p className="rounded-xl bg-secondary p-4 text-sm text-muted-foreground">
                    {t("noTranscript")}
                  </p>
                ) : (
                  <ul className="space-y-2.5">
                    {transcript.map((e, i) => {
                      const agent = entryRole(e) === "agent";
                      const text = entryText(e);
                      return (
                        <li
                          key={e.id ?? i}
                          className={cn("flex", agent ? "justify-start" : "justify-end")}
                        >
                          <div
                            className={cn(
                              "max-w-[85%] rounded-2xl px-3.5 py-2 text-sm break-words",
                              agent
                                ? "rounded-tl-sm bg-primary text-primary-foreground"
                                : "rounded-tr-sm bg-secondary text-secondary-foreground",
                            )}
                          >
                            <p
                              className={cn(hasUrdu(text) && "font-urdu text-right")}
                              dir={hasUrdu(text) ? "rtl" : "ltr"}
                            >
                              {text}
                            </p>
                          </div>
                        </li>
                      );
                    })}
                  </ul>
                )}
              </div>
            </>
          ) : null}
        </div>
      </SheetContent>
    </Sheet>
  );
}
