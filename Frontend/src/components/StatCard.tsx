import { useEffect, useState } from "react";
import type { LucideIcon } from "lucide-react";
import { Area, AreaChart, ResponsiveContainer } from "recharts";
import { useReducedMotion } from "@/lib/theme";
import { cn } from "@/lib/utils";

function useCountUp(target: number, enabled: boolean) {
  const [value, setValue] = useState(enabled ? 0 : target);
  useEffect(() => {
    if (!enabled) {
      setValue(target);
      return;
    }
    let frame = 0;
    const steps = 24;
    const id = setInterval(() => {
      frame += 1;
      setValue(Math.round((target * frame) / steps));
      if (frame >= steps) clearInterval(id);
    }, 18);
    return () => clearInterval(id);
  }, [target, enabled]);
  return value;
}

export function StatCard({
  icon: Icon,
  label,
  value,
  numeric,
  trend,
  highlight,
  onClick,
  tone = "default",
}: {
  icon: LucideIcon;
  label: string;
  value: string;
  numeric?: number;
  trend?: number[];
  highlight?: boolean;
  onClick?: () => void;
  tone?: "default" | "gold" | "live";
}) {
  const reduced = useReducedMotion();
  const counted = useCountUp(numeric ?? 0, !reduced && numeric !== undefined);
  const display = numeric !== undefined && !reduced ? String(counted) : value;
  const stroke =
    tone === "gold" ? "var(--gold)" : tone === "live" ? "var(--live)" : "var(--primary)";
  const data = (trend && trend.length ? trend : [2, 4, 3, 6, 5, 8, 7]).map((v, i) => ({
    i,
    v,
  }));

  const Wrapper = onClick ? "button" : "div";

  return (
    <Wrapper
      {...(onClick ? { onClick, type: "button" as const } : {})}
      className={cn(
        "surface-panel group relative overflow-hidden p-5 text-left transition-shadow duration-200",
        onClick && "hover:shadow-lift focus-visible:shadow-lift",
        highlight && "ring-2 ring-live/60",
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="truncate text-sm font-medium text-muted-foreground">{label}</p>
          <p className="mt-1 text-[2rem] leading-tight font-semibold tracking-tight text-foreground tabular-nums">
            {display}
          </p>
        </div>
        <span
          className={cn(
            "grid size-9 shrink-0 place-items-center rounded-xl",
            tone === "gold"
              ? "bg-gold/15 text-gold-foreground"
              : tone === "live"
                ? "bg-live/15 text-success"
                : "bg-secondary text-primary",
          )}
        >
          <Icon className="size-4.5" aria-hidden="true" />
        </span>
      </div>
      <div className="mt-3 h-9 opacity-80">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={data} margin={{ top: 2, bottom: 0, left: 0, right: 0 }}>
            <defs>
              <linearGradient id={`spark-${label}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={stroke} stopOpacity={0.35} />
                <stop offset="100%" stopColor={stroke} stopOpacity={0} />
              </linearGradient>
            </defs>
            <Area
              type="monotone"
              dataKey="v"
              stroke={stroke}
              strokeWidth={2}
              fill={`url(#spark-${label})`}
              isAnimationActive={!reduced}
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </Wrapper>
  );
}
