import { getApiBase } from "./config";
import { normalizeCall, normalizeCalls, normalizeStats } from "./normalize";
import type { AppConfig, Call, CallStats, ChatReply, Direction, RtcToken } from "./types";

export class ApiError extends Error {
  friendly: string;
  hint: string;
  /** HTTP status, when the failure came back from the server at all. */
  status?: number | undefined;
  constructor(friendly: string, hint: string, technical?: string, status?: number) {
    super(technical || friendly);
    this.friendly = friendly;
    this.hint = hint;
    this.status = status;
  }
}

function describe(status: number): { friendly: string; hint: string } {
  if (status === 404)
    return {
      friendly: "We couldn't find that record.",
      hint: "It may have been removed. Go back and refresh the list.",
    };
  if (status === 409 || status === 400)
    return {
      friendly: "The phone service couldn't accept that request.",
      hint: "Check the number and try again.",
    };
  if (status >= 500)
    return {
      friendly: "The server had a problem completing that.",
      hint: "Wait a moment and try again. If it keeps happening, tell the IT team.",
    };
  return {
    friendly: "That didn't work.",
    hint: "Please try again in a moment.",
  };
}

let inflight = 0;
const listeners = new Set<(busy: boolean) => void>();

export function onNetworkBusy(fn: (busy: boolean) => void) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function setBusy(delta: number) {
  inflight = Math.max(0, inflight + delta);
  listeners.forEach((l) => l(inflight > 0));
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  setBusy(1);
  try {
    const res = await fetch(`${getApiBase()}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
    });
    if (!res.ok) {
      const d = describe(res.status);
      throw new ApiError(d.friendly, d.hint, `${res.status} ${path}`, res.status);
    }
    if (res.status === 204) return undefined as T;
    return (await res.json()) as T;
  } catch (err) {
    if (err instanceof ApiError) throw err;
    throw new ApiError(
      "Couldn't reach the phone service.",
      "Check your internet connection, then try again.",
      String(err),
    );
  } finally {
    setBusy(-1);
  }
}

export const api = {
  config: () => request<AppConfig>("/api/config"),
  stats: async (): Promise<CallStats> => normalizeStats(await request<unknown>("/api/calls/stats")),
  calls: async (direction?: Direction): Promise<{ calls: Call[] }> => {
    // The server defaults to 50, which quietly drops older calls off the end of
    // the history — they are still stored, they just stop being shown, which
    // reads as "my calls disappeared".
    const params = new URLSearchParams({ limit: "500" });
    if (direction) params.set("direction", direction);
    const res = await request<{ calls?: unknown }>(`/api/calls?${params}`);
    return { calls: normalizeCalls(res?.calls) };
  },
  /** Permanent: the row and the audio file both go.
   *
   * A 404 is treated as success: the only way to get one here is a call that is
   * already gone, and telling somebody their delete failed because the thing
   * they wanted deleted does not exist is a lie about the outcome. Delete is
   * the one verb where "it was not there" and "it is not there now" are the
   * same result. */
  deleteCall: async (id: string) => {
    try {
      return await request<unknown>(`/api/calls/${id}`, { method: "DELETE" });
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) return undefined;
      throw err;
    }
  },
  call: async (id: string): Promise<Call> => {
    const row = normalizeCall(await request<unknown>(`/api/calls/${id}`));
    if (!row) throw new ApiError("We couldn't find that call.", "Go back and refresh the list.");
    return row;
  },
  placeCall: async (phone_number: string): Promise<Call> => {
    const row = normalizeCall(
      await request<unknown>("/api/calls/outbound", {
        method: "POST",
        body: JSON.stringify({ phone_number }),
      }),
    );
    if (!row) {
      throw new ApiError(
        "The call was placed but we lost track of it.",
        "Check the call log in a moment to see what happened.",
      );
    }
    return row;
  },
  /** Rings the counselor first, then bridges the student in. Ayesha stays out. */
  placeManualCall: async (phone_number: string, operator_number: string): Promise<Call> => {
    const row = normalizeCall(
      await request<unknown>("/api/calls/manual", {
        method: "POST",
        body: JSON.stringify({ phone_number, operator_number }),
      }),
    );
    if (!row) {
      throw new ApiError(
        "The call was placed but we lost track of it.",
        "Check the call log in a moment to see what happened.",
      );
    }
    return row;
  },
  rtcToken: () => request<RtcToken>("/api/rtc/token", { method: "POST" }),
  /**
   * Tell the server about a call the browser placed itself.
   *
   * Every other mode is dialled server-side, so the call log and the recording
   * saver learn about it for free. This one is dialled from here, and without
   * this the call happens with nothing on the server knowing it existed.
   */
  registerBrowserCall: (call_id: string, phone_number: string) =>
    request<unknown>("/api/rtc/calls", {
      method: "POST",
      body: JSON.stringify({ call_id, phone_number }),
    }),
  /**
   * Hang up, and get the closed row back.
   *
   * The response carries the call as the server settled it — real end time and
   * real talk time, already reconciled with the carrier — so the screen that
   * just ended a call can show the true duration instead of whatever the list
   * happened to be caching a moment earlier.
   */
  endCall: async (id: string): Promise<Call | null> => {
    const body = await request<{ call?: unknown }>(`/api/calls/${id}/end`, { method: "POST" });
    return body && typeof body === "object" && body.call ? normalizeCall(body.call) : null;
  },
  /**
   * Hand the server a recording the browser made itself.
   *
   * Talk-tab calls are not recorded by the provider — they run under its
   * built-in WebRTC configuration, which has recording off and no API left to
   * change it. This is how those calls get audio at all.
   */
  uploadRecording: async (id: string, audio: Blob): Promise<void> => {
    const response = await fetch(`${getApiBase()}/api/calls/${id}/recording`, {
      method: "POST",
      headers: { "Content-Type": audio.type || "audio/webm" },
      body: audio,
    });
    if (!response.ok) {
      throw new ApiError(
        "The call recording could not be saved.",
        `The server answered ${response.status}.`,
      );
    }
  },
  /**
   * Tell the server how recording went, so it reaches the log.
   *
   * Both outcomes are reported, not just failures: "started" followed by no
   * upload, a named failure, and complete silence are three different faults,
   * and only the log can tell them apart.
   */
  reportRecordingProblem: (id: string, reason: string, ok = false) =>
    fetch(`${getApiBase()}/api/calls/${id}/recording-problem`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason, ok }),
    }).catch(() => undefined),
  /**
   * A direct, time-limited link to the audio in storage.
   *
   * Playing through the API means the whole file crosses the network twice
   * before the first note sounds — several seconds on a long call. A direct
   * link streams from storage, so playback starts on the first chunk and
   * seeking works. Returns null when there is no such link (local disk in
   * development), and the caller falls back to fetching the bytes.
   */
  recordingLink: async (
    id: string,
  ): Promise<{ url: string; mime: string; filename: string } | null> => {
    try {
      const response = await fetch(`${getApiBase()}/api/calls/${id}/recording?link=true`);
      if (!response.ok) return null;
      const body = (await response.json()) as { url?: string; mime?: string; filename?: string };
      return body.url
        ? {
            url: body.url,
            mime: body.mime ?? "audio/wav",
            filename: body.filename ?? `call-${id}`,
          }
        : null;
    } catch {
      return null;
    }
  },
  recordingUrl: (id: string, download = false) =>
    `${getApiBase()}/api/calls/${id}/recording${download ? "?download=true" : ""}`,
  /**
   * The master workbook: every call ever taken, three sheets, one file.
   *
   * Fetched rather than linked so a failure is a message instead of a browser
   * tab showing raw JSON, and so the filename the server chose is the one the
   * file is saved under. The server builds it fresh from the call log on each
   * request — there is no stored file to go stale, and downloading can never
   * disturb the records it is built from.
   */
  downloadExcel: async (): Promise<string> => {
    setBusy(1);
    try {
      const response = await fetch(`${getApiBase()}/api/exports/calls.xlsx`);
      if (!response.ok) {
        const d = describe(response.status);
        throw new ApiError(
          response.status === 503
            ? "Excel export is not available on the server yet."
            : "The Excel report could not be built.",
          response.status === 503
            ? "The server needs redeploying to install the spreadsheet package."
            : d.hint,
          `${response.status} /api/exports/calls.xlsx`,
        );
      }
      // The server names the file; the fallback is only for a proxy that strips
      // the header. Quotes and any path are removed — a filename is all we take
      // from a header, never a directory to write into.
      const disposition = response.headers.get("Content-Disposition") ?? "";
      const named = /filename="?([^";]+)"?/i.exec(disposition)?.[1];
      const filename = (named ?? "Voice_Agent_Call_Records.xlsx").split(/[\\/]/).pop()!;

      const url = URL.createObjectURL(await response.blob());
      try {
        const a = document.createElement("a");
        a.href = url;
        a.download = filename;
        a.click();
      } finally {
        // Revoked on a tick, not immediately: some browsers have not started
        // reading the blob when click() returns, and revoking first gives the
        // user a zero-byte file.
        setTimeout(() => URL.revokeObjectURL(url), 10_000);
      }
      return filename;
    } catch (err) {
      if (err instanceof ApiError) throw err;
      throw new ApiError(
        "Couldn't reach the phone service.",
        "Check your internet connection, then try again.",
        String(err),
      );
    } finally {
      setBusy(-1);
    }
  },
  /**
   * Link to the Google Sheet copy of the workbook. The server refreshes the
   * sheet before answering, so what opens includes the latest call.
   */
  driveReportLink: async (): Promise<string> => {
    setBusy(1);
    try {
      const response = await fetch(`${getApiBase()}/api/exports/drive`);
      if (!response.ok) {
        const d = describe(response.status);
        throw new ApiError(
          response.status === 404
            ? "Google Drive is not set up on the server."
            : "The report could not be updated in Google Drive.",
          response.status === 404 ? "Add the Google credentials to the backend." : d.hint,
          `${response.status} /api/exports/drive`,
        );
      }
      const body = (await response.json()) as { url?: string };
      if (!body.url) throw new ApiError("Google Drive returned no link.", "Try again.", "no url");
      return body.url;
    } catch (err) {
      if (err instanceof ApiError) throw err;
      throw new ApiError(
        "Couldn't reach the phone service.",
        "Check your internet connection, then try again.",
        String(err),
      );
    } finally {
      setBusy(-1);
    }
  },
  /** Re-run the post-call summary for one call. */
  summariseCall: (id: string, force = false) =>
    request<unknown>(`/api/calls/${id}/summary${force ? "?force=true" : ""}`, {
      method: "POST",
    }),
  chat: (message: string, session_id: string) =>
    request<ChatReply>("/api/chat", {
      method: "POST",
      body: JSON.stringify({ message, session_id }),
    }),
};

export const queryKeys = {
  config: ["config"] as const,
  stats: ["stats"] as const,
  calls: (direction?: Direction) => ["calls", direction ?? "all"] as const,
  call: (id: string) => ["call", id] as const,
};
