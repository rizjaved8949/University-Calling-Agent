import { isLiveStatus } from "./normalize";
import type { Call, TranscriptEntry } from "./types";

export function digitsOnly(v: string) {
  return v.replace(/\D/g, "");
}

/** Format Pakistani national digits as "300 1234567". */
export function formatNational(digits: string) {
  const d = digits.slice(0, 10);
  if (d.length <= 3) return d;
  return `${d.slice(0, 3)} ${d.slice(3)}`;
}

export function formatE164(dial: string, national: string) {
  return `${dial}${digitsOnly(national)}`;
}

export function prettyNumber(raw?: string) {
  if (!raw) return "Unknown number";
  const d = digitsOnly(raw);
  if (raw.startsWith("+92") || d.startsWith("92")) {
    const nat = d.replace(/^92/, "");
    return `+92 ${formatNational(nat)}`;
  }
  return raw;
}

export function maskNumber(raw?: string) {
  const pretty = prettyNumber(raw);
  if (pretty === "Unknown number") return pretty;
  return pretty.replace(/(\d{3})(?=\s?\d{2,}$)/, "$1").replace(/\d{4}$/, "••••");
}

export function validatePkNumber(national: string): string | null {
  const d = digitsOnly(national);
  if (d.length === 0) return "Enter the student's mobile number to continue.";
  if (!d.startsWith("3")) return "Pakistani mobile numbers start with 3, for example 300 1234567.";
  if (d.length < 10) return `That number is short — ${10 - d.length} more digit(s) needed.`;
  if (d.length > 10) return "That number is too long. A mobile number has 10 digits after +92.";
  return null;
}

export function formatDuration(seconds?: number) {
  const s = Math.max(0, Math.round(seconds ?? 0));
  const m = Math.floor(s / 60);
  const h = Math.floor(m / 60);
  if (h > 0) return `${h}:${String(m % 60).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  return `${m}:${String(s % 60).padStart(2, "0")}`;
}

export function formatTalkTime(seconds?: number) {
  const s = Math.max(0, Math.round(seconds ?? 0));
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

export function relativeTime(iso?: string) {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const diff = Math.round((Date.now() - then) / 1000);
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} hr ago`;
  return new Date(iso).toLocaleDateString();
}

/** Wall-clock time, e.g. "6:44 PM" — what a person means by "when". */
export function clockTime(iso?: string) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

/** "12 Aug, 6:44 PM" for anything that did not happen today. */
export function stampTime(iso?: string) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const today = new Date().toDateString() === d.toDateString();
  return today
    ? clockTime(iso)
    : `${d.toLocaleDateString([], { day: "numeric", month: "short" })}, ${clockTime(iso)}`;
}

export function callTime(c: Call) {
  return c.started_at || c.created_at;
}

export function callEndTime(c: Call) {
  return c.ended_at;
}

export function callDuration(c: Call) {
  return c.duration_seconds ?? c.duration ?? 0;
}

/**
 * How long this call has been going, counted here rather than asked for.
 *
 * Talk time starts at pickup — ringing is not conversation — and a live row's
 * stored duration is only as fresh as the last event, so a call with nothing
 * happening on it would sit at the same number for minutes.
 */
export function liveDuration(c: Call) {
  const from = c.answered_at || c.started_at;
  if (!from) return callDuration(c);
  const started = new Date(from).getTime();
  if (Number.isNaN(started)) return callDuration(c);
  return Math.max(0, Math.round((Date.now() - started) / 1000));
}

export function callNumber(c: Call) {
  return c.phone_number || c.to_number || c.from_number;
}

export function callStatus(c: Call) {
  return (c.status || c.state || "unknown").toLowerCase();
}

export function isActiveCall(c: Call) {
  return isLiveStatus(c.status);
}

export function entryText(e: TranscriptEntry) {
  return e.text ?? e.content ?? "";
}

export function entryRole(e: TranscriptEntry): "agent" | "caller" {
  const r = (e.role ?? e.speaker ?? "").toLowerCase();
  return r.includes("agent") || r.includes("assistant") || r.includes("ayesha")
    ? "agent"
    : "caller";
}

export function hasUrdu(text: string) {
  return /[\u0600-\u06FF]/.test(text);
}

export function toCsv(rows: Record<string, string | number>[]) {
  const first = rows[0];
  if (!first) return "";
  const headers = Object.keys(first);
  const escape = (v: string | number) => `"${String(v).replace(/"/g, '""')}"`;
  return [
    headers.join(","),
    ...rows.map((r) => headers.map((h) => escape(r[h] ?? "")).join(",")),
  ].join("\n");
}
