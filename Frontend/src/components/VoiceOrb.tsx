import { useReducedMotion } from "@/lib/theme";
import { cn } from "@/lib/utils";

export type OrbState = "idle" | "agent" | "caller" | "thinking" | "error";

const labels: Record<OrbState, string> = {
  idle: "Listening",
  agent: "Ayesha is speaking",
  caller: "Caller is speaking",
  thinking: "Checking the knowledge base",
  error: "Something went wrong",
};

/**
 * Ayesha's signature presence: a layered radial orb.
 * Colour + a written label always carry the state (never colour alone).
 */
export function VoiceOrb({
  state = "idle",
  size = 220,
  className,
}: {
  state?: OrbState;
  size?: number;
  className?: string;
}) {
  const reduced = useReducedMotion();

  const tint =
    state === "agent"
      ? "var(--gold)"
      : state === "caller"
        ? "var(--primary)"
        : state === "error"
          ? "var(--destructive)"
          : "var(--primary)";

  const duration = state === "agent" ? "1.6s" : state === "caller" ? "2s" : "4.5s";

  return (
    <div className={cn("flex flex-col items-center gap-4", className)}>
      <div
        className="relative grid place-items-center"
        style={{ width: size, height: size }}
        role="img"
        aria-label={labels[state]}
      >
        <span
          className="absolute inset-0 rounded-full opacity-40 blur-2xl transition-colors duration-300"
          style={{ background: `radial-gradient(circle at 50% 45%, ${tint}, transparent 68%)` }}
        />
        <span
          className="absolute rounded-full border border-border/60"
          style={{
            width: size * 0.86,
            height: size * 0.86,
            animation: reduced ? undefined : `orb-breathe ${duration} ease-in-out infinite`,
          }}
        />
        <span
          className="absolute rounded-full shadow-lift transition-[background] duration-300"
          style={{
            width: size * 0.62,
            height: size * 0.62,
            background: `radial-gradient(circle at 35% 30%, color-mix(in oklab, ${tint} 55%, white 45%), ${tint} 62%, color-mix(in oklab, ${tint} 70%, black 30%) 100%)`,
            animation: reduced ? undefined : `orb-breathe ${duration} ease-in-out infinite`,
          }}
        />
        {state === "thinking" && !reduced ? (
          <span
            className="absolute rounded-full opacity-70 mix-blend-screen"
            style={{
              width: size * 0.62,
              height: size * 0.62,
              background: `linear-gradient(100deg, transparent 25%, color-mix(in oklab, var(--gold) 70%, white 30%) 50%, transparent 75%)`,
              backgroundSize: "200% 100%",
              animation: "orb-shimmer 1.8s linear infinite",
            }}
          />
        ) : null}
      </div>
      <p className="text-sm font-medium text-muted-foreground">{labels[state]}</p>
    </div>
  );
}
