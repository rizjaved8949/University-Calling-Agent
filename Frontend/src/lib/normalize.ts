/**
 * Adapters between the backend's wire format and the shapes the UI consumes.
 *
 * The Python backend speaks SQLite-row-shaped JSON: the primary key is
 * `call_id`, timestamps are epoch floats (`start_time` / `end_time`), the
 * duration field is `duration`, and WebSocket frames are tagged with `kind`
 * using camelCase keys. The components expect `id`, ISO timestamps,
 * `duration_seconds`, and `type`. Everything is reconciled here, once, so no
 * component has to know about either dialect.
 */
import type { TKey } from "./i18n";
import type { Call, CallStats, RealtimeEvent } from "./types";

type Row = Record<string, unknown>;

function num(v: unknown): number | undefined {
  return typeof v === "number" && Number.isFinite(v) ? v : undefined;
}

function str(v: unknown): string | undefined {
  return typeof v === "string" && v ? v : undefined;
}

/** Epoch seconds (the backend stores REAL) to an ISO string. */
function epochToIso(v: unknown): string | undefined {
  const n = num(v);
  if (n === undefined || n <= 0) return undefined;
  // Tolerate milliseconds in case a future field is stored that way.
  const ms = n > 1e11 ? n : n * 1000;
  const d = new Date(ms);
  return Number.isNaN(d.getTime()) ? undefined : d.toISOString();
}

/** A backend call row to the Call model the components read. */
export function normalizeCall(row: unknown): Call | null {
  if (!row || typeof row !== "object") return null;
  const r = row as Row;
  const id = str(r["call_id"]) ?? str(r["id"]);
  if (!id) return null;

  const started = epochToIso(r["start_time"]) ?? str(r["started_at"]);
  const ended = epochToIso(r["end_time"]) ?? str(r["ended_at"]);
  // Pickup, which is when talk time and the recording begin. Missing while the
  // phone is still ringing, which is exactly how the timer tells the difference.
  const answered = epochToIso(r["answer_time"]) ?? str(r["answered_at"]);

  return {
    ...(r as object),
    id,
    ...(started ? { started_at: started } : {}),
    ...(answered ? { answered_at: answered } : {}),
    ...(ended ? { ended_at: ended } : {}),
    duration_seconds: num(r["duration"]) ?? num(r["duration_seconds"]) ?? 0,
    recording_ready: Boolean(r["recording_ready"]),
  } as Call;
}

export function normalizeCalls(rows: unknown): Call[] {
  if (!Array.isArray(rows)) return [];
  return rows.map(normalizeCall).filter((c): c is Call => c !== null);
}

/**
 * The backend returns `{total, inbound, outbound, talk_time, recordings, active}`.
 * Emit those alongside the longer aliases the dashboard reads first, so the
 * cards resolve on the first key they try.
 */
export function normalizeStats(raw: unknown): CallStats {
  const r = (raw && typeof raw === "object" ? raw : {}) as Row;
  const total = num(r["total"]) ?? num(r["total_calls"]) ?? 0;
  const inbound = num(r["inbound"]) ?? num(r["inbound_calls"]) ?? 0;
  const outbound = num(r["outbound"]) ?? num(r["outbound_calls"]) ?? 0;
  const talk = num(r["talk_time"]) ?? num(r["total_duration"]) ?? 0;
  const active = num(r["active"]) ?? num(r["active_calls"]) ?? 0;
  const recordings = num(r["recordings"]) ?? 0;

  return {
    ...r,
    total,
    total_calls: total,
    inbound,
    inbound_calls: inbound,
    outbound,
    outbound_calls: outbound,
    talk_time: talk,
    total_duration: talk,
    active,
    active_calls: active,
    recordings,
  };
}

/**
 * A WebSocket frame to the event shape the realtime provider switches on.
 *
 * The backend publishes `{kind, at, ...payload}` where the call id is `callId`
 * and call frames nest the row under `call`. Snapshot frames carry `calls` and
 * `stats` instead.
 */
export function normalizeEvent(frame: unknown): RealtimeEvent | null {
  if (!frame || typeof frame !== "object") return null;
  const f = frame as Row;
  const type = str(f["kind"]) ?? str(f["type"]);
  if (!type) return null;

  const at = epochToIso(f["at"]) ?? str(f["timestamp"]) ?? new Date().toISOString();
  const nested = f["call"] && typeof f["call"] === "object" ? normalizeCall(f["call"]) : null;
  const callId = str(f["callId"]) ?? str(f["call_id"]) ?? nested?.id;

  return {
    ...f,
    type,
    timestamp: at,
    ...(callId ? { call_id: callId } : {}),
    ...(nested ? { call: nested } : {}),
    ...(Array.isArray(f["calls"]) ? { calls: normalizeCalls(f["calls"]) } : {}),
    ...(f["stats"] ? { stats: normalizeStats(f["stats"]) } : {}),
  } as RealtimeEvent;
}

/**
 * Statuses that mean a call is still on the line.
 *
 * ENDING belongs here: it means the hangup has been sent but the line is not
 * confirmed down yet. Leaving it out made those calls read as finished, so the
 * dashboard offered a recording for a call that was still in progress.
 */
const LIVE_STATUSES = new Set([
  "DIALING",
  "RINGING",
  "ANSWERED",
  "CONNECTING_AGENT",
  "BRIDGED",
  "ENDING",
]);

export function isLiveStatus(status?: string): boolean {
  return LIVE_STATUSES.has((status ?? "").toUpperCase());
}

export function isFinishedStatus(status?: string): boolean {
  const s = (status ?? "").toUpperCase();
  return s === "COMPLETED" || s === "FAILED";
}

/**
 * The backend status turned into a translation key.
 *
 * The stored values are a state machine, not English: a call that has been
 * picked up reads CONNECTING_AGENT or BRIDGED depending on how far the agent
 * leg has got, and printing those raw showed the person watching the dashboard
 * an implementation detail instead of what happened. The distinctions that
 * matter to them are: still ringing, picked up, talking, over.
 *
 * ANSWERED and BRIDGED are kept apart because they are genuinely different
 * moments — the student has lifted the phone, and the student is talking to
 * Ayesha — and with pre-dial on, the second follows the first immediately.
 *
 * Unknown values fall through to the raw string rather than "Unknown": a status
 * this does not recognise is one the backend added, and showing it is how it
 * gets noticed.
 */
const STATUS_KEYS: Record<string, TKey> = {
  DIALING: "statusDialing",
  RINGING: "statusRinging",
  ANSWERED: "statusAnswered",
  CONNECTING_AGENT: "statusConnecting",
  BRIDGED: "statusBridged",
  ENDING: "statusEnding",
  COMPLETED: "statusEnded",
  FAILED: "statusFailed",
  NO_ANSWER: "statusNoAnswer",
  BUSY: "statusBusy",
};

export function statusKey(status?: string): TKey | null {
  return STATUS_KEYS[(status ?? "").toUpperCase()] ?? null;
}

/** `statusKey` with the fallback applied, for the common "just render it" case. */
export function describeStatus(status: string | undefined, t: (k: TKey) => string): string {
  const key = statusKey(status);
  if (key) return t(key);
  return status ? status.replace(/_/g, " ").toLowerCase() : t("statusUnknown");
}

/**
 * A carrier outcome code turned into something an admissions officer can act on.
 * A wrong number fails at the carrier, not at validation, so this text is the
 * only explanation the user ever gets.
 */
export function describeFailure(outcome?: string): string {
  const code = (outcome ?? "").toUpperCase();
  const map: Record<string, string> = {
    NOT_FOUND: "that number does not exist",
    INVALID_DESTINATION: "that number does not exist",
    UNALLOCATED_NUMBER: "that number is not in service",
    NO_ROUTE: "that number could not be reached",
    BUSY: "the line was busy",
    NO_ANSWER: "nobody answered",
    REJECTED: "the call was rejected",
    CANCELLED: "the call was cancelled",
    UNREACHABLE: "the phone was switched off or out of coverage",
    INSUFFICIENT_FUNDS: "the telephony account is out of credit",
  };
  for (const [key, text] of Object.entries(map)) {
    if (code.includes(key)) return text;
  }
  return outcome ? `the carrier said: ${outcome}` : "the carrier did not connect it";
}
