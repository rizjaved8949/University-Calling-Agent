import { useCallback, useEffect, useRef, useState } from "react";
import {
  AudioOptions,
  CallsApiEvent,
  InfobipRTCEvent,
  PhoneCallOptions,
  PhoneCallRecordingOptions,
  createInfobipRtc,
} from "infobip-rtc";
import type { InfobipRTC, PhoneCall } from "infobip-rtc";
import { ApiError, api } from "@/lib/api";
import { startCallRecorder, type CallRecorder } from "@/lib/call-recorder";
import { createLevelProbe, createMicFilter } from "@/lib/mic-filter";

export type SoftphoneState = "idle" | "connecting" | "ringing" | "on-call" | "error";

/** What the browser currently thinks about our claim on the microphone. */
export type MicPermission =
  "unknown" | "prompt" | "granted" | "denied" | "missing" | "insecure" | "unsupported";

/**
 * Turn a getUserMedia rejection into something the operator can act on.
 *
 * `dismissed` separates "you closed the prompt" from "the browser is refusing
 * to show it": both arrive as NotAllowedError, but only the second one is a
 * dead end that site settings must undo, and telling someone to dig through
 * settings when they merely clicked away is how you lose them.
 */
function micErrorMessage(
  err: unknown,
  dismissed = false,
): { message: string; permission: MicPermission } {
  if (err instanceof DOMException) {
    if (err.name === "SecurityError") {
      return {
        message: `Microphones only work on a secure page. Open this app over https, or on localhost instead of ${window.location.hostname}.`,
        permission: "insecure",
      };
    }
    if (err.name === "NotSupportedError") {
      return {
        message: "This browser can't reach a microphone. Try Chrome, Edge, or Safari.",
        permission: "unsupported",
      };
    }
    if (err.name === "NotFoundError" || err.name === "OverconstrainedError") {
      return { message: "No microphone was found on this device.", permission: "missing" };
    }
    if (err.name === "NotReadableError") {
      return {
        message: "Your microphone is busy in another app. Close it, then try again.",
        permission: "prompt",
      };
    }
    if (err.name === "NotAllowedError") {
      return dismissed
        ? {
            message: "The permission prompt was dismissed. Try again and choose Allow.",
            permission: "prompt",
          }
        : {
            message:
              "Microphone access is blocked for this site. Open the padlock in the address bar, set Microphone to Allow, then reload.",
            permission: "denied",
          };
    }
  }
  return { message: "We couldn't reach your microphone.", permission: "unknown" };
}

/** The standing answer, without prompting. Undefined when the browser won't say. */
async function readMicPermission(): Promise<MicPermission | undefined> {
  try {
    const status = await navigator.permissions?.query({ name: "microphone" as PermissionName });
    return status?.state as MicPermission | undefined;
  } catch {
    return undefined;
  }
}

/**
 * Ask the browser for the mic and immediately hand it back.
 *
 * Only getUserMedia raises the permission prompt, and Chrome only raises it
 * from a user gesture on a secure origin — so this is called when the operator
 * picks "Use my mic", not on page load where it would be silently ignored. The
 * tracks are stopped right away: holding them would leave the recording dot lit
 * between calls, and the SDK opens its own stream when it dials.
 */
export async function requestMicrophone(): Promise<void> {
  // Chrome drops navigator.mediaDevices entirely on an insecure origin, so a
  // plain http://192.168.x.x dev URL looks identical to an unsupported browser
  // unless we check the context first.
  if (!window.isSecureContext) {
    throw new DOMException("Microphones require a secure context", "SecurityError");
  }
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new DOMException("getUserMedia unavailable", "NotSupportedError");
  }
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  for (const track of stream.getTracks()) track.stop();
}

/** How loud the student stays in your ear while you are talking over them. */
const PLAYBACK_DUCK = 0.15;
/**
 * How long after you stop talking the earpiece stays down.
 *
 * This is the window your own voice comes back in: out to Infobip, down the
 * phone line, out of their speaker, into their microphone, and all the way
 * back. Restore any sooner and the tail lands at full volume, which is exactly
 * the repeat you hear.
 */
const ECHO_TAIL_MS = 700;

/**
 * Wait for the SDK's websocket to come up.
 *
 * `connect()` returns immediately but the socket is not up yet, and every
 * outgoing call is gated on CONNECTED inside the SDK — dialling on the next
 * line throws, which is what surfaced as a bare "call could not be started".
 */
function awaitConnection(rtc: InfobipRTC, timeoutMs = 10_000): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => {
      reject(
        new ApiError("The calling service did not answer.", "Check your connection and try again."),
      );
    }, timeoutMs);

    rtc.on(InfobipRTCEvent.CONNECTED, () => {
      window.clearTimeout(timer);
      resolve();
    });
    rtc.on(InfobipRTCEvent.DISCONNECTED, (event) => {
      window.clearTimeout(timer);
      reject(
        new ApiError(
          "The calling service refused the connection.",
          event?.reason ? `Infobip said: ${event.reason}` : "Try again in a moment.",
        ),
      );
    });
    rtc.connect();
  });
}

/**
 * The browser as a phone: your microphone straight out to a PSTN number.
 *
 * Nothing here touches the agent. Infobip bridges your mic to the student and
 * that is the whole call, which is why this mode has no transcript — there is
 * no Realtime session listening to produce one.
 *
 * The SDK is imported lazily by the caller mounting this hook, and the access
 * token is fetched per call: it is short-lived by design, and a stale one fails
 * at exactly the wrong moment.
 */
export function useSoftphone() {
  const [state, setState] = useState<SoftphoneState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [muted, setMuted] = useState(false);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [micPermission, setMicPermission] = useState<MicPermission>("unknown");

  const rtcRef = useRef<InfobipRTC | null>(null);
  const callRef = useRef<PhoneCall | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  // How loud the student currently is. The mic filter reads this to duck while
  // they talk, so a loudspeaker at their end cannot start a feedback loop.
  const farEndRef = useRef<{ level: () => number; stop: () => void } | null>(null);
  // Recording this call ourselves. See call-recorder: the provider refuses to
  // record browser-dialled calls, so the audio only exists if we capture it.
  const recorderRef = useRef<CallRecorder | null>(null);
  // Strict half-duplex. The filter reads it live through a ref, so flipping it
  // mid-call takes effect on the next 50ms tick rather than the next call.
  const [echoGuard, setEchoGuard] = useState(false);
  const echoGuardRef = useRef(false);
  echoGuardRef.current = echoGuard;
  const restoreRef = useRef<number | null>(null);

  /**
   * Turn the earpiece down while you talk, and for a moment after.
   *
   * The mic-side duck stops an echo building; this is what stops you hearing
   * the lap that is already on its way back. Under echo guard it goes to full
   * silence, which is what makes a loop arithmetically impossible rather than
   * merely quiet.
   */
  const suppressPlayback = useCallback((speaking: boolean) => {
    const el = audioRef.current;
    if (!el) return;
    if (restoreRef.current !== null) window.clearTimeout(restoreRef.current);
    if (speaking) {
      el.volume = echoGuardRef.current ? 0 : PLAYBACK_DUCK;
      restoreRef.current = null;
    } else {
      restoreRef.current = window.setTimeout(() => {
        if (audioRef.current) audioRef.current.volume = 1;
        restoreRef.current = null;
      }, ECHO_TAIL_MS);
    }
  }, []);

  // One audio element for the far end, created once and reused. Without it the
  // call connects and both sides are mute: the SDK hands us a MediaStream and
  // nothing plays it.
  useEffect(() => {
    const el = new Audio();
    el.autoplay = true;
    audioRef.current = el;
    return () => {
      el.pause();
      el.srcObject = null;
      audioRef.current = null;
    };
  }, []);

  // Permissions API tells us the standing answer without prompting, so the UI
  // can say "blocked" up front instead of only after a failed dial. Firefox and
  // Safari may not know the "microphone" name at all; unknown is fine, we just
  // fall back to asking.
  useEffect(() => {
    let cancelled = false;
    let status: PermissionStatus | null = null;
    const sync = () => {
      if (!cancelled && status) setMicPermission(status.state as MicPermission);
    };
    navigator.permissions
      ?.query({ name: "microphone" as PermissionName })
      .then((result) => {
        status = result;
        sync();
        result.addEventListener("change", sync);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
      status?.removeEventListener("change", sync);
    };
  }, []);

  /** Raise the browser's own permission prompt. Call it from a click. */
  const requestMic = useCallback(async () => {
    setError(null);
    try {
      await requestMicrophone();
      setMicPermission("granted");
      return true;
    } catch (err) {
      const standing = await readMicPermission();
      const { message, permission } = micErrorMessage(err, standing === "prompt");
      setMicPermission(permission);
      setError(message);
      return false;
    }
  }, []);

  const reset = useCallback(() => {
    callRef.current = null;
    farEndRef.current?.stop();
    farEndRef.current = null;
    // A call that ended mid-duck would otherwise hand the next one a quiet
    // earpiece and no pending timer to undo it.
    if (restoreRef.current !== null) window.clearTimeout(restoreRef.current);
    restoreRef.current = null;
    if (audioRef.current) audioRef.current.volume = 1;
    rtcRef.current?.disconnect();
    rtcRef.current = null;
    if (audioRef.current) audioRef.current.srcObject = null;
    setMuted(false);
    setStartedAt(null);
  }, []);

  /**
   * Stop recording and send the audio up.
   *
   * Runs before `reset`, which tears the streams down. Failure here is logged
   * and swallowed: losing the recording is bad, but throwing inside a hangup
   * handler would leave the UI stuck mid-call, which is worse.
   */
  const finishRecording = useCallback(async (callId: string) => {
    const recorder = recorderRef.current;
    recorderRef.current = null;
    if (!recorder) return; // the reason was already reported when it failed to start
    try {
      const audio = await recorder.stop();
      if (!audio || audio.size < 1024) {
        void api.reportRecordingProblem(callId, `recording was empty (${audio?.size ?? 0} bytes)`);
        return;
      }
      await api.uploadRecording(callId, audio);
    } catch (err) {
      const reason = err instanceof Error ? err.message : String(err);
      console.warn("could not save this call's recording", err);
      void api.reportRecordingProblem(callId, `upload failed — ${reason}`.slice(0, 280));
    }
  }, []);

  const hangup = useCallback(() => {
    callRef.current?.hangup();
    reset();
    setState("idle");
  }, [reset]);

  const toggleMute = useCallback(async () => {
    const call = callRef.current;
    if (!call) return;
    const next = !call.muted();
    await call.mute(next);
    setMuted(next);
  }, []);

  const dial = useCallback(
    async (phoneNumber: string) => {
      setError(null);
      setState("connecting");
      try {
        // Prompt before anything else. This still runs inside the click that
        // started the dial, which is what keeps Chrome willing to show the
        // dialog; doing it after the token round-trip loses the user gesture on
        // some browsers and the request is rejected without ever asking.
        await requestMicrophone();
        setMicPermission("granted");

        const { token, from } = await api.rtcToken();
        if (!token) throw new ApiError("No calling token", "Try again in a moment.");

        const rtc = createInfobipRtc(token, { debug: false });
        rtcRef.current = rtc;
        await awaitConnection(rtc);

        const options = PhoneCallOptions.builder()
          .setFrom((from || "").replace(/^\+/, ""))
          // Recording is asked for here rather than server-side: this call is
          // dialled from the browser, so the dialog the server would attach
          // recording to does not exist until the SDK creates it. Same AUDIO
          // recording the agent's own calls get, saved by the same poller.
          .setRecordingOptions(new PhoneCallRecordingOptions("AUDIO"))
          // The raw laptop mic sends the whole room down the line, and anything
          // that returns our own audio turns that into a loop. See mic-filter.
          .setAudioOptions(
            AudioOptions.builder()
              .setAudioFilter(
                createMicFilter(
                  () => farEndRef.current?.level() ?? 0,
                  () => echoGuardRef.current,
                  suppressPlayback,
                ),
              )
              .build(),
          )
          .build();
        // Bare digits on the wire — Infobip rejects a leading `+` with
        // INVALID_REQUEST. The E.164 form is kept for the call log below.
        const call = rtc.callPhone(phoneNumber.replace(/\D/g, ""), options);
        callRef.current = call;

        // Registered before anyone answers, so a call that rings out or fails
        // still appears in the log instead of vanishing. Never fatal: losing
        // the log entry is not a reason to drop a call that is already dialling.
        void api
          .registerBrowserCall(call.id(), phoneNumber)
          .catch((err) => console.warn("call not registered for logging", err));

        call.on(CallsApiEvent.RINGING, () => setState("ringing"));
        call.on(CallsApiEvent.ESTABLISHED, (event) => {
          if (audioRef.current) audioRef.current.srcObject = event.stream;
          farEndRef.current?.stop();
          farEndRef.current = createLevelProbe(event.stream);
          // Start at pickup, not at dial: ringing is not conversation, and
          // recording it would put a minute of ringtone in front of every call.
          void startCallRecorder(event.stream, (reason) => {
            console.warn("recording problem:", reason);
            void api.reportRecordingProblem(call.id(), reason);
          })
            .then((recorder) => {
              recorderRef.current = recorder;
              if (recorder) {
                void api.reportRecordingProblem(call.id(), "capturing both sides", true);
              }
            })
            .catch(() => undefined);
          setStartedAt(Date.now());
          setState("on-call");
        });
        call.on(CallsApiEvent.HANGUP, () => {
          // Whoever hung up — this browser or the student — the server is not
          // told by anyone else. Without this the row stays open and its timer
          // keeps climbing on every dashboard until something else closes it,
          // which is what made a 141-second call read as fourteen minutes.
          void finishRecording(call.id());
          void api.endCall(call.id()).catch(() => undefined);
          reset();
          setState("idle");
        });
        call.on(CallsApiEvent.ERROR, (event) => {
          // A failed call is still an open row until someone says otherwise.
          void finishRecording(call.id());
          void api.endCall(call.id()).catch(() => undefined);
          setError(event.errorCode?.name || "The call failed.");
          reset();
          setState("error");
        });
      } catch (err) {
        if (err instanceof DOMException) {
          const standing = await readMicPermission();
          const { message, permission } = micErrorMessage(err, standing === "prompt");
          setMicPermission(permission);
          setError(message);
        } else if (err instanceof ApiError) {
          setError(`${err.friendly} ${err.hint}`);
        } else {
          // Anything else is the SDK refusing the dial. Its own wording says
          // more than ours does, so pass it through rather than swallow it.
          const detail = err instanceof Error ? err.message : "";
          setError(
            detail ? `The call could not be started: ${detail}` : "The call could not be started.",
          );
        }
        reset();
        setState("error");
      }
    },
    [reset, suppressPlayback, finishRecording],
  );

  useEffect(() => () => reset(), [reset]);

  return {
    state,
    error,
    muted,
    startedAt,
    micPermission,
    echoGuard,
    toggleEchoGuard: () => setEchoGuard((on) => !on),
    requestMic,
    dial,
    hangup,
    toggleMute,
  };
}
