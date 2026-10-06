# VoxOps — UI/UX & Frontend Build Spec

**A complete, paste-ready build brief for the multi-tenant frontend of a voice-agent
SaaS — marketing site, auth screens, onboarding, dashboards, settings and docs hub.**

Derived from two working single-tenant systems: an Infobip/PSTN voice agent and a
WhatsApp Business Calling agent.

---

## 0. Scope — read this first

**This build is frontend only. No backend, no database, no server code, no cloud
services, no authentication provider.**

Everything is a React single-page application with realistic in-repo fixture data. The
app looks, feels and behaves exactly like the finished product — forms validate, tables
filter, transcripts stream, toasts fire, the onboarding wizard completes — but nothing
leaves the browser. There is no schema to migrate and no credential ever actually
stored.

Concretely, in scope and out:

| In scope | Out of scope |
|---|---|
| Every screen, state and interaction | Any server, API, or serverless function |
| Design system, theming, light/dark, RTL | Any database or schema |
| TypeScript types for every entity | Any real auth provider or OAuth |
| Fixture data store with realistic seeds | Any real credential storage |
| Simulated latency, loading, error states | Any telephony, WhatsApp or model calls |
| A typed API layer with one swappable adapter | Implementing the other adapter |

The one architectural concession to the future is in §4: all data access goes through a
typed API layer with a mock adapter, so that when a backend eventually exists it is
wired in one place rather than unpicked from forty components.

### Technology

React + TypeScript + Vite + Tailwind CSS + shadcn/ui. Client-side routing. Charts via
Recharts. Icons via Lucide. No other dependency unless a prompt names it.

### How to use this document

Do not paste it all at once. An AI app builder given one five-thousand-word
instruction builds everything shallowly and then fights itself on every subsequent
edit. It performs far better given a tight foundation prompt followed by additive
screen-by-screen prompts that reference names already established.

So:

1. Paste **Prompt 1** from §6. Let it finish completely. Change nothing yet.
2. Paste **Prompt 2**, then **3**, and so on, in order, one at a time.
3. Between prompts, correct anything that is wrong *then* — not three prompts later.

Sections §1–§5 are reference material you do not paste. They are what the prompts were
built from, and what you consult when the builder asks a question or goes astray.

---

## 1. What the product is

Companies sign up, upload their knowledge base, write their agent's persona, connect
their own telephony and WhatsApp credentials, and get a working voice agent that
answers and places calls — on a phone number, on WhatsApp, or both — plus WhatsApp
text messaging. The platform ships the guides and credential walkthroughs that make
setup self-serve.

Working name throughout: **VoxOps**. Replace it if you have a real name.

### The two systems being generalised

| | Phone build | WhatsApp build |
|---|---|---|
| Channel | PSTN — a real phone number | WhatsApp Business Calling |
| Media | Carrier dials the model over SIP; audio never enters the app | WebRTC terminated locally and bridged to the model |
| Extras | Spreadsheet and Drive export, post-call summaries, reference tokens, outbound WhatsApp follow-up, a second model engine | Human-counselor takeover from the browser, call-permission requests, template messaging |

Both share one design the UI must reflect: **retrieval is a tool, not a pipeline
stage.** The carrier bridges the caller straight to the speech-to-speech model, which
is handed a `search_knowledge_base` function; the service answers that function with
real embedding search over the tenant's documents. That is why responses land near
300ms, and it is why the UI must treat *retrieval events* as first-class live
telemetry. A tenant debugging "why did my agent say it didn't know" needs to see the
query, the score and the threshold — a transcript alone cannot tell them.

### Multi-tenancy is the whole job

The two existing systems configure one customer through roughly 110 environment
variables. This product turns every one of them into a tenant-scoped, UI-editable
setting with a sane default and an explanation. §3 is that taxonomy. **The settings
surface is not a side feature here — it is the product.**

---

## 2. Entities and fixture data

These are TypeScript types and seeded fixtures in the repo. No database. Define them
in `src/lib/types.ts` and seed them in `src/lib/fixtures/`.

```ts
Organization      id, name, slug, logoUrl, accentColor, plan, countryCode,
                  defaultLanguage, timezone, createdAt, trialEndsAt,
                  onboardingStep
Profile           id, fullName, email, avatarUrl
Membership        orgId, userId, role: 'owner'|'admin'|'operator'|'viewer', status
Invitation        orgId, email, role, expiresAt, status

Agent             id, orgId, name, avatarUrl, status: 'draft'|'live'|'paused',
                  personaId, knowledgeBaseId, voiceId, channels[], createdAt
Persona           id, orgId, agentName, gender, greeting, roleDescription,
                  languagePolicy, toneNotes, forbiddenPhrases[], escalationRules,
                  closingBehaviour, compiledPrompt (derived, read-only),
                  promptOverride?
Voice             id, orgId, voiceName, speed, language, sttModel, noiseReduction,
                  turnDetection, vadThreshold, vadSilenceMs, vadPrefixPaddingMs,
                  vadEagerness, interruptionEnabled, greetingDelaySeconds

KnowledgeBase     id, orgId, name, status: 'empty'|'indexing'|'ready'|'failed',
                  chunkSize, chunkOverlap, embeddingModel, similarityThreshold,
                  topK, maxChunksPerSection, maxContextChars, chunkCount,
                  lastIndexedAt, indexError
KbDocument        id, kbId, filename, mime, bytes, pages, status, uploadedBy,
                  chunkCount, error
KbChunk           id, kbId, documentId, section, title, page, category, preview, score

Channel           id, orgId, type: 'sim'|'whatsapp_call'|'whatsapp_message',
                  status: 'disconnected'|'pending'|'verifying'|'connected'|'error',
                  displayNumber, provider, lastCheckedAt, errorDetail, config
Credential        id, orgId, channelId, keyName, lastFour, setBy, setAt
                  — metadata only; see §4

Call              id, orgId, agentId, channelType, direction: 'INBOUND'|'OUTBOUND',
                  phoneNumber, fromNumber, toNumber, status, mode: 'agent'|'operator',
                  startedAt, answeredAt, endedAt, durationSeconds, outcome, summary,
                  recordingState: 'RECORDING'|'PENDING'|'READY'|'NONE',
                  recordingScope: 'two_way'|'caller_only'|'agent_only'|'none',
                  recordingUrl, operatorNumber, lastEvent, error
TranscriptEntry   id, callId, role, speaker, text, timestamp
RagQuery          id, callId, query, topScore, threshold, hitCount, answered,
                  sections[], timestamp

Message           id, orgId, callId?, toNumber, templateId?, body, status, sentAt
WaTemplate        id, orgId, name, language, category,
                  status: 'approved'|'pending'|'rejected', bodyPreview, variables[]

UsageEvent        id, orgId, kind: 'call_minute'|'message'|'storage', quantity,
                  occurredAt
Plan              id, name, priceMonthly, includedMinutes, includedMessages,
                  agentLimit, seatLimit, storageMb, features[]
ApiKeyMeta        id, orgId, name, lastFour, scopes[], createdBy, lastUsedAt
WebhookEndpoint   id, orgId, url, events[], secretLastFour, status
AuditEntry        id, orgId, actorName, action, targetType, targetId, createdAt
Guide             slug, title, category, channel, bodyMd, readingMinutes, updatedAt
```

### Enums the UI must honour exactly

- **`recordingState`** — `RECORDING` while the call is live, `PENDING` while the
  provider composes the file, `READY` when playable, `NONE` when there is no audio at
  all. The player must say *"this call was never recorded"* for `NONE` rather than
  offering a retry that can never succeed. This was a real bug fixed once already in
  the source systems; do not reintroduce it.
- **`Channel.status`** — five states, each with distinct UI: `disconnected` (empty,
  with a call to action), `pending` (credentials entered, not yet verified),
  `verifying` (in progress), `connected` (green, shows the number), `error` (red,
  shows `errorDetail` verbatim plus a link to the matching guide).
- **`mode`** — `agent` versus `operator`. History must filter on it, and a call detail
  view must say plainly whether the agent or a human handled it.

### Fixture realism

Seed enough data that every screen looks alive and every edge case is reachable:
about 400 calls spread over 90 days across both channels and both modes, a mix of
outcomes, several calls with `recordingState: 'NONE'` and several `PENDING`, two or
three agents in different statuses, one knowledge base with eight documents including
one `failed`, all five channel statuses represented, approved and pending and rejected
templates, and usage that puts one plan limit past 80%.

---

## 3. The settings taxonomy

This is the ~110 environment variables of the source systems, reorganised into tabs a
tenant can reason about. Every field needs a default, a one-line explanation, and —
where given below — the *reason* shown as helper text. Those reasons are not
decoration; they are the difference between a tenant choosing a sane value and filing
a support ticket.

### Agent → Persona
`agentName`, `gender` (drives grammatical-agreement rules in the compiled prompt for
languages that inflect verbs by speaker gender), `greeting` (spoken verbatim as the
first line of every call), `roleDescription`, `languagePolicy`, `toneNotes`,
`forbiddenPhrases[]`, `escalationRules`, `closingBehaviour`.

A live preview pane renders the compiled prompt read-only beside the form. An
**Advanced** disclosure allows overriding it wholesale, with a clear warning that this
detaches it from the form.

### Agent → Voice & speech
`voiceName`, `speed` — default `0.95`, *helper text: "long vowels can clip at 1.0
through a narrowband phone codec"* — `language`, `sttModel` (*leaving it blank disables
transcripts*), `noiseReduction`, `turnDetection`, `vadThreshold`, `vadSilenceMs`,
`vadPrefixPaddingMs`, `vadEagerness`, `interruptionEnabled`, `greetingDelaySeconds`.

### Agent → Model
`realtimeModel`, `realtimeMaxOutputTokens`, `realtimeReasoningEffort`, then separately
the text model used by the chat feature: `llmModel`, `llmTemperature`, `llmMaxTokens`.
Then `summaryEnabled`, `summaryModel`.

### Knowledge base
Upload zone (PDF/DOCX/TXT/MD with a max size), document list with per-document status
and chunk count, a re-index action, and the retrieval controls: `chunkSize`,
`chunkOverlap`, `embeddingModel`, `similarityThreshold` — default `0.60`, *helper text:
"embedding models sit high on the cosine scale; unrelated text still scores around
0.52 while genuine matches land at 0.70 and above. Below this line the agent says it
does not know rather than inventing an answer."* — `topK`, `maxChunksPerSection`,
`maxContextChars`.

Plus a **Test search** panel: type a question, see ranked chunks with scores, with the
threshold drawn as a line across the results. This is the most useful debugging tool in
the product. Build it properly.

### Channels → Phone (SIM / PSTN)
Provider picker, structured so more can be added. Fields: API key, base URL,
application ID, calls configuration ID, phone number, SIP trunk ID, WebRTC application
ID, media stream config name. Then call behaviour: `maxCallDurationSeconds`,
`callConnectTimeoutSeconds`, `wrapUpWarningSeconds`, `hangupGraceSeconds`.

### Channels → WhatsApp calling
App ID, app secret (*verifies every webhook's HMAC-SHA256 signature; without it the
service refuses all webhooks*), Graph API version, access token, business account ID,
phone number ID, business phone number in E.164, and a webhook verify token the tenant
chooses themselves.

Capability flags, **every one defaulting off** so that saving credentials alone can
never start answering or placing calls — state that on the page: `callingEnabled`,
`callingAutoAccept`, `preAcceptCalls`, `mediaRelayEnabled` (*required for outbound*),
`outboundEnabled`, `operatorCallingEnabled`, `recordTwoWay`, `recordAgentAudio`.

Advanced media: `mediaSampleRate` (24000), STUN URL, TURN URL, TURN username, TURN
credential, `mediaConnectTimeoutSeconds`, `outboundAnswerTimeoutSeconds`. *Helper text
on TURN: "without it, media fails behind strict NAT even when signaling succeeds."*

### Channels → WhatsApp messaging
Template manager with approval status, body preview and variable mapping. Plus
`defaultTemplate`, `detailsTemplate`, `messageLanguage`, `countryCode`,
`inboundEnabled`, `topicMaxChars`, `detailsMaxChars`.

The UI must make the dependency visible: an unapproved template means the agent is
**not offered that tool at all**, so it never promises a message that cannot be sent.
Render that as a disabled capability stating the reason, never as a silent failure.

### Recording & storage
`recordCalls`, `recordingType`, `recordAgentAudio`, `recordFromMediaStream`,
`recordBrowserClientSide`, `recordingGraceSeconds`, `recordingTailWaitSeconds`,
`transcodeRecordings`, `transcodeTimeoutSeconds`, `recordingFilePrefix`,
`transcriptStoreEnabled`, retention period, archive target and signed-URL lifetime.

### Integrations & export
Drive/Sheets sync (client ID, client secret, refresh token, folder ID, sync toggle,
sync delay), spreadsheet export filename, outbound webhook endpoints, API keys.

### Organisation
Name, slug, logo, accent colour, default country code (*expands a nationally formatted
number such as `03191611020` into E.164 before it reaches a provider, which accepts
nothing else*), default language, timezone, operator phone number, log level.

---

## 4. Four constraints that shape the build

### 4.1 One typed API layer, one adapter

All data access goes through `src/lib/api/`. Define one function per logical endpoint.
Each reads a single exported `USE_MOCK` constant: when `true` — which is the only mode
this build ships — it returns seeded fixtures after a realistic delay of 150–600ms,
occasionally simulating a failure so error states are reachable. The `false` branch is
a stub that throws "not implemented".

No component ever calls `fetch` directly, and no component imports a fixture directly.
This is the single most important instruction in the document: it is what lets a
backend be attached later in one file instead of being unpicked from forty components.

### 4.2 Credentials are write-only, even with nothing behind them

A tenant pastes provider secrets into a browser form. Even in a frontend-only build
the UI must behave as the real one will:

- No secret is ever rendered back. An already-set credential shows as
  `•••• ••••  ·  ····7f3a` with a **Replace** action — never a prefilled password
  input with a reveal toggle.
- Submitting stores only metadata in the fixture store: key name, last four
  characters, who set it, when. **The secret value itself is discarded immediately and
  written nowhere** — not to the fixture store, not to `localStorage`, not to a log.
- Each credential change appends an audit entry.

Build it this way from the first prompt. Retrofitting write-only semantics onto a form
that already round-trips values is a rewrite.

### 4.3 Readiness is computed, and flags default off

The source systems derive a set of readiness booleans and then hide or disable features
accordingly, rather than offering something that fails. Keep exactly that: a single
`useOrgReadiness()` hook feeds every disabled state, every empty state and the
onboarding checklist. Every disabled control carries a tooltip naming the missing
prerequisite and linking to its guide. **Never offer an action that cannot succeed.**

### 4.4 Simulated session, full screens

Auth is a client-side simulation: a session object in React context, persisted to
`localStorage` so a refresh keeps you signed in. Any email and any password of six or
more characters signs you in. Build the complete set of screens anyway — sign-up,
log-in, forgot password, verification pending, accept invitation — because they are
part of the deliverable even though nothing authenticates. A "sign in with Google"
button is present and simulates success.

Organisation switching, roles and permission-gated UI are all real behaviours driven by
the fixture store.

---

## 5. Design direction

The source application uses a design system called *Institutional Warmth*: a deep
institutional blue anchor, one warm gold accent, generous whitespace, oklch colour
tokens throughout, `Plus Jakarta Sans` for Latin text and `Noto Nastaliq Urdu` for
Urdu, with two elevation shadows instead of borders. It is good — but it is one
university's palette.

For a multi-tenant product: keep the structure, neutralise the hue, and make the accent
a per-tenant token. A deep slate-indigo primary, neutral surfaces, and an `--accent`
read at runtime from the active organisation's `accentColor`, so a tenant's workspace
carries their colour without a rebuild. The gold stays as the platform's own marketing
accent.

Non-negotiables:

- **oklch tokens only.** No component hardcodes a colour, ever.
- **Light and dark both first-class**, defined on `:root`, under a `[data-theme]`
  override, and under a `prefers-color-scheme` media query. `body` gets an explicit
  background.
- **Bilingual and RTL-aware from the start.** Urdu renders in the Urdu font; the layout
  survives `dir="rtl"` on every screen. All copy goes through an i18n provider with
  `en` and `ur`. Adding this later means touching every component.
- **A live state is a real state.** `live` / `reconnecting` / `offline` as a persistent
  pill in the shell, with `aria-live="polite"`.
- Semantic tokens beyond the defaults: `--success`, `--warning`, `--live`, `--surface`,
  and `--chart-1` through `--chart-5`.
- Calm density. This is an operations tool people stare at for hours — no gradient
  washes, no neon, no decorative illustration.

---

## 6. The prompts

### Prompt 1 — Foundation: design system, i18n, data layer, auth screens, shell

```
Build the foundation of VoxOps, a multi-tenant SaaS where companies create and run AI
voice agents that answer and place real phone and WhatsApp calls, grounded in their
own uploaded knowledge base.

IMPORTANT SCOPE: this is a frontend-only build. Do not create any backend, database,
server function, or cloud integration. No authentication provider. Everything runs in
the browser against realistic fixture data defined in the repo.

This prompt is foundation only. Do not build feature screens yet — later prompts add
them. Build these six things well instead.

1. DESIGN SYSTEM
A token-based design system in the global stylesheet. All colours are oklch; no
component ever hardcodes a colour.
- Primary: deep slate-indigo — professional, calm, suitable for white-labelling.
- Platform accent: warm gold, used sparingly.
- Tenant accent: an --accent token overwritten at runtime from the active
  organisation's accent colour, so each tenant's workspace carries their own colour.
- Semantic tokens: --success, --warning, --destructive, --live, --surface, --card,
  --muted, --border, --ring, and --chart-1 through --chart-5.
- Two elevation shadows, --shadow-soft and --shadow-lift; prefer them over borders.
- A radius scale derived from a single --radius of 0.875rem.
- Fonts: "Plus Jakarta Sans" for Latin, "Noto Nastaliq Urdu" for Urdu, "JetBrains
  Mono" for numbers, IDs, phone numbers and code.
- Full light AND dark themes on :root, under [data-theme="dark"], and under a
  prefers-color-scheme media query. body gets an explicit background.
Generous whitespace, calm density, no gradient-heavy or neon styling.

2. INTERNATIONALISATION, FROM THE START
An i18n provider with English and Urdu. Every visible string comes from it — no bare
literals in components. Urdu renders in the Urdu font. The entire layout must survive
dir="rtl" without breaking. A language toggle sits in the app header.

3. TYPES AND FIXTURES
Define TypeScript types for every entity in src/lib/types.ts: Organization, Profile,
Membership, Invitation, Agent, Persona, Voice, KnowledgeBase, KbDocument, KbChunk,
Channel, Credential, Call, TranscriptEntry, RagQuery, Message, WaTemplate, UsageEvent,
Plan, ApiKeyMeta, WebhookEndpoint, AuditEntry, Guide.
Seed realistic fixtures in src/lib/fixtures/: about 400 calls over 90 days across both
channels and both agent and operator modes, mixed outcomes, several with no recording
and several still composing; three agents in different statuses; one knowledge base
with eight documents including one failed; every channel status represented; approved,
pending and rejected message templates; and usage that puts one plan limit past 80%.

4. TYPED DATA LAYER — the most important instruction here
All data access goes through src/lib/api/, one function per logical endpoint. Each
checks a single exported USE_MOCK constant: when true it returns fixture data after a
realistic 150–600ms delay and occasionally simulates a failure so error states are
reachable. The false branch is a stub that throws "not implemented".
No component may call fetch directly, and no component may import a fixture directly.
Mutations update an in-memory store so the app feels real across navigation.

5. SIMULATED AUTH AND TENANCY
A session in React context, persisted to localStorage so a refresh stays signed in.
Any email with a password of six or more characters signs in. A "sign in with Google"
button simulates success. No real auth provider.
Build the full screens anyway, matching the design system — a centred card, the product
mark, one sentence of value proposition, no stock illustration: sign up, log in, forgot
password, verification pending, accept invitation.
A user can belong to several organisations and switch between them from a workspace
switcher at the top of the sidebar. Roles are owner, admin, operator and viewer, and
permission-gated UI is a real behaviour driven by the fixture store. Protected routes
redirect to log in.

6. APPLICATION SHELL
A collapsible left sidebar with the workspace switcher at the top and these nav items,
each a stub page with a titled empty state for now:
  Dashboard · Agents · Knowledge · Channels · Live · History · Messages ·
  Guides · Team · Usage · Settings
A top bar with: breadcrumb, a connection-state pill showing live / reconnecting /
offline with aria-live="polite", a thin global progress bar for in-flight requests, the
language toggle, a theme toggle, and a user menu.
Mobile: the sidebar becomes a sheet; the layout works at 375px with 16px gutters and no
horizontal scroll.

Finally, a public marketing landing page at / for signed-out visitors: a hero stating
that companies can launch an Urdu- and English-speaking voice agent on their own phone
number and WhatsApp in an afternoon; a three-step how-it-works; a channel comparison of
phone versus WhatsApp calling versus WhatsApp messaging; a pricing section with three
tiers; and a footer. Signed-in users visiting / go to the dashboard.
```

### Prompt 2 — Onboarding wizard, readiness model, dashboard

```
Add the tenant onboarding flow, the readiness model the whole app depends on, and the
dashboard. Still frontend-only — no backend, no database.

READINESS
A useOrgReadiness() hook returning computed booleans: knowledgeBaseReady,
phoneChannelReady, whatsappCallingReady, whatsappMessagingReady, operatorCallingReady,
personaReady, agentLive. Every capability flag in this product defaults to OFF, so
entering credentials alone must never make an agent start answering calls.
Every disabled control anywhere in the app uses this hook and carries a tooltip naming
the missing prerequisite with a link to the relevant guide. Never offer an action that
cannot succeed.

ONBOARDING WIZARD
Six steps at /onboarding, resumable — it remembers the furthest step reached and can be
re-entered from the dashboard checklist.
1. Organisation — name, logo upload, accent colour picker, country code, default
   language, timezone.
2. Create your agent — name, gender, and a greeting that will be spoken verbatim as the
   first line of every call.
3. Knowledge base — drag-and-drop upload of PDF/DOCX/TXT/MD with per-file progress,
   then a simulated indexing state showing documents parsed, chunks produced, and a
   ready or failed outcome.
4. Choose channels — three large selectable cards: Phone number (SIM/PSTN), WhatsApp
   calling, WhatsApp messaging. Multi-select. Each states which credentials it will
   require and roughly how long setup takes.
5. Connect credentials — only the forms for the channels chosen in step 4. Every secret
   field is WRITE-ONLY: never prefilled, no reveal toggle. An already-set credential
   renders as masked dots plus its last four characters with a Replace button. On
   submit, store only that metadata and discard the secret value immediately — write it
   nowhere, not even to localStorage. Beside each form, an inline collapsible
   walkthrough of where to find that value in the provider's dashboard, with a link to
   the full guide.
6. Test and go live — a readiness checklist where each item is either green or states
   exactly what is missing; a test search against the knowledge base; a test call
   button; and a final Go live switch disabled until the prerequisites pass.

DASHBOARD
Replace the stub. For an organisation that has not finished onboarding, the primary
content is the resumable setup checklist. For a live organisation: stat tiles for calls
today, talk time, answer rate, active calls now and messages sent; a 14-day call volume
chart split by channel; a live-calls strip; a recent-calls table; and a channel health
row showing each connected channel's status.
```

### Prompt 3 — Agents, persona builder, voice and model settings

```
Build the Agents section. Frontend-only, against the fixture store.

/agents — a list of agent cards: avatar, name, status badge (draft, live or paused),
channel icons, a calls-this-week sparkline, and quick actions. Plus a New agent button
and a designed empty state.

/agents/:id — tabs: Persona, Voice & speech, Model, Knowledge, Channels, Test.

PERSONA TAB — the centrepiece. Two columns: a structured form on the left, and on the
right a live read-only preview of the compiled system prompt that updates as the form
changes. Fields: agent name, gender, greeting, role description, language policy, tone
notes, forbidden phrases as a tag input, escalation rules, closing behaviour. Include
gender because it drives grammatical-agreement rules in the compiled prompt for
languages that inflect verbs by speaker gender — explain that inline.
Offer starting templates: Admissions, Customer support, Collections, Appointment
booking, Lead qualification.
An Advanced disclosure allows overriding the compiled prompt wholesale, with a clear
warning that this detaches it from the form above and the form will no longer update it.

VOICE & SPEECH TAB — a voice picker with a preview play button; a speed slider
defaulting to 0.95 with the helper text "long vowels can clip at 1.0 through a
narrowband phone codec"; language; a transcription model field noting that leaving it
blank disables transcripts; noise reduction. Then a collapsed "Advanced turn-taking"
group — turn detection mode, VAD threshold, silence milliseconds, prefix padding,
eagerness, interruption enabled, greeting delay — because most tenants should never
need to touch these.

MODEL TAB — the realtime voice model, max output tokens and reasoning effort; then a
separate group for the text model used by the chat feature with its temperature and max
tokens; then post-call summary enabled and the summary model.

TEST TAB — a simulated text chat against this agent's knowledge base, each reply
showing its cited sources with section, title, page and similarity score; plus a button
to place a test call to a number you type, which runs a scripted mock call.

Every form autosaves with an explicit saved indicator, and shows a confirmation naming
what will change when the edit affects an agent whose status is live.
```

### Prompt 4 — Knowledge base and test search

```
Build the Knowledge section. Frontend-only; indexing is simulated with progress states.

/knowledge — overview: status (empty, indexing, ready or failed), document count, total
chunks, last indexed time, and a prominent Re-index action that warns it will rebuild
all embeddings.

DOCUMENTS — a drag-and-drop upload zone, then a table of documents with filename, type
icon, size, page count, chunk count, uploaded-by and per-document status. A failed
document shows its error verbatim, never a generic message. Rows can be previewed,
re-indexed or deleted; deletion warns that the agent will immediately stop being able to
answer from that document.

RETRIEVAL SETTINGS — chunk size, chunk overlap, embedding model, top K, max chunks per
section, max context characters, and the similarity threshold. The threshold gets a
slider defaulting to 0.60 with this helper text: "Embedding models sit high on the
cosine scale — unrelated text still scores around 0.52, while genuine matches land at
0.70 and above. Below this line the agent says it does not know rather than inventing an
answer. Raise it if the agent answers things it shouldn't; lower it if it refuses things
it should know."

TEST SEARCH — build this properly; it is the most valuable debugging tool in the
product. A query box. Results as ranked cards showing section, title, page, category,
similarity score and a text excerpt. The threshold drawn as a labelled horizontal line
across the ranked list, so it is instantly visible which results would have been used
and which were discarded. And a verdict line stating whether the agent would have
answered or declined, and why.
```

### Prompt 5 — Channels and credential forms

```
Build the Channels section — where tenants connect their own provider accounts.
Frontend-only: no credential is ever stored or transmitted (see the rule below).

/channels — a card per channel type: Phone number (SIM/PSTN), WhatsApp calling,
WhatsApp messaging. Each shows a status chip with five distinct states — disconnected,
pending, verifying, connected, error — plus the display number when connected, the last
checked time, and a Test connection action that runs a simulated check. An error card
shows the provider's error detail verbatim and links to the matching troubleshooting
guide.

CREDENTIAL FORMS — one page per channel. THE RULE: every secret input is write-only.
Never prefilled, no reveal toggle. An already-set secret renders as masked dots plus its
last four characters, with who set it, when, and a Replace button. On submit, keep only
that metadata in the store and discard the secret value immediately — write it nowhere,
including localStorage. Nothing in the app can read a secret back.
Beside every field group, an inline collapsible walkthrough of exactly where to find
that value in the provider's dashboard, with a link to the full guide.

Phone channel: provider picker structured so more can be added, API key, base URL,
application ID, calls configuration ID, phone number, SIP trunk ID, WebRTC application
ID, media stream config name. Then call behaviour — max call duration, connect timeout,
wrap-up warning seconds, hangup grace seconds.

WhatsApp calling: app ID, app secret (note that it verifies every webhook's HMAC
signature and that without it the service refuses all webhooks), Graph API version,
access token, business account ID, phone number ID, business phone number in E.164, and
a webhook verify token the tenant chooses themselves. Display the webhook URL they must
paste into the provider's dashboard, with a copy button.
Then a Capabilities panel of toggles, every one defaulting OFF, under a banner stating
that entering credentials alone never starts answering or placing calls: accept inbound
calls, auto-answer with the agent, pre-accept calls, media relay enabled (marked as
required for outbound), outbound calling enabled, human-operator calling enabled, record
both sides, record agent audio.
Then an Advanced media group: sample rate, STUN URL, TURN URL, TURN username, TURN
credential, connect timeout, outbound answer timeout — noting that without TURN, media
fails behind strict NAT even when signaling succeeds.

WhatsApp messaging: a template manager listing templates with approval status, body
preview and variable mapping. Make the dependency explicit — an unapproved template
means the agent is not offered that tool at all, so it never promises a message that
cannot be sent. Render that as a disabled capability stating the reason, never as a
silent failure.

Every credential change appends an audit entry.
```

### Prompt 6 — Live monitoring, call history, messages

```
Build the Live, History and Messages sections. Frontend-only: realtime behaviour comes
from a fixture event generator on a timer inside the data layer, not from a socket.

/live — the operations view. A list of active calls, each expandable into a live panel
showing: an animated voice orb reflecting who is speaking, caller number, channel,
direction, agent name, an elapsed timer, a live transcript that streams in and
auto-scrolls, and a retrieval activity feed showing each knowledge-base query with its
top score against the threshold and whether it was answered. Actions: listen in, take
over as a human operator, and end call.
Handle these event types: call.created, call.updated, transcript, rag.query,
agent.interrupted, recording.ready, agent.accepted, agent.error, call.error.
A designed empty state when no calls are active, and a clear offline state when the
simulated connection drops.

/history — a dense, fast table: time, direction arrow, phone number in the mono font,
channel icon, agent-or-operator mode, duration, outcome badge, recording indicator.
Filters for date range, channel, direction, mode, outcome and agent, plus a text search
across number and transcript. Saved views. Export to CSV and XLSX generated client-side.

/history/:id — a detail sheet: a summary header, the post-call summary text, and a
tabbed body of Transcript, Recording and Events. The transcript is timestamped,
speaker-labelled, searchable, and bilingual with the correct font per script.
The recording player must respect recording state exactly:
  RECORDING — a live indicator, no player.
  PENDING   — "still being composed by the provider", with a retry.
  READY     — a waveform player with speed control, download, and seeking from the
              transcript.
  NONE      — states plainly that this call was never recorded, and offers no retry,
              because a retry can never succeed.
Also show recording scope: whether the audio contains both sides, only the caller, or
only the agent.

/messages — WhatsApp message history with delivery status, a composer that can send
either a free-form message or an approved template with its variables filled, and a
per-contact thread view.
```

### Prompt 7 — Guides hub, team, usage, settings

```
Build the remaining sections. Frontend-only.

/guides — the self-serve documentation hub, a real product feature rather than a link
to external docs. A searchable, categorised library of markdown guides stored as repo
fixtures, with a category sidebar, reading-time estimates, copyable code and config
blocks, callout blocks for warnings and prerequisites, and inline "do this now" buttons
that deep-link into the relevant settings page. Seed these guides with real written
content:
  Getting started in 30 minutes
  Creating a provider app and getting WhatsApp Business Calling credentials
  Registering and verifying your WhatsApp business number
  Getting a phone number and SIP trunk from your telephony provider
  Pointing webhooks at VoxOps
  Writing a knowledge base an agent can actually answer from
  Tuning the similarity threshold
  Writing a persona and greeting that sound human on a phone line
  Getting WhatsApp message templates approved
  Recording, retention and consent
  Troubleshooting by error code
  Going live: the pre-launch checklist
Each guide page has related-guides links at the bottom and a "was this helpful" control.
Also surface contextual guide links from empty states and from disabled-control tooltips
across the whole app.

/team — the member list with role badges; invite by email with a role; pending
invitations with resend and revoke; role change; removal. A comparison table on the page
documenting what each of owner, admin, operator and viewer can do. Plus an audit log
view with actor, action, target and timestamp, filterable.

/usage — current-period usage against plan limits for call minutes, messages, storage,
agents and seats, each as a labelled meter; a daily usage chart; a cost breakdown by
channel; invoice history; and a plan comparison with upgrade actions. Warn visibly at
80% and at 100% of any limit.

/settings — organisation profile and branding including the accent colour that themes
the workspace; localisation defaults including the country code used to expand
nationally formatted numbers into E.164; recording and retention policy; storage archive
target; integrations for Drive and Sheets sync and spreadsheet export; outbound webhook
endpoints with event selection; API keys shown as last-four only with scopes and
last-used time; and a danger zone for deleting the organisation behind a typed-name
confirmation.
```

### Prompt 8 — Polish pass

```
A final pass across the whole app. Do not add features.

- Every list has a designed empty state: an icon, one sentence saying what will appear
  here, and the single action that makes it appear.
- Every async surface has a skeleton loader shaped like its real content — never a
  centred spinner on a blank page.
- Every error state says what failed, what to try, and links to the matching guide.
- Every destructive action confirms, and names exactly what will be lost.
- Full keyboard navigation; visible focus rings using --ring; correct ARIA on the
  connection pill, the live transcript, the usage meters and every tab set. Verify
  contrast in both themes.
- Verify the entire app at 375px: no horizontal scroll, 16px gutters, tables become
  cards, the sidebar becomes a sheet.
- Verify dark mode on every screen, including charts and the waveform player.
- Verify dir="rtl" under the Urdu locale on every screen.
- A command palette on Cmd/Ctrl+K for navigation and common actions.
- Toasts on every mutation, with undo where undo is possible.
- Numbers, IDs and phone numbers in the mono font with tabular figures throughout.
- Confirm no component calls fetch directly and no component imports a fixture
  directly — all data access goes through the data layer.
```

---

## 7. Handoff notes — explicitly not part of this build

Recorded so the frontend's shape makes sense to whoever picks up the server side later.

**The data layer is the seam.** Every screen already calls typed functions in
`src/lib/api/`. Attaching a real service means implementing the `USE_MOCK === false`
branch there and nothing else. The existing systems already expose most of the needed
surface — configuration, chat, knowledge search, call list and statistics, call detail,
recording, transcript, summary, outbound dial, the WhatsApp endpoints, and a live events
socket.

**The real engineering project is the tenant dimension.** In both source systems every
one of the ~110 settings is read from the process environment at boot, and every
endpoint serves a single customer. Making those per-organisation — and scoping every
query by it — is the work that this frontend is designed to sit on top of, and no amount
of UI work moves it.

**Secrets will need a vault.** This build keeps no secret at all, which is the safest
possible starting point. When a server exists, the write-only UI contract must be
matched by storage the client genuinely cannot read back, with decryption happening only
inside the service that places calls.

---

*Research basis: a 9,205-line single-file FastAPI backend for the Infobip/PSTN agent
with a second model engine for carrier-side audio, and a 4,760-line backend for the
WhatsApp Business Calling agent with a local WebRTC media bridge — their route tables,
environment-variable surfaces, persona template, frontend type definitions and design
tokens.*
