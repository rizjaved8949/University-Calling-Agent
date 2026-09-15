# University Voice Agent — Backend

RAG knowledge base + Pakistani Urdu voice agent + Infobip telephony, in one
FastAPI service. Deploys to Render.

---

## How it works

```
Caller ──PSTN──► Infobip ──SIP trunk──► OpenAI Realtime (speech ⇄ speech)
                    │                          │
                    │ webhooks                 │ webhook: realtime.call.incoming
                    ▼                          ▼
              ┌──────────────────── this service ────────────────────┐
              │  /webhooks/infobip   /webhooks/openai                 │
              │  accepts the call, then opens a WebSocket to it       │
              │                          │                            │
              │             tool call: search_knowledge_base            │
              │                          ▼                            │
              │   PDF → chunks → embeddings → cosine search → context │
              └───────────────────────────────────────────────────────┘
```

### Why RAG is a tool, not a pipeline stage

Audio never passes through Python. Infobip bridges the caller straight to OpenAI
Realtime, which does speech-to-text, reasoning, text-to-speech and barge-in
natively at roughly 300ms. Putting our own STT → LLM → TTS chain in the middle
would mean streaming raw audio through a free-tier container — hundreds of extra
milliseconds and many more ways to fail.

So retrieval is injected the other way round. The Realtime session is given a
`search_knowledge_base` function. When the caller asks something factual, the
model calls it, this service runs real embedding search, and the model speaks the
grounded result. Same RAG guarantees, none of the latency.

---

## RAG pipeline

| Stage | Implementation |
| --- | --- |
| Extraction | PyMuPDF; running headers and page numbers stripped |
| Tables | Extracted row-wise, so `LL.B \| Per Cr.Hr.: 16,740 \| Total: 2,505,520` stays one fact |
| Chunking | Split on the document's own `KB-x.y` section markers, not a blind token window |
| Overlap | `CHUNK_OVERLAP` characters, only when a section exceeds `CHUNK_SIZE` |
| Metadata | document, page, section, title, part, category, chunk_id, content_hash |
| Embeddings | `fastembed` (ONNX, no PyTorch) — `BAAI/bge-small-en-v1.5`, 67MB |
| Index | Normalised `float32` matrix; cosine similarity is a plain dot product |
| Caching | Keyed by PDF hash + model + chunk settings; rebuilt only when one changes |
| Filtering | `category` filter applied before ranking |
| Dedup | Identical content hashes dropped; max `MAX_CHUNKS_PER_SECTION` per section |
| Threshold | `SIMILARITY_THRESHOLD` — below it, the agent says it does not know |

**On the threshold:** bge models sit high on the cosine scale. Unrelated text
still scores ~0.52 while genuine matches land at 0.70+. The default of `0.60`
separates them, which is what makes the agent refuse off-topic questions instead
of confidently inventing university facts. Verify with:

```bash
curl "http://localhost:8000/api/knowledge/search?q=capital+of+Brazil"   # 0 results
curl "http://localhost:8000/api/knowledge/search?q=BS+Computer+Science+fee"
```

---

## Local development

```bash
cd backend
python -m venv .venv && .venv\Scripts\activate     # PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env                              # then fill in credentials
python app.py
```

First boot downloads the embedding model (~67MB) and builds the index (~30s).
Subsequent boots load from `.cache/` in under a second.

Health check: <http://localhost:8000/health>

### Webhooks in local development

Both providers need a public HTTPS URL:

```bash
cloudflared tunnel --url http://localhost:8000
```

Put the printed URL in `PUBLIC_BASE_URL`, then register:

- **OpenAI** → Settings › Project › Webhooks → `<url>/webhooks/openai`, event `realtime.call.incoming`
- **Infobip** → Developer Tools › Subscriptions → channel `VOICE_VIDEO`, notification profile `<url>/webhooks/infobip`, and **criteria `callsConfigurationId` = your configuration id**

The criteria is easy to miss and produces this exact error if omitted:
`Subscription for calls configuration ID [...] does not exist.`

---

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness plus knowledge-base and telephony status |
| `GET` | `/api/config` | Non-sensitive settings for the dashboard |
| `POST` | `/api/chat` | Ask the knowledge base in text |
| `GET` | `/api/knowledge/search` | Raw retrieval — use it to tune `TOP_K` / `SIMILARITY_THRESHOLD` |
| `POST` | `/api/calls/outbound` | Place a call — `{"phone_number": "+923001234567"}` |
| `GET` | `/api/calls` | Call log, optional `?direction=INBOUND` |
| `GET` | `/api/calls/stats` | Dashboard totals |
| `GET` | `/api/calls/{id}` | One call |
| `POST` | `/api/calls/{id}/end` | Hang up |
| `GET` | `/api/calls/{id}/recording` | Metadata, or the audio with `?download=true` |
| `GET` | `/api/calls/{id}/transcript` | The call as text, in the order it was spoken |
| `POST` | `/api/calls/{id}/summary` | Re-run the post-call summary (`?force=true` to redo a finished one) |
| `GET` | `/api/exports/calls.xlsx` | **The master workbook — every call ever taken, in one file** |
| `POST` | `/webhooks/infobip` | Call lifecycle events |
| `POST` | `/webhooks/openai` | `realtime.call.incoming`, signature-verified |
| `WS` | `/ws/events` | Live calls, transcripts, RAG queries, interruptions |

Recordings are proxied through the backend, so the Infobip key never reaches the
browser.

### The master workbook

`/api/exports/calls.xlsx` is what the dashboard's **Download Excel** button
asks for. There is one workbook and it is that response: three sheets — *Call
Summary*, *Student Details* and *Follow-Up Queries* — joined on Call ID, holding
every call the database has, oldest row first.

It is generated from the call log on each request rather than being a file that
gets appended to. That is deliberate, and it is what makes the two rules that
matter structural rather than merely intended: no call can be overwritten,
because nothing is written; and two administrators downloading at once cannot
corrupt anything, because both are reading. A stored workbook would have had to
survive concurrent appends, an ephemeral filesystem and a half-finished write
during a deploy, and losing call history to any of those is permanent.

Nothing in it is invented. A cell is empty when the caller never said the thing
— which is a fact the follow-up desk can act on, unlike a plausible guess. For
the same reason a call where nothing was learned about the caller gets no row on
*Student Details* at all.

### After a call ends

Every finished call is summarised once, in the background, from its stored
transcript: what the caller wanted, what they were told, what they were not, and
what still has to be sent to them. The result lands on the call row and is what
the workbook reports. It is idempotent — the finish webhook, the reconcile loop
and the stale-call reaper all trigger it, and only the first one does the work.

Transcript lines are stored per call (`TRANSCRIPT_STORE_ENABLED`), and go when
the call is deleted. Recordings are unaffected by any of this and still live in
Supabase.

---

## Deploying to Render

1. Push this folder to its own GitHub repository.
2. Render → **New → Web Service** → connect the repo.
3. Runtime **Docker**, plan **Free**, region **Singapore** (lowest latency to Pakistan).
4. Add the environment variables from `.env.example`. **Do not set `PORT`** —
   Render injects it and the app reads it.
5. Deploy, then set `PUBLIC_BASE_URL` to the Render URL and re-point both
   provider webhooks at it.

`render.yaml` describes all of this if you prefer a Blueprint deploy.

### Free-tier realities

- **Cold starts.** The service sleeps after inactivity and takes ~50s to wake.
  The frontend allows for this with a 60s timeout, but a caller will not wait —
  keep it warm with an uptime pinger if you are demoing live.
- **512MB RAM.** This is why the stack uses `fastembed` (ONNX, ~70MB) rather
  than `sentence-transformers` (PyTorch, ~2GB), which would be OOM-killed.
- **Ephemeral disk.** SQLite call logs reset on every redeploy. Attach a Render
  disk, or point `DATABASE_PATH` at managed Postgres, if history must survive.
- **The embedding index is warmed during the Docker build**, so a cold start
  does not also pay for a model download.

---

## Configuration

Everything is environment-driven — see `.env.example` for the full list with
comments. The values worth knowing:

| Variable | Default | Effect |
| --- | --- | --- |
| `MAX_CALL_DURATION_SECONDS` | `180` | Hard call ceiling. `300` gives 5 minutes. Enforced by Infobip, so it holds even if this service dies. |
| `INTERRUPTION_ENABLED` | `true` | Barge-in — the caller can talk over the agent |
| `TTS_VOICE` | `marin` | `marin` and `cedar` are the top quality tier; `marin` reads female |
| `TURN_DETECTION` | `server_vad` | `server_vad` is snappier; `semantic_vad` waits for a complete thought |
| `TOP_K` | `5` | Chunks retrieved per query |
| `SIMILARITY_THRESHOLD` | `0.60` | Below this the agent admits it does not know |
| `EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` | Swap for `paraphrase-multilingual-MiniLM-L12-v2` (220MB) to embed Urdu-script queries directly |
| `RESUME_OFFER_ENABLED` | `true` | After a barge-in, let the agent offer the part it was cut off saying — once, in a later turn, only if it matters |
| `SUMMARY_ENABLED` | `true` | Summarise each call when it ends. Off means the Excel report has call and follow-up data but no summaries |
| `SUMMARY_MODEL` | *(LLM_MODEL)* | Model used for the summary, if it should differ from the chat one |
| `TRANSCRIPT_STORE_ENABLED` | `true` | Store what was said. Off leaves recordings as the only record, and disables summaries in practice |
| `EXCEL_FILENAME` | `Voice_Agent_Call_Records.xlsx` | What the downloaded workbook is called |

Interruption settings only apply when turn detection is on; `TURN_DETECTION=none`
disables barge-in regardless of `INTERRUPTION_ENABLED`.

---

## Notes

- `AGENT_GREETING` and the system prompt are the only places the agent's persona
  lives. Numbers are spoken in lakh, never "million" — Pakistani callers do not
  use it, and the model will otherwise mis-convert large fees.
- Phone numbers are masked in logs (`9231***020`). Transcripts are not logged.
- The knowledge base PDF ships with the image. Replacing it invalidates the
  cache automatically via the content hash.
