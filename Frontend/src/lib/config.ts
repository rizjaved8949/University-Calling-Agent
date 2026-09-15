/**
 * Single source of truth for the backend base URL.
 * Reads VITE_API_URL, falls back to http://localhost:8000,
 * and allows a ?api= query override for testing.
 */
const DEFAULT_API = "http://localhost:8000";

function readEnvBase(): string {
  const fromEnv = (import.meta as { env?: Record<string, string | undefined> }).env?.[
    "VITE_API_URL"
  ];
  return (fromEnv && fromEnv.trim()) || DEFAULT_API;
}

function stripTrailingSlash(url: string) {
  return url.replace(/\/+$/, "");
}

export function getApiBase(): string {
  if (typeof window !== "undefined") {
    const override = new URLSearchParams(window.location.search).get("api");
    if (override) return stripTrailingSlash(override);
  }
  return stripTrailingSlash(readEnvBase());
}

export function getWsUrl(path = "/ws/events"): string {
  const base = getApiBase();
  try {
    const url = new URL(base, typeof window !== "undefined" ? window.location.href : undefined);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    url.pathname = stripTrailingSlash(url.pathname) + path;
    url.search = "";
    return url.toString();
  } catch {
    return `ws://localhost:8000${path}`;
  }
}
