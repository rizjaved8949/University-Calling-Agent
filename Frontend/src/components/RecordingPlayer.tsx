import { useCallback, useEffect, useRef, useState } from "react";
import { AlertCircle, Disc, Download, Loader2, MicOff, Play, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import type { RecordingState } from "@/lib/types";
import { cn } from "@/lib/utils";

type Status = "idle" | "loading" | "ready" | "pending" | "error";

/**
 * A filename extension that matches the bytes.
 *
 * Provider recordings are WAV; calls recorded in the browser are webm/opus.
 * Saving a webm as "call.wav" produces a file the operator double-clicks and
 * their media player refuses — the audio is fine, the name is a lie.
 */
function extensionFor(mime: string): string {
  const type = (mime || "").toLowerCase();
  for (const [needle, extension] of [
    ["webm", "webm"],
    ["ogg", "ogg"],
    ["mp4", "mp4"],
    ["mpeg", "mp3"],
    ["mp3", "mp3"],
  ] as const) {
    if (type.includes(needle)) return extension;
  }
  return "wav";
}

/**
 * Call audio, fetched as a blob rather than handed to `<audio src>` directly.
 *
 * Pointing an audio element straight at the endpoint looks simpler but fails
 * silently in exactly the case that matters: while Infobip is still composing
 * the two legs the endpoint answers 404, and the element renders a dead player
 * with no explanation and no way to retry. Fetching means we can tell
 * "not ready yet" apart from "actually broken", and the same blob backs both
 * playback and the download button, so saving the file costs no second request.
 */
export function RecordingPlayer({
  callId,
  state: serverState,
  compact = false,
}: {
  callId: string;
  /**
   * What the server says exists. Without it every empty response looks like
   * "not ready yet", so a call that was never recorded shows a retry button
   * that can never succeed — and people keep pressing it, once every couple of
   * seconds, each press another request for a file that does not exist.
   */
  state?: RecordingState | undefined;
  compact?: boolean;
}) {
  const { t } = useI18n();
  // Compact players sit one-per-row in the call log, so they wait to be asked.
  // Fetching on mount there would pull every recording in the list at once, and
  // the endpoint can spend seconds composing a call's two legs on first request.
  const [status, setStatus] = useState<Status>(compact ? "idle" : "loading");
  const [url, setUrl] = useState<string | null>(null);
  const [mime, setMime] = useState("audio/wav");
  const urlRef = useRef<string | null>(null);

  const load = useCallback(async () => {
    setStatus("loading");
    try {
      // Fastest path: a direct link to storage. The audio element then streams
      // it — sound starts on the first chunk instead of after the entire file
      // has travelled storage → server → here, which on a seven-minute call was
      // several seconds of staring at nothing.
      const link = await api.recordingLink(callId);
      if (link) {
        if (urlRef.current) URL.revokeObjectURL(urlRef.current);
        urlRef.current = null;
        setMime(link.mime);
        setUrl(link.url);
        setStatus("ready");
        return;
      }

      const res = await fetch(api.recordingUrl(callId));
      if (res.status === 404) {
        setStatus("pending");
        return;
      }
      if (!res.ok) {
        setStatus("error");
        return;
      }
      const blob = await res.blob();
      if (!blob.size) {
        setStatus("pending");
        return;
      }
      if (urlRef.current) URL.revokeObjectURL(urlRef.current);
      const next = URL.createObjectURL(blob);
      urlRef.current = next;
      setMime(blob.type || "audio/wav");
      setUrl(next);
      setStatus("ready");
    } catch {
      setStatus("error");
    }
  }, [callId]);

  useEffect(() => {
    if (!compact && serverState !== "NONE" && serverState !== "RECORDING") void load();
    return () => {
      if (urlRef.current) {
        URL.revokeObjectURL(urlRef.current);
        urlRef.current = null;
      }
    };
  }, [load, compact, serverState]);

  // Composition finishes a little after the call does, so a pending recording is
  // worth one automatic look rather than making someone sit and click.
  useEffect(() => {
    if (status !== "pending" || serverState === "NONE") return;
    const retry = window.setTimeout(() => void load(), 15_000);
    return () => window.clearTimeout(retry);
  }, [status, serverState, load]);

  // The call is still up: audio is being captured, but there is none to play yet.
  if (serverState === "RECORDING") {
    return (
      <p className="flex items-center gap-2 text-sm text-muted-foreground">
        <Disc className="size-4 shrink-0 animate-pulse text-destructive" aria-hidden="true" />
        Recording…
      </p>
    );
  }

  // Nothing was captured and nothing is coming. Say so plainly: an honest dead
  // end is kinder than a retry button that will never do anything.
  if (serverState === "NONE" && status !== "ready") {
    return (
      <p className="flex items-center gap-2 text-sm text-muted-foreground">
        <MicOff className="size-4 shrink-0" aria-hidden="true" />
        {t("recordingNone")}
      </p>
    );
  }

  if (status === "idle") {
    return (
      <Button variant="secondary" size="sm" className="gap-1.5" onClick={() => void load()}>
        <Play className="size-4" aria-hidden="true" />
        {t("play")}
      </Button>
    );
  }

  if (status === "loading") {
    return (
      <p className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" aria-hidden="true" />
        {t("recordingLoading")}
      </p>
    );
  }

  if (status === "pending" || status === "error") {
    return (
      <div className="space-y-2">
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <AlertCircle className="size-4 shrink-0" aria-hidden="true" />
          {status === "pending" ? t("recordingPending") : t("recordingFailedLoad")}
        </p>
        <Button variant="secondary" size="sm" className="gap-1.5" onClick={() => void load()}>
          <RotateCcw className="size-4" aria-hidden="true" />
          {t("retry")}
        </Button>
      </div>
    );
  }

  const extension = extensionFor(mime);

  return (
    <div className="space-y-3">
      {/* Compact players are only ever mounted by pressing Play, so start
          straight away rather than making the user click a second time. */}
      <div className={cn("flex items-center gap-2", !compact && "block")}>
        <audio controls autoPlay={compact} className="w-full" src={url ?? undefined}>
          <track kind="captions" />
        </audio>
        {/* The call log needs this as much as the detail panel does — more, in
            fact, since the log is where people go looking for a call to keep.
            Icon only here to fit the row; the panel below has room for words. */}
        {compact ? (
          <Button
            variant="ghost"
            size="sm"
            className="shrink-0 text-muted-foreground hover:text-foreground"
            asChild
          >
            <a
              href={url ?? undefined}
              download={`call-${callId}.${extension}`}
              aria-label={t("download")}
              title={t("download")}
            >
              <Download className="size-4" aria-hidden="true" />
            </a>
          </Button>
        ) : null}
      </div>
      {compact ? null : (
        <Button variant="secondary" size="sm" className="gap-1.5" asChild>
          {/* Downloads the blob we already hold, so this never re-hits the
              provider and always saves the same audio that just played. */}
          <a href={url ?? undefined} download={`call-${callId}.${extension}`}>
            <Download className="size-4" aria-hidden="true" />
            {t("download")}
          </a>
        </Button>
      )}
    </div>
  );
}
