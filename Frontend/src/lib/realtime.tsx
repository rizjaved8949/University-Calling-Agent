import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";
import { toast } from "sonner";
import { getWsUrl } from "./config";
import { useQueryClient } from "@tanstack/react-query";
import { describeFailure, isFinishedStatus, isLiveStatus, normalizeEvent } from "./normalize";
import { queryKeys } from "./api";
import type { Call, RealtimeEvent } from "./types";

export type ConnState = "live" | "reconnecting" | "offline";

export type TranscriptBubble = {
  id: string;
  callId?: string | undefined;
  kind: "message" | "rag" | "interrupt";
  role: "agent" | "caller" | "system";
  text: string;
  at: string;
};

type Ctx = {
  state: ConnState;
  events: RealtimeEvent[];
  bubbles: TranscriptBubble[];
  activeCallId: string | null;
  setActiveCallId: (id: string | null) => void;
  speaking: "agent" | "caller" | "thinking" | "idle";
  /** Calls the backend has told us have a recording on disk, this session. */
  recordingReady: (id: string) => boolean;
  clearBubbles: () => void;
};

const RealtimeContext = createContext<Ctx | null>(null);

const MAX_EVENTS = 60;

function pick(ev: RealtimeEvent): Record<string, unknown> {
  // Backend frames are flat; the call row (when present) is nested under `call`.
  const call = (ev as { call?: Record<string, unknown> }).call;
  return { ...(ev.payload || {}), ...(ev.data || {}), ...ev, ...(call || {}) };
}

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

export function RealtimeProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<ConnState>("reconnecting");
  const [events, setEvents] = useState<RealtimeEvent[]>([]);
  const [bubbles, setBubbles] = useState<TranscriptBubble[]>([]);
  const [activeCallId, setActiveCallId] = useState<string | null>(null);
  const [speaking, setSpeaking] = useState<Ctx["speaking"]>("idle");
  // A set, not a single id: one slot meant a recording.ready for call B erased
  // the fact that call A's audio was available, and the player vanished.
  const [readyRecordings, setReadyRecordings] = useState<ReadonlySet<string>>(new Set());
  const qc = useQueryClient();
  const speakTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const attemptRef = useRef(0);
  const closedRef = useRef(false);

  const clearBubbles = useCallback(() => setBubbles([]), []);

  const markSpeaking = useCallback((who: Ctx["speaking"]) => {
    setSpeaking(who);
    if (speakTimer.current) clearTimeout(speakTimer.current);
    speakTimer.current = setTimeout(() => setSpeaking("idle"), 2600);
  }, []);

  const handle = useCallback(
    (ev: RealtimeEvent) => {
      setEvents((prev) => [ev, ...prev].slice(0, MAX_EVENTS));
      const d = pick(ev);
      const callId = str(d["call_id"]) || str(d["id"]) || undefined;
      const at = str(d["timestamp"]) || new Date().toISOString();

      switch (ev.type) {
        case "snapshot": {
          // First frame after connecting: seed the caches so the dashboard has
          // numbers before the REST queries land.
          const snapshotCalls = (ev as { calls?: Call[] }).calls;
          if (Array.isArray(snapshotCalls)) {
            qc.setQueryData(queryKeys.calls(), { calls: snapshotCalls });
            const live = snapshotCalls.find((c) => isLiveStatus(c.status));
            if (live) setActiveCallId((cur) => cur ?? live.id);
          }
          const snapshotStats = (ev as { stats?: unknown }).stats;
          if (snapshotStats) qc.setQueryData(queryKeys.stats, snapshotStats);
          break;
        }
        case "call.created":
        case "call.updated": {
          const status = str(d["status"]);
          if (callId && isFinishedStatus(status)) {
            setSpeaking("idle");
            setActiveCallId((cur) => (cur === callId ? null : cur));
          } else if (callId && isLiveStatus(status)) {
            setActiveCallId((cur) => cur ?? callId);
          }
          // A wrong or unreachable number only fails at the carrier, so this
          // event is the one place the reason ever surfaces.
          if (status.toUpperCase() === "FAILED") {
            toast.error(`Call to ${str(d["phone_number"]) || "that number"} didn't connect.`, {
              description: `The carrier reported: ${describeFailure(str(d["outcome"]))}.`,
            });
          }
          void qc.invalidateQueries({ queryKey: queryKeys.calls() });
          void qc.invalidateQueries({ queryKey: queryKeys.stats });
          break;
        }
        case "transcript": {
          const raw = (str(d["role"]) || str(d["speaker"]) || "").toLowerCase();
          const role: "agent" | "caller" =
            raw.includes("agent") || raw.includes("assistant") || raw.includes("ayesha")
              ? "agent"
              : "caller";
          const text = str(d["text"]) || str(d["content"]);
          if (!text) break;
          markSpeaking(role);
          setBubbles((prev) => [
            ...prev,
            { id: `${at}-${prev.length}`, callId, kind: "message", role, text, at },
          ]);
          break;
        }
        case "rag.query": {
          const q = str(d["query"]) || str(d["question"]) || str(d["text"]);
          markSpeaking("thinking");
          setBubbles((prev) => [
            ...prev,
            {
              id: `rag-${at}-${prev.length}`,
              callId,
              kind: "rag",
              role: "system",
              text: q,
              at,
            },
          ]);
          break;
        }
        case "agent.interrupted": {
          markSpeaking("caller");
          setBubbles((prev) => [
            ...prev,
            {
              id: `int-${at}-${prev.length}`,
              callId,
              kind: "interrupt",
              role: "system",
              text: "",
              at,
            },
          ]);
          break;
        }
        case "recording.ready": {
          if (callId) setReadyRecordings((prev) => new Set(prev).add(callId));
          void qc.invalidateQueries({ queryKey: queryKeys.calls() });
          break;
        }
        case "call.deleted": {
          // Someone erased it — here, or in another window. Either way the row
          // must go now rather than at the next refetch.
          void qc.invalidateQueries({ queryKey: queryKeys.calls() });
          void qc.invalidateQueries({ queryKey: queryKeys.stats });
          break;
        }
        case "agent.error":
        case "call.error": {
          toast.error("Ayesha ran into a problem on this call.", {
            description:
              str(d["message"]) || "The student may need a call back. Try placing the call again.",
          });
          break;
        }
        default:
          break;
      }
    },
    [markSpeaking, qc],
  );

  useEffect(() => {
    closedRef.current = false;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;

    const connect = () => {
      if (closedRef.current) return;
      let socket: WebSocket;
      try {
        socket = new WebSocket(getWsUrl());
      } catch {
        scheduleRetry();
        return;
      }
      socketRef.current = socket;

      socket.onopen = () => {
        attemptRef.current = 0;
        setState("live");
      };
      socket.onmessage = (msg) => {
        try {
          const parsed = JSON.parse(msg.data as string) as unknown;
          const frames = Array.isArray(parsed) ? parsed : [parsed];
          for (const frame of frames) {
            const event = normalizeEvent(frame);
            if (event) handle(event);
          }
        } catch {
          /* ignore malformed frames */
        }
      };
      socket.onerror = () => setState((s) => (s === "live" ? "reconnecting" : s));
      socket.onclose = () => {
        socketRef.current = null;
        scheduleRetry();
      };
    };

    const scheduleRetry = () => {
      if (closedRef.current) return;
      attemptRef.current += 1;
      setState(attemptRef.current > 3 ? "offline" : "reconnecting");
      const delay = Math.min(64000, 1000 * 2 ** Math.min(attemptRef.current, 6));
      retryTimer = setTimeout(connect, delay);
    };

    connect();
    return () => {
      closedRef.current = true;
      if (retryTimer) clearTimeout(retryTimer);
      socketRef.current?.close();
    };
  }, [handle]);

  // A new call starts a clean transcript. Callers used to have to remember to
  // clear it themselves, which meant an inbound call — or a second call placed
  // from another tab — opened on top of the previous conversation's bubbles.
  const previousCall = useRef<string | null>(null);
  useEffect(() => {
    if (activeCallId && activeCallId !== previousCall.current) setBubbles([]);
    previousCall.current = activeCallId;
  }, [activeCallId]);

  const recordingReady = useCallback((id: string) => readyRecordings.has(id), [readyRecordings]);

  const value = useMemo(
    () => ({
      state,
      events,
      bubbles,
      activeCallId,
      setActiveCallId,
      speaking,
      recordingReady,
      clearBubbles,
    }),
    [state, events, bubbles, activeCallId, speaking, recordingReady, clearBubbles],
  );

  return <RealtimeContext.Provider value={value}>{children}</RealtimeContext.Provider>;
}

export function useRealtime() {
  const ctx = useContext(RealtimeContext);
  if (!ctx) throw new Error("useRealtime must be used inside RealtimeProvider");
  return ctx;
}
