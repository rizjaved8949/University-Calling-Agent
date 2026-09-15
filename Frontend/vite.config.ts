import { tanstackStart } from "@tanstack/react-start/plugin/vite";
import viteReact from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { nitro } from "nitro/vite";
import { defineConfig } from "vite";

// Plugin order matters: tanstackStart must run before viteReact.
// No deployment preset is pinned — nitro auto-detects the host at build time
// (Vercel, Netlify, Cloudflare) and falls back to a portable node server
// locally, so `npm run build && npm run preview` works on any machine.
export default defineConfig({
  plugins: [
    tanstackStart({
      // Route the bundled server entry through src/server.ts, our SSR error wrapper.
      server: { entry: "server" },
    }),
    viteReact(),
    tailwindcss(),
    nitro({
      // Security headers belong here rather than in vercel.json: nitro emits the
      // Build Output API v3 routing table, and Vercel ignores vercel.json
      // routing/header rules when a project ships that output.
      routeRules: {
        "/**": {
          headers: {
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "strict-origin-when-cross-origin",
            // microphone=(self) — the browser softphone needs getUserMedia. An
            // empty allowlist disables it document-wide: getUserMedia rejects
            // with NotAllowedError and the permission prompt never appears, so
            // it reads exactly like a user-denied mic with no way to undo it.
            // (self) keeps it to our own origin; embedded frames get nothing.
            "Permissions-Policy": "geolocation=(), camera=(), microphone=(self)",
          },
        },
      },
    }),
  ],
  resolve: {
    // Resolves the "@/*" alias from tsconfig.json (native in Vite 8).
    tsconfigPaths: true,
    // Duplicate copies of these break hooks and router context at runtime.
    dedupe: ["react", "react-dom", "@tanstack/react-router", "@tanstack/react-start"],
  },
  server: {
    port: 5173,
  },
});
