import { useEffect, useMemo, useState } from "react";
import { createFileRoute } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Headphones, Info, Mic, MicOff, Phone, PhoneOff, Smartphone } from "lucide-react";
import { toast } from "sonner";
import { RecordingPlayer } from "@/components/RecordingPlayer";
import { Button } from "@/components/ui/button";
import { ApiError, api, queryKeys } from "@/lib/api";
import {
  digitsOnly,
  formatDuration,
  formatE164,
  formatNational,
  prettyNumber,
  validatePkNumber,
} from "@/lib/format";
import { isLiveStatus } from "@/lib/normalize";
import { useSoftphone } from "@/lib/softphone";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/manual")({
  head: () => ({
    meta: [
      { title: "Talk yourself · Admissions Voice Agent" },
      {
        name: "description",
        content: "Call a student and speak to them yourself. Ayesha stays off the line entirely.",
      },
    ],
  }),
  component: ManualPage,
});

type Mode = "phone" | "browser";

const OPERATOR_KEY = "operator-number";

/** Live elapsed time since a start instant, or --:-- before there is one. */
function Elapsed({ startedAt }: { startedAt: number | null }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (startedAt === null) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [startedAt]);
  if (startedAt === null) return <span className="font-mono text-2xl">--:--</span>;
  return (
    <span className="font-mono text-2xl tabular-nums">
      {formatDuration(Math.max(now - startedAt, 0) / 1000)}
    </span>
  );
}

function NumberField({
  id,
  label,
  value,
  onChange,
  hint,
  error,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  hint: string;
  error?: string | null;
}) {
  return (
    <div>
      <label htmlFor={id} className="text-sm font-medium text-foreground">
        {label}
      </label>
      <div className="mt-2 flex items-center gap-2 rounded-xl border border-input bg-card px-3 py-2 focus-within:ring-2 focus-within:ring-ring">
        <span className="shrink-0 text-base font-medium text-muted-foreground">🇵🇰 +92</span>
        <input
          id={id}
          inputMode="numeric"
          autoComplete="tel-national"
          value={formatNational(value)}
          onChange={(e) => onChange(digitsOnly(e.target.value).slice(0, 10))}
          placeholder="300 1234567"
          className="min-w-0 flex-1 bg-transparent py-1.5 font-mono text-lg tracking-wide outline-none"
        />
      </div>
      <p
        className={cn(
          "mt-1.5 text-xs",
          error && value ? "text-destructive" : "text-muted-foreground",
        )}
      >
        {(value && error) || hint}
      </p>
    </div>
  );
}

function ManualPage() {
  const qc = useQueryClient();
  const [mode, setMode] = useState<Mode>("phone");
  const [student, setStudent] = useState("");
  const [operator, setOperator] = useState("");
  const [bridgeCallId, setBridgeCallId] = useState<string | null>(null);
  const [finishedId, setFinishedId] = useState<string | null>(null);

  const config = useQuery({ queryKey: queryKeys.config, queryFn: () => api.config(), retry: 1 });
  const softphone = useSoftphone();

  const browserReady = config.data?.manual_browser_ready ?? false;

  // Remember the counselor's own number: it is the same one every time, and
  // retyping it before every call is the fastest way to make a feature unused.
  useEffect(() => {
    const saved = window.localStorage.getItem(OPERATOR_KEY);
    if (saved) setOperator(digitsOnly(saved).replace(/^92/, "").slice(0, 10));
    else if (config.data?.operator_phone_number) {
      setOperator(digitsOnly(config.data.operator_phone_number).replace(/^92/, "").slice(0, 10));
    }
  }, [config.data]);

  const calls = useQuery({
    queryKey: queryKeys.calls(),
    queryFn: () => api.calls(),
    refetchInterval: bridgeCallId ? 5_000 : false,
    retry: 1,
  });

  const bridgeCall = useMemo(
    () => (calls.data?.calls ?? []).find((c) => c.id === bridgeCallId) ?? null,
    [calls.data, bridgeCallId],
  );

  // The bridged call finished: swap the panel for its recording.
  useEffect(() => {
    if (!bridgeCallId || !bridgeCall) return;
    if (!isLiveStatus(bridgeCall.status)) {
      setFinishedId(bridgeCallId);
      setBridgeCallId(null);
    }
  }, [bridgeCall, bridgeCallId]);

  const studentError = validatePkNumber(student);
  const operatorError = validatePkNumber(operator);
  const studentE164 = formatE164("+92", student);
  const operatorE164 = formatE164("+92", operator);

  const place = useMutation({
    mutationFn: () => api.placeManualCall(studentE164, operatorE164),
    onSuccess: (call) => {
      window.localStorage.setItem(OPERATOR_KEY, operatorE164);
      setFinishedId(null);
      setBridgeCallId(call.id);
      void qc.invalidateQueries({ queryKey: queryKeys.calls() });
      toast.success("Your phone is ringing — pick up and we'll connect the student.");
    },
    onError: (err) => {
      const e = err as ApiError;
      toast.error(e.friendly ?? "Couldn't start the call.", { description: e.hint ?? "" });
    },
  });

  const endBridge = useMutation({
    mutationFn: () => api.endCall(bridgeCallId as string),
    onSuccess: () => {
      if (bridgeCallId) setFinishedId(bridgeCallId);
      setBridgeCallId(null);
      void qc.invalidateQueries({ queryKey: queryKeys.calls() });
      toast.success("Call ended.");
    },
    onError: (err) => {
      const e = err as ApiError;
      toast.error(e.friendly ?? "Couldn't end the call.", { description: e.hint ?? "" });
    },
  });

  // ---- Browser softphone, mid-call ----------------------------------------
  if (mode === "browser" && softphone.state !== "idle" && softphone.state !== "error") {
    const label =
      softphone.state === "connecting"
        ? "Connecting…"
        : softphone.state === "ringing"
          ? "Ringing…"
          : "Connected";
    return (
      <section className="mx-auto max-w-md">
        <div className="surface-panel flex flex-col items-center gap-5 p-6">
          <span className="grid size-24 place-items-center rounded-full bg-primary/10 text-primary">
            <Headphones className="size-10" aria-hidden="true" />
          </span>
          <div className="text-center">
            <p className="text-lg font-semibold text-foreground">{prettyNumber(studentE164)}</p>
            <p className="mt-1 text-sm text-muted-foreground">{label} · you are speaking</p>
          </div>
          <Elapsed startedAt={softphone.startedAt} />
          <div className="flex w-full gap-2">
            <Button
              variant="secondary"
              size="lg"
              className="h-14 flex-1 gap-2"
              onClick={() => void softphone.toggleMute()}
              disabled={softphone.state !== "on-call"}
            >
              {softphone.muted ? (
                <MicOff className="size-5" aria-hidden="true" />
              ) : (
                <Mic className="size-5" aria-hidden="true" />
              )}
              {softphone.muted ? "Unmute" : "Mute"}
            </Button>
            <Button
              variant="destructive"
              size="lg"
              className="h-14 flex-1 gap-2 font-semibold"
              onClick={softphone.hangup}
            >
              <PhoneOff className="size-5" aria-hidden="true" />
              Hang up
            </Button>
          </div>
          {/* Last resort against a room that feeds back. Strict half-duplex
              cannot echo, but you cannot interrupt while it is on, so it is a
              choice the counselor makes rather than something we impose. */}
          <button
            type="button"
            onClick={softphone.toggleEchoGuard}
            className="text-sm text-muted-foreground underline-offset-4 hover:underline"
          >
            {softphone.echoGuard
              ? "Echo guard on — you can't interrupt. Turn off"
              : "Hearing yourself echo? Turn on echo guard"}
          </button>
        </div>
      </section>
    );
  }

  // ---- Phone bridge, mid-call ---------------------------------------------
  if (bridgeCallId) {
    const answered = Boolean(bridgeCall?.answered_at);
    return (
      <section className="mx-auto max-w-md">
        <div className="surface-panel flex flex-col items-center gap-5 p-6">
          <span className="grid size-24 place-items-center rounded-full bg-primary/10 text-primary">
            <Smartphone className="size-10" aria-hidden="true" />
          </span>
          <div className="text-center">
            <p className="text-lg font-semibold text-foreground">{prettyNumber(studentE164)}</p>
            <p className="mt-1 text-sm text-muted-foreground">
              {answered
                ? "Connected — you are speaking"
                : `Ringing ${prettyNumber(operatorE164)}. Answer to be connected.`}
            </p>
          </div>
          <Elapsed
            startedAt={bridgeCall?.answered_at ? Date.parse(bridgeCall.answered_at) : null}
          />
          <Button
            variant="destructive"
            size="lg"
            className="h-14 w-full gap-2 font-semibold"
            disabled={endBridge.isPending}
            onClick={() => endBridge.mutate()}
          >
            <PhoneOff className="size-5" aria-hidden="true" />
            End call
          </Button>
        </div>
      </section>
    );
  }

  // ---- Dialer --------------------------------------------------------------
  const canDial =
    mode === "phone"
      ? !studentError && !operatorError && !place.isPending
      : !studentError && browserReady;

  return (
    <div className="mx-auto max-w-md space-y-6">
      {finishedId ? (
        <section className="surface-panel space-y-3 p-5">
          <p className="font-semibold text-foreground">Call finished</p>
          <RecordingPlayer callId={finishedId} />
        </section>
      ) : null}

      <section className="surface-panel p-5 sm:p-6">
        <h1 className="text-lg font-semibold text-foreground">Talk to a student yourself</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Ayesha stays off the line completely. Because no AI is listening, there is no live
          transcript for these calls — the recording is saved as usual.
        </p>

        <div className="mt-5 grid grid-cols-2 gap-2" role="radiogroup" aria-label="How you speak">
          {(
            [
              { id: "phone", label: "Ring my phone", icon: Smartphone },
              { id: "browser", label: "Use my mic", icon: Headphones },
            ] as const
          ).map((option) => (
            <button
              key={option.id}
              type="button"
              role="radio"
              aria-checked={mode === option.id}
              onClick={() => {
                setMode(option.id);
                // The click is the user gesture the browser needs, so ask now
                // rather than making them discover the block at dial time.
                if (option.id === "browser" && softphone.micPermission !== "granted") {
                  void softphone.requestMic();
                }
              }}
              className={cn(
                "flex flex-col items-center gap-1.5 rounded-xl border px-3 py-4 text-sm font-medium transition-colors",
                mode === option.id
                  ? "border-primary bg-primary/10 text-primary"
                  : "border-border bg-secondary text-secondary-foreground hover:bg-accent",
              )}
            >
              <option.icon className="size-5" aria-hidden="true" />
              {option.label}
            </button>
          ))}
        </div>

        <p className="mt-3 flex gap-2 rounded-lg bg-secondary p-3 text-xs text-muted-foreground">
          <Info className="mt-px size-3.5 shrink-0" aria-hidden="true" />
          {mode === "phone"
            ? "We call your phone first. Once you answer, the student is dialled and connected to you."
            : softphone.micPermission === "granted"
              ? "Your laptop microphone is ready."
              : "Your laptop microphone is used. Your browser will ask for permission."}
        </p>

        <div className="mt-5 space-y-4">
          <NumberField
            id="student"
            label="Student's number"
            value={student}
            onChange={setStudent}
            hint="The person you want to speak to."
            error={studentError}
          />
          {mode === "phone" ? (
            <NumberField
              id="operator"
              label="Your number"
              value={operator}
              onChange={setOperator}
              hint="We ring this first. Saved for next time."
              error={operatorError}
            />
          ) : null}
        </div>

        {mode === "browser" && !config.isLoading && !browserReady ? (
          <p className="mt-4 rounded-lg bg-warning/10 p-3 text-xs text-warning">
            Telephony isn't configured on the backend, so no calls can be placed at all.
          </p>
        ) : null}

        {mode === "browser" && softphone.error ? (
          <div className="mt-4 rounded-lg bg-destructive/10 p-3 text-xs text-destructive">
            <p>{softphone.error}</p>
            {/* Retrying only helps where the browser is still willing to ask.
                Once it is "denied" the prompt never comes back from script. */}
            {softphone.micPermission === "prompt" || softphone.micPermission === "unknown" ? (
              <button
                type="button"
                className="mt-2 font-semibold underline underline-offset-2"
                onClick={() => void softphone.requestMic()}
              >
                Ask for microphone access again
              </button>
            ) : null}
          </div>
        ) : null}

        <Button
          size="lg"
          className="mt-5 h-14 w-full gap-2 text-base font-semibold"
          disabled={!canDial}
          onClick={() => {
            if (mode === "phone") place.mutate();
            else void softphone.dial(studentE164);
          }}
        >
          <Phone className="size-5" aria-hidden="true" />
          {mode === "phone" ? "Ring me, then the student" : "Call from this browser"}
        </Button>
      </section>
    </div>
  );
}
