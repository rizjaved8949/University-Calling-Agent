import { useEffect, useState } from "react";
import { Link, useRouterState } from "@tanstack/react-router";
import {
  Copy,
  Headphones,
  Home,
  Languages,
  MessageCircleQuestion,
  Moon,
  Phone,
  Sun,
  History as HistoryIcon,
} from "lucide-react";
import { toast } from "sonner";
import { BrandMark } from "@/components/Brand";
import { Button } from "@/components/ui/button";
import { onNetworkBusy } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { useRealtime } from "@/lib/realtime";
import { useTheme } from "@/lib/theme";
import { prettyNumber } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { TKey } from "@/lib/i18n";

const NAV: { to: string; key: TKey; icon: typeof Home }[] = [
  { to: "/", key: "home", icon: Home },
  { to: "/call", key: "call", icon: Phone },
  { to: "/manual", key: "manual", icon: Headphones },
  { to: "/history", key: "history", icon: HistoryIcon },
  { to: "/ask", key: "ask", icon: MessageCircleQuestion },
];

function ConnectionPill() {
  const { state } = useRealtime();
  const { t } = useI18n();
  const map = {
    live: { label: t("live"), dot: "bg-live", text: "text-success" },
    reconnecting: { label: t("reconnecting"), dot: "bg-warning", text: "text-warning" },
    offline: { label: t("offline"), dot: "bg-destructive", text: "text-destructive" },
  } as const;
  const s = map[state];
  return (
    <span
      className="inline-flex items-center gap-2 rounded-full border border-border bg-card px-3 py-1.5 text-xs font-medium"
      aria-live="polite"
    >
      <span className="relative flex size-2">
        {state === "live" ? (
          <span className="absolute inline-flex size-2 rounded-full bg-live [animation:live-ping_1.8s_ease-out_infinite]" />
        ) : null}
        <span className={cn("relative inline-flex size-2 rounded-full", s.dot)} />
      </span>
      <span className={s.text}>{s.label}</span>
    </span>
  );
}

function GlobalProgress() {
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const off = onNetworkBusy(setBusy);
    return () => {
      off();
    };
  }, []);
  return (
    <div className="fixed inset-x-0 top-0 z-50 h-0.5" aria-hidden="true">
      <div
        className={cn(
          "h-full bg-gold transition-[width,opacity] duration-500 ease-out",
          busy ? "w-2/3 opacity-100" : "w-full opacity-0",
        )}
      />
    </div>
  );
}

export function AppShell({
  children,
  agentNumber,
}: {
  children: React.ReactNode;
  agentNumber?: string;
}) {
  const { t, lang, setLang } = useI18n();
  const { theme, toggle } = useTheme();
  const pathname = useRouterState({ select: (s) => s.location.pathname });

  const copyNumber = async () => {
    if (!agentNumber) return;
    await navigator.clipboard.writeText(agentNumber);
    toast.success(t("copied"));
  };

  return (
    <div className="min-h-screen bg-background" dir="ltr">
      <GlobalProgress />
      <header className="sticky top-0 z-40 border-b border-border bg-card/90 backdrop-blur">
        <div className="mx-auto flex max-w-7xl items-center gap-3 px-4 py-3 sm:px-6">
          <BrandMark />
          <div className="min-w-0 flex-1">
            <p className="truncate text-[15px] leading-tight font-semibold text-foreground">
              {t("appName")}
            </p>
            <p className="truncate text-xs text-muted-foreground">{t("university")}</p>
          </div>

          {agentNumber ? (
            <button
              type="button"
              onClick={copyNumber}
              className="hidden items-center gap-2 rounded-full border border-border bg-secondary px-3 py-1.5 text-xs font-medium text-secondary-foreground transition-colors hover:bg-accent md:inline-flex"
              aria-label={`${t("agentNumber")} ${prettyNumber(agentNumber)}`}
            >
              <Phone className="size-3.5" aria-hidden="true" />
              {prettyNumber(agentNumber)}
              <Copy className="size-3.5 opacity-60" aria-hidden="true" />
            </button>
          ) : null}

          <ConnectionPill />

          <Button
            variant="ghost"
            size="sm"
            onClick={() => setLang(lang === "en" ? "ur" : "en")}
            aria-label="Switch language"
            className="gap-1.5"
          >
            <Languages className="size-4" aria-hidden="true" />
            <span className={lang === "en" ? "font-urdu text-base" : ""}>{t("language")}</span>
          </Button>

          <Button variant="ghost" size="icon" onClick={toggle} aria-label={t("theme")}>
            {theme === "dark" ? <Sun className="size-4" /> : <Moon className="size-4" />}
          </Button>
        </div>
      </header>

      <div className="mx-auto flex w-full max-w-7xl gap-6 px-4 sm:px-6">
        <nav aria-label="Main" className="hidden w-52 shrink-0 py-6 md:block">
          <ul className="sticky top-24 space-y-1">
            {NAV.map(({ to, key, icon: Icon }) => {
              const active = to === "/" ? pathname === "/" : pathname.startsWith(to);
              return (
                <li key={to}>
                  <Link
                    to={to}
                    className={cn(
                      "flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition-colors",
                      active
                        ? "bg-primary text-primary-foreground shadow-soft"
                        : "text-muted-foreground hover:bg-accent hover:text-foreground",
                    )}
                  >
                    <Icon className="size-4.5" aria-hidden="true" />
                    {t(key)}
                  </Link>
                </li>
              );
            })}
          </ul>
        </nav>

        <main className="min-w-0 flex-1 py-6 pb-28 md:pb-10">{children}</main>
      </div>

      <nav
        aria-label="Main"
        className="fixed inset-x-0 bottom-0 z-40 border-t border-border bg-card/95 backdrop-blur md:hidden"
      >
        <ul className="mx-auto flex max-w-lg">
          {NAV.map(({ to, key, icon: Icon }) => {
            const active = to === "/" ? pathname === "/" : pathname.startsWith(to);
            return (
              <li key={to} className="flex-1">
                <Link
                  to={to}
                  className={cn(
                    "flex min-h-16 flex-col items-center justify-center gap-1 text-xs font-medium transition-colors",
                    active ? "text-primary" : "text-muted-foreground",
                  )}
                >
                  <Icon className={cn("size-5", active && "text-primary")} aria-hidden="true" />
                  {t(key)}
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>
    </div>
  );
}
