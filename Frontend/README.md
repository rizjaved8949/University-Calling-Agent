# University Voice Agent — Admissions Console

The web console for **Ayesha**, the AI admissions voice agent of the University of Central
Punjab. Admissions staff use it to place and monitor calls, follow a live transcript, review
past calls with recordings, and test the knowledge base without dialling anyone.

Built with React 19, TanStack Start (router + SSR), TanStack Query, Tailwind CSS v4,
shadcn/ui, and Framer Motion.

## Requirements

Node.js 20 or newer, and npm.

## Getting started

```sh
npm install
npm run dev
```

The app starts on <http://localhost:5173> and expects the Python backend on
<http://localhost:8000>.

## Configuration

The backend base URL is resolved in [`src/lib/config.ts`](src/lib/config.ts), in this order:

1. A `?api=https://…` query parameter — handy for pointing a local page at a deployed backend.
2. The `VITE_API_URL` environment variable.
3. `http://localhost:8000`.

Production builds read [`.env.production`](.env.production), which is committed and points at
the Render backend. `npm run dev` ignores that file and falls back to `http://localhost:8000`,
so local development always talks to a local backend. To override without editing code, set
`VITE_API_URL` in the Vercel dashboard.

This is a public URL, never a secret — it is baked into the browser bundle at build time. Every
credential stays on the backend; the browser only ever talks to the endpoints listed below.

## Scripts

| Command             | Purpose                            |
| ------------------- | ---------------------------------- |
| `npm run dev`       | Dev server with HMR                |
| `npm run build`     | Production build                   |
| `npm run preview`   | Serve the production build locally |
| `npm run lint`      | ESLint                             |
| `npm run typecheck` | TypeScript, no emit                |
| `npm run format`    | Prettier                           |

## Backend contract

REST, all under the configured base URL:

| Method | Path                       | Purpose                          |
| ------ | -------------------------- | -------------------------------- |
| GET    | `/api/config`              | Agent number and readiness flags |
| GET    | `/api/calls/stats`         | Dashboard counters               |
| GET    | `/api/calls?direction=`    | Call list                        |
| GET    | `/api/calls/:id`           | Single call                      |
| POST   | `/api/calls/outbound`      | Place a call                     |
| POST   | `/api/calls/:id/end`       | Hang up                          |
| GET    | `/api/calls/:id/recording` | Recording audio                  |
| POST   | `/api/chat`                | Knowledge-base chat              |

Live updates arrive over a WebSocket at `/ws/events`. The backend tags each frame with a
`kind` field and camelCase keys; [`src/lib/normalize.ts`](src/lib/normalize.ts) adapts both the
socket frames and the REST payloads into the shapes the UI components consume, so the rest of
the codebase sees one consistent model.

## Deployment (Vercel)

This app server-renders, so it does **not** deploy as a static site. Nitro detects Vercel from
its build environment and emits Build Output API v3 into `.vercel/output` — a static asset
folder plus one serverless function for SSR. Vercel picks that up automatically.

Import the repository on Vercel and set:

| Setting | Value |
| --- | --- |
| Root Directory | `frontend` |
| Framework Preset | Other |
| Build Command | `npm run build` (from [`vercel.json`](vercel.json)) |
| Output Directory | leave empty — `.vercel/output` is detected |

`VITE_API_URL` only needs setting in the dashboard if you want to override
[`.env.production`](.env.production).

Security headers (`X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy`,
`Permissions-Policy`) are declared as nitro `routeRules` in
[`vite.config.ts`](vite.config.ts), not in `vercel.json` — Vercel ignores `vercel.json` routing
and header rules for projects that ship Build Output API v3, so they would silently do nothing
there. `Permissions-Policy` allows `microphone=(self)` — the browser softphone on
[`/manual`](src/routes/manual.tsx) needs `getUserMedia`, and an empty allowlist blocks the
permission prompt outright.

### After the first deploy

Add the Vercel URL to the backend's `FRONTEND_URL` environment variable on Render (it is a
comma-separated CORS allowlist). Until you do, the browser will block every request and the
console will sit at "Couldn't reach the phone service".

To pin the SSR function nearer to Pakistan, add `"regions": ["bom1"]` (Mumbai) to
`vercel.json`.
