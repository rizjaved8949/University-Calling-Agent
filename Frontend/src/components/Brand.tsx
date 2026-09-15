import { cn } from "@/lib/utils";

/**
 * Abstract monogram treatment — deliberately not a reproduction of the
 * official crest. Used in the header and identity areas.
 */
export function BrandMark({ className }: { className?: string }) {
  return (
    <span
      className={cn(
        "inline-flex size-10 shrink-0 items-center justify-center rounded-xl bg-primary text-primary-foreground shadow-soft ring-1 ring-gold/40",
        className,
      )}
      aria-hidden="true"
    >
      <svg viewBox="0 0 32 32" className="size-6" fill="none" role="presentation">
        <path
          d="M6 6v11a10 10 0 0 0 20 0V6"
          stroke="currentColor"
          strokeWidth="2.6"
          strokeLinecap="round"
        />
        <path d="M16 24.5v3.5" stroke="var(--gold)" strokeWidth="2.6" strokeLinecap="round" />
      </svg>
    </span>
  );
}
