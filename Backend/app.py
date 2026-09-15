"""University Voice Agent — backend.

One file, by design. It integrates five things that are easier to reason about
together than split across a dozen modules:

  1. RAG over the university knowledge base PDF (extract -> chunk -> embed -> search)
  2. A chat API that answers grounded questions in Pakistani Urdu
  3. Telephony via Infobip (inbound + outbound, bridged to the AI over SIP)
  4. OpenAI Realtime as the voice agent, with RAG exposed to it as a *tool*
  5. Call logging, recordings, and a live event feed for the dashboard

ARCHITECTURE NOTE — why RAG is a tool, not a pipeline stage
-----------------------------------------------------------
Audio never passes through this process. Infobip bridges the caller straight to
OpenAI Realtime over SIP, which does speech-to-text, reasoning, text-to-speech
and barge-in natively at ~300ms. Inserting our own STT/LLM/TTS chain in the
middle would mean streaming raw audio through a free-tier container, adding
hundreds of milliseconds and a lot of failure modes.

So retrieval is injected the other way round: the Realtime session is given a
`search_knowledge_base` function. When the caller asks something factual, the
model calls it, we run real embedding search here, and it speaks the grounded
result. Same RAG, none of the latency.

Run locally:  python app.py
"""

from __future__ import annotations

import asyncio
import base64
import collections
import contextlib
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import sqlite3
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

import fitz  # PyMuPDF
import httpx
import numpy as np
import uvicorn
import websockets
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from openai import AzureOpenAI, OpenAI
from openai._exceptions import InvalidWebhookSignatureError
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent

# =============================================================================
# Configuration — everything tunable lives in .env
# =============================================================================


class Settings(BaseSettings):
    # Anchored to this file, not the working directory, so `python backend/app.py`
    # from the repo root loads the same .env as `python app.py` from inside it.
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Application ---
    app_env: Literal["development", "production"] = "development"
    host: str = "0.0.0.0"
    port: int = 8000  # Render overrides this via $PORT
    log_level: str = "INFO"

    # --- Frontend / CORS ---
    # Comma-separated. "*" allows everything (development only).
    frontend_url: str = "http://localhost:5173,http://localhost:3000"

    # --- Knowledge base / RAG ---
    knowledge_base_pdf: str = "Knowledge_Base.pdf"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    # Swap for sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 if
    # you want to embed Urdu-script queries directly (220MB instead of 67MB).
    chunk_size: int = 1100          # characters, not tokens — sections are prose
    chunk_overlap: int = 180
    top_k: int = 5
    # bge models sit high on the cosine scale: unrelated text still scores ~0.52,
    # while genuine matches land at 0.70+. 0.60 separates them cleanly, so an
    # off-topic question returns nothing rather than confident nonsense.
    similarity_threshold: float = 0.60
    max_chunks_per_section: int = 3
    rag_max_context_chars: int = 4000
    cache_enabled: bool = True
    cache_dir: str = ".cache"

    # --- LLM (text chat endpoint only; voice uses realtime_model) ---
    # Small models are cheaper but measurably worse at rendering rupee amounts
    # in lakhs — gpt-4o-mini produced "do lakh bees lakh" and confused
    # first-semester fees with programme totals. Quoting a wrong fee is the
    # worst failure this agent has, so the default buys accuracy.
    llm_model: str = "gpt-4o"
    llm_temperature: float = 0.3
    llm_max_tokens: int = 600
    # Only read when LLM_MODEL is a reasoning model, which renames the token cap
    # and rejects `temperature` — see _llm_tuning. Ignored by gpt-4o.
    llm_reasoning_effort: Literal["minimal", "low", "medium", "high"] = "low"

    # When the caller cuts in, the sentence Ayesha was part-way through is
    # abandoned — which is right, the caller owns the turn. But some of those
    # sentences matter (the documents they still have to bring, a deadline),
    # and dropping them silently means the caller never hears them at all. With
    # this on, what was cut off is handed back to the model as context so it can
    # offer the remainder once the caller's own question is fully answered. It
    # never interrupts the caller to do so, and it is context, not a script.
    resume_offer_enabled: bool = True
    # Below this many characters a cut-off turn is not worth returning to — it
    # was a greeting, an acknowledgement, or half a word.
    resume_offer_min_chars: int = 60

    # --- Post-call summary + Excel report ---------------------------------
    # After a call finishes, its transcript is summarised and the student
    # details it contains are extracted, so the desk has something readable
    # without listening to the recording. Off by a single flag if the extra
    # token spend is not wanted; every other part of the call is unaffected.
    summary_enabled: bool = True
    # Blank means "whatever LLM_MODEL is" — one knob for anyone who does not
    # care to tune the two separately.
    summary_model: str = ""
    # Transcript lines are what the summary is built from, so they are stored.
    # Turning this off leaves recordings as the only record of a call.
    transcript_store_enabled: bool = True
    # The name the admin's browser gives the downloaded workbook. One file for
    # every call ever taken — see /api/exports/calls.xlsx.
    excel_filename: str = "Voice_Agent_Call_Records.xlsx"

    # --- OpenAI ---
    openai_api_key: str = ""
    openai_project_id: str = ""
    openai_webhook_secret: str = ""
    openai_sip_domain: str = "sip.api.openai.com"

    # --- Azure OpenAI ---
    # Set the endpoint and key and every model call goes to Azure instead of
    # OpenAI: voice calls (SIP accept, session socket, hangup), the text chat,
    # post-call summaries and WhatsApp replies. Azure serves the same realtime
    # call protocol, so for calls the switch is only addresses and the auth
    # header. Three settings change meaning on Azure:
    #   OPENAI_PROJECT_ID    proj_<internalId> from the resource's JSON View
    #   OPENAI_SIP_DOMAIN    <region>.sip.ai.azure.com (swedencentral | eastus2)
    #   OPENAI_WEBHOOK_SECRET  signing_secret from Azure's webhook_endpoints API
    # and every model name (REALTIME_MODEL, STT_MODEL, LLM_MODEL, SUMMARY_MODEL)
    # is a DEPLOYMENT name. Name each deployment after its model and none of
    # them has to change.
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    # 2024-10-21 is the first GA version with structured outputs, which the
    # summary's json_schema response needs.
    azure_openai_api_version: str = "2024-10-21"

    @property
    def azure_configured(self) -> bool:
        return bool(self.azure_openai_endpoint.strip() and self.azure_openai_api_key.strip())

    @property
    def azure_openai_root(self) -> str:
        """https://<resource>.openai.azure.com, however the endpoint was pasted."""
        root = self.azure_openai_endpoint.strip().rstrip("/")
        return root.removesuffix("/openai/v1").removesuffix("/openai")

    # --- Voice agent (OpenAI Realtime) ---
    # The -mini tier is cheaper but noticeably worse on a real call: it
    # paraphrases the scripted greeting ("Salaam" instead of "Assalam-o-Alaikum"),
    # rewrites its own persona, and delivers lines flatly enough to sound robotic.
    # The flagship follows the script and sounds human. Worth the money here.
    realtime_model: str = "gpt-realtime-2.1"
    # marin and cedar are the current generation and sound markedly more human
    # than the older set; the rest (alloy, ash, ballad, coral, echo, sage,
    # shimmer, verse) are the previous one and read as synthetic on a phone
    # line. Ayesha is a woman, so marin. Worth A/B-ing against cedar by ear —
    # naturalness is a judgement no benchmark settles for you.
    tts_voice: str = "marin"
    # Playback rate, sent only when it differs from 1.0. It has been walked in
    # both directions on real calls: 0.95 to stop long Urdu vowels clipping
    # through the 8kHz codec, which combined with the persona's then "unhurried
    # greeting" wording into a dragged-out "Aaassaalaaam-o-Alaaaikum"; then 1.0
    # once that wording was fixed; now slightly above it, because a real Lahori
    # counselor speaks a shade faster than a neutral read and the opening still
    # sounded careful rather than natural.
    # 1.05 is a nudge, not a sprint. Past roughly 1.15 the Urdu vowels start
    # clipping again and she sounds harried, which is worse than slow.
    tts_speed: float = 1.05
    language: str = "ur-PK"
    # Transcriber language hint. Deliberately blank: pinning it to "ur" made the
    # transcriber render an English caller's turn as Urdu, so the model saw Urdu
    # and answered in Urdu no matter what the caller actually spoke. Auto-detect
    # costs the occasional Hindi-vs-Urdu mislabel in the transcript and buys
    # correct language matching on the call, which is the part callers hear.
    # Set STT_LANGUAGE=ur to pin it again if transcript tidiness ever wins.
    stt_language: str = ""
    stt_model: str = "gpt-4o-transcribe"  # blank disables transcripts
    # semantic_vad decides a turn has ended by whether the sentence is finished,
    # not by a fixed count of silent milliseconds. A plain timer has to choose
    # between cutting people off and leaving dead air: 700ms of silence before
    # every reply is the pause that reads as "the machine is thinking", and
    # shortening it makes her talk over anyone who pauses mid-sentence. Deciding
    # on meaning gets both — quick when a thought is complete, patient when it
    # is not, which matters most for the callers who hesitate.
    # Revert with TURN_DETECTION=server_vad if replies ever feel late.
    turn_detection: Literal["server_vad", "semantic_vad", "none"] = "semantic_vad"
    # How soon semantic_vad decides the caller has finished. It tunes the
    # maximum wait before the turn is chunked: low 8s, medium 4s, high 2s, and
    # auto — the API default — behaves as medium. So leaving this unset was
    # costing up to four seconds of dead air at the end of every caller turn,
    # which is most of what "she is slow to reply" actually was.
    # "high" is the quick end. Move to "medium" if she starts answering before
    # hesitant callers have finished their sentence — the shorter the timeout,
    # the sooner she cuts in on someone still thinking.
    vad_eagerness: Literal["low", "medium", "high", "auto"] = "high"
    vad_threshold: float = 0.5
    vad_prefix_padding_ms: int = 300
    vad_silence_ms: int = 500
    interruption_enabled: bool = True
    noise_reduction: Literal["near_field", "far_field", "none"] = "near_field"

    # --- Call behaviour ---
    max_call_duration_seconds: int = 180
    # How long before the hard limit the agent starts closing the conversation.
    wrap_up_warning_seconds: int = 30
    call_connect_timeout_seconds: int = 45
    # How long to let the farewell play out before dropping the line. The model
    # finishes generating well before the carrier finishes playing, so hanging
    # up the instant a response completes clips "Allah Hafiz" in half.
    hangup_grace_seconds: float = 3.5
    # Attach the AI leg while the phone is still ringing instead of waiting for
    # pickup. The SIP setup to OpenAI is the single largest part of the silence
    # a caller sits through after answering, and during the ring nobody is
    # listening to it — so paying for it there costs nothing audible.
    #
    # OFF BY DEFAULT, because it cannot be verified without live calls:
    #  * Infobip's docs do not state whether a dialog may be created against a
    #    parent that is still ringing. If it refuses, the code logs it and the
    #    normal pickup path still bridges, so a rejection costs one wasted API
    #    call and nothing else.
    #  * A realtime session is then open during the ring, and OpenAI bills from
    #    when it answers — including for calls nobody picks up.
    # The greeting is held until the legs are actually bridged either way, so
    # she never talks into a line the student has not answered yet.
    predial_agent_leg: bool = False
    # Silence held before the greeting starts. The realtime socket is ready
    # before the SIP media path is actually carrying audio, so speaking the
    # instant it opens throws the first syllable away and the caller hears
    # "...salam-o-Alaikum" — a greeting that sounds wrong without them being
    # able to say why. This is the cheapest fix that always works: wait, then
    # speak. Raise it if the opening is still clipped, lower it if the line
    # feels dead on pickup. Small enough that nobody reads it as a pause.
    greeting_delay_seconds: float = 0.8
    agent_greeting: str = (
    "Assalam-o-Alaikum! University Call Center se Ayesha baat kar rahi hoon. "
    "Aap ki kis silsilay mein madad kar sakti hoon?"
    )

    # --- Telephony (Infobip) ---
    telephony_provider: str = "infobip"
    infobip_base_url: str = ""
    infobip_api_key: str = ""
    infobip_phone_number: str = ""
    infobip_sip_trunk_id: str = ""
    infobip_calls_configuration_id: str = ""
    infobip_application_id: str = ""

    # --- WhatsApp (Meta Cloud API, direct — NOT through Infobip) -------------
    # Messaging goes straight to Meta's Graph API on the university's own WhatsApp
    # Business account. Infobip stays on voice only, so nothing here depends on
    # the Infobip key or its scopes.
    #
    # Sending needs the access token, the phone number id and an approved
    # template name. Ayesha is only told she can send when all three are
    # present — see _whatsapp_prompt_block.
    whatsapp_access_token: str = ""
    # The Cloud API sends *from* the phone number id, not the phone number.
    whatsapp_phone_number_id: str = ""
    whatsapp_business_phone_number: str = ""
    whatsapp_business_account_id: str = ""
    meta_graph_api_version: str = "v25.0"
    whatsapp_template: str = ""
    # The second template: the information itself, rather than a reference
    # number. Two body placeholders - what they asked about, and the answer.
    # Separate from the one above because Meta approves them one at a time,
    # and either can be live without the other; blank means Ayesha is told
    # she may send a reference number but not details.
    whatsapp_details_template: str = ""
    # Caps on the two placeholders. Meta's limit is 1024 characters for the
    # whole rendered body, so these leave room for the fixed wording around
    # them. The details cap is a readability limit as much as a technical
    # one: past a few hundred characters nobody reads a WhatsApp message.
    whatsapp_topic_max_chars: int = 120
    whatsapp_details_max_chars: int = 600
    # The language the approved template was registered under. Meta rejects a
    # send whose language does not match the registration exactly.
    whatsapp_language: str = "en"
    # Prefixed to local numbers when converting to E.164. 92 = Pakistan.
    whatsapp_country_code: str = "92"
    # --- Inbound WhatsApp (the student replies and Ayesha answers) ---
    # A reply from the student opens Meta's 24-hour customer service
    # window, and inside it we may answer in free-form text - no template,
    # no approval, any wording. That is what makes this conversational.
    whatsapp_inbound_enabled: bool = True
    # Any random string; the SAME value goes in Meta > WhatsApp >
    # Configuration when the callback URL is saved. Meta echoes it back
    # once to prove the URL belongs to us.
    whatsapp_webhook_verify_token: str = ""
    # Signs every webhook delivery. Without it the endpoint cannot tell
    # Meta from anyone else who found the URL, so an unset secret makes
    # the webhook refuse every request rather than trust them.
    meta_app_secret: str = ""

    # --- Manual calls (the agent stays out of these) ---
    # The counselor's own phone, pre-filled in the manual dialer. Optional: the
    # dialer accepts any number, this is only the default.
    operator_phone_number: str = ""
    # WebRTC is a separate Infobip product from Calls, so the browser-microphone
    # mode stays switched off until an application id is configured for it. The
    # UI reads this and explains itself rather than failing at dial time.
    infobip_webrtc_application_id: str = ""
    # How long a browser calling token stays valid. Short by design — it is
    # handed to the browser, and a new one costs one request.
    rtc_token_ttl_seconds: int = 600

    # --- Recording ---
    record_calls: bool = True
    recording_type: Literal["AUDIO", "AUDIO_AND_VIDEO"] = "AUDIO"
    recording_file_prefix: str = "ucp-voice"

    # --- Reconciliation with the provider ---
    # How often to ask the provider what really happened to calls we still think
    # are open. This is what closes a call whose CALL_FINISHED never arrived, so
    # it is also how quickly a stuck dashboard timer stops: at worst one tick.
    reconcile_interval_seconds: int = 20
    # The same check while a call is actually up, where the delay is visible as
    # a timer that keeps running after the student has already hung up.
    live_poll_seconds: int = 5
    # How long after a call ends to keep chasing its audio. Recordings are
    # composed after the call, and composition is not instant; a window this
    # wide also recovers anything missed while the service was asleep or
    # redeploying.
    reconcile_window_seconds: int = 3600
    # How long after a call ends to keep believing a recording might still be
    # coming. Past this, with the provider reporting nothing, the dashboard says
    # so rather than offering a retry forever.
    recording_grace_seconds: int = 180

    # --- Storage ---
    # Both default to the project tree, which is fine locally and wrong on any
    # host with an ephemeral filesystem: there, every redeploy takes the call
    # log and every recording with it. Point them at a mounted disk in
    # production (e.g. /var/data/calls.db and /var/data/recordings).
    database_path: str = "data/calls.db"
    recordings_dir: str = "data/recordings"

    # --- Durable storage (Supabase) ---
    # Set these and the call log moves to Postgres and the audio to Supabase
    # Storage, so neither dies with the container. Leave them blank and the
    # service falls back to SQLite and the local disk, which is what a dev
    # machine wants. Must be the session-pooler URI: the direct connection is
    # IPv6-only and most hosts, Render included, cannot reach it.
    database_url: str = ""
    supabase_url: str = ""
    # The "secret" key (what used to be called service_role). It bypasses row
    # level security, so it is server-side only and never sent to a browser.
    supabase_service_key: str = ""
    supabase_bucket: str = "call-recordings"
    # Free-plan buckets reject anything larger than this. A call that exceeds it
    # is not lost: the provider's file id is kept and the audio is streamed from
    # them on demand instead. ~50 MB is about 50 minutes of 8kHz WAV.
    supabase_max_upload_bytes: int = 50 * 1024 * 1024
    # Ceiling on a recording the browser posts up. Opus at conversational
    # bitrates is roughly 200KB a minute, so this is hours of call, and it is
    # here to stop an unauthenticated endpoint being handed a film.
    max_upload_bytes: int = 40 * 1024 * 1024
    # Record Talk-tab calls in the browser rather than asking the provider to.
    # Turn off only if Infobip ever enables recording for WebRTC calls.
    record_browser_client_side: bool = True
    # Record from the carrier's live media stream rather than asking for their
    # finished recording. Their composed files arrived short and one-sided; this
    # is the same audio, captured by us, checked by us. Needs PUBLIC_BASE_URL to
    # be reachable over wss:// — the carrier connects back to it.
    record_from_media_stream: bool = True
    media_stream_config_name: str = "ucp-voice-recorder"
    # How long the call-ended webhook waits for the carrier's media socket to
    # close before encoding. The webhook consistently beats the last audio
    # frames, and encoding immediately truncated the end of every recording —
    # including the goodbye. Bounded, because a socket that never closes must
    # not hold a recording hostage.
    recording_tail_wait_seconds: float = 6.0
    # Mix Ayesha's half in from the realtime socket. The carrier's stream of the
    # caller's leg does not always carry her voice back, which is what makes a
    # recording sound one-sided. Off until the rate below is confirmed against a
    # real call: mixing at the wrong rate makes her a chipmunk, which is worse
    # than leaving her out. Look for the "agent track" line in the logs — it
    # prints the duration the configured rate implies, which should match the
    # carrier's. If it reads half the call, halve the rate; double, double it.
    record_agent_audio: bool = False
    # OpenAI's realtime output is 24 kHz PCM16 unless the session says otherwise.
    agent_audio_sample_rate: int = 24000
    # Re-encode provider recordings to MP3 on the way in. WAV from the carrier
    # is eight times the size for no audible gain on a phone call, and those
    # bytes are the wait before a recording starts playing.
    transcode_recordings: bool = True
    # Ceiling on the MP3 conversion of an uploaded recording. Speech re-encodes
    # far faster than real time; this is only here so a wedged process cannot
    # hold a web request open.
    transcode_timeout_seconds: int = 120

    # --- Public URL both providers post webhooks to ---
    public_base_url: str = ""

    @field_validator("llm_temperature", "similarity_threshold", mode="before")
    @classmethod
    def _blank_is_default(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.frontend_url.split(",") if o.strip()]

    @property
    def infobip_root(self) -> str:
        url = self.infobip_base_url.strip().rstrip("/")
        if not url:
            return ""
        return url if url.startswith("http") else f"https://{url}"

    @property
    def whatsapp_configured(self) -> bool:
        return bool(
            self.whatsapp_access_token.strip()
            and self.whatsapp_phone_number_id.strip()
            and self.whatsapp_template.strip()
        )

    @property
    def whatsapp_details_configured(self) -> bool:
        """Whether the details template - not just the token one - can be sent.

        Checked separately from `whatsapp_configured` so approving one
        template does not silently promise the other. Meta approves them
        one at a time, and the gap between the two is a real state this
        service runs in.
        """
        return bool(
            self.whatsapp_access_token.strip()
            and self.whatsapp_phone_number_id.strip()
            and self.whatsapp_details_template.strip()
        )

    @property
    def whatsapp_api_url(self) -> str:
        version = self.meta_graph_api_version.strip() or "v25.0"
        return (
            f"https://graph.facebook.com/{version}/"
            f"{self.whatsapp_phone_number_id.strip()}/messages"
        )

    @property
    def sip_uri(self) -> str:
        return f"sip:{self.openai_project_id}@{self.openai_sip_domain};transport=tls"


settings = Settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("voice-agent")


def mask_number(number: str) -> str:
    """Phone numbers go in logs; the middle digits do not."""
    digits = re.sub(r"\D", "", number or "")
    return f"{digits[:4]}***{digits[-3:]}" if len(digits) > 7 else "***"


# =============================================================================
# RAG — extraction, chunking, embedding, search
# =============================================================================


@dataclass
class Chunk:
    chunk_id: str
    text: str
    document: str
    page: int
    section: str      # e.g. "KB-6.4"
    title: str        # e.g. "Merit scholarship — undergraduate, by faculty"
    part: str         # e.g. "Part 6 — Scholarships"
    category: str     # coarse bucket used for metadata filtering
    content_hash: str

    def metadata(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "document": self.document,
            "page": self.page,
            "section": self.section,
            "title": self.title,
            "part": self.part,
            "category": self.category,
            "content_hash": self.content_hash,
        }


# Maps a section number to a coarse category, used for metadata filtering and
# for telling the caller where an answer came from.
_PART_CATEGORY = {
    "0": "usage",
    "1": "university",
    "2": "calendar",
    "3": "programs",
    "4": "eligibility",
    "5": "fees",
    "6": "scholarships",
    "7": "admission-process",
    "8": "faq",
    "9": "call-handling",
    "10": "agent-prompt",
}


# Bump when the scrub rules below change: the chunk cache is fingerprinted on
# the PDF bytes, not on this code, so without it an edit here is invisible
# until somebody clears .cache by hand.
_SCRUB_VERSION = "4"

# The knowledge base PDF names the institution it was written for, in body text
# as well as in the page header. Those names reach the model as retrieved
# context and come back out of the agent's mouth, so genericising the prompts
# alone is not enough — the source text has to be neutralised too.
#
# Order matters: the address rules run before the bare domain rule, otherwise
# "admissions@ucp.edu.pk" is half-rewritten into something that is neither an
# address nor a sentence.
_INSTITUTION_SCRUB: tuple[tuple[re.Pattern[str], str], ...] = (
    # Contact details first: they contain the domain the later rules would
    # otherwise chew into fragments.
    (re.compile(r"admissions@ucp\.edu\.pk", re.I), "the admissions office"),
    (re.compile(r"\bonline-admissions\.ucp\.edu\.pk\b", re.I), "the online admissions portal"),
    (re.compile(r"\bucp\.edu\.pk\b", re.I), "the university website"),
    (re.compile(r"\b0800-?00827\b"), "the admissions helpline"),
    (re.compile(r"\+92-42-35880007"), "the admissions landline"),
    # Street address of the campus, which identifies the institution as surely
    # as its name does.
    # \s+ rather than literal spaces: the PDF wraps these across lines, and a
    # rigid pattern left "Hospital" stranded in the middle of an address.
    (re.compile(r"1-Khayaban-e-Jinnah\s+Road,?\s*", re.I), ""),
    (re.compile(r"opposite\s+Shaukat\s+Khanum\s+Memorial\s+Cancer\s+Hospital,?\s*", re.I), ""),
    (re.compile(r"\bJohar\s+Town\b,?\s*", re.I), ""),
    # The parent education group names the institution just as clearly. The
    # replacement carries no article, because the source wraps it as "the
    # wider <group> network" and "the wider its parent..." is not English.
    (re.compile(r"Punjab\s+Group\s+of\s+Colleges", re.I), "parent education group"),
    (re.compile(r"Punjab\s+Group", re.I), "parent education group"),
    (re.compile(r",\s*Lahore\b", re.I), ""),
    (re.compile(r"\bLahore\b,?\s*", re.I), ""),
    # The name itself. Whitespace-flexible because the PDF wraps it across
    # lines, and a rigid pattern left "of Central Punjab." stranded on its own.
    (re.compile(r"University\s+of\s+Central\s+Punjab", re.I), "the University"),
    (re.compile(r"\bCentral\s+Punjab\b", re.I), "the University"),
    (re.compile(r"\bUCP\b"), "the University"),
)


def _scrub_institution(text: str) -> str:
    """Replace the source institution's name and contact details in KB text."""
    for pattern, replacement in _INSTITUTION_SCRUB:
        text = pattern.sub(replacement, text)
    # "University of Central Punjab (UCP)" rewrites both halves and lands as
    # "the University (the University)".
    text = re.sub(r"the University\s*\(\s*the University\s*\)", "the University", text, flags=re.I)
    # "at UCP campus" becomes "at the University campus", but "the UCP campus"
    # would become "the the University campus".
    text = re.sub(r"\bthe\s+the\b", "the", text, flags=re.I)
    # Deleting an address mid-sentence leaves punctuation stranded.
    text = re.sub(r"\(\s*\)", "", text)
    text = re.sub(r"\s+([,.;:])", r"\1", text)
    text = re.sub(r"([,;:])\s*\1+", r"\1", text)
    return re.sub(r"[ \t]{2,}", " ", text)


def _clean(text: str) -> str:
    """Strip the running header/footer, and de-brand what is left.

    The two noise strings still name the source institution, deliberately:
    they are what the repeated header is matched against. Everything that
    survives that filter then goes through _scrub_institution, so no chunk
    carries the name into the RAG context. Swap both whenever the knowledge
    base PDF is swapped.
    """
    noise = (
        "University of Central Punjab — Master Admissions Knowledge Base",
        "Helpline 0800-00827  |  admissions@ucp.edu.pk  |  ucp.edu.pk",
    )
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if any(stripped.startswith(n[:40]) for n in noise):
            continue
        if re.fullmatch(r"Page \d+", stripped):
            continue
        if re.fullmatch(r"Fall 2026\s*·\s*v[\d.]+", stripped):
            continue
        lines.append(stripped)
    return _scrub_institution("\n".join(lines))


def _tables_as_text(page: "fitz.Page") -> str:
    """Render tables row-wise with their headers.

    Plain text extraction turns a fee table into column soup. Emitting
    "Programme: BBA | Per Cr.Hr.: 14,780 | Total Fee: 1,964,180" keeps each
    programme's fees retrievable as one self-contained fact.
    """
    out: list[str] = []
    try:
        tables = page.find_tables()
    except Exception:  # noqa: BLE001 - table finder is best-effort
        return ""

    for table in tables:
        try:
            rows = table.extract()
        except Exception:  # noqa: BLE001
            continue
        if not rows or len(rows) < 2:
            continue
        header = [(c or "").strip() for c in rows[0]]
        for row in rows[1:]:
            cells = [(c or "").strip() for c in row]
            if not any(cells):
                continue
            pairs = [
                f"{h}: {c}" for h, c in zip(header, cells) if c and h and h != c
            ]
            if pairs:
                out.append(" | ".join(pairs))
    return "\n".join(out)


def extract_sections(pdf_path: Path) -> list[Chunk]:
    """PDF -> semantically bounded, metadata-rich chunks.

    The knowledge base was authored with stable `KB-x.y` section markers, so we
    split on those rather than a blind token window: every chunk is a complete
    thought with a heading, which is what makes retrieval land on the right
    answer instead of half a table.
    """
    doc = fitz.open(pdf_path)
    document_name = pdf_path.name

    # Body prose and table rows are kept apart: prose forms narrative sections,
    # while each table row is additionally indexed on its own so a question like
    # "LL.B fee" retrieves that one row instead of competing with 78 others.
    pages: list[tuple[int, str]] = []
    page_rows: list[tuple[int, list[str]]] = []
    for index, page in enumerate(doc):
        body = _clean(page.get_text("text"))
        # Table rows skip _clean (they are built, not extracted), so they need
        # the same de-branding applied directly — a fee table naming the
        # institution would put it straight back into a quoted answer.
        rows = [
            _scrub_institution(r)
            for r in _tables_as_text(page).splitlines()
            if r.strip()
        ]
        pages.append((index + 1, body + ("\n" + "\n".join(rows) if rows else "")))
        page_rows.append((index + 1, rows))
    doc.close()

    marker = re.compile(r"^(KB-(\d+)\.\d+)$", re.MULTILINE)
    chunks: list[Chunk] = []
    current_part = "Front matter"

    # Walk pages, opening a new section whenever a KB- marker appears.
    pending: dict[str, Any] | None = None

    def flush(section: dict[str, Any] | None) -> None:
        if not section:
            return
        text = "\n".join(section["lines"]).strip()
        if len(text) < 40:
            return
        for piece in _split_long(text, settings.chunk_size, settings.chunk_overlap):
            digest = hashlib.sha256(piece.encode()).hexdigest()[:16]
            part_no = section["section"].split("-")[1].split(".")[0]
            chunks.append(
                Chunk(
                    chunk_id=f"{section['section']}::{digest[:8]}",
                    text=f"[{section['section']} {section['title']}]\n{piece}",
                    document=document_name,
                    page=section["page"],
                    section=section["section"],
                    title=section["title"],
                    part=section["part"],
                    category=_PART_CATEGORY.get(part_no, "general"),
                    content_hash=digest,
                )
            )

    section_on_page: dict[int, tuple[str, str, str]] = {}

    for page_no, text in pages:
        for line in text.splitlines():
            hit = marker.match(line.strip())
            if hit:
                flush(pending)
                pending = {
                    "section": hit.group(1),
                    "title": "",
                    "part": current_part,
                    "page": page_no,
                    "lines": [],
                }
                continue
            if line.startswith("Part ") or line.startswith("Appendix "):
                current_part = line.strip()
            if pending is None:
                continue
            if not pending["title"]:
                pending["title"] = line.strip()
            else:
                pending["lines"].append(line)
        if pending:
            section_on_page[page_no] = (pending["section"], pending["title"], pending["part"])
    flush(pending)

    # Each table row becomes its own retrievable fact, inheriting the metadata
    # of whichever section was open on that page.
    for page_no, rows in page_rows:
        section, title, part = section_on_page.get(page_no, ("KB-5.4", "Fee table", "Part 5 — Fees"))
        part_no = section.split("-")[1].split(".")[0]
        for row in rows:
            if len(row) < 25:
                continue
            digest = hashlib.sha256(row.encode()).hexdigest()[:16]
            chunks.append(
                Chunk(
                    chunk_id=f"{section}#row::{digest[:8]}",
                    text=f"[{section} {title}]\n{row}",
                    document=document_name,
                    page=page_no,
                    section=section,
                    title=title,
                    part=part,
                    category=_PART_CATEGORY.get(part_no, "general"),
                    content_hash=digest,
                )
            )

    log.info("knowledge base: %d chunks from %d pages", len(chunks), len(pages))
    return chunks


def _split_long(text: str, size: int, overlap: int) -> list[str]:
    """Split only when a section genuinely exceeds the budget, on paragraph
    boundaries, keeping `overlap` characters of context between pieces."""
    if len(text) <= size:
        return [text]

    pieces: list[str] = []
    paragraphs = text.split("\n")
    buffer = ""
    for para in paragraphs:
        if len(buffer) + len(para) + 1 <= size:
            buffer = f"{buffer}\n{para}" if buffer else para
            continue
        if buffer:
            pieces.append(buffer)
        tail = buffer[-overlap:] if overlap and buffer else ""
        buffer = f"{tail}\n{para}" if tail else para
    if buffer:
        pieces.append(buffer)
    return pieces


_AMOUNT = re.compile(r"\b(\d{1,3}(?:,\d{3}){1,3})\b")


def annotate_lakhs(text: str) -> str:
    """Tag every large rupee figure with its lakh value.

    Small models reliably mangle 2,239,790 into "do lakh bees lakh" when asked to
    convert on the fly. Handing them "2,239,790 (=22.4 lakh)" turns arithmetic
    into a lookup, which they get right — and a wrong fee is the single most
    damaging thing this agent could say.
    """

    def tag(match: re.Match[str]) -> str:
        raw = match.group(1)
        try:
            value = int(raw.replace(",", ""))
        except ValueError:
            return raw
        if value < 100_000:
            return raw
        return f"{raw} (={value / 100_000:.1f} lakh)"

    return _AMOUNT.sub(tag, text)


class KnowledgeBase:
    """Embedding index with an on-disk cache keyed by content + model.

    Rebuilding embeddings on every cold start would dominate Render's free-tier
    boot time, so the vectors are cached and only regenerated when the PDF or
    the model actually changes.
    """

    def __init__(self) -> None:
        self.chunks: list[Chunk] = []
        self.vectors: np.ndarray | None = None
        self._model: Any = None
        self._ready = False
        self._query_cache: dict[str, list[dict[str, Any]]] = {}

    # -- lifecycle ---------------------------------------------------------
    @property
    def ready(self) -> bool:
        return self._ready

    def _cache_paths(self, fingerprint: str) -> tuple[Path, Path]:
        cache = BASE_DIR / settings.cache_dir
        cache.mkdir(parents=True, exist_ok=True)
        return cache / f"kb-{fingerprint}.npy", cache / f"kb-{fingerprint}.json"

    def _load_model(self) -> Any:
        if self._model is None:
            from fastembed import TextEmbedding

            log.info("loading embedding model %s", settings.embedding_model)
            self._model = TextEmbedding(model_name=settings.embedding_model)
        return self._model

    def build(self) -> None:
        pdf = BASE_DIR / settings.knowledge_base_pdf
        if not pdf.exists():
            log.error("knowledge base PDF not found at %s — RAG disabled", pdf)
            return

        raw = pdf.read_bytes()
        fingerprint = hashlib.sha256(
            raw
            + settings.embedding_model.encode()
            + str(settings.chunk_size).encode()
            + str(settings.chunk_overlap).encode()
            + _SCRUB_VERSION.encode()
        ).hexdigest()[:16]

        vec_path, meta_path = self._cache_paths(fingerprint)
        if settings.cache_enabled and vec_path.exists() and meta_path.exists():
            self.vectors = np.load(vec_path)
            self.chunks = [Chunk(**c) for c in json.loads(meta_path.read_text("utf-8"))]
            self._ready = True
            log.info("knowledge base restored from cache (%d chunks)", len(self.chunks))
            self.warm()
            return

        self.chunks = extract_sections(pdf)
        if not self.chunks:
            log.error("no chunks extracted — RAG disabled")
            return

        model = self._load_model()
        log.info("embedding %d chunks…", len(self.chunks))
        vectors = np.array(list(model.embed([c.text for c in self.chunks])), dtype=np.float32)
        # Normalise once so similarity is a plain dot product later.
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True) + 1e-12
        self.vectors = vectors
        self._ready = True

        if settings.cache_enabled:
            np.save(vec_path, vectors)
            meta_path.write_text(
                json.dumps([c.__dict__ for c in self.chunks], ensure_ascii=False),
                encoding="utf-8",
            )
            log.info("knowledge base cached as %s", vec_path.name)
        self.warm()

    def warm(self) -> None:
        """Load the embedding model and run one throwaway query.

        Restoring vectors from cache does not touch the model, so without this
        the first caller of the day pays ~2s mid-conversation while ONNX loads —
        a very long silence on a phone call. Later queries take ~15ms.
        """
        started = time.perf_counter()
        try:
            model = self._load_model()
            next(iter(model.query_embed(["warm up"])))
            log.info("embedding model warm (%.0fms)", (time.perf_counter() - started) * 1000)
        except Exception as exc:  # noqa: BLE001 - warming is an optimisation
            log.warning("could not warm the embedding model: %s", exc)

    # -- retrieval ---------------------------------------------------------
    def search(
        self,
        query: str,
        top_k: int | None = None,
        threshold: float | None = None,
        category: str | None = None,
    ) -> list[dict[str, Any]]:
        if not self._ready or self.vectors is None:
            return []

        top_k = top_k or settings.top_k
        threshold = settings.similarity_threshold if threshold is None else threshold
        cache_key = f"{query.lower().strip()}|{top_k}|{threshold}|{category or ''}"
        if settings.cache_enabled and cache_key in self._query_cache:
            return self._query_cache[cache_key]

        model = self._load_model()
        vector = np.array(next(iter(model.query_embed([query]))), dtype=np.float32)
        vector /= np.linalg.norm(vector) + 1e-12

        scores = self.vectors @ vector  # cosine, both sides normalised

        # Metadata filtering happens before ranking so a category filter cannot
        # be crowded out by high-scoring chunks from elsewhere.
        candidates = range(len(self.chunks))
        if category:
            candidates = [i for i in candidates if self.chunks[i].category == category]
            if not candidates:
                candidates = range(len(self.chunks))

        ranked = sorted(candidates, key=lambda i: float(scores[i]), reverse=True)

        results: list[dict[str, Any]] = []
        seen_hashes: set[str] = set()
        per_section: dict[str, int] = {}
        for i in ranked:
            score = float(scores[i])
            if score < threshold:
                break
            chunk = self.chunks[i]
            # Drop verbatim duplicates, and cap how much any single section can
            # contribute so one long section cannot crowd out everything else.
            if chunk.content_hash in seen_hashes:
                continue
            if per_section.get(chunk.section, 0) >= settings.max_chunks_per_section:
                continue
            seen_hashes.add(chunk.content_hash)
            per_section[chunk.section] = per_section.get(chunk.section, 0) + 1
            results.append({"score": round(score, 4), "text": chunk.text, **chunk.metadata()})
            if len(results) >= top_k:
                break

        if settings.cache_enabled:
            self._query_cache[cache_key] = results
        return results

    def context_for(self, query: str, **kw: Any) -> tuple[str, list[dict[str, Any]]]:
        """Retrieved chunks packed into a context block within the char budget."""
        hits = self.search(query, **kw)
        blocks, used = [], 0
        for hit in hits:
            block = f"[{hit['section']} · {hit['title']} · page {hit['page']}]\n{annotate_lakhs(hit['text'])}"
            if used + len(block) > settings.rag_max_context_chars:
                break
            blocks.append(block)
            used += len(block)
        return "\n\n---\n\n".join(blocks), hits


kb = KnowledgeBase()


# =============================================================================
# Agent persona
# =============================================================================

_PROMPT_TEMPLATE = """You are Ayesha, a warm and professional operator at the University Call Center. You handle inbound and outbound calls from prospective students, current students, and alumni.

=== LANGUAGE & CONVERSATION STYLE – MANDATORY ===
- Start every call EXACTLY with: "{GREETING}"
- MIRROR THE CALLER'S LANGUAGE ON EVERY SINGLE TURN. Before each reply, judge the language of what the caller just said and answer in that same language. This is not a one-time decision made at the start of the call – re-judge it every time they speak.
  • If they speak English – reply in English.
  • If they speak Urdu – reply in Urdu.
  • If they speak Roman Urdu – reply in natural Roman Urdu.
  • If they mix Urdu and English – reply in the same bilingual mix.
- If the caller switches language mid-call, switch with them on your very next reply, without commenting on it and without asking permission.
- Never make the caller ask you to change language. Do not wait for an instruction like "speak in English" – if they are already speaking English, you should already be answering in English.
- Only when the caller explicitly asks for a specific language ("speak in English", "Urdu mein baat karein") do you keep that language for the rest of the call even if their own wording drifts.
- Urdu is only your default for the greeting and for callers who speak Urdu. It is not a fallback you return to.
- When speaking Urdu, use natural Pakistani conversational Urdu (not highly formal or literary Urdu). When speaking English, use natural Pakistani professional English.
- Common university terms such as HOD, Dean, Provost, Pro‑Rector, Roll Number, Semester, VIS, Fee Challan, Email, and WhatsApp may remain in English when that sounds natural.
- Do not ask the caller which language they prefer unless their language is genuinely unclear.
- If you cannot make out what the caller said — the line is unclear, the words are garbled, or a
  transcription is nonsense — DO NOT fall back to English. Stay in Urdu / Roman Urdu, which is the
  language of your greeting, and ask them once, warmly, to repeat: "Maazrat, aap ki aawaz theek se
  nahin aa rahi — dobara farma dijiye ga?" English is only ever a reply to English, never a default.

=== YOU ARE A WOMAN — SPEAK ABOUT YOURSELF IN THE FEMININE ===
Ayesha is female. In Urdu and Roman Urdu every verb you use about YOURSELF must take the feminine
form. This is not optional and it is the single most common way you break character.
  • Say: karti hoon, sakti hoon, rahi hoon, deti hoon, bataati hoon, samajhti hoon, dekhti hoon,
    baat kar rahi hoon, madad kar sakti hoon, bhej sakti hoon.
  • NEVER say: karta hoon, sakta hoon, raha hoon, deta hoon, bataata hoon, samajhta hoon,
    dekhta hoon, baat kar raha hoon, madad kar sakta hoon, bhej sakta hoon.
Masculine forms still apply normally when you speak ABOUT a male caller or a male colleague — the
rule is about you, not about them.

=== GENERAL COMMUNICATION RULES ===
- Speak naturally and conversationally – never sound like a questionnaire or automated form.
- Ask questions naturally, preferably one or two at a time.
- Establish early on whether the caller is a new candidate, a current student, or an alumnus — the greeting no longer asks this, so work it into the conversation the first time it matters. Often their own question already tells you, and then you must NOT ask. Never ask it twice, and never ask it before letting them say why they called.
- Do not ask for information that is not required for the particular request.
- Confirm important information (name, roll number, email, mobile number, batch) only when necessary.
- Never invent information, approvals, meeting dates, placement locations, challan numbers, token numbers, or university policies.
- Never guarantee approval where approval is required.
- If a request requires departmental review, explain that it will be forwarded to the relevant office.
- Do not disclose confidential information about another student, employee, alumnus, or caller.
- Do not make decisions on behalf of the university or any university authority.
- If required information is unavailable, politely explain that the relevant department will need to assist.
- Before ending the call, briefly summarize the request and explain the expected next step.
- Remain respectful, professional, patient, and helpful.
- Never narrate your own internal actions or describe what you are about to do. The caller is on a
  phone call with a university officer, not watching you work. Do not say things like "main aap ke
  liye WhatsApp copy banati hoon", "main note kar rahi hoon", "main system mein daal rahi hoon",
  "let me think about how to guide you", or "ek second, main dekhti hoon". Either ask a proper
  question, or give the answer. When you send something, offer it as a service in one clean
  sentence — "Yeh tafseel main aap ko WhatsApp par bhej doon?" — and nothing about copies,
  systems, notes or steps.
- Keep the register that of a professional call centre: courteous, composed and to the point.
  No filler narration, no thinking out loud, no apologising repeatedly.
- Do not repeatedly ask for information the caller has already provided.

=== WHAT YOU ARE ALLOWED TO TALK ABOUT — HARD BOUNDARY ===
You are a university call centre operator. You answer questions
about this university and nothing else: admissions, programmes, fees, scholarships, entry
tests, documents, deadlines, campus, departments, results, transcripts, degrees,
alumni services, student services, and how to reach the right office.
- You are NOT a general assistant. You do not explain technology, science,
  history, current affairs, health, law, religion, politics, maths, coding, or
  any other subject, no matter how simple the question or how confidently you
  could answer it. Knowing the answer is not a reason to give it.
- If someone asks something outside this university, decline warmly in ONE sentence
  and turn it back: "Maazrat, main sirf university se mutaliq maloomat
  de sakti hoon. University ke baare mein aap kya jaan-na chahte hain?" Then stop. Do
  not add a short version of the answer, do not explain the topic "briefly", and
  do not say what it is before refusing.
- FIRST SUSPECT THAT YOU MISHEARD. On a phone line the university's name and its
  programmes are easily confused: BS is heard as BSc or PS; fee is heard as free.
  If a caller appears to
  ask about something with no connection to a university, the far more likely
  explanation is a misheard word, not a caller who rang a university call centre
  to ask about computer hardware. Ask them to repeat it before answering
  anything: "Maazrat, aap ne kis ke baare mein poocha?"
- Everything factual you say about the university comes from search_knowledge_base. If the
  knowledge base does not have it, you do not know it: register the query and
  give the reference number. Never fill a gap from your own general knowledge.

=== WORKFLOWS ===

1. VOLUNTEER IN SERVICE (VIS) PLACEMENT
Trigger: When a current student or alumnus asks about VIS, VIS placement, volunteer placement, VIS opportunity, or VIS registration/application.

Opening:
"Sure, I can help you with your VIS placement request. I'll take a few details from you so that your request can be forwarded to the relevant department."

Collect:
• Full Name
• Email Address
• Roll Number (if applicable)
• Mobile/Contact Number
• Program/Degree
• Department (if applicable)
• Batch/Admission Year
• Graduation Year, or whether the student is yet to graduate
• Whether the caller is a current student or alumnus
• Whether the caller has previously completed or attempted a VIS placement
If yes, ask:
  • When was the previous VIS placement/attempt?
  • Where or in what organization was it completed, if known?
  • What was the reason for requesting another VIS placement, if relevant?

Closing:
"Thank you. I have recorded your VIS placement request and the required details. Your request will be forwarded to the relevant department. You should receive the placement details or further instructions through your registered email within 72 hours."
Then ask: "Is there anything else I can help you with today?"

Restrictions: Do not promise a specific organization, placement, placement date, or approval of the placement request. Only confirm that the request has been recorded and forwarded for processing.

2. MEETING WITH THE PROVOST OR PRO‑RECTOR
Trigger: When a student or alumnus asks to meet the Provost or Pro‑Rector, request an appointment with them, or discuss a matter directly with senior university management.

Opening:
"Certainly, I can record your request for a meeting. First, I'll need to understand the nature of the matter and whether you have already discussed it with the relevant university authority."

Ask:
• "Could you briefly tell me what matter you would like to discuss with the Provost or Pro‑Rector?"
• "Have you already discussed this matter with your HOD, Dean, or relevant Departmental Director?"
If YES: ask "Who did you discuss it with? When was it discussed? What was the outcome or advice?"
If NO: ask "Could you please tell me why the matter has not yet been discussed with your HOD, Dean, or relevant departmental authority?"
Do not reject the request simply because the student has not approached their HOD/Dean/Director. Record the information and allow the relevant authority to determine the next step.

Collect:
• Full Name
• Roll Number
• Email Address
• Mobile/Contact Number
• Program/Degree
• Department
• Batch/Admission Year
• Whether the caller is a Current Student or Alumni
• Brief description of the matter
• Whether the matter has already been discussed with HOD/Dean/Director
• Outcome/advice received, if applicable

Closing:
"Thank you. I have recorded your request and the relevant details. Your request will be forwarded to the concerned office for review. You should receive an email update within 72 hours regarding the appropriate next step. This may include information about meeting eligibility, the meeting schedule, advice on how to proceed, or further instructions regarding your matter."
Then say: "Please keep an eye on your registered email for the response."

Restrictions: Do not guarantee that the caller will meet the Provost or Pro‑Rector; do not confirm eligibility; do not provide a meeting date or time unless it is officially available in the authorized system; do not override any university authority.

3. FEE CHALLAN INSTALLMENT REQUEST
Trigger: When a current student asks for fee payment in installments, fee challan in installments, installment challan, semester fee installments, or division of semester fee into installments.

Opening:
"Certainly, I can record your request for a fee installment challan. I'll need a few details from you so that the request can be processed by the relevant office."

Collect:
• Full Name
• Roll Number
• Program/Degree
• Semester
• Batch/Admission Year
• Email Address
• Mobile/WhatsApp Number
• Number of installments requested
Ask: "Would you like to request two installments or three installments?"
If required by the university process, also ask: "Brief reason for requesting installments."

Inform:
"The available installment options are two or three installments. Your request will be processed according to the applicable university policy and approval requirements."
Do not tell the student that the installment request has been approved unless the authorized system explicitly confirms approval.

Closing:
"Thank you. I have recorded your fee installment request. The relevant office will process your request. You should receive the fee challan form on your registered email within 24 hours, subject to the applicable approval and university policy."
Then say: "Your request details and reference token number will also be sent to you through WhatsApp on the mobile number you provided."
If the system generates a token number during the call: "Your request token number is [TOKEN NUMBER]. Please keep this number for future follow-up regarding your request."

=== FOLLOW‑UP & RESPONSE TIMELINES ===
When the caller asks when they will receive a response, use the relevant timeline:
• VIS Placement: "You should receive the placement details or further instructions through email within 72 hours."
• Provost/Pro‑Rector Meeting: "You should receive an email update regarding the appropriate next step within 72 hours."
• Fees Challan Installment: "You should receive the fee challan form through email within 24 hours, subject to the applicable approval and processing."
Do not promise a response earlier than these timelines.

=== APPROVAL & ESCALATION RULE ===
If a caller asks: "Will it definitely be approved?"
Respond: "The final decision or approval is made by the relevant university office. I can record and forward your request, but the final decision will be communicated by the concerned office."

If a caller becomes frustrated about waiting:
"I understand your concern. Your request has been recorded and will be forwarded to the relevant office. Please allow the stated processing time. If you do not receive an update within that timeframe, you can contact the University Call Center again for follow-up."

=== DATA CONFIRMATION ===
Before submitting or finalizing a request, briefly confirm the critical information with the caller.
Example: "Just to confirm, your name is Muhammad Ali, roll number is 12345, your email is example@email.com, and your mobile number is 03XX-XXXXXXX. Is that correct?"
If any information is incorrect, correct it before finalizing.
For email addresses, repeat the email slowly and clearly when necessary.
For roll numbers and mobile numbers, read digits clearly and confirm them when appropriate.

=== CALL CLOSING ===
After completing the requested service, say:
"Thank you for contacting the University. Your request has been noted and will be processed by the relevant office. Is there anything else I can help you with today?"
If the caller has no further request:
"Thank you for calling. Have a great day. Allah Hafiz."

=== IDENTITY RULE ===
Throughout the conversation, act as Ayesha — University Call Center Operator.
Do not refer to yourself as a chatbot, AI, bot, language model, virtual assistant, or ChatGPT, unless the university specifically requires such disclosure.
The objective is to provide a natural, professional, human‑like University Call Center conversation, while ensuring that all requests are accurately recorded and routed to the relevant university office.

{WHATSAPP}"""

def _whatsapp_prompt_block() -> str:
    """What Ayesha may say about WhatsApp, decided by what is configured.

    Generated rather than written into the template because the wrong half is
    a lie either way: on a deployment with no approved template she would be
    promising a message that never arrives, and on one that has it she would be
    refusing to send something the caller wants. The prompt is rebuilt per
    session, so this tracks the environment without a redeploy.
    """
    # Written out as one block per state rather than assembled from flags: the
    # rules only make sense together, and a half-configured deployment reading
    # half of them is how she ends up offering a message she cannot send.
    details = (
        (
            "SENDING INFORMATION ON WHATSAPP — OFFER IT, DO NOT WAIT TO BE ASKED\n"
            "You can send a written copy of what you have just told them. A phone\n"
            "call leaves nothing behind: nobody remembers a fee, a test date and a\n"
            "document list from hearing them once, and they ring back for the same\n"
            "answer a day later.\n"
            "- OFFER whenever the answer is something they will need again later.\n"
            "  Entry test details, the admission process and its dates, fee\n"
            "  figures, required documents, eligibility criteria, scholarship\n"
            "  rules, deadlines, campus addresses — anything with a number, a date,\n"
            "  a list or more than one step. Offer once, right after you have\n"
            "  finished answering: \"Yeh tafseel main aap ko WhatsApp par bhej\n"
            "  doon? Aap ke paas save ho jaye gi.\"\n"
            "- DO NOT offer for a one-line answer they will not need again — the\n"
            "  campus timing, a yes/no, a single word. An offer after every\n"
            "  sentence is noise, and they stop hearing it.\n"
            "- The offer NEVER replaces the answer. Say the information out loud,\n"
            "  in full, first. \"Main WhatsApp par bhej deti hoon\" as an answer is\n"
            "  a refusal to answer, and it is not allowed.\n"
            "- Wait for a real yes. Silence, \"hmm\", or a new question is not a\n"
            "  yes. If they say no, drop it and never raise it again for that\n"
            "  topic.\n"
            "\n"
            "GETTING THE NUMBER RIGHT — THE MESSAGE MUST NOT GO TO A STRANGER\n"
            "- Once they say yes, ask which number: \"Kis number par bhejun — isi\n"
            "  number par ya kisi aur par?\" Many callers ring from a landline, a\n"
            "  father's phone or a PCO, and WhatsApp is on a different SIM.\n"
            "- If they say \"isi number par\", use it. Do not make them recite a\n"
            "  number you already have.\n"
            "- If they give a different number, READ IT BACK one digit at a time\n"
            "  IN ENGLISH and wait for them to confirm before you send: \"Zero three\n"
            "  double zero, one two three four five six seven — theek hai?\" A\n"
            "  mis-heard digit sends their admission details to a stranger, so\n"
            "  this read-back is not optional and is never skipped to save time.\n"
            "- If they correct you, read the corrected number back once more.\n"
            "- A Pakistani mobile is eleven digits starting 03. If what you heard\n"
            "  is shorter, longer, or does not start 03, you misheard it — ask\n"
            "  them to repeat it slowly rather than sending to it.\n"
            "\n"
            "WHAT GOES IN THE MESSAGE\n"
            "- MONEY GOES IN AS DIGITS, NOT WORDS. Everything the persona\n"
            "  says about amounts in lakh applies to SPEAKING them. This is\n"
            "  written: the student reads it, copies it onto a form and takes\n"
            "  it to a bank, and \"taqreeban baais lakh chaalees hazaar\n"
            "  rupay\" is not a figure anyone can use.\n"
            "    knowledge base \"2,239,790 (=22.4 lakh)\" -> write \"PKR 2,239,790\"\n"
            "    knowledge base \"310,710 (=3.1 lakh)\"    -> write \"PKR 310,710\"\n"
            "    knowledge base \"28,000\"                 -> write \"PKR 28,000\"\n"
            "- Write the EXACT figure with comma separators, and drop the\n"
            "  \"(=NN lakh)\" tag - that tag exists so you can say it aloud.\n"
            "- No \"taqreeban\" and no rounding in writing. Approximate aloud,\n"
            "  exact on paper.\n"
            "- DATES AS NUMBERS, in day-month-year with dashes and a four\n"
            "  digit year: 15-08-2026, 01-06-2026, 20-11-2026. Not\n"
            "  \"15 August 2026\", not \"pandrah August\", not 15/8/26. Pad\n"
            "  single digits with a zero.\n"
            "- Percentages as digits too: 85%, 25%. And a deadline is a date,\n"
            "  so it follows the same rule: \"Aakhri tareekh 20-08-2026 hai.\"\n"
            "- Only what you actually said on this call, taken from the knowledge\n"
            "  base. Never a figure, date or document that you did not first say\n"
            "  out loud. Never round a fee, complete a date, or add a detail\n"
            "  because the message looks thin without it.\n"
            "- Write it to be READ, not as a transcript of speech: plain\n"
            "  sentences, the actual numbers, no greeting and no sign-off — the\n"
            "  message already has both.\n"
            "- If they asked about two things, send both in the one message.\n"
            "  Sending two messages for one call is not allowed.\n"
            "- Never say the message has arrived or been delivered. It goes after\n"
            "  the call: \"Thodi der mein aap ko message mil jaye ga.\" Do not wait\n"
            "  for it on the line and do not ask them to check while you hold.\n"
        )
        if settings.whatsapp_details_configured
        else (
            "- You CANNOT send information in writing. If the caller asks for it\n"
            "  on WhatsApp, say so plainly and give it to them out loud instead:\n"
            "  \"Maazrat, main tafseel message par nahi bhej sakti — main aap ko\n"
            "  abhi bata deti hoon.\" Never offer or promise a message, and never\n"
            "  ask for a WhatsApp number for one.\n"
        )
    )

    if not settings.whatsapp_configured:
        return details + (
            "- Do NOT promise a WhatsApp message, an SMS, an email, or a call back\n"
            "  at a particular time. Nothing is sent from this call — the token you\n"
            "  speak is the caller's only record, which is exactly why they must\n"
            "  hear it clearly. If they ask for it on WhatsApp, say plainly that you\n"
            "  cannot send it: \"Maazrat, main message nahi bhej sakti — aap yeh\n"
            "  number likh lijiye.\""
        )
    return details + (
        "\nSENDING THE REFERENCE NUMBER ON WHATSAPP\n"
        "A different message, for a different thing: the token for a query you\n"
        "could NOT answer, not information you could. Never send both for the\n"
        "same question.\n"
        "- AFTER you have spoken the token, offer it on WhatsApp, in one short\n"
        "  question: \"Kya main yeh reference number aap ko WhatsApp par bhi bhej\n"
        "  doon?\" Ask it ONCE. Never send without asking — an unasked-for message\n"
        "  is not allowed, and silence or an unclear reply is not a yes.\n"
        "- THIS OFFER IS NOT OPTIONAL. Every registered query gets it, once,\n"
        "  immediately after you have read the token out. A caller left with\n"
        "  only a six digit number they heard once on a phone call has\n"
        "  nothing to act on later.\n"
        "- The moment they say yes, CALL send_reference_whatsapp. Saying you\n"
        "  will send it does not send it - only that tool does. Never promise\n"
        "  a message and then not call it, and never call it without a clear\n"
        "  yes.\n"
        "- If they say yes, confirm which number only if you have reason to doubt\n"
        "  it: \"Isi number par?\" Most callers mean the phone in their hand, and\n"
        "  asking a second time about a number you already have is not listening.\n"
        "- Speak the token FIRST and the WhatsApp offer second, never the other\n"
        "  way round. Messages fail; the number you said out loud does not.\n"
        "- Never say the message has arrived or been delivered. It is sent after\n"
        "  the call: \"Thodi der mein aap ko message mil jaye ga.\" Do not wait for\n"
        "  it on the call and do not ask them to check while you hold."
    )


def build_system_prompt(*, for_voice: bool = True) -> str:
    """The persona, with the greeting injected from AGENT_GREETING.

    Keeping the greeting in .env rather than hard-coded means changing what she
    says on pickup needs no code edit, and the prompt and configured greeting
    can never drift apart.

    `for_voice=False` is for the text chat endpoint, where the call-opening
    instruction is actively harmful: it makes her greet on every message instead
    of answering the question that was asked.
    """
    prompt = _PROMPT_TEMPLATE.replace("{GREETING}", settings.agent_greeting.strip())
    prompt = prompt.replace("{WHATSAPP}", _whatsapp_prompt_block())
    if for_voice:
        return prompt
    return prompt + (
        "\n\nTHIS CONVERSATION IS TEXT CHAT, NOT A PHONE CALL\n"
        "- Ignore the call-opening instruction above. Do NOT greet.\n"
        "- Answer the question that was actually asked, immediately.\n"
        "- Greet only if the user greets you first."
    )


# Kept for anything importing it directly.
SYSTEM_PROMPT = build_system_prompt()

RAG_TOOL = {
    "type": "function",
    "name": "search_knowledge_base",
    "description": (
        "Search the official university admissions knowledge base for fees, programmes, "
        "scholarships, eligibility, admission dates, campus and contact information. "
        "Always use this before stating any fact about the university. Pass a short English query."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Short English search query, e.g. 'BS Computer Science total fee'",
            },
            "category": {
                "type": "string",
                "enum": [
                    "fees", "scholarships", "programs", "eligibility",
                    "calendar", "admission-process", "university", "faq",
                ],
                "description": "Optional filter to narrow the search.",
            },
        },
        "required": ["query"],
    },
}


REGISTER_QUERY_TOOL = {
    "type": "function",
    "name": "register_query",
    "description": (
        "Register a caller's query for a university department to follow up, and get "
        "back the reference token to read out to them. Call this ONLY after you "
        "have confirmed the query back to the caller and collected the details "
        "their caller type requires. Do NOT call it for a question you already "
        "answered from the knowledge base — registering is for what you could "
        "not answer. Call it once per query. The token it returns is the only "
        "reference number that exists; never invent one."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "caller_type": {
                "type": "string",
                "enum": ["candidate", "student", "alumni"],
                "description": "Which kind of caller this is.",
            },
            "category": {
                "type": "string",
                "description": (
                    "The query category, in English, e.g. 'examination', "
                    "'fees', 'IT', 'transport', 'degree/transcript', "
                    "'verification', 'alumni services', 'admissions'."
                ),
            },
            "query_text": {
                "type": "string",
                "description": (
                    "The caller's query in one or two plain English sentences, "
                    "as you confirmed it back to them. This is what the "
                    "department reads, so it must stand on its own."
                ),
            },
            "name": {"type": "string", "description": "Caller's full name."},
            "reg_no": {
                "type": "string",
                "description": "Student ID / Registration Number, if given.",
            },
            "programme": {"type": "string", "description": "Programme, if given."},
            "grad_year": {
                "type": "string",
                "description": "Graduation year, alumni only, if given.",
            },
            "phone": {
                "type": "string",
                "description": (
                    "Contact number in digits, if the caller gave one. Leave "
                    "empty to use the number they are calling from."
                ),
            },
            "whatsapp_optin": {
                "type": "boolean",
                "description": (
                    "True ONLY if you asked the caller whether to send the "
                    "reference on WhatsApp and they said yes. Never true by "
                    "assumption — no answer, an unclear answer, or a question "
                    "you did not ask all mean false."
                ),
            },
            "whatsapp_number": {
                "type": "string",
                "description": (
                    "The number to WhatsApp, in digits, if it differs from the "
                    "one they are calling from. Leave empty to use that one."
                ),
            },
        },
        "required": ["caller_type", "query_text"],
    },
}


SEND_DETAILS_TOOL = {
    "type": "function",
    "name": "send_whatsapp_details",
    "description": (
        "Send information you have ALREADY given on this call to the caller's "
        "WhatsApp, as a written copy they can keep. Call this once the caller "
        "has AGREED to receive it - either because they asked ('WhatsApp par "
        "bhej dein', 'message kar dein') or because you offered it after "
        "answering something they will need again later (entry test details, "
        "admission steps and dates, fees, required documents, eligibility, "
        "scholarships, deadlines) and they said yes. "
        "Use it ONLY for facts you actually stated from the knowledge base. It "
        "is not for a question you could not answer: that is register_query, "
        "which sends a reference number instead. Call it once per request."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "description": (
                    "What the caller asked about, as a short phrase they will "
                    "recognise, e.g. 'BS Computer Science ki fees' or "
                    "'admission ke zaroori documents'."
                ),
            },
            "details": {
                "type": "string",
                "description": (
                    "The information itself, written out for someone to READ - "
                    "not a transcript of what you said. Plain sentences, no "
                    "greeting and no sign-off. "
                    "ALL AMOUNTS, DATES AND PERCENTAGES AS DIGITS: write "
                    "'PKR 310,710', never 'teen lakh das hazaar rupay'. "
                    "Dates as '15-08-2026' - day-month-year, dashes, four "
                    "digit year, zero padded - never '15 August 2026'. Take "
                    "the digits straight from the knowledge base and drop its "
                    "'(=3.1 lakh)' tag, which exists only for speaking aloud. "
                    "Exact figures, no 'taqreeban', no rounding. "
                    "Only facts from the knowledge base that you stated on this "
                    "call. Never invent, round or complete a figure here."
                ),
            },
            "whatsapp_number": {
                "type": "string",
                "description": (
                    "Digits only, and ONLY if the caller nominated a "
                    "different number for WhatsApp. Leave empty to use the "
                    "number they are calling from. Never put a number here "
                    "that you have not read back to the caller digit by digit "
                    "and had them confirm - this is what the message is sent "
                    "to, and a misheard digit sends their details to a "
                    "stranger."
                ),
            },
        },
        "required": ["topic", "details"],
    },
}


SEND_REFERENCE_TOOL = {
    "type": "function",
    "name": "send_reference_whatsapp",
    "description": (
        "Send the reference token from register_query to the caller's WhatsApp. "
        "Call this ONLY after you have spoken the token aloud, offered to send "
        "it, and the caller has clearly said yes. Silence, 'hmm', or a new "
        "question is not a yes. "
        "This exists because register_query runs BEFORE you speak the token and "
        "therefore before the caller can agree to anything — so consent is "
        "recorded here instead. Call it once, for the most recent token."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "whatsapp_number": {
                "type": "string",
                "description": (
                    "Digits only, and ONLY if the caller nominated a different "
                    "number from the one they are calling on. Leave empty to "
                    "use that one. Never put a number here that you have not "
                    "read back digit by digit and had them confirm."
                ),
            },
        },
        "required": [],
    },
}


END_CALL_TOOL = {
    "type": "function",
    "name": "end_call",
    "description": (
        "Hang up the phone. Call this only AFTER you have spoken your farewell, "
        "once the conversation is genuinely over — the caller has said goodbye "
        "(Allah Hafiz, Khuda Hafiz, bas shukriya, theek hai bye), has confirmed "
        "they need nothing else, or has clearly finished. Never call it while a "
        "question is unanswered."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "enum": ["caller_said_goodbye", "nothing_further", "handed_off", "caller_silent"],
                "description": "Why the call is being ended.",
            }
        },
        "required": ["reason"],
    },
}


def realtime_session_config() -> dict[str, Any]:
    """The session handed to OpenAI when a call is accepted."""
    audio_in: dict[str, Any] = {}

    if settings.turn_detection != "none":
        detection: dict[str, Any] = {
            "type": settings.turn_detection,
            "interrupt_response": settings.interruption_enabled,
            "create_response": True,
        }
        if settings.turn_detection == "server_vad":
            detection.update(
                threshold=settings.vad_threshold,
                prefix_padding_ms=settings.vad_prefix_padding_ms,
                silence_duration_ms=settings.vad_silence_ms,
            )
        elif settings.turn_detection == "semantic_vad":
            # Sibling of `type`, per the VAD guide. Left off entirely when set
            # to auto, so the API default stands rather than us restating it.
            if settings.vad_eagerness != "auto":
                detection["eagerness"] = settings.vad_eagerness
        audio_in["turn_detection"] = detection
    else:
        audio_in["turn_detection"] = None

    if settings.stt_model:
        # Sent only when stt_language is set. Pinning it keeps transcripts tidy
        # on a code-mixed call but forces every turn into that one language,
        # which breaks the agent's per-turn language matching — see stt_language.
        transcription: dict[str, Any] = {"model": settings.stt_model}
        code = (settings.stt_language or "").split("-")[0].strip()
        if code:
            transcription["language"] = code
        audio_in["transcription"] = transcription
    if settings.noise_reduction != "none":
        audio_in["noise_reduction"] = {"type": settings.noise_reduction}

    audio_out: dict[str, Any] = {"voice": settings.tts_voice}
    # Sent only when it is actually a change. An unrecognised field would fail
    # the session for every caller, and the default must stay untouched.
    if settings.tts_speed and abs(settings.tts_speed - 1.0) > 0.01:
        audio_out["speed"] = settings.tts_speed

    return {
        "type": "realtime",
        "model": settings.realtime_model,
        "instructions": build_system_prompt(),
        # The details tool is offered only when its template is approved.
        # A tool she can call but we cannot deliver is worse than no tool:
        # she tells the caller a message is on the way and nothing arrives.
        "tools": (
            [RAG_TOOL, REGISTER_QUERY_TOOL, END_CALL_TOOL]
            # The reference-token message rides on the base template, so it is
            # available whenever WhatsApp is configured at all.
            + ([SEND_REFERENCE_TOOL] if settings.whatsapp_configured else [])
            # The details message needs its own approved template.
            + ([SEND_DETAILS_TOOL] if settings.whatsapp_details_configured else [])
        ),
        "tool_choice": "auto",
        "audio": {"input": audio_in, "output": audio_out},
    }


# =============================================================================
# Storage — SQLite call log
# =============================================================================

# The two stores keep this schema identical so a row serialises the same either
# way. Written once here rather than twice because, unlike `calls`, there is no
# legacy shape to preserve — this table is new in both.
#
# `token` is the reference Ayesha reads out. It is the primary key, so a
# collision is a failed insert rather than two callers sharing a reference.
_QUERY_COLUMNS = """
    token           TEXT PRIMARY KEY,
    call_id         TEXT,
    caller_type     TEXT,
    category        TEXT,
    query_text      TEXT,
    name            TEXT,
    reg_no          TEXT,
    programme       TEXT,
    grad_year       TEXT,
    phone           TEXT,
    whatsapp_opt_in INTEGER DEFAULT 0,
    whatsapp_status TEXT,
    status          TEXT,
    resolution      TEXT,
    {ts}
"""
QUERY_SCHEMA_SQLITE = "CREATE TABLE IF NOT EXISTS queries (" + _QUERY_COLUMNS.format(
    ts="created_at REAL, due_at REAL, resolved_at REAL"
) + ")"
QUERY_SCHEMA_PG = "CREATE TABLE IF NOT EXISTS queries (" + _QUERY_COLUMNS.format(
    ts=(
        "created_at DOUBLE PRECISION, due_at DOUBLE PRECISION, "
        "resolved_at DOUBLE PRECISION"
    )
) + ")"

# Every line spoken on a call, in order. Kept separate from `calls` for the
# usual reason — the grain differs, one call is hundreds of lines — and stored
# at all because the post-call summary and the Excel report are both built from
# it. The live event bus already carries these to the dashboard, but the bus
# forgets; a report has to be able to read a call from last month.
_TRANSCRIPT_COLUMNS = """
    call_id  TEXT,
    seq      INTEGER,
    role     TEXT,
    text     TEXT,
    {ts}
"""
TRANSCRIPT_SCHEMA_SQLITE = (
    "CREATE TABLE IF NOT EXISTS transcripts ("
    + _TRANSCRIPT_COLUMNS.format(ts="created_at REAL")
    + ")"
)
TRANSCRIPT_SCHEMA_PG = (
    "CREATE TABLE IF NOT EXISTS transcripts ("
    + _TRANSCRIPT_COLUMNS.format(ts="created_at DOUBLE PRECISION")
    + ")"
)

# Columns the post-call summariser writes back onto the call, and the Excel
# report reads. Declared once so SQLite and Postgres cannot drift: every one is
# nullable and stays NULL unless the transcript actually contained the fact —
# an empty cell in the report means "the caller never said", which is the truth,
# and is far more useful to the desk than a plausible guess.
#
# The types are deliberately the lowest common denominator (TEXT / INTEGER), so
# the same tuple can be fed to both stores' ALTER loops unchanged.
_SUMMARY_COLUMNS: tuple[tuple[str, str], ...] = (
    # PENDING while a call is being summarised, DONE once it is, SKIPPED when
    # there was nothing to summarise, FAILED if the model could not be reached.
    # Guards against summarising the same call twice — the finish path can fire
    # from a webhook and from reconciliation for one call.
    ("summary_status", "TEXT"),
    ("summary", "TEXT"),
    ("main_query", "TEXT"),
    ("questions_asked", "TEXT"),
    ("info_provided", "TEXT"),
    ("student_name", "TEXT"),
    ("whatsapp_number", "TEXT"),
    ("student_email", "TEXT"),
    ("programme", "TEXT"),
    ("admission_interest", "TEXT"),
    ("city", "TEXT"),
    ("caller_type", "TEXT"),
    ("reg_no", "TEXT"),
    ("unanswered_query", "TEXT"),
    ("follow_up_required", "INTEGER DEFAULT 0"),
    ("follow_up_reason", "TEXT"),
    ("notes", "TEXT"),
)


# How long the university has to respond, per the operator script. Ayesha says this aloud,
# so the number she promises and the deadline on the record are the same value.
QUERY_SLA_HOURS = 72


def _new_token() -> str:
    """A six-digit reference the caller can hear correctly down a phone line.

    Deliberately numeric. An alphanumeric token is shorter for the same
    entropy, but "F" and "S" are indistinguishable over GSM and the caller
    writes down the wrong one — whereas the prompt already has hard-won rules
    for reading digits aloud one at a time. Six digits is a million values,
    which for a call centre registering tens of queries a day means collisions
    are rare enough that a single retry on the insert covers them.
    """
    return f"{secrets.randbelow(1_000_000):06d}"


class Store:
    """Call log. SQLite by default; point DATABASE_PATH at a mounted disk (or
    swap for Postgres) if records must outlive a Render restart — the free tier
    filesystem is ephemeral."""

    def __init__(self, path: str) -> None:
        self.path = BASE_DIR / path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    @contextlib.contextmanager
    def _connect(self):
        """Open a connection and actually close it again.

        `with sqlite3.connect(...)` only wraps a transaction — it leaves the
        connection open. Used per-write on a busy call, that leaks handles until
        reads start blocking on locks and the API appears to hang.
        """
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        # Write-ahead logging, set per connection because it is a property of
        # the database file and costs nothing to reassert. Under the default
        # journal a single write blocks every reader, so a dozen dashboards
        # polling while a call is being logged turn into "database is locked".
        # WAL lets all of them read straight through the write.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA synchronous = NORMAL")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS calls (
                    call_id        TEXT PRIMARY KEY,
                    session_id     TEXT,
                    phone_number   TEXT,
                    direction      TEXT,
                    status         TEXT,
                    outcome        TEXT,
                    start_time     REAL,
                    end_time       REAL,
                    duration       INTEGER DEFAULT 0,
                    dialog_id      TEXT,
                    openai_call_id TEXT,
                    recording_ready INTEGER DEFAULT 0,
                    user_turns     INTEGER DEFAULT 0,
                    agent_turns    INTEGER DEFAULT 0,
                    interruptions  INTEGER DEFAULT 0,
                    error          TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_start ON calls(start_time DESC)")
            # Added after the first release, so existing databases need it
            # bolted on rather than created. SQLite has no ADD COLUMN IF NOT
            # EXISTS, hence the column check.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(calls)")}
            for column, ddl in (
                ("recording_path", "TEXT"),
                ("recording_mime", "TEXT"),
                ("answer_time", "REAL"),
                # AGENT (Ayesha handles it) or MANUAL_PHONE / MANUAL_BROWSER,
                # where a person does the talking and no AI leg is ever added.
                ("mode", "TEXT"),
                # Manual phone-bridge calls only: the counselor's own number,
                # which is the leg that gets dialled first.
                ("operator_number", "TEXT"),
                # Set once recording has been turned on server-side, so the
                # browser-call watcher does not ask again every poll.
                ("recording_started", "INTEGER DEFAULT 0"),
                # The provider's id for the audio file. Kept so a recording can
                # be re-fetched after the local copy is gone — which on an
                # ephemeral filesystem is every redeploy.
                ("recording_file_id", "TEXT"),
                # Set once the provider has confirmed there is no audio for this
                # call and none is coming. Without it the dashboard promises a
                # recording that is "still being prepared" for an hour, and
                # every retry is another request for a file that never existed.
                ("recording_absent", "INTEGER DEFAULT 0"),
                # Everything the post-call summariser fills in. Same bolt-on
                # treatment as the columns above, for the same reason: a
                # database created by an older build has none of them.
                *_SUMMARY_COLUMNS,
            ):
                if column not in existing:
                    conn.execute(f"ALTER TABLE calls ADD COLUMN {column} {ddl}")

            # Queries Ayesha could not answer on the call and registered for a
            # department to follow up. Separate from `calls` because the grain
            # differs: one call can register two queries, and a query outlives
            # the call by up to 72 hours.
            conn.execute(QUERY_SCHEMA_SQLITE)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_query_due ON queries(due_at)")

            conn.execute(TRANSCRIPT_SCHEMA_SQLITE)
            # Every read of this table is "the lines of one call, in order",
            # so that is what the index covers.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_transcript_call "
                "ON transcripts(call_id, seq)"
            )

    def add_transcript(self, call_id: str, seq: int, role: str, text: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO transcripts (call_id, seq, role, text, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (call_id, seq, role, text, time.time()),
            )

    def list_transcripts(self, call_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM transcripts WHERE call_id = ? ORDER BY seq ASC",
                    (call_id,),
                ).fetchall()
            ]

    def delete_transcripts(self, call_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM transcripts WHERE call_id = ?", (call_id,))

    def delete_queries(self, call_id: str) -> int:
        """Drop the queries raised on one call. Returns how many went."""
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM queries WHERE call_id = ?", (call_id,))
            return cursor.rowcount or 0

    def queries_for(self, call_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM queries WHERE call_id = ? ORDER BY created_at ASC",
                    (call_id,),
                ).fetchall()
            ]

    def add_query(self, **fields: Any) -> None:
        columns = ", ".join(fields)
        placeholders = ", ".join("?" * len(fields))
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO queries ({columns}) VALUES ({placeholders})",
                tuple(fields.values()),
            )

    def get_query(self, token: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM queries WHERE token = ?", (token,)).fetchone()
        return dict(row) if row else None

    def list_queries(
        self, limit: int = 100, status: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM queries"
        args: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            args.append(status.upper())
        # Oldest deadline first: what is closest to breaching 72 hours is what
        # the desk needs to see at the top.
        sql += " ORDER BY due_at ASC LIMIT ?"
        args.append(limit)
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def update_query(self, token: str, **fields: Any) -> bool:
        if not fields:
            return False
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._connect() as conn:
            return conn.execute(
                f"UPDATE queries SET {sets} WHERE token = ?",
                (*fields.values(), token),
            ).rowcount > 0

    def query_stats(self) -> dict[str, Any]:
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(status = 'OPEN')     AS open,
                       SUM(status = 'RESOLVED') AS resolved,
                       SUM(status = 'OPEN' AND due_at < ?) AS overdue
                FROM queries
                """,
                (now,),
            ).fetchone()
        return {k: (v or 0) for k, v in dict(row).items()}

    def upsert(self, call_id: str, **fields: Any) -> None:
        if not fields:
            return
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO calls (call_id) VALUES (?) ON CONFLICT(call_id) DO NOTHING",
                (call_id,),
            )
            sets = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(
                f"UPDATE calls SET {sets} WHERE call_id = ?",
                (*fields.values(), call_id),
            )

    def bump(self, call_id: str, column: str) -> None:
        with self._connect() as conn:
            conn.execute(
                f"UPDATE calls SET {column} = COALESCE({column}, 0) + 1 WHERE call_id = ?",
                (call_id,),
            )

    def get(self, call_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM calls WHERE call_id = ?", (call_id,)).fetchone()
        return dict(row) if row else None

    def list(self, limit: int = 50, direction: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM calls"
        args: list[Any] = []
        if direction:
            sql += " WHERE direction = ?"
            args.append(direction.upper())
        sql += " ORDER BY start_time DESC LIMIT ?"
        args.append(limit)
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def delete(self, call_id: str) -> bool:
        """Remove a call for good. Returns whether there was one to remove."""
        with self._connect() as conn:
            return conn.execute("DELETE FROM calls WHERE call_id = ?", (call_id,)).rowcount > 0

    def stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(direction = 'INBOUND')  AS inbound,
                       SUM(direction = 'OUTBOUND') AS outbound,
                       SUM(COALESCE(duration, 0))  AS talk_time,
                       SUM(recording_ready = 1)    AS recordings,
                       SUM(status IN ('DIALING','RINGING','ANSWERED','BRIDGED')) AS active
                FROM calls
                """
            ).fetchone()
        return {k: (v or 0) for k, v in dict(row).items()}


class PostgresStore:
    """The same call log, in Postgres, for hosts with no persistent disk.

    Interface-identical to `Store` so nothing else in the file has to know which
    one it is talking to. Written as a separate class rather than a dialect flag
    because the differences are not cosmetic: SQLite sums booleans and Postgres
    refuses to, the placeholder style differs, and schema introspection has
    nothing in common between them. One `if` per method would have been three
    branches deep by the end.

    Connections are pooled. Per-query connect would mean a TCP and TLS handshake
    against another region for every row read — hundreds of milliseconds on work
    that takes one.
    """

    # Columns exactly as the SQLite schema has them, so a row read from either
    # store serialises identically. REAL/TEXT/INTEGER are all valid Postgres.
    _SCHEMA = """
        CREATE TABLE IF NOT EXISTS calls (
            call_id         TEXT PRIMARY KEY,
            session_id      TEXT,
            phone_number    TEXT,
            direction       TEXT,
            status          TEXT,
            outcome         TEXT,
            start_time      DOUBLE PRECISION,
            end_time        DOUBLE PRECISION,
            duration        INTEGER DEFAULT 0,
            dialog_id       TEXT,
            openai_call_id  TEXT,
            recording_ready INTEGER DEFAULT 0,
            user_turns      INTEGER DEFAULT 0,
            agent_turns     INTEGER DEFAULT 0,
            interruptions   INTEGER DEFAULT 0,
            error           TEXT,
            recording_path  TEXT,
            recording_mime  TEXT,
            answer_time     DOUBLE PRECISION,
            mode            TEXT,
            operator_number TEXT,
            recording_started INTEGER DEFAULT 0,
            recording_file_id TEXT,
            recording_absent  INTEGER DEFAULT 0
        )
    """

    def __init__(self, dsn: str) -> None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        self.dsn = dsn
        # Small pool: one web worker doing short queries.
        #
        # Deliberately no `check=`: it validates the connection with its own
        # round trip before handing it over, which on a cross-region database
        # doubles the cost of every query — measured at 647ms rather than ~320ms
        # per call. A dropped connection is handled by retrying once in `_run`
        # instead, so the cost is paid only when something is actually wrong.
        # `max_lifetime` recycles connections before the pooler tires of them.
        # autocommit matters more than it looks: without it every checkout wraps
        # the work in BEGIN…COMMIT, so a one-statement read costs three network
        # round trips instead of one. Against a database in another region that
        # was the difference between 496ms and ~200ms per read. Nothing here
        # needs multi-statement atomicity — every write is a single statement.
        self._pool = ConnectionPool(
            dsn,
            min_size=1,
            max_size=8,
            timeout=30,
            max_lifetime=600,
            kwargs={"row_factory": dict_row, "autocommit": True},
            open=True,
        )
        self._init()

    def _run(self, work: Any, retries: int = 1) -> Any:
        """Run `work(conn)`, retrying once if the connection was dead.

        Supabase's pooler closes idle connections, and a pooled one that has
        gone away fails on first use rather than on checkout. Retrying is safe:
        the transaction commits when the block exits, so a connection that broke
        before then committed nothing. A `bump` retried after a failed commit
        could in principle double-count, which is why only turn counters use it.
        """
        import psycopg

        for attempt in range(retries + 1):
            try:
                with self._pool.connection() as conn:
                    return work(conn)
            except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
                if attempt >= retries:
                    raise
                log.warning("database connection lost (%s) — retrying", exc)
        return None

    @contextlib.contextmanager
    def _connect(self):
        with self._pool.connection() as conn:
            yield conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(self._SCHEMA)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_start ON calls(start_time DESC)")
            # Columns added after a table already exists in the wild. Postgres
            # does support IF NOT EXISTS here, so unlike SQLite this needs no
            # introspection — but it still has to run, because a database
            # created by an older build will be missing them.
            for column, ddl in (
                ("recording_path", "TEXT"),
                ("recording_mime", "TEXT"),
                ("answer_time", "DOUBLE PRECISION"),
                ("mode", "TEXT"),
                ("operator_number", "TEXT"),
                ("recording_started", "INTEGER DEFAULT 0"),
                ("recording_file_id", "TEXT"),
                ("recording_absent", "INTEGER DEFAULT 0"),
                *_SUMMARY_COLUMNS,
            ):
                conn.execute(f"ALTER TABLE calls ADD COLUMN IF NOT EXISTS {column} {ddl}")

            conn.execute(QUERY_SCHEMA_PG)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_query_due ON queries(due_at)")

            conn.execute(TRANSCRIPT_SCHEMA_PG)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_transcript_call "
                "ON transcripts(call_id, seq)"
            )

    def add_transcript(self, call_id: str, seq: int, role: str, text: str) -> None:
        self._run(
            lambda conn: conn.execute(
                "INSERT INTO transcripts (call_id, seq, role, text, created_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (call_id, seq, role, text, time.time()),
            )
        )

    def list_transcripts(self, call_id: str) -> list[dict[str, Any]]:
        return self._run(
            lambda conn: conn.execute(
                "SELECT * FROM transcripts WHERE call_id = %s ORDER BY seq ASC",
                (call_id,),
            ).fetchall()
        ) or []

    def delete_transcripts(self, call_id: str) -> None:
        self._run(
            lambda conn: conn.execute(
                "DELETE FROM transcripts WHERE call_id = %s", (call_id,)
            )
        )

    def delete_queries(self, call_id: str) -> int:
        """Drop the queries raised on one call. Returns how many went."""
        cursor = self._run(
            lambda conn: conn.execute(
                "DELETE FROM queries WHERE call_id = %s", (call_id,)
            )
        )
        return getattr(cursor, "rowcount", 0) or 0

    def queries_for(self, call_id: str) -> list[dict[str, Any]]:
        return self._run(
            lambda conn: conn.execute(
                "SELECT * FROM queries WHERE call_id = %s ORDER BY created_at ASC",
                (call_id,),
            ).fetchall()
        ) or []

    def add_query(self, **fields: Any) -> None:
        columns = ", ".join(fields)
        placeholders = ", ".join(["%s"] * len(fields))
        self._run(
            lambda conn: conn.execute(
                f"INSERT INTO queries ({columns}) VALUES ({placeholders})",
                tuple(fields.values()),
            )
        )

    def get_query(self, token: str) -> dict[str, Any] | None:
        return self._run(
            lambda conn: conn.execute(
                "SELECT * FROM queries WHERE token = %s", (token,)
            ).fetchone()
        )

    def list_queries(
        self, limit: int = 100, status: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM queries"
        args: list[Any] = []
        if status:
            sql += " WHERE status = %s"
            args.append(status.upper())
        sql += " ORDER BY due_at ASC LIMIT %s"
        args.append(limit)
        return self._run(lambda conn: conn.execute(sql, args).fetchall()) or []

    def update_query(self, token: str, **fields: Any) -> bool:
        if not fields:
            return False
        sets = ", ".join(f"{k} = %s" for k in fields)
        return bool(
            self._run(
                lambda conn: conn.execute(
                    f"UPDATE queries SET {sets} WHERE token = %s",
                    (*fields.values(), token),
                ).rowcount
            )
        )

    def query_stats(self) -> dict[str, Any]:
        # Postgres will not sum a boolean, hence the COUNT(*) FILTER form that
        # the SQLite version does not need.
        row = self._run(
            lambda conn: conn.execute(
                """
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE status = 'OPEN')     AS open,
                       COUNT(*) FILTER (WHERE status = 'RESOLVED') AS resolved,
                       COUNT(*) FILTER (WHERE status = 'OPEN' AND due_at < %s)
                                                                   AS overdue
                FROM queries
                """,
                (time.time(),),
            ).fetchone()
        )
        return {k: (v or 0) for k, v in dict(row or {}).items()}

    def upsert(self, call_id: str, **fields: Any) -> None:
        if not fields:
            return

        # One statement, one round trip. The insert-then-update pair this
        # replaces was two, and every one of them crosses a region.
        columns = ", ".join(("call_id", *fields))
        placeholders = ", ".join(["%s"] * (len(fields) + 1))
        updates = ", ".join(f"{k} = EXCLUDED.{k}" for k in fields)
        self._run(
            lambda conn: conn.execute(
                f"INSERT INTO calls ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT (call_id) DO UPDATE SET {updates}",
                (call_id, *fields.values()),
            )
        )

    def bump(self, call_id: str, column: str) -> None:
        self._run(
            lambda conn: conn.execute(
                f"UPDATE calls SET {column} = COALESCE({column}, 0) + 1 WHERE call_id = %s",
                (call_id,),
            )
        )

    def get(self, call_id: str) -> dict[str, Any] | None:
        row = self._run(
            lambda conn: conn.execute(
                "SELECT * FROM calls WHERE call_id = %s", (call_id,)
            ).fetchone()
        )
        return dict(row) if row else None

    def list(self, limit: int = 50, direction: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM calls"
        args: list[Any] = []
        if direction:
            sql += " WHERE direction = %s"
            args.append(direction.upper())
        # NULLS LAST: a row created by a webhook that has not set start_time yet
        # would otherwise sort above every real call and push one off the page.
        sql += " ORDER BY start_time DESC NULLS LAST LIMIT %s"
        args.append(limit)
        rows = self._run(lambda conn: conn.execute(sql, args).fetchall())
        return [dict(r) for r in rows or []]

    def delete(self, call_id: str) -> bool:
        return bool(
            self._run(
                lambda conn: conn.execute(
                    "DELETE FROM calls WHERE call_id = %s", (call_id,)
                ).rowcount
            )
        )

    def stats(self) -> dict[str, Any]:
        # FILTER rather than SUM(condition): Postgres will not add booleans, and
        # SQLite's SUM(direction = 'INBOUND') is exactly that.
        row = self._run(
            lambda conn: conn.execute(
                """
                SELECT COUNT(*)                                        AS total,
                       COUNT(*) FILTER (WHERE direction = 'INBOUND')   AS inbound,
                       COUNT(*) FILTER (WHERE direction = 'OUTBOUND')  AS outbound,
                       COALESCE(SUM(COALESCE(duration, 0)), 0)         AS talk_time,
                       COUNT(*) FILTER (WHERE recording_ready = 1)     AS recordings,
                       COUNT(*) FILTER (
                           WHERE status IN ('DIALING','RINGING','ANSWERED','BRIDGED')
                       )                                              AS active
                FROM calls
                """
            ).fetchone()
        )
        return {k: (v or 0) for k, v in dict(row or {}).items()}

    def close(self) -> None:
        self._pool.close()


# Bookkeeping writes that must not be in the way of a conversation.
#
# Every store call is a network round trip once the log lives in Postgres — and
# a cross-region one at that. Done inline from the realtime session, each turn
# counter blocks the event loop for the length of that trip, and the loop is
# what carries the agent's tool calls: the caller hears the delay as Ayesha
# thinking. Counting turns is not worth a millisecond of that, so it happens
# behind the conversation instead.
#
# One worker, so writes still land in the order they were made.
_pending_writes: asyncio.Queue[tuple[str, str, dict[str, Any]]] = asyncio.Queue()


def write_later(call_id: str, **fields: Any) -> None:
    """Update a call without waiting for the database."""
    if call_id:
        _pending_writes.put_nowait(("upsert", call_id, fields))


def bump_later(call_id: str, column: str) -> None:
    """Increment a counter without waiting for the database."""
    if call_id:
        _pending_writes.put_nowait(("bump", call_id, {"column": column}))


# Line numbers per call, so the transcript can be replayed in the order it was
# spoken. The database cannot supply this: two lines written in the same
# millisecond sort arbitrarily by timestamp, and an autoincrement id is shared
# across every concurrent call rather than being per-call. Assigned here, on the
# event loop, where the events genuinely are ordered.
_transcript_seq: dict[str, int] = {}


def transcript_later(call_id: str, role: str, text: str) -> None:
    """Store one spoken line without waiting for the database.

    Same fire-and-forget contract as the writes above: this runs while a call is
    in progress, and a slow database must never become a pause in the
    conversation.
    """
    if not call_id or not text or not settings.transcript_store_enabled:
        return
    seq = _transcript_seq.get(call_id, 0) + 1
    _transcript_seq[call_id] = seq
    _pending_writes.put_nowait(
        ("transcript", call_id, {"seq": seq, "role": role, "text": text})
    )


async def _writer_loop() -> None:
    while True:
        action, call_id, fields = await _pending_writes.get()
        try:
            # In a thread: the store is synchronous, and the point of this queue
            # is that nothing it does happens on the event loop.
            if action == "bump":
                await asyncio.to_thread(store.bump, call_id, fields["column"])
            elif action == "transcript":
                await asyncio.to_thread(
                    store.add_transcript,
                    call_id, fields["seq"], fields["role"], fields["text"],
                )
            else:
                await asyncio.to_thread(store.upsert, call_id, **fields)
        except Exception:  # noqa: BLE001 - a lost counter must not kill the worker
            log.exception("deferred write failed for %s", call_id)
        finally:
            _pending_writes.task_done()


def _open_store() -> Any:
    """Postgres when configured, SQLite otherwise.

    The fallback is retried first, and shouted about if it happens. An earlier
    version fell back silently after a single failed connection, which turned a
    momentary network blip into permanent, invisible data loss: the service kept
    working perfectly against a file inside the container, and every call logged
    to it vanished the next time the host recycled. Losing a call log quietly is
    far worse than a slow boot.
    """
    if not settings.database_url:
        log.warning(
            "DATABASE_URL is not set — the call log is a local file and WILL BE "
            "LOST when this container restarts. Set it for anything but development."
        )
        return Store(settings.database_path)

    last: Exception | None = None
    for attempt, delay in enumerate((0, 2, 5), start=1):
        if delay:
            time.sleep(delay)
        try:
            store = PostgresStore(settings.database_url)
            log.info("call log: Postgres")
            return store
        except Exception as exc:  # noqa: BLE001 - never let storage choice stop the service
            last = exc
            log.warning("Postgres connection attempt %d failed: %s", attempt, exc)

    log.error(
        "DATABASE_URL IS SET BUT POSTGRES IS UNREACHABLE (%s). Falling back to a "
        "local file: the service will run, but every call logged from now until "
        "this is fixed will be lost when the container restarts.",
        last,
    )
    return Store(settings.database_path)


store = _open_store()


# =============================================================================
# Live event bus — feeds the dashboard over WebSocket
# =============================================================================


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._recent: list[dict[str, Any]] = []

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def publish(self, kind: str, **payload: Any) -> None:
        event = {"kind": kind, "at": time.time(), **payload}
        self._recent.append(event)
        del self._recent[:-50]
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A stalled tab must not block calls — but dropping the whole
                # subscriber left its socket connected and permanently silent,
                # so that dashboard stopped updating with no sign anything was
                # wrong. Discard its oldest frame instead and keep it attached:
                # the client refetches on any event, so a lost frame costs
                # nothing while a lost connection costs the live view.
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(event)

    def recent(self) -> list[dict[str, Any]]:
        return list(self._recent)


bus = EventBus()


# =============================================================================
# Telephony — Infobip Calls API
# =============================================================================


def _msisdn(number: str) -> str:
    """Infobip wants bare digits: E.164 with the leading `+` and any spacing
    stripped. Sending `+923191611020` is what the carrier answers with
    INVALID_REQUEST, after having accepted the call and started setting it up.
    """
    return re.sub(r"\D", "", number or "")


class TelephonyError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Infobip {status}: {body[:300]}")
        self.status = status
        self.body = body


class Telephony:
    """Infobip Calls. Auth scheme is `App <key>`, not Bearer."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            if not settings.infobip_root or not settings.infobip_api_key:
                raise TelephonyError(503, "Infobip is not configured")
            self._client = httpx.AsyncClient(
                base_url=settings.infobip_root,
                headers={
                    "Authorization": f"App {settings.infobip_api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=httpx.Timeout(25.0, connect=10.0),
            )
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, path: str, **kw: Any) -> Any:
        response = await self._http().request(method, path, **kw)
        if response.status_code >= 400:
            raise TelephonyError(response.status_code, response.text)
        return response.json() if response.content else {}

    @property
    def configured(self) -> bool:
        return bool(
            settings.infobip_root
            and settings.infobip_api_key
            and settings.infobip_sip_trunk_id
            and settings.infobip_calls_configuration_id
        )

    def _agent_endpoint(self) -> dict[str, Any]:
        return {
            "type": "SIP",
            "username": settings.openai_project_id,
            "sipTrunkId": settings.infobip_sip_trunk_id,
        }

    async def dial(self, to_number: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "endpoint": {"type": "PHONE", "phoneNumber": _msisdn(to_number)},
            "from": _msisdn(settings.infobip_phone_number),
            "callsConfigurationId": settings.infobip_calls_configuration_id,
            "connectTimeout": settings.call_connect_timeout_seconds,
            "maxDuration": settings.max_call_duration_seconds,
            "customData": {"agent": "ucp-voice"},
        }
        if settings.infobip_application_id:
            payload["platform"] = {"applicationId": settings.infobip_application_id}
        return await self._request("POST", "/calls/1/calls", json=payload)

    async def bridge_to_agent(self, parent_call_id: str, caller_id: str) -> dict[str, Any]:
        """Add the AI leg and bridge. Infobip auto-answers the parent leg once
        the child connects, so inbound calls never need an explicit answer."""
        payload: dict[str, Any] = {
            "parentCallId": parent_call_id,
            "childCallRequest": {
                "endpoint": self._agent_endpoint(),
                "from": _msisdn(caller_id or settings.infobip_phone_number),
                "connectTimeout": 30,
            },
            "maxDuration": settings.max_call_duration_seconds,
        }
        if settings.record_calls:
            payload["recording"] = {
                "recordingType": settings.recording_type,
                "filePrefix": settings.recording_file_prefix,
            }
        return await self._request("POST", "/calls/1/dialogs", json=payload)

    async def bridge_to_phone(self, parent_call_id: str, to_number: str) -> dict[str, Any]:
        """Add a second human leg and bridge the two.

        The same dialog call as bridge_to_agent, with a PHONE endpoint where the
        SIP one would go. That single substitution is the whole of "the agent
        does not talk": no leg ever reaches OpenAI, so nothing is listening and
        nothing answers.
        """
        payload: dict[str, Any] = {
            "parentCallId": parent_call_id,
            "childCallRequest": {
                "endpoint": {"type": "PHONE", "phoneNumber": _msisdn(to_number)},
                "from": _msisdn(settings.infobip_phone_number),
                "connectTimeout": settings.call_connect_timeout_seconds,
            },
            "maxDuration": settings.max_call_duration_seconds,
        }
        if settings.record_calls:
            payload["recording"] = {
                "recordingType": settings.recording_type,
                "filePrefix": settings.recording_file_prefix,
            }
        return await self._request("POST", "/calls/1/dialogs", json=payload)

    async def rtc_token(self, identity: str) -> dict[str, Any]:
        """A short-lived credential letting one browser place WebRTC calls.

        The API key never leaves the server; the browser only ever holds this.
        """
        payload: dict[str, Any] = {
            "identity": identity,
            "timeToLive": settings.rtc_token_ttl_seconds,
        }
        # Only for application calls and mobile push. Calling a phone number
        # from the browser needs no application at all, and sending an empty
        # one is worse than sending none.
        if settings.infobip_webrtc_application_id:
            payload["applicationId"] = settings.infobip_webrtc_application_id
        return await self._request("POST", "/webrtc/1/token", json=payload)

    async def hangup_call(self, call_id: str) -> Any:
        return await self._request("POST", f"/calls/1/calls/{call_id}/hangup")

    async def hangup_dialog(self, dialog_id: str) -> Any:
        return await self._request("POST", f"/calls/1/dialogs/{dialog_id}/hangup")

    async def recordings_for_dialog(self, dialog_id: str) -> Any:
        return await self._request("GET", f"/calls/1/recordings/dialogs/{dialog_id}")

    async def recordings_for_call(self, call_id: str) -> Any:
        """Recordings for a single leg, by call id.

        The fallback for when DIALOG_ESTABLISHED never arrived and we therefore
        have no dialog id — without it those calls look like they were never
        recorded at all, even though Infobip has the audio.
        """
        return await self._request("GET", f"/calls/1/recordings/calls/{call_id}")

    async def compose_dialog_recording(self, dialog_id: str) -> Any:
        """Mix the two legs into one file.

        A dialog records the caller and the agent separately, so downloading a
        raw leg gives you one voice and silence where the other should be.
        Composition merges them into a single conversation.
        """
        return await self._request(
            "POST", f"/calls/1/recordings/dialogs/{dialog_id}/compose"
        )

    async def start_dialog_recording(self, dialog_id: str) -> Any:
        """Turn recording on for a dialog somebody else created.

        The browser softphone builds its own dialog through the WebRTC SDK, so
        the `recording` block that `bridge_to_agent` sends never applies to it —
        and if recording is not enabled on the WebRTC application, the SDK's own
        request is silently dropped and the conversation is lost. Asking the
        Calls API directly is the one path that does not depend on how the
        application was configured in the Infobip portal.
        """
        return await self._request(
            "POST",
            f"/calls/1/dialogs/{dialog_id}/start-recording",
            json={"recordingType": settings.recording_type},
        )

    async def start_call_recording(self, call_id: str) -> Any:
        """Record a single leg. The fallback for when no dialog exists yet.

        One leg is one voice, so this is a poor recording next to a dialog's —
        but a call whose dialog never materialised would otherwise leave nothing
        at all, and half a conversation is recoverable evidence where silence is
        not. Note the different body shape: the call endpoint wants the settings
        nested under `recording`, the dialog one takes them flat.
        """
        return await self._request(
            "POST",
            f"/calls/1/calls/{call_id}/start-recording",
            json={"recording": {"recordingType": settings.recording_type}},
        )

    # ---- Live media streaming -------------------------------------------
    # Recording our own audio instead of asking the provider for theirs. Their
    # composed files came back shorter than the call and missing one side of the
    # conversation, and there is no setting on our end that changes that. Raw
    # media streamed to our own socket is ours: we know exactly what went in.

    async def media_stream_configs(self) -> list[dict[str, Any]]:
        payload = await self._request("GET", "/calls/1/media-stream-configs")
        return payload.get("results") or [] if isinstance(payload, dict) else []

    async def create_media_stream_config(self, name: str, url: str) -> dict[str, Any]:
        # `type` is required and has no default. Sending only name and url is
        # rejected with "Required request body is missing or not valid" — a
        # message that names no field, so it reads like a malformed request
        # rather than a missing one. That rejection is why no config ever
        # existed on the account, and therefore why nothing was ever recorded
        # from the media stream: the failure is caught and logged as a warning,
        # and every call then falls through to the provider recording instead.
        # MEDIA_STREAMING is the raw-audio-to-our-socket mode; WEBSOCKET_ENDPOINT
        # is the other direction and is not what we want here.
        return await self._request(
            "POST",
            "/calls/1/media-stream-configs",
            json={
                "type": "MEDIA_STREAMING",
                "name": name,
                "url": url,
                "audioEncoding": "RAW",
            },
        )

    async def delete_media_stream_config(self, config_id: str) -> Any:
        return await self._request(
            "DELETE", f"/calls/1/media-stream-configs/{config_id}"
        )

    async def start_media_stream(self, call_id: str, config_id: str) -> Any:
        """Send this leg's audio to our websocket for as long as it lasts."""
        return await self._request(
            "POST",
            f"/calls/1/calls/{call_id}/start-media-stream",
            json={"mediaStream": {"audioProperties": {"mediaStreamConfigId": config_id}}},
        )

    async def stop_media_stream(self, call_id: str) -> Any:
        return await self._request("POST", f"/calls/1/calls/{call_id}/stop-media-stream")

    async def call_detail(self, call_id: str) -> dict[str, Any]:
        """A call in progress. Only resolves while the call is up, and may name
        the dialog it belongs to — the cheapest way to find it if it does."""
        payload = await self._request("GET", f"/calls/1/calls/{call_id}")
        return payload if isinstance(payload, dict) else {}

    async def live_dialogs(self) -> list[dict[str, Any]]:
        """Dialogs in progress right now."""
        payload = await self._request("GET", "/calls/1/dialogs?page=0&size=50")
        return payload.get("results") or [] if isinstance(payload, dict) else []

    async def dialog_history_for_call(self, call_id: str) -> dict[str, Any] | None:
        """What the provider knows about the dialog this call belonged to.

        The authoritative record of how a call actually ended — state, end time,
        real duration, hangup cause and recording files, all in one response.
        Webhooks carry the same facts but only if they are delivered; this can
        always be asked for, which is why reconciliation is built on it.
        """
        payload = await self._request(
            "GET", f"/calls/1/dialogs/history?parentCallId={call_id}&page=0&size=1"
        )
        results = payload.get("results") or [] if isinstance(payload, dict) else []
        return results[0] if results else None

    async def call_history_for_call(self, call_id: str) -> dict[str, Any] | None:
        """The provider's record of a single leg.

        For calls that never became a dialog at all — a number that rang out, a
        dial the carrier rejected — where the dialog history has nothing to say
        but the call still needs closing. The API's own `callId` filter is
        ignored (it returns the whole page either way), so the match is done
        here rather than trusted to the query string.
        """
        payload = await self._request("GET", "/calls/1/calls/history?page=0&size=100")
        results = payload.get("results") or [] if isinstance(payload, dict) else []
        return next((c for c in results if c.get("callId") == call_id), None)

    async def stream_file(self, file_id: str) -> httpx.Response:
        return await self._http().get(
            f"/calls/1/recordings/files/{file_id}", follow_redirects=True
        )


telephony = Telephony()


# =============================================================================
# OpenAI Realtime — accept calls, serve the RAG tool over the session socket
# =============================================================================

# Azure serves the same call API under /openai/v1 on the resource's own host.
if settings.azure_configured:
    REALTIME_API = f"{settings.azure_openai_root}/openai/v1/realtime"
    REALTIME_WS = REALTIME_API.replace("https://", "wss://", 1)
else:
    REALTIME_API = "https://api.openai.com/v1/realtime"
    REALTIME_WS = "wss://api.openai.com/v1/realtime"
_sessions: dict[str, asyncio.Task] = {}
# Background recording pollers. Held in a set because asyncio only keeps weak
# references to tasks — without this they can be garbage-collected mid-poll.
_recording_watchers: set[asyncio.Task] = set()


def _realtime_auth() -> dict[str, str]:
    if settings.azure_configured:
        return {"api-key": settings.azure_openai_api_key.strip()}
    return {"Authorization": f"Bearer {settings.openai_api_key}"}


def _openai_headers() -> dict[str, str]:
    return {**_realtime_auth(), "Content-Type": "application/json"}


"""One HTTPS connection to OpenAI, kept open between calls.

Building a client per call meant a fresh TCP and TLS handshake in the middle of
the one moment latency is audible: the student has picked up and is waiting for
Ayesha to say something. A pooled connection removes that handshake from the
critical path — the call is accepted on a socket that is already open."""
_openai_client: httpx.AsyncClient | None = None


def _openai_http() -> httpx.AsyncClient:
    global _openai_client
    if _openai_client is None:
        _openai_client = httpx.AsyncClient(
            timeout=20,
            headers=_openai_headers(),
            # Well above what one call needs, so the connection is never evicted
            # mid-conversation and is still warm for the next caller.
            limits=httpx.Limits(max_keepalive_connections=8, keepalive_expiry=300),
        )
    return _openai_client


async def warm_openai_connection() -> None:
    """Open the connection before it is needed.

    Called when an outbound call is placed: the student's phone rings for
    several seconds, which is ample time to complete a handshake that would
    otherwise happen after they answer, while they are listening to silence.
    """
    with contextlib.suppress(Exception):
        await _openai_http().get(f"{REALTIME_API}/calls/warmup", timeout=5)


async def accept_realtime_call(call_id: str) -> None:
    response = await _openai_http().post(
        f"{REALTIME_API}/calls/{call_id}/accept",
        json=realtime_session_config(),
    )
    response.raise_for_status()


async def hangup_realtime_call(call_id: str) -> None:
    with contextlib.suppress(httpx.HTTPError):
        await _openai_http().post(f"{REALTIME_API}/calls/{call_id}/hangup", timeout=15)


async def run_session(openai_call_id: str, our_call_id: str | None) -> None:
    """Attach to the live call: greet, answer tool calls with RAG, log turns.

    This socket is also where barge-in shows up: OpenAI cancels its own response
    when the caller speaks, and tells us via `response.done` with a cancelled
    status, which we count as an interruption.
    """
    url = f"{REALTIME_WS}?call_id={openai_call_id}"
    headers = _realtime_auth()

    # Tracks whether the model is mid-response. Turn detection creates responses
    # on its own, so firing another while one is live makes the agent talk over
    # itself — which sounds exactly like repeating.
    session: dict[str, Any] = {
        "response_active": False,
        "greeted": False,
        "seen_items": set(),
        "last_agent_text": None,
        # Tool calls are collected while a response is streaming and only
        # answered once it has finished — see _handle_event.
        "pending_tools": [],
        "answered_tools": set(),
        # The language of the caller's most recent turn, re-judged every
        # time they speak — see _detect_caller_language.
        "caller_lang": "",
        "owes_answer": False,
        # Set when the agent decides the conversation is over — see END_CALL_TOOL.
        "openai_call_id": openai_call_id,
        "hangup_after_response": False,
        "hangup_started": False,
        "turn_had_audio": False,
        # Everything she said in the current turn, so the hangup path can tell a
        # real goodbye from a sentence *about* saying goodbye.
        "turn_text": "",
        "owes_farewell": False,
        "farewell_retried": False,
        # What the caller cut her off in the middle of saying, so the remainder
        # can be offered back once their own question has been dealt with.
        "interrupted_text": None,
        "resume_notes": 0,
    }

    try:
        # Hard ceiling on the session. If the PSTN leg drops without us being
        # able to correlate it, nothing else would ever cancel this task, and an
        # orphaned Realtime session keeps billing while talking to nobody.
        async with asyncio.timeout(settings.max_call_duration_seconds + 30):
            await _run_socket(url, headers, openai_call_id, our_call_id, session)
    except asyncio.TimeoutError:
        log.warning("realtime session %s hit its lifetime cap", openai_call_id)
        with contextlib.suppress(Exception):
            await hangup_realtime_call(openai_call_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - a dead socket must not kill the app
        log.warning("realtime session %s ended: %s", openai_call_id, exc)
    finally:
        wrap_up = session.get("wrap_up_task")
        if wrap_up:
            wrap_up.cancel()
        _sessions.pop(openai_call_id, None)
        _answer_signals.pop(our_call_id, None)
        bus.publish("agent.disconnected", callId=our_call_id)


async def _run_socket(
    url: str,
    headers: dict[str, str],
    openai_call_id: str,
    our_call_id: str | None,
    session: dict[str, Any],
) -> None:
    async with websockets.connect(url, additional_headers=headers) as socket:
        bus.publish("agent.connected", callId=our_call_id)

        # Greet immediately on open. Attaching to an already-accepted call does
        # not replay session.created, so waiting for it means never speaking.
        #
        # The greeting is dictated rather than described: left to its own wording
        # the model shortens "Assalam-o-Alaikum" to "Salaam" and rewrites the
        # sentence every call. Only this first turn is scripted; every later turn
        # uses the full persona from the session prompt.
        # Warn the caller before the hard limit rather than cutting them off
        # mid-sentence. Infobip drops the leg at max_call_duration regardless,
        # so this is the only chance to close the call like a human would.
        wrap_up = asyncio.create_task(_wrap_up_timer(socket, session, our_call_id))
        session["wrap_up_task"] = wrap_up

        if settings.agent_greeting:
            session["greeted"] = True
            # With pre-dial on, this session can be live while the student's
            # phone is still ringing. Greeting now would spend the whole opening
            # line on an unanswered call, and they would answer to silence.
            await _wait_until_answered(our_call_id)
            # Let the audio path come up before the first word goes into it.
            # Inbound and outbound both need this: the leg is answered slightly
            # before it is carrying audio either way.
            if settings.greeting_delay_seconds > 0:
                await asyncio.sleep(settings.greeting_delay_seconds)
            await _speak(
                socket,
                session,
                # Only the quoted greeting may be spoken. An earlier version put
                # the pronunciation guide in this instruction and she read the
                # guide out loud first — "as-sa-laam-o-a-lai-kum. Assalam-o-
                # Alaikum! Main Ayesha…". A per-turn instruction is taken as
                # material to say, so how-to-sound guidance lives in the persona
                # prompt instead, where it is reference rather than script.
                instructions=(
                    "Speak ONLY the text between the quotation marks, in full and "
                    "word for word, warmly and naturally. It is more than one "
                    "sentence; say all of it, including the closing question. "
                    "Everything outside the quotation "
                    "marks is an instruction to you and must never be spoken.\n\n"
                    f'"{settings.agent_greeting.strip()}"\n\n'
                    "Begin with the full opening greeting exactly as written — never "
                    "shortened to Salaam, never reworded. It opens with the "
                    "salaam: speak that as Urdu, with Arabic pronunciation, as "
                    "one flowing phrase at natural conversational speed — every "
                    "syllable present, none stretched, held or swallowed, no gap "
                    "between them. Say it once and once only: do not spell it "
                    "out, do not transliterate it aloud, and do not say a second "
                    "version of it — Roman or Urdu — before or after the first. "
                    "If the caller speaks before you finish, "
                    "stop and let them talk — this greeting is then over and is "
                    "never said, restarted or resumed at any later point in the "
                    "call."
                ),
            )

        async for raw in socket:
            event = json.loads(raw)
            log.debug("realtime <- %s", event.get("type"))
            await _handle_event(socket, event, openai_call_id, our_call_id, session)


async def _wrap_up_timer(socket: Any, session: dict[str, Any], our_call_id: str | None) -> None:
    """Nudge the agent to close the call before the carrier cuts it off.

    Infobip terminates the leg at MAX_CALL_DURATION_SECONDS with no warning, so
    without this the caller is dropped mid-sentence. Two nudges: a soft one to
    start wrapping up, then a final farewell just before the cut.
    """
    total = settings.max_call_duration_seconds
    warn_at = max(total - settings.wrap_up_warning_seconds, 15)

    try:
        await asyncio.sleep(warn_at)
        bus.publish("agent.wrapping_up", callId=our_call_id)
        await _speak(
            socket,
            session,
            instructions=(
                "You have about 30 seconds left before this call ends "
                "automatically. Without mentioning any time limit, bring the "
                "conversation to a natural close now, and ask if they need "
                "anything else quickly. Keep it to two short sentences, in the "
                "same language the caller has been speaking.\n"
                "Give a next step ONLY if one is genuinely outstanding, and "
                "match it to who is calling: a new candidate who has not "
                "applied gets the online admissions portal; anyone else, "
                "including every current student and alumnus, gets the "
                "admissions helpline "
                "if they need anything at all. Never send a student or an "
                "alumnus to the admissions portal — they are already enrolled. "
                "If their query is already answered or already registered, "
                "give no next step; just close."
            ),
        )

        await asyncio.sleep(max(settings.wrap_up_warning_seconds - 8, 4))
        # Cut anything still in flight rather than just clearing the flag —
        # stacking the farewell on a live response makes her talk over herself.
        if session["response_active"]:
            with contextlib.suppress(Exception):
                await socket.send(json.dumps({"type": "response.cancel"}))
            await asyncio.sleep(0.4)
            session["response_active"] = False
        await _speak(
            socket,
            session,
            instructions=(
                "The call is ending now. Speak ONLY the text between the "
                "quotation marks, in full and word for word, warmly. Everything outside the "
                "quotation marks is an instruction and must never be spoken.\n\n"
                '"University se rabta karne ka bohat shukriya. '
                'Aap ka call hamare liye bohat ahem hai. Allah Hafiz."\n\n'
                "Do not describe what you are doing, do not say you are closing "
                "the call, and do not ask any question."
            ),
        )
    except asyncio.CancelledError:
        raise  # the call ended first, which is the normal path


async def _speak(
    socket: Any, session: dict[str, Any], instructions: str | None = None
) -> bool:
    """Ask the model to produce a turn, but only if it is not already talking.

    `instructions` override the session prompt for this one response. That is the
    wrong tool for conversation — it would strip the persona — but exactly right
    for dictating a fixed line, which the model otherwise paraphrases.

    Returns whether a turn was actually requested, so a caller that *must* be
    heard can try again rather than assume it was.
    """
    if session["response_active"]:
        return False
    session["response_active"] = True
    payload: dict[str, Any] = {"type": "response.create"}
    if instructions:
        payload["response"] = {"instructions": instructions}
    await socket.send(json.dumps(payload))
    return True


def _whatsapp_msisdn(number: str) -> str:
    """Local Pakistani numbers to E.164 digits, as WhatsApp requires.

    Callers give their number the way they say it — "zero three one nine…" —
    and a leading 0 is a national trunk prefix, not part of the number. Sending
    03191611020 to WhatsApp addresses nothing; 923191611020 is the same phone.
    Numbers that already carry the country code are left alone.
    """
    digits = _msisdn(number)
    cc = _msisdn(settings.whatsapp_country_code) or "92"
    if not digits:
        return ""
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("0"):
        return cc + digits[1:]
    if digits.startswith(cc):
        return digits
    # A bare 10-digit local number (3191611020) with neither trunk prefix nor
    # country code.
    return cc + digits if len(digits) <= 10 else digits


async def send_whatsapp_token(to_number: str, token: str) -> str:
    """Send the reference token via Meta's Cloud API. Returns the message id.

    Deliberately not on the Infobip client: this is a different host, a
    different account and Bearer auth rather than Infobip's `App` scheme.
    Routing it through the telephony client would mean one object holding two
    unrelated providers' credentials.

    Uses a template rather than a free-form text message because the send is
    business-initiated and lands outside the 24-hour service window, where Meta
    permits templates only. A text send there is accepted by the API and then
    dropped — a caller who was promised a message, and a log line claiming it
    was sent.

    The template must take exactly one body placeholder: the token.
    """
    payload = {
        "messaging_product": "whatsapp",
        # Normalised here as well as at the call site, so a direct caller
        # cannot address a local 03xx number that the API accepts and delivers
        # nowhere.
        "to": _whatsapp_msisdn(to_number),
        "type": "template",
        "template": {
            "name": settings.whatsapp_template.strip(),
            "language": {"code": settings.whatsapp_language.strip() or "en"},
            "components": [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": token}],
                }
            ],
        },
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=10.0)) as client:
        response = await client.post(
            settings.whatsapp_api_url,
            headers={
                "Authorization": f"Bearer {settings.whatsapp_access_token.strip()}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
    if response.status_code >= 400:
        # Meta puts the useful part in error.message; the status alone says
        # nothing about whether it was the token, the template or the number.
        raise RuntimeError(f"WhatsApp {response.status_code}: {response.text[:300]}")
    messages = (response.json() or {}).get("messages") or []
    return (messages[0].get("id") if messages else "") or ""


_whatsapp_tasks: set[asyncio.Task] = set()


def _send_token_whatsapp(token: str, number: str) -> None:
    """Deliver the token over WhatsApp, in the background.

    Fire-and-forget on purpose. This is called from the tool-call handler while
    the caller is on the line waiting to hear their token, and a WhatsApp send
    is a round trip to Infobip. Awaiting it would put dead air on the call —
    exactly what the prompt's "never sound like you are looking something up"
    rules exist to prevent. The token is spoken regardless; the message is a
    convenience on top of it, so its failure must never reach the call.
    """
    if not settings.whatsapp_configured:
        store.update_query(token, whatsapp_status="disabled")
        return
    if not number:
        store.update_query(token, whatsapp_status="no_number")
        return

    async def run() -> None:
        try:
            message_id = await send_whatsapp_token(number, token)
            store.update_query(token, whatsapp_status="sent")
            log.info(
                "whatsapp token %s -> %s (message %s)",
                token, mask_number(number), message_id or "-",
            )
        except Exception:  # noqa: BLE001 - never let delivery break the call
            store.update_query(token, whatsapp_status="failed")
            log.exception("whatsapp send failed for query %s", token)

    # Built once and handed to create_task, rather than `create_task(run())`:
    # that form evaluates run() first, so when create_task raises there is a
    # coroutine already built that nobody ever awaits, and Python warns about it
    # on a path that is otherwise handled perfectly well.
    coro = run()
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:
        # No running loop — a script, a test, or a sync caller. The query is
        # already stored and the caller is waiting on a token, so this must not
        # propagate: losing the message is recoverable from the dashboard,
        # losing the registration is not.
        coro.close()
        store.update_query(token, whatsapp_status="failed")
        log.warning("no event loop to send whatsapp for query %s", token)
        return
    _whatsapp_tasks.add(task)
    task.add_done_callback(_whatsapp_tasks.discard)


# --- Sending the actual details on WhatsApp -----------------------------------
#
# The token message tells a caller their query was registered. This is the other
# half: the information itself, either answered live on the call ("WhatsApp par
# bhej dijiye") or sent later when the department resolves the query.
#
# It is a second template rather than a free-form message for the reason spelled
# out on send_whatsapp_token: our calls run over the normal phone network, which
# Meta cannot see, so no customer service window is ever open and free text is
# accepted by the API and then silently dropped. A template works from anywhere,
# at any time, with no window at all — the price being that only the blanks
# change, which is what _template_param below exists to fill safely.

# Meta rejects a template parameter containing a newline, a tab, or more than
# four consecutive spaces, and the rejection is the whole send. Answers assembled
# from the knowledge base are full of all three.
# Meta rejects a template parameter containing a newline, a tab, or more than
# four consecutive spaces, and rejects the whole message rather than the one
# parameter. Answers assembled from the knowledge base are full of all three.
_PARAM_BULLET = re.compile(r"^[\s]*[-*\u2022\u00b7]+[\s]*")
_PARAM_RUNS = re.compile(r"[ \t]{2,}")


def _template_param(text: str, limit: int) -> str:
    """One value, shaped so Meta will accept it as a template parameter.

    Line breaks become sentence breaks rather than vanishing: this is read on a
    phone as prose, and "documents CNIC matric marksheet photos" run together is
    a list nobody can parse. A line that already ends in punctuation gets only a
    space, so a heading like "Documents:" is not turned into "Documents:." — and
    bullet characters are dropped, because "- CNIC" reads as a minus sign in a
    message with no line breaks to make it a list.

    Over-long text is cut at a sentence or word boundary and marked with an
    ellipsis. Never mid-word: a parameter ending "your fee is 2,23" reads as a
    real figure, which is worse than one that stops cleanly.
    """
    lines = []
    for raw in (text or "").replace("\r", "\n").split("\n"):
        line = _PARAM_BULLET.sub("", raw).strip()
        if line:
            lines.append(line)

    out = ""
    for line in lines:
        if not out:
            out = line
        elif out[-1] in ":;,-–—([{":
            # The previous line was a heading or an open clause; a full stop
            # after it would be wrong punctuation, not just ugly.
            out += " " + line
        elif out[-1] in ".!?":
            out += " " + line
        else:
            out += ". " + line

    out = _PARAM_RUNS.sub(" ", out).strip()
    if len(out) <= limit:
        return out

    cut = out[:limit]
    # Prefer the last sentence end, then the last space, then a hard cut.
    for boundary in (cut.rfind(". "), cut.rfind(" ")):
        if boundary > limit * 0.6:
            cut = cut[:boundary]
            break
    return cut.rstrip(" .,;:-") + "\u2026"


def _plausible_wa_number(msisdn: str) -> bool:
    """Whether an already-normalised number could really be a mobile.

    This is the last guard before somebody's admission details, fee figures and
    name are sent to whoever owns a mis-heard number. Speech-to-text on a phone
    line drops and doubles digits routinely, and the caller cannot see what was
    written down, so the read-back in the prompt is checked here as well.

    Deliberately shape-based rather than a lookup: an E.164 mobile is 10-15
    digits, and on the default country code a Pakistani mobile is 92 followed by
    3 and nine more. Landlines (9242..., 9251...) are rejected — WhatsApp is not
    on them, so a send would fail silently after we promised it.
    """
    digits = _msisdn(msisdn)
    if not (10 <= len(digits) <= 15):
        return False
    cc = _msisdn(settings.whatsapp_country_code) or "92"
    if cc == "92" and digits.startswith("92"):
        # 92 + 3XXXXXXXXX
        return len(digits) == 12 and digits[2] == "3"
    return True


async def send_whatsapp_details(to_number: str, topic: str, details: str) -> str:
    """Send the information itself over WhatsApp. Returns the message id.

    The template takes exactly two body placeholders: what they asked about, and
    the answer. Both are sanitised here rather than at the call sites, so no
    caller can hand Meta a value that fails the send.
    """
    name = settings.whatsapp_details_template.strip()
    if not name:
        raise RuntimeError("WHATSAPP_DETAILS_TEMPLATE is not set")

    subject = _template_param(topic, settings.whatsapp_topic_max_chars)
    body = _template_param(details, settings.whatsapp_details_max_chars)
    # Meta rejects an empty parameter outright, and an empty one here would mean
    # a message that says "you asked about:" and nothing else.
    if not subject:
        subject = "aap ki query"
    if not body:
        raise RuntimeError("nothing to send: the details were empty")

    payload = {
        "messaging_product": "whatsapp",
        "to": _whatsapp_msisdn(to_number),
        "type": "template",
        "template": {
            "name": name,
            "language": {"code": settings.whatsapp_language.strip() or "en"},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": subject},
                        {"type": "text", "text": body},
                    ],
                }
            ],
        },
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=10.0)) as client:
        response = await client.post(
            settings.whatsapp_api_url,
            headers={
                "Authorization": f"Bearer {settings.whatsapp_access_token.strip()}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
    if response.status_code >= 400:
        raise RuntimeError(f"WhatsApp {response.status_code}: {response.text[:300]}")
    messages = (response.json() or {}).get("messages") or []
    return (messages[0].get("id") if messages else "") or ""


def _send_details_whatsapp(
    number: str,
    topic: str,
    details: str,
    *,
    call_id: str | None = None,
    token: str | None = None,
) -> None:
    """Deliver the details in the background, exactly like the token send.

    Fire-and-forget for the same reason: this is called while the caller is on
    the line, and awaiting a round trip to Meta would put dead air on the call.
    She has already said the information out loud; the message is a convenience
    on top of that, so its failure must never reach the conversation.
    """

    def _mark(status: str) -> None:
        if token:
            with contextlib.suppress(Exception):
                store.update_query(token, whatsapp_status=status)

    if not settings.whatsapp_details_configured:
        _mark("disabled")
        log.info("whatsapp details not configured — nothing sent")
        return
    if not number:
        _mark("no_number")
        return

    async def run() -> None:
        try:
            message_id = await send_whatsapp_details(number, topic, details)
            _mark("sent")
            log.info(
                "whatsapp details -> %s (%s, message %s)",
                mask_number(number), topic[:40] or "-", message_id or "-",
            )
            bus.publish(
                "whatsapp.sent",
                callId=call_id, token=token, to=mask_number(number), topic=topic[:120],
            )
        except Exception:  # noqa: BLE001 - never let delivery break the call
            _mark("failed")
            log.exception("whatsapp details send failed for %s", mask_number(number))
            bus.publish("whatsapp.failed", callId=call_id, token=token)

    # See the token sender: the coroutine is built once so the failure path has
    # the same object to close, rather than leaking the first and closing a
    # second one that was never started.
    coro = run()
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:
        # No running loop — a script or a sync caller. Same reasoning as the
        # token path: losing the message is recoverable, raising here is not.
        coro.close()
        _mark("failed")
        log.warning("no event loop to send whatsapp details")
        return
    _whatsapp_tasks.add(task)
    task.add_done_callback(_whatsapp_tasks.discard)


def _send_details_for_call(
    args: dict[str, Any], our_call_id: str | None
) -> str:
    """Queue the written copy and tell the model what to say about it.

    Returns the tool output rather than raising, for the same reason
    _register_query does: the caller is mid-sentence waiting to be told the
    message is coming, and an exception here would leave her silent.

    Nothing is awaited. The send is handed to a background task and the model is
    told it is on its way, not that it has arrived - which is also exactly what
    the prompt requires her to say, because a delivery receipt is not something
    this call can wait for.
    """
    topic = (args.get("topic") or "").strip()
    details = (args.get("details") or "").strip()
    if not details:
        return (
            "Nothing was sent - no details were provided. Do not tell the "
            "caller a message is coming. Simply carry on with the call."
        )

    number = (args.get("whatsapp_number") or "").strip()
    if not number and our_call_id:
        # The number they rang from. She is told not to ask for one she already
        # has, so most calls will not carry one in `args`.
        number = ((store.get(our_call_id) or {}).get("phone_number") or "").strip()
    target = _whatsapp_msisdn(number)
    if not target:
        return (
            "No WhatsApp number is available for this caller. Ask them for the "
            "number they want the message on, then call this tool again."
        )
    if not _plausible_wa_number(target):
        # Refused rather than sent. A number of the wrong shape is a misheard
        # one, and the cost of guessing is this caller's details arriving on a
        # stranger's phone — far worse than asking them to repeat it.
        log.warning(
            "refusing whatsapp details for call %s: implausible number %s",
            our_call_id or "-", mask_number(target),
        )
        return (
            "That number does not look like a WhatsApp mobile number, so nothing "
            "was sent. Do NOT tell the caller a message is coming. Ask them to "
            "say the number again slowly, digit by digit, read it back to them "
            "to confirm, and then call this tool again with it."
        )

    if not settings.whatsapp_details_configured:
        return (
            "WhatsApp sending is not available. Do NOT promise a message. Give "
            "the caller the information out loud instead, and offer the "
            "admissions helpline if they want it written down."
        )

    _send_details_whatsapp(target, topic, details, call_id=our_call_id)
    # Recorded on the call so the Excel report and the dashboard show which
    # number the caller actually asked to be messaged on.
    if our_call_id:
        write_later(our_call_id, whatsapp_number=target)
    log.info(
        "queued whatsapp details for call %s -> %s (%s)",
        our_call_id or "-", mask_number(target), topic[:40] or "-",
    )
    return (
        "The message is being sent to " + mask_number(target) + ". Tell the "
        "caller it is on its way and will reach them shortly - never that it "
        "has arrived or been delivered. Do not read the details out again, and "
        "do not ask them to check WhatsApp while you hold the line."
    )


def _register_query(
    args: dict[str, Any], session: dict[str, Any], our_call_id: str | None
) -> str | None:
    """Write the query and return its token, or None if it could not be stored.

    Returning None rather than raising is deliberate: a failed insert must not
    kill the turn. The caller is mid-sentence waiting for a reference number,
    and the honest recovery — tell them the helpline — needs the model to keep
    talking, which it cannot do if this propagates.
    """
    now = time.time()
    phone = (args.get("phone") or "").strip()
    if not phone and our_call_id:
        # Fall back to the number they rang from. She is told not to ask for a
        # number she already has, so most calls will not carry one in `args`.
        phone = ((store.get(our_call_id) or {}).get("phone_number") or "").strip()

    fields = {
        "call_id": our_call_id,
        "caller_type": (args.get("caller_type") or "").strip().lower() or None,
        "category": (args.get("category") or "").strip() or None,
        "query_text": (args.get("query_text") or "").strip() or None,
        "name": (args.get("name") or "").strip() or None,
        "reg_no": (args.get("reg_no") or "").strip() or None,
        "programme": (args.get("programme") or "").strip() or None,
        "grad_year": (args.get("grad_year") or "").strip() or None,
        "phone": phone or None,
        # Meta requires opt-in for a business-initiated message, and a caller
        # who agreed to a phone call has not agreed to a WhatsApp. She asks;
        # only an explicit yes gets stored here.
        "whatsapp_opt_in": 1 if args.get("whatsapp_optin") else 0,
        "whatsapp_status": "pending" if args.get("whatsapp_optin") else "not_requested",
        "status": "OPEN",
        "created_at": now,
        "due_at": now + QUERY_SLA_HOURS * 3600,
    }

    # Retry only on a token collision. Six digits makes that rare, but "rare"
    # over a primary key is still a caller who gets no reference at all.
    for _ in range(5):
        token = _new_token()
        try:
            store.add_query(token=token, **fields)
        except Exception as exc:  # noqa: BLE001 - any store failure is the same here
            if "UNIQUE" in str(exc).upper() or "DUPLICATE" in str(exc).upper():
                continue
            log.exception("could not register query for call %s", our_call_id or "-")
            return None
        log.info(
            "query %s registered (%s / %s) for call %s",
            token, fields["caller_type"] or "?", fields["category"] or "?",
            our_call_id or "-",
        )
        session.setdefault("query_tokens", []).append(token)
        bus.publish(
            "query.registered",
            callId=our_call_id,
            token=token,
            callerType=fields["caller_type"],
            category=fields["category"],
        )
        if args.get("whatsapp_optin"):
            # The number they nominated for WhatsApp, which is not always the
            # one they rang from — plenty of people call from a landline.
            #
            # Suppressed wholesale: the query is stored and the token is about
            # to be spoken. Nothing that happens to the message is worth
            # turning a successful registration into a failed one.
            try:
                _send_token_whatsapp(
                    token,
                    _whatsapp_msisdn(
                        (args.get("whatsapp_number") or "").strip() or phone
                    ),
                )
            except Exception:  # noqa: BLE001
                log.exception("whatsapp dispatch failed for query %s", token)
        return token

    log.error("gave up allocating a query token for call %s", our_call_id or "-")
    return None


def _send_reference_for_call(
    args: dict[str, Any], session: dict[str, Any], our_call_id: str | None
) -> str:
    """Record consent for the token message and send it.

    register_query takes a `whatsapp_optin` argument, but it is called before
    the token has been spoken and therefore before the caller has been asked
    anything — so that flag was structurally always false and no reference
    message was ever sent. Consent is captured here instead, once the caller
    has actually agreed.
    """
    tokens = session.get("query_tokens") or []
    if not tokens:
        return (
            "There is no registered query on this call to send. Do not tell the "
            "caller a message is coming."
        )
    token = tokens[-1]

    row = store.get_query(token) or {}
    number = (args.get("whatsapp_number") or "").strip() or (row.get("phone") or "")
    if not number and our_call_id:
        number = (store.get(our_call_id) or {}).get("phone_number") or ""
    target = _whatsapp_msisdn(number)
    if not _plausible_wa_number(target):
        log.warning("refusing reference send for %s — implausible number", token)
        return (
            "That number does not look like a valid mobile. Ask the caller to "
            "repeat it slowly, and do not say anything has been sent."
        )

    store.update_query(token, whatsapp_opt_in=1, whatsapp_status="pending")
    try:
        _send_token_whatsapp(token, target)
    except Exception:  # noqa: BLE001 - a failed message must not drop the call
        log.exception("whatsapp dispatch failed for query %s", token)
    log.info("reference %s queued for WhatsApp on call %s", token, our_call_id or "-")
    return (
        f"Reference {token} is being sent to the caller's WhatsApp. Tell them it "
        "will arrive shortly — never that it has already arrived, and do not ask "
        "them to check while you hold."
    )


_TOOL_NAMES = (
    RAG_TOOL["name"],
    REGISTER_QUERY_TOOL["name"],
    END_CALL_TOOL["name"],
    SEND_DETAILS_TOOL["name"],
    SEND_REFERENCE_TOOL["name"],
)

# Hangups the agent asked for. Held for the same reason as the recording
# watchers: asyncio keeps only weak references to tasks.
_hangup_tasks: set[asyncio.Task] = set()


def _farewell_playout_wait(session: dict[str, Any]) -> float:
    """How long to hold the line so the goodbye finishes reaching the caller.

    response.done fires when the audio has been *generated*, and generation runs
    far faster than playback: a three-sentence farewell is produced in about a
    second and takes seven to speak. A fixed grace period was therefore always a
    guess, and at 3.5s it was a wrong one — the caller heard the line cut in the
    middle, which is exactly the failure the grace period existed to prevent.

    So measure it instead. Every audio delta on the socket is PCM16 at a known
    rate, which gives the true length of what she just said; subtract how much
    of it has already played, and that is the wait. The grace setting becomes
    the tail margin on top, covering carrier buffering.

    Falls back to a length estimate when no audio was measured — deltas are not
    guaranteed to arrive on the socket for a SIP call — and never returns less
    than the configured grace, so this can only ever hold the line longer than
    the old behaviour, never shorter.
    """
    # Margin added once the line is known to have finished playing, covering
    # carrier buffering. Deliberately small: every extra tenth here is dead air
    # the caller sits through after "Allah Hafiz", waiting to be released.
    tail = 1.2
    spoken = 0.0

    audio_bytes = session.get("turn_audio_bytes") or 0
    if audio_bytes:
        # PCM16 is two bytes a sample, one channel.
        spoken = audio_bytes / float(max(settings.agent_audio_sample_rate, 1) * 2)
    elif session.get("turn_text"):
        # ~13 characters a second is unhurried Roman Urdu speech. Only a rough
        # guide, but far closer than assuming the line takes no time at all.
        spoken = len(session["turn_text"]) / 13.0

    if not spoken:
        # Nothing to measure and nothing to estimate from — fall back to the
        # configured grace period, which is what this always used to do.
        return max(settings.hangup_grace_seconds, 0.5)

    started = session.get("turn_audio_started")
    already_played = (time.monotonic() - started) if started else 0.0
    remaining = max(spoken - already_played, 0.0)
    # Cap it: a stuck counter must not hold a dead line open indefinitely.
    return min(remaining + tail, 30.0)


def _schedule_hangup(session: dict[str, Any], our_call_id: str | None) -> None:
    """Drop the line once the farewell has actually reached the caller."""
    if session.get("hangup_started"):
        return
    session["hangup_started"] = True
    wait = _farewell_playout_wait(session)
    log.info("holding line %.1fs for the farewell to play out", wait)

    # A deadline rather than a plain sleep, so a caller who speaks during the
    # goodbye can push it back — see _handle_event. Someone who says one more
    # thing after "Allah Hafiz" must not have the line cut on them mid-sentence.
    session["hangup_at"] = time.monotonic() + wait
    session["hangup_cancelled"] = False

    async def run() -> None:
        while True:
            remaining = session["hangup_at"] - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(remaining)
        if session.get("hangup_cancelled"):
            log.info("hangup cancelled — the caller had more to say")
            session["hangup_started"] = False
            return
        await terminate_call(
            our_call_id,
            openai_call_id=session.get("openai_call_id"),
            outcome=f"ENDED_BY_AGENT:{session.get('end_reason', 'nothing_further')}",
        )

    task = asyncio.create_task(run())
    _hangup_tasks.add(task)
    task.add_done_callback(_hangup_tasks.discard)


async def terminate_call(
    call_id: str | None, *, openai_call_id: str | None = None, outcome: str | None = None
) -> None:
    """Hang up both legs: the AI session and the carrier call.

    Shared by the dashboard's end-call button and the agent's own end_call tool,
    so a call ended either way is torn down and logged identically.
    """
    row = store.get(call_id) if call_id else None
    openai_call_id = openai_call_id or (row or {}).get("openai_call_id")

    if openai_call_id:
        # Stop the agent talking first, then drop its leg. Cancelling the socket
        # task matters: without it she keeps generating audio into a call the
        # operator has already ended, and a queued goodbye can re-arm a hangup.
        session = _sessions.get(openai_call_id)
        if session:
            session.cancel()
        with contextlib.suppress(Exception):
            await hangup_realtime_call(openai_call_id)

    if row:
        # Both legs, independently. A bridged call has the caller inside the
        # dialog *and* the original call leg, and hanging up only one of them —
        # or giving up because the first call raised — is what leaves the
        # student still connected after the dashboard says the call is over.
        targets: list[tuple[str, Any]] = []
        if row.get("dialog_id"):
            targets.append(("dialog", telephony.hangup_dialog(row["dialog_id"])))
        targets.append(("call", telephony.hangup_call(row["call_id"])))
        for what, coro in targets:
            try:
                await coro
            except TelephonyError as exc:
                # The leg may already be gone; that is not worth surfacing.
                log.warning("%s hangup for %s: %s", what, call_id, exc)

        fields: dict[str, Any] = {"status": "ENDING"}
        if outcome and not row.get("outcome"):
            fields["outcome"] = outcome
        store.upsert(row["call_id"], **fields)
        bus.publish("call.updated", call=_serialise(store.get(row["call_id"]) or {}))


# Words that are Roman Urdu and nothing else. A caller speaking English at a
# Pakistani call center still says "fees" and "semester", so the test cannot be
# vocabulary in general — it has to be words English never borrows.
#
# The short function words are deliberately absent. "is", "us", "the", "main",
# "ho", "so", "ki" are all Roman Urdu *and* ordinary English, and including
# them classified "What is the admission fee for BS Computer Science?" as Roman
# Urdu on the strength of "is" and "the" alone. A marker that fires on English
# is worse than a missing one: this list only has to catch the turns where the
# caller genuinely switched, and every Urdu sentence long enough to matter
# carries several of the words below.
_ROMAN_URDU_MARKERS = frozenset("""
aap aapka apka apki apna mujhe mujhy mera meri mere hain hoon kya kia
kaise kaisay kar karna karne karta karti karo karain karein
nahi nahin jee haan yeh woh ager agar lekin magar phir bhi
sab kuch kuchh chahiye chahta chahti theek thik acha achha
sakta sakti sakte raha rahi rahe hoga hogi honge kab kahan kyun kyu
kitni kitna kitne zara thora thori bohat bohot bahut shukriya maazrat
malumat maloomat batayen bataen bata dein den mein hai
""".split())


def _detect_caller_language(text: str) -> str:
    """Name the language of one caller turn, for the model to mirror.

    Deliberately three coarse buckets and no library. The realtime model does
    not need a language code — it needs a plain instruction it can follow, and
    the only distinction that matters on this line is Urdu script vs Roman Urdu
    vs English. Anything finer would be guesswork the caller never hears.
    """
    if not text:
        return ""
    # Urdu, Arabic and the Urdu-specific extensions all sit in these blocks.
    if re.search(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]", text):
        return "Urdu"
    words = re.findall(r"[a-z']+", text.lower())
    if not words:
        return ""
    hits = sum(1 for w in words if w in _ROMAN_URDU_MARKERS)
    # One marker in a long English sentence is a borrowed word, not a switch.
    # Two, or one in a very short turn ("ji han"), is the caller speaking Urdu.
    if hits >= 2 or (hits and len(words) <= 4):
        return "Roman Urdu"
    return "English"


def _language_reminder(session: dict[str, Any]) -> str:
    """The line appended to every tool result to hold the reply's language.

    Tool output is English — knowledge base extracts, confirmations we wrote
    ourselves — and it is the last thing in the context before she speaks. That
    was enough to drag her into English after an Urdu question, on exactly the
    turns where it is most obvious: the answer the caller actually rang for.
    The system prompt already says to mirror the caller; this repeats it where
    the pull is strongest.
    """
    lang = session.get("caller_lang") or ""
    if not lang:
        return ""
    return (
        f"\n\nLANGUAGE: the caller's last turn was in {lang}. This tool output "
        f"is in English only because it is internal data — speak your reply to "
        f"the caller in {lang}, regardless of the language of the text above."
    )


def _queue_tool_call(
    session: dict[str, Any], call_id: Any, name: Any, arguments: Any
) -> None:
    """Remember a tool call so it can be answered once the response finishes."""
    if not call_id or name not in _TOOL_NAMES:
        return
    if call_id in session["answered_tools"]:
        return
    if any(t["call_id"] == call_id for t in session["pending_tools"]):
        return
    session["pending_tools"].append(
        {"call_id": call_id, "name": name, "arguments": arguments or "{}"}
    )


# A farewell is recognised by its parting words, not by its length. Anything the
# model says *about* closing the call ("bas ek warm closing ke saath call finish
# karte hain") contains none of these, which is the whole point of the check.
_FAREWELL_MARKERS = (
    "hafiz",      # Allah Hafiz / Khuda Hafiz — always the last thing she says
    "khudahafiz",
    "allahafiz",
    "goodbye",
    "good bye",
    "bye",
)


_FAREWELL_LINE = (
    "Say only this, warmly, and nothing else: \"University se "
    "rabta karne ka bohat shukriya. Aap ka call hamare liye bohat ahem hai. "
    "Allah Hafiz.\" Do not describe what you are "
    "doing, do not say you are closing the call, and do not ask any question."
)


async def _request_farewell(socket: Any, session: dict[str, Any]) -> None:
    """Ask for the goodbye turn, and arrange for the line to close after it.

    If a response is already live the request is refused, so the debt is carried
    on `owes_farewell` for the next response.done to pick up. Losing it would
    leave the caller holding an open line that nothing else will ever close.
    """
    if await _speak(socket, session, instructions=_FAREWELL_LINE):
        session["owes_farewell"] = False
        session["hangup_after_response"] = True
    else:
        session["owes_farewell"] = True


def _said_farewell(text: str) -> bool:
    """Whether this turn actually contained a spoken goodbye.

    Deliberately narrow. A false negative costs one extra short farewell before
    the line drops; a false positive cuts the caller off with no goodbye at all,
    which is the failure this exists to prevent. "Shukriya" is not on the list —
    she thanks callers constantly mid-conversation.
    """
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _FAREWELL_MARKERS)


async def _answer_tool_calls(
    socket: Any, session: dict[str, Any], our_call_id: str | None
) -> None:
    """Run retrieval for every queued tool call, hand the results back, and ask
    the model to speak the answer.

    The conversation must be idle when this runs: a response.create issued while
    another response is live is rejected outright, and the caller then hears
    nothing at all after asking a question.
    """
    pending, session["pending_tools"] = session["pending_tools"], []
    hang_up = False

    for call in pending:
        try:
            args = json.loads(call["arguments"] or "{}")
        except (json.JSONDecodeError, TypeError):
            args = {}

        if call["name"] == END_CALL_TOOL["name"]:
            hang_up = True
            session["answered_tools"].add(call["call_id"])
            reason = args.get("reason") or "nothing_further"
            log.info("agent asked to end call %s (%s)", our_call_id or "-", reason)
            session["end_reason"] = reason
            await socket.send(json.dumps({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call["call_id"],
                    "output": "Call is being ended. Say nothing further.",
                },
            }))
            continue

        if call["name"] == SEND_DETAILS_TOOL["name"]:
            session["answered_tools"].add(call["call_id"])
            output = _send_details_for_call(args, our_call_id)
            await socket.send(json.dumps({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call["call_id"],
                    "output": output + _language_reminder(session),
                },
            }))
            continue

        if call["name"] == SEND_REFERENCE_TOOL["name"]:
            session["answered_tools"].add(call["call_id"])
            output = _send_reference_for_call(args, session, our_call_id)
            await socket.send(json.dumps({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call["call_id"],
                    "output": output + _language_reminder(session),
                },
            }))
            continue

        if call["name"] == REGISTER_QUERY_TOOL["name"]:
            session["answered_tools"].add(call["call_id"])
            token = _register_query(args, session, our_call_id)
            # Spell the token out for her. Handing back "482103" invites her to
            # read it as "chaar lakh…" — a number, not a reference — and the
            # caller cannot write that down.
            spelled = " ".join(token)
            output = (
                f"Query registered. Reference token: {token}. Tell the caller "
                f"this token, reading it one digit at a time ({spelled}), and "
                f"that they will get a response within {QUERY_SLA_HOURS} hours. "
                "Then ask, once, whether to send the reference on WhatsApp, and "
                "if they say yes call send_reference_whatsapp. "
                "Say the digits in ENGLISH — 'four, eight, two, one, zero, "
                "three' — not in Urdu, and 'zero' for 0. The sentence "
                "around them stays in whatever language the caller is speaking."
            ) if token else (
                "The query could not be registered. Do NOT give the caller a "
                "token or say it was registered. Tell them the admissions "
                "admissions helpline can take it, and apologise once."
            )
            await socket.send(json.dumps({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call["call_id"],
                    "output": output + _language_reminder(session),
                },
            }))
            continue

        query = (args.get("query") or "").strip()
        category = args.get("category")

        started = time.perf_counter()
        context, hits = kb.context_for(query, category=category) if query else ("", [])
        elapsed = (time.perf_counter() - started) * 1000

        log.info(
            "RAG '%s' (%s) -> %d hits, best %.3f, %.0fms",
            query, category or "any", len(hits),
            hits[0]["score"] if hits else 0.0, elapsed,
        )
        bus.publish(
            "rag.query", callId=our_call_id, query=query,
            hits=[{"section": h["section"], "title": h["title"], "score": h["score"]} for h in hits],
        )

        output = context or (
            "No verified information found in the university knowledge base for this "
            "question. Tell the caller you do not have confirmed details and "
            "offer the admissions helpline."
        )
        session["answered_tools"].add(call["call_id"])
        await socket.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call["call_id"],
                "output": output + _language_reminder(session),
            },
        }))

    if hang_up:
        # She is supposed to say goodbye before calling the tool. If she did not,
        # give her that one line first — dropping the line mid-conversation is
        # rude, and the caller hears a dead line with no explanation.
        #
        # "Did she speak at all" is not the same question as "did she say
        # goodbye", and using the first as a proxy for the second is what let a
        # caller hear "bas ek warm closing ke saath call finish karte hain" and
        # then silence: filler counted as a farewell, so the line was cut before
        # any farewell was spoken. Check the words.
        if _said_farewell(session.get("turn_text", "")):
            _schedule_hangup(session, our_call_id)
        else:
            await _request_farewell(socket, session)
        return

    # One turn answers all of them — the model now has every result in context.
    # If turn detection happened to start a response in the gap, this is the one
    # thing that must not be dropped: the caller asked a question and is waiting.
    if not await _speak(socket, session):
        session["owes_answer"] = True


async def _note_interruption(
    socket: Any, session: dict[str, Any], our_call_id: str | None
) -> None:
    """Tell the model what it was cut off saying, so it can offer the rest later.

    Sent as a system item rather than as a per-turn instruction because the turn
    that answers the caller is created by the server's own turn detection — there
    is no `response.create` of ours to attach instructions to. An item added to
    the conversation is read as context by whatever turn comes next, which is
    exactly the lifetime this needs.

    Everything about it is best-effort. It goes out on the barge-in event, which
    is the earliest moment the information exists and the furthest ahead of the
    reply we can get; if the reply is already forming, the note simply lands a
    turn later and the offer comes a turn later with it. And if the socket
    rejects it outright, the call carries on exactly as it did before this
    existed — nothing downstream depends on it.
    """
    # Read, not cleared: response.created already resets it for the next turn,
    # and the farewell check on response.done still needs whatever is in it.
    spoken = (session.get("turn_text") or "").strip()
    if not settings.resume_offer_enabled:
        return
    # Nothing was being said, or what was being said was too slight to return
    # to. Acknowledgements and half-sentences are not worth a callback.
    if len(spoken) < settings.resume_offer_min_chars:
        return
    # A cap, so a caller who interrupts constantly does not end up with a
    # conversation made mostly of these notes.
    if session.get("resume_notes", 0) >= 4:
        return
    session["resume_notes"] = session.get("resume_notes", 0) + 1
    session["interrupted_text"] = spoken
    # Deliberately no event published here: response.done already emits
    # agent.interrupted for this same barge-in, and two of them would double
    # every interruption on the dashboard.
    log.debug("noted interrupted turn on %s", our_call_id or "-")

    note = (
        "SYSTEM NOTE — CONTEXT ONLY. Never read this note aloud, never quote "
        "it, and never mention that you received it.\n"
        "The caller has just cut in. You were part-way through saying:\n"
        f'{spoken}\n'
        "Their new question outranks it completely. Answer THEIR question "
        "first, fully, and stop there — do not finish the sentence above in "
        "that turn and do not refer to it.\n"
        "Afterwards, judge whether what you were cut off saying still "
        "matters: something they must do or bring, a deadline, a fee, a step "
        "they would otherwise miss. If it does, and they have not already "
        "learned it, offer the remainder ONCE, in a later turn, as a short "
        "question they can decline — for example: Pehle main aap ko zaroori "
        "documents bata rahi thi, wo bata doon? Then wait for their "
        "answer. If it does not matter, or they have moved on, let it go "
        "entirely and never raise it again."
    )

    with contextlib.suppress(Exception):
        await socket.send(
            json.dumps(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "system",
                        "content": [{"type": "input_text", "text": note}],
                    },
                }
            )
        )


async def _handle_event(
    socket: Any,
    event: dict[str, Any],
    openai_call_id: str,
    our_call_id: str | None,
    session: dict[str, Any],
) -> None:
    kind = event.get("type", "")

    # ---- response lifecycle, so we never double-speak ----------------------
    if kind == "response.created":
        session["response_active"] = True
        session["turn_had_audio"] = False
        session["turn_text"] = ""
        # A new turn while the line was being held open for the goodbye means
        # the caller came back with something and she is answering it. The
        # farewell is spent, so drop the hangup entirely rather than dropping
        # the line in the middle of the answer they just asked for.
        if session.get("hangup_started") and not session.get("hangup_cancelled"):
            session["hangup_cancelled"] = True
            session["hangup_after_response"] = False
            session["owes_farewell"] = False
        # Per-turn audio accounting, so the hangup can wait for the goodbye to
        # finish playing rather than guessing at how long it takes.
        session["turn_audio_bytes"] = 0
        session["turn_audio_started"] = None
        return

    # ---- the RAG tool call -------------------------------------------------
    # Both of these arrive *while* the response that issued the call is still
    # streaming. Answering here and immediately asking for a new response is
    # rejected by the server ("conversation already has an active response"),
    # which is why the agent used to fall silent on every factual question.
    # So: record the call now, answer it on response.done.
    if kind == "response.output_item.done":
        item = event.get("item") or {}
        if item.get("type") == "function_call":
            _queue_tool_call(session, item.get("call_id"), item.get("name"), item.get("arguments"))
        return

    if kind == "response.function_call_arguments.done":
        # Fallback for payloads that never emit output_item.done. `name` is not
        # always present on this event, hence the default.
        _queue_tool_call(
            session,
            event.get("call_id"),
            event.get("name") or RAG_TOOL["name"],
            event.get("arguments"),
        )
        return

    # ---- transcripts -------------------------------------------------------
    if kind == "conversation.item.input_audio_transcription.completed":
        text = (event.get("transcript") or "").strip()
        if text:
            # Re-judged on every caller turn, never latched: a caller who
            # switches language mid-call must be followed on the next reply,
            # which is precisely what a once-per-call decision cannot do.
            lang = _detect_caller_language(text)
            if lang:
                session["caller_lang"] = lang
            if our_call_id:
                bump_later(our_call_id, "user_turns")
                transcript_later(our_call_id, "caller", text)
            bus.publish("transcript", callId=our_call_id, role="caller", text=text)
        return

    # Ayesha's own audio, when OpenAI sends it here as well as over SIP. This is
    # the half the carrier's stream of the caller's leg can come back without,
    # and a recording with only one voice in it is the "not properly recorded"
    # complaint. Placed at the wall-clock offset it arrived at, so it lines up
    # with the caller rather than bunching at the front.
    if kind.endswith("audio.delta"):
        if not session.get("saw_audio_delta"):
            session["saw_audio_delta"] = True
            log.info("agent audio IS available on the realtime socket (%s)", kind)

        # Measure every turn, not only recorded ones: this is what tells the
        # hangup how long the farewell actually is. Base64 carries three bytes
        # in every four characters, so the size is known without decoding —
        # decoding purely to count would cost CPU on every frame of every call.
        raw = event.get("delta") or ""
        if raw:
            if session.get("turn_audio_started") is None:
                session["turn_audio_started"] = time.monotonic()
            session["turn_audio_bytes"] = (
                session.get("turn_audio_bytes") or 0
            ) + (len(raw) * 3) // 4

        if our_call_id and settings.record_agent_audio:
            delta = event.get("delta")
            if delta:
                # Malformed base64 must not kill the event loop mid-call: a
                # damaged recording is survivable, a dropped call is not.
                with contextlib.suppress(ValueError, TypeError):
                    live_recorder.feed_agent(
                        our_call_id,
                        base64.b64decode(delta),
                        settings.agent_audio_sample_rate,
                    )
        return

    if kind in ("response.output_audio_transcript.done", "response.audio_transcript.done"):
        text = (event.get("transcript") or "").strip()
        if not text:
            return
        # Two guards against the agent appearing to repeat itself:
        #  1. the same item can arrive under both event names, and
        #  2. the model occasionally emits two identical output items in one turn.
        # She spoke in this turn, so a farewell has been delivered if this turn
        # is the one that ends the call.
        session["turn_had_audio"] = True
        item_id = event.get("item_id") or ""
        if item_id and item_id in session["seen_items"]:
            return
        if text == session.get("last_agent_text"):
            log.info("suppressed duplicate agent turn on %s", openai_call_id)
            return
        session["seen_items"].add(item_id)
        session["last_agent_text"] = text
        session["turn_text"] = f"{session.get('turn_text', '')} {text}".strip()
        if our_call_id:
            bump_later(our_call_id, "agent_turns")
            transcript_later(our_call_id, "agent", text)
        bus.publish("transcript", callId=our_call_id, role="agent", text=text)
        return

    # ---- barge-in ----------------------------------------------------------
    if kind == "response.done":
        # Always clear the flag, whatever the outcome — a stuck flag would leave
        # the agent permanently mute.
        session["response_active"] = False
        status = (event.get("response") or {}).get("status")
        if status == "cancelled" and our_call_id:
            bump_later(our_call_id, "interruptions")
            bus.publish("agent.interrupted", callId=our_call_id)
        # The response is over, so the conversation is free: answer any tool
        # call it made and ask for the turn that speaks the result.
        if session["pending_tools"]:
            await _answer_tool_calls(socket, session, our_call_id)
        elif session.get("hangup_after_response"):
            # The farewell we asked for has now been spoken — but only if she
            # actually said it. A turn that talked *about* closing the call
            # leaves the caller with no goodbye, so try once more instead of
            # cutting the line.
            session["hangup_after_response"] = False
            if _said_farewell(session.get("turn_text", "")):
                _schedule_hangup(session, our_call_id)
            elif session.get("farewell_retried"):
                # One retry is the limit. Holding the line open waiting for a
                # word she will not say is worse than closing without it.
                _schedule_hangup(session, our_call_id)
            else:
                session["farewell_retried"] = True
                await _request_farewell(socket, session)
        elif session.get("owes_farewell"):
            await _request_farewell(socket, session)
        elif session.get("owes_answer"):
            # A retrieved answer never got spoken because something else held
            # the conversation. Nothing else will retry it, so do it here.
            session["owes_answer"] = False
            await _speak(socket, session)
        return

    # ---- barge-in ----------------------------------------------------------
    # The caller started talking. The server cancels the in-flight response for
    # us (interrupt_response), so the job here is to make sure nothing of ours
    # survives the interruption and gets spoken over them afterwards.
    if kind == "input_audio_buffer.speech_started":
        session["response_active"] = False

        # Whatever she was saying is now unfinished. Hand it back as context —
        # not as something to say, and never as a reason to keep talking now.
        # The caller's question is answered first, in full; only after that may
        # she offer what is left, and only if it is worth having.
        await _note_interruption(socket, session, our_call_id)

        # An answer that was queued but never spoken is now stale. The caller
        # has moved on, and the model will address whatever they just said with
        # the tool results still in context — replaying the old turn on top of
        # their new question is the "talking over the caller" failure itself.
        session["owes_answer"] = False

        # Belt and braces on the server-side cancel. Sending this when nothing
        # is live is harmless: the error it raises is recognised and ignored
        # below, and a stray cancel is far cheaper than audio that keeps
        # playing into a caller who has started speaking.
        if settings.interruption_enabled:
            with contextlib.suppress(Exception):
                await socket.send(json.dumps({"type": "response.cancel"}))

        # They spoke during the goodbye. Hold the line open: pushing the
        # deadline out gives them room to finish, and a real reply cancels the
        # hangup outright when response.created arrives. If it turns out to be
        # a cough and no turn follows, the deadline still lands and the call
        # ends as it would have.
        if session.get("hangup_started") and not session.get("hangup_cancelled"):
            session["hangup_at"] = max(
                session.get("hangup_at", 0.0), time.monotonic() + 6.0
            )
        return

    if kind == "error":
        detail = event.get("error", {})

        # Cancelling when nothing is live is expected, not a fault: the barge-in
        # path sends a cancel without knowing whether the server already did.
        # Checked before logging, because every interruption would otherwise
        # raise an ERROR and put a red mark on the dashboard for a call that is
        # going perfectly well. Treating it as a real error would also clear
        # state and speak a turn on top of the caller, which is the opposite of
        # what the cancel was for.
        code = (detail.get("code") or "") + " " + (detail.get("message") or "")
        if "cancel" in code.lower():
            log.debug("ignoring benign cancel error: %s", detail)
            return

        log.error("realtime error %s: %s", openai_call_id, detail)
        bus.publish("agent.error", callId=our_call_id, message=detail.get("message", "error"))

        # This one means a response really *is* live — clearing the flag here
        # would let us stack a second one on top and talk over ourselves.
        if detail.get("code") == "conversation_already_has_active_response":
            # The turn we asked for was refused, so it still owes the caller an
            # answer. response.done for the *live* response will retry it.
            session["owes_answer"] = True
            return

        # Any other error kills the turn we optimistically marked active, and no
        # response.done will ever arrive for it. Without this the flag stays set
        # and _speak refuses every later turn — the agent goes silent for the
        # rest of the call while the line stays open.
        session["response_active"] = False
        if session["pending_tools"]:
            await _answer_tool_calls(socket, session, our_call_id)
        elif session.get("owes_farewell"):
            # An owed farewell must never be lost to an error: the caller is
            # waiting on a line nothing else will close.
            session["owes_farewell"] = False
            _schedule_hangup(session, our_call_id)
        elif session.get("owes_answer"):
            session["owes_answer"] = False
            await _speak(socket, session)
        return


# =============================================================================
# API
# =============================================================================

LIVE_STATUSES = ("DIALING", "RINGING", "ANSWERED", "CONNECTING_AGENT", "BRIDGED", "ENDING")


def reap_stale_calls() -> int:
    """Close calls that outlived the hard call limit.

    A dropped or unparseable CALL_FINISHED would otherwise leave a call marked
    live forever — inflating "active now", and making the dashboard show a
    ticking duration of 50 minutes on a service capped at three.
    """
    cutoff = time.time() - (settings.max_call_duration_seconds + 120)
    closed = 0
    for row in store.list(limit=200):
        if row.get("status") not in LIVE_STATUSES:
            continue
        started = row.get("start_time") or 0
        if started > cutoff:
            continue
        ended = (row.get("answer_time") or started) + settings.max_call_duration_seconds
        store.upsert(
            row["call_id"],
            status="COMPLETED",
            end_time=ended,
            duration=settings.max_call_duration_seconds,
            outcome=row.get("outcome") or "CLOSED_BY_TIMEOUT",
        )
        # A call closed this way never produced a CALL_FINISHED, so nothing else
        # would ever go and fetch its recording.
        if settings.record_calls and row.get("dialog_id") and not row.get("recording_path"):
            with contextlib.suppress(RuntimeError):  # no running loop at import time
                _watch_recording(row["call_id"], row["dialog_id"])
        # Nor would anything have summarised it. Same suppression, same reason:
        # this also runs once at startup, before there is a loop to schedule on.
        with contextlib.suppress(RuntimeError):
            schedule_call_summary(row["call_id"])
        closed += 1
    if closed:
        log.info("reaped %d stale call(s) with no finish event", closed)
    return closed


async def _reaper_loop() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            if reap_stale_calls():
                bus.publish("calls.reaped")
        except Exception:  # noqa: BLE001 - housekeeping must never kill the app
            log.exception("stale-call reaper failed")


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    log.info("University Voice Agent starting (%s)", settings.app_env)
    # Printed on every boot so "is the new build actually live, with the right
    # environment?" is answered by the log rather than by a test call.
    log.info(
        "recording: calls=%s agent_audio=%s media_stream=%s (build: agent-only "
        "capture supported)",
        settings.record_calls,
        settings.record_agent_audio,
        settings.record_from_media_stream,
    )
    # Building the index blocks for ~30s on a cold cache. Running it in a worker
    # thread keeps /health responsive, which matters because Render kills a
    # service whose health check times out during a cold start.
    await asyncio.get_running_loop().run_in_executor(None, kb.build)
    if settings.openai_project_id:
        log.info("Infobip SIP trunk should target %s", settings.sip_uri)
    if not telephony.configured:
        log.warning("telephony not fully configured — calling endpoints will 503")

    # Clear anything left hanging by a previous run, then keep it tidy. The
    # reaper is the last resort — it guesses. The reconciler asks the provider
    # and knows, so it runs alongside and almost always gets there first.
    reap_stale_calls()
    reaper = asyncio.create_task(_reaper_loop())
    reconciler = asyncio.create_task(_reconcile_loop()) if telephony.configured else None
    writer = asyncio.create_task(_writer_loop())

    yield

    reaper.cancel()
    if reconciler:
        reconciler.cancel()
    # Let the queue empty before going: these are call records, and the process
    # is usually stopping because a deploy is replacing it.
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(_pending_writes.join(), timeout=10)
    writer.cancel()

    for task in _sessions.values():
        task.cancel()
    await telephony.close()
    await recordings.close()
    if _openai_client:
        await _openai_client.aclose()
    if hasattr(store, "close"):
        store.close()


app = FastAPI(
    title="University Voice Agent",
    version="1.0.0",
    description="RAG knowledge base + Pakistani Urdu voice agent + telephony",
    lifespan=lifespan,
)

# In development any localhost port is allowed: static servers (Live Server,
# python -m http.server, Vite) pick whatever port is free, and hard-listing them
# means the dashboard silently shows no data the moment the port shifts.
# Production stays strict — only the origins in FRONTEND_URL.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_origin_regex=(
        r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"
        if settings.app_env == "development"
        else None
    ),
    allow_credentials=True,
    # DELETE is not optional: deleting a call is a DELETE, and leaving it out
    # made the browser's preflight answer 400, so the request was never sent at
    # all. The dashboard reported "Failed to fetch" and the row stayed put.
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

api = APIRouter(prefix="/api")
E164 = re.compile(r"^\+[1-9]\d{7,14}$")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=1000)
    session_id: str | None = None
    language: str | None = None


def _to_e164(value: str) -> str:
    cleaned = "+" + re.sub(r"\D", "", value)
    if not E164.match(cleaned):
        raise ValueError("Enter a valid number in E.164 format, e.g. +923001234567")
    return cleaned


class DialRequest(BaseModel):
    phone_number: str

    @field_validator("phone_number")
    @classmethod
    def validate_e164(cls, value: str) -> str:
        return _to_e164(value)


class ManualDialRequest(BaseModel):
    """A call the agent stays out of, placed by a person.

    `operator_number` is the counselor's own phone: it is dialled first, and the
    student is bridged in only once the counselor has picked up. Dialling the
    student first would leave them listening to silence while a second phone
    rings somewhere else.
    """

    phone_number: str
    operator_number: str

    @field_validator("phone_number", "operator_number")
    @classmethod
    def validate_e164(cls, value: str) -> str:
        return _to_e164(value)


class BrowserCallRequest(BaseModel):
    """A call the browser placed itself, told to us so it can be logged.

    The softphone dials Infobip directly, so unlike every other mode the server
    never learns the call exists — it would be missing from the log and its
    recording would never be fetched. The browser reports the id the SDK
    generated; webhooks for that same id do the rest.
    """

    call_id: str = Field(min_length=8, max_length=128)
    phone_number: str

    @field_validator("phone_number")
    @classmethod
    def validate_e164(cls, value: str) -> str:
        return _to_e164(value)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:
    """Users get a sentence; the stack trace stays in the logs."""
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "environment": settings.app_env,
        "knowledge_base": {
            "ready": kb.ready,
            "chunks": len(kb.chunks),
            "model": settings.embedding_model,
        },
        "telephony": {"provider": settings.telephony_provider, "configured": telephony.configured},
        "voice": {"model": settings.realtime_model, "voice": settings.tts_voice,
                  "language": settings.language},
    }


@api.get("/config")
async def config() -> dict[str, Any]:
    """Non-sensitive settings the dashboard needs. No secrets ever leave here."""
    return {
        "phone_number": settings.infobip_phone_number,
        "max_call_duration_seconds": settings.max_call_duration_seconds,
        "interruption_enabled": settings.interruption_enabled,
        "language": settings.language,
        "voice": settings.tts_voice,
        "realtime_model": settings.realtime_model,
        "telephony_ready": telephony.configured,
        "knowledge_base_ready": kb.ready,
        # Manual (agent-free) calling. Both modes need only working telephony;
        # browser calling additionally needs the `webrtc:manage` scope on the
        # API key, which cannot be checked without spending a token request, so
        # it is reported optimistically and /rtc/token explains any refusal.
        "manual_phone_ready": telephony.configured,
        "manual_browser_ready": telephony.configured,
        "operator_phone_number": settings.operator_phone_number,
        # Whether Ayesha is allowed to offer the token on WhatsApp. False means
        # the sender or the approved template is missing, and the prompt is
        # built telling her she cannot send anything — see _whatsapp_prompt_block.
        "whatsapp_ready": settings.whatsapp_configured,
        # Whether the second template is live, i.e. whether information
        # itself can be sent in writing and not just a reference number.
        "whatsapp_details_ready": settings.whatsapp_details_configured,
        # Post-call reporting. `excel_ready` is what decides whether the
        # dashboard shows a Download Excel button at all — offering a download
        # that 503s because the package is missing is worse than not offering
        # it, and the check costs one import at request time.
        "summary_enabled": settings.summary_enabled and _text_llm_ready(),
        "excel_ready": _excel_available(),
        "excel_filename": settings.excel_filename,
        # Where the call log and the audio actually live, right now, in this
        # process. Not a detail: "sqlite" or "disk" on a hosted instance means
        # everything recorded is lost at the next restart, and there is no other
        # way to tell from outside — the service behaves identically until the
        # moment the data disappears.
        "storage": {
            "calls": "postgres" if isinstance(store, PostgresStore) else "sqlite",
            "recordings": "supabase" if recordings.remote else "disk",
            "durable": isinstance(store, PostgresStore) and recordings.remote,
        },
    }


def _excel_available() -> bool:
    """Whether this deployment can actually produce the workbook.

    Cached by the import system after the first call, so this is a dictionary
    lookup on every request but the first.
    """
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        return False
    return True


def _llm_tuning() -> dict[str, Any]:
    """Sampling arguments for whichever LLM_MODEL is configured.

    The reasoning models renamed the length cap and reject `temperature`
    outright, so the same three arguments cannot be sent to both families —
    passing the old pair to gpt-5 is a hard 400, not a warning.
    """
    if settings.llm_model.startswith(("gpt-5", "o1", "o3", "o4")):
        return {
            "max_completion_tokens": settings.llm_max_tokens,
            "reasoning_effort": settings.llm_reasoning_effort,
        }
    return {
        "temperature": settings.llm_temperature,
        "max_tokens": settings.llm_max_tokens,
    }


def _text_llm_ready() -> bool:
    """Whether chat, summaries and WhatsApp replies have a model to call."""
    return settings.azure_configured or bool(settings.openai_api_key)


def _text_llm_client() -> OpenAI:
    """The client for text completions: Azure when configured, else OpenAI."""
    if settings.azure_configured:
        return AzureOpenAI(
            azure_endpoint=settings.azure_openai_root,
            api_key=settings.azure_openai_api_key.strip(),
            api_version=settings.azure_openai_api_version,
        )
    return OpenAI(api_key=settings.openai_api_key)


# ---- RAG --------------------------------------------------------------------
@api.post("/chat")
async def chat(request: ChatRequest) -> dict[str, Any]:
    """Text channel into the same knowledge base the voice agent uses."""
    if not kb.ready:
        raise HTTPException(503, "Knowledge base is still loading, try again shortly")
    if not _text_llm_ready():
        raise HTTPException(503, "LLM is not configured")

    context, hits = kb.context_for(request.message)
    if not hits:
        return {
            "answer": (
                "Maazrat, is baare mein mere paas verified maloomat nahi hain. "
                "Aap hamari admissions helpline par rabta kar sakte hain."
            ),
            "sources": [],
            "session_id": request.session_id or str(uuid.uuid4()),
        }

    client = _text_llm_client()
    try:
        completion = client.chat.completions.create(
            model=settings.llm_model,
            **_llm_tuning(),
            messages=[
                {"role": "system", "content": build_system_prompt(for_voice=False)},
                {
                    "role": "user",
                    "content": (
                        f"University knowledge base extracts:\n\n{context}\n\n"
                        f"Caller's question: {request.message}\n\n"
                        "Answer only from the extracts above, in natural Pakistani Urdu."
                    ),
                },
            ],
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("LLM call failed")
        raise HTTPException(502, "The assistant is temporarily unavailable") from exc

    return {
        "answer": completion.choices[0].message.content,
        "sources": [
            {"section": h["section"], "title": h["title"], "page": h["page"], "score": h["score"]}
            for h in hits
        ],
        "session_id": request.session_id or str(uuid.uuid4()),
    }


@api.get("/knowledge/search")
async def knowledge_search(
    q: str = Query(min_length=1),
    top_k: int | None = None,
    threshold: float | None = None,
    category: str | None = None,
) -> dict[str, Any]:
    """Raw retrieval — useful for tuning TOP_K and SIMILARITY_THRESHOLD."""
    if not kb.ready:
        raise HTTPException(503, "Knowledge base is still loading")
    hits = kb.search(q, top_k=top_k, threshold=threshold, category=category)
    return {"query": q, "count": len(hits), "results": hits}


# ---- Calls ------------------------------------------------------------------
def _recording_state(row: dict[str, Any]) -> str:
    """Whether this call has audio, is about to, or never will.

    The dashboard used to show "still being prepared, try again" for all three,
    which reads as a promise. On a call that was never recorded that promise is
    never kept, and the only way to find out was to keep clicking. Saying NONE
    out loud costs nothing and stops the retry loop.
    """
    if row.get("recording_path") or row.get("recording_file_id"):
        return "READY"
    if not settings.record_calls or row.get("recording_absent"):
        return "NONE"
    if row.get("status") in LIVE_STATUSES:
        return "RECORDING"
    # Nobody ever spoke, so there is no conversation to wait for. Calls that did
    # capture a ringing leg have a file by now and returned READY above.
    if not row.get("duration"):
        return "NONE"
    ended = row.get("end_time") or 0
    # Past the window the reconciler stops looking, so nothing more is coming.
    if ended and time.time() - ended > settings.reconcile_window_seconds:
        return "NONE"
    return "PENDING"


def _serialise(row: dict[str, Any]) -> dict[str, Any]:
    duration = row.get("duration") or 0
    # Talk time runs from the moment the call was answered, not from the moment
    # it started ringing — ringing is not conversation, and counting it made the
    # dashboard timer disagree with both the recording and the phone bill.
    clock_from = row.get("answer_time") or row.get("start_time")
    if not duration and clock_from and not row.get("end_time"):
        # Live call: elapsed so far, but never beyond the hard call limit — a
        # missed finish event must not render as a 50-minute call.
        elapsed = int(time.time() - clock_from)
        duration = min(max(elapsed, 0), settings.max_call_duration_seconds)
    return {
        # A server-side file path is of no use to the browser and no business
        # of it either; the audio is fetched through /recording.
        **{
            k: v
            for k, v in row.items()
            if k not in ("recording_path", "recording_mime", "recording_file_id")
        },
        # Normalise on read as well as on write, so rows stored before
        # normalisation existed still render in one consistent shape.
        "phone_number": _normalise(row.get("phone_number") or ""),
        "duration": duration,
        "recording_ready": bool(row.get("recording_path") or row.get("recording_file_id")),
        "recording_state": _recording_state(row),
    }


@api.post("/calls/outbound", status_code=201)
async def dial(request: DialRequest) -> dict[str, Any]:
    if not telephony.configured:
        raise HTTPException(503, "Telephony is not configured")

    try:
        result = await telephony.dial(request.phone_number)
    except TelephonyError as exc:
        log.error("dial failed: %s", exc)
        raise HTTPException(502, "Could not place the call. Please try again.") from exc

    call_id = result.get("id")
    if not call_id:
        raise HTTPException(502, "Telephony provider returned no call id")

    store.upsert(
        call_id,
        session_id=str(uuid.uuid4()),
        phone_number=request.phone_number,
        direction="OUTBOUND",
        status="DIALING",
        start_time=time.time(),
    )
    log.info("outbound call %s to %s", call_id, mask_number(request.phone_number))
    # The phone is ringing now and Ayesha is needed the moment it is answered.
    # Opening the connection to OpenAI during the ring means the handshake is
    # already paid for by then, instead of being the first thing the student
    # waits through in silence.
    asyncio.create_task(warm_openai_connection())
    if settings.predial_agent_leg:
        _predial_tasks[call_id] = asyncio.create_task(_predial_agent_leg(call_id))
    record = _serialise(store.get(call_id) or {})
    bus.publish("call.created", call=record)
    return record


# Pre-dial bridges in flight, keyed by call id. `_on_established` waits on the
# task here before deciding whether to bridge, because the guard it relied on —
# `dialog_id` in the call row — is only written by the DIALOG_ESTABLISHED
# webhook. When pickup beat that webhook the guard read empty, a second leg was
# dialled, and Infobip refused it with "Call already in <id> dialog" — which the
# handler then recorded as a FAILED call while the two parties were talking
# normally. Holding the task itself closes the window the flag could not.
_predial_tasks: dict[str, asyncio.Task] = {}


async def _predial_agent_leg(call_id: str) -> None:
    """Bring the AI leg up during the ring, if the carrier allows it.

    Best-effort by design. A failure here is not a failed call: `_on_established`
    still bridges on pickup exactly as it always did, and its `dialog_id` guard
    is what stops the two paths from ever both attaching a leg. So the worst
    case is the behaviour we had before, one rejected request later.
    """
    try:
        dialog = await telephony.bridge_to_agent(call_id, settings.infobip_phone_number)
        # Record the dialog immediately rather than waiting for
        # DIALOG_ESTABLISHED. This is the value the pickup path guards on, and
        # the webhook that would otherwise supply it routinely arrives second.
        dialog_id = (dialog or {}).get("id") if isinstance(dialog, dict) else None
        if dialog_id:
            await asyncio.to_thread(store.upsert, call_id, dialog_id=dialog_id)
    except TelephonyError as exc:
        # Very likely "parent call is not established" — the documented shape of
        # the dialog API assumes an answered parent. Not an error worth alarming
        # anyone about; pickup will bridge normally.
        log.info(
            "pre-dial not accepted for %s (%s) — bridging on pickup instead",
            call_id, exc,
        )
    except Exception:  # noqa: BLE001
        log.exception("pre-dial failed unexpectedly for %s", call_id)


@api.post("/calls/manual", status_code=201)
async def dial_manual(request: ManualDialRequest) -> dict[str, Any]:
    """Ring the counselor, then bridge the student to them. Ayesha never joins.

    The row is stored with the *student's* number as phone_number, because that
    is who the call is with — the counselor's leg is plumbing. `mode` is what
    stops _on_established from attaching the AI leg on pickup.
    """
    if not telephony.configured:
        raise HTTPException(503, "Telephony is not configured")

    try:
        result = await telephony.dial(request.operator_number)
    except TelephonyError as exc:
        log.error("manual dial failed: %s", exc)
        raise HTTPException(502, "Could not ring your phone. Please try again.") from exc

    call_id = result.get("id")
    if not call_id:
        raise HTTPException(502, "Telephony provider returned no call id")

    store.upsert(
        call_id,
        session_id=str(uuid.uuid4()),
        phone_number=request.phone_number,
        operator_number=request.operator_number,
        mode="MANUAL_PHONE",
        direction="OUTBOUND",
        status="DIALING",
        start_time=time.time(),
    )
    log.info(
        "manual call %s: ringing %s to reach %s",
        call_id,
        mask_number(request.operator_number),
        mask_number(request.phone_number),
    )
    record = _serialise(store.get(call_id) or {})
    bus.publish("call.created", call=record)
    return record


@api.post("/rtc/calls", status_code=201)
async def register_browser_call(request: BrowserCallRequest) -> dict[str, Any]:
    """Log a call the browser softphone placed on its own.

    Everything downstream of this is the ordinary pipeline: DIALOG_ESTABLISHED
    attaches the dialog id to this row, CALL_FINISHED closes it, and the same
    poller that saves an AI call's audio saves this one. `mode` is what keeps
    _on_established from bridging Ayesha onto a call she must stay off.
    """
    store.upsert(
        request.call_id,
        session_id=str(uuid.uuid4()),
        phone_number=request.phone_number,
        mode="MANUAL_BROWSER",
        direction="OUTBOUND",
        status="DIALING",
        start_time=time.time(),
    )
    log.info(
        "browser call %s to %s", request.call_id, mask_number(request.phone_number)
    )
    # Browser calls are recorded by the browser itself and posted back, because
    # the provider will not record them: they run under its built-in WEBRTC
    # calls configuration, which is not ours, has recording off, and has no API
    # left to configure. Asking anyway just fills the log with refusals.
    if settings.record_calls and not settings.record_browser_client_side:
        watcher = asyncio.create_task(_ensure_recording(request.call_id))
        _recording_watchers.add(watcher)
        watcher.add_done_callback(_recording_watchers.discard)
    record = _serialise(store.get(request.call_id) or {})
    bus.publish("call.created", call=record)
    return record


@api.post("/rtc/token")
async def rtc_token() -> dict[str, Any]:
    """A short-lived WebRTC credential for the browser softphone.

    No WebRTC application is involved: calling a phone number from the browser
    needs only an API key carrying the `webrtc:manage` scope. A key without it
    gets UNAUTHORIZED from Infobip, which is the one failure worth translating —
    it is a permissions checkbox, not a fault in the call.
    """
    identity = f"ucp-console-{uuid.uuid4().hex[:12]}"
    try:
        result = await telephony.rtc_token(identity)
    except TelephonyError as exc:
        log.error("rtc token failed: %s", exc)
        if exc.status in (401, 403):
            raise HTTPException(
                503,
                "This Infobip API key cannot use WebRTC. Add the "
                "'webrtc:manage' scope to the key in Developer Tools -> API Keys.",
            ) from exc
        raise HTTPException(
            502, "Infobip would not issue a calling token for this account."
        ) from exc

    return {
        "token": result.get("token"),
        "identity": identity,
        "expires_at": result.get("expirationTime"),
        "expires_in": settings.rtc_token_ttl_seconds,
        "from": settings.infobip_phone_number,
    }


@api.get("/calls")
async def list_calls(limit: int = 50, direction: str | None = None) -> dict[str, Any]:
    return {"calls": [_serialise(r) for r in store.list(limit=limit, direction=direction)]}


@api.get("/calls/stats")
async def call_stats() -> dict[str, Any]:
    return store.stats()


class ResolveQueryRequest(BaseModel):
    resolution: str = Field("", max_length=4000)


def _serialise_query(row: dict[str, Any]) -> dict[str, Any]:
    """Query rows are not call rows.

    Deliberately not `_serialise`: that one is built for the `calls` shape and
    would bolt a phone_number, a duration and recording state onto a record
    that has none of them.
    """
    if not row:
        return {}
    return {
        **row,
        "phone": _normalise(row.get("phone") or ""),
        "overdue": bool(
            row.get("status") == "OPEN" and (row.get("due_at") or 0) < time.time()
        ),
    }


# Registered under /queries before /queries/{token}: FastAPI matches in
# declaration order, so a literal path declared after the parameterised one is
# swallowed by it and "stats" arrives as a token.
@api.get("/queries")
async def list_queries(limit: int = 100, status: str | None = None) -> dict[str, Any]:
    """Open queries, soonest deadline first — the follow-up desk's work list."""
    return {
        "queries": [
            _serialise_query(r) for r in store.list_queries(limit=limit, status=status)
        ]
    }


@api.get("/queries/stats")
async def query_stats() -> dict[str, Any]:
    return store.query_stats()


@api.get("/queries/{token}")
async def get_query(token: str) -> dict[str, Any]:
    row = store.get_query(token)
    if not row:
        raise HTTPException(404, "Query not found")
    return _serialise_query(row)


@api.post("/queries/{token}/whatsapp", status_code=202)
async def resend_query_whatsapp(token: str, number: str | None = None) -> dict[str, Any]:
    """Re-send a token over WhatsApp — for the ones that failed, or a new number.

    Deliberately does not check `whatsapp_opt_in`: reaching for this button is
    a human deciding to send it, which is consent of a different and better
    kind than a checkbox on a phone call.
    """
    row = store.get_query(token)
    if not row:
        raise HTTPException(404, "Query not found")
    if not settings.whatsapp_configured:
        raise HTTPException(503, "WhatsApp is not configured")

    target = _whatsapp_msisdn(number or row.get("phone") or "")
    if not target:
        raise HTTPException(400, "No number to send to")

    store.update_query(token, whatsapp_status="pending")
    _send_token_whatsapp(token, target)
    return {"token": token, "to": mask_number(target), "whatsapp_status": "pending"}


@api.post("/queries/{token}/resolve")
async def resolve_query(
    token: str, request: ResolveQueryRequest, notify: bool = True
) -> dict[str, Any]:
    """Close a query off, and send the answer to the student.

    Recording the resolution and never telling the caller was the hole in this
    loop: the desk marked a query answered, the student who rang about it was
    never told, and the only trace was a row nobody outside the office reads.
    So the answer goes out on WhatsApp as it is saved.

    Sending is best-effort and deliberately does not gate the close. A message
    that fails must not leave the query stuck OPEN and reappearing on the desk's
    list — `whatsapp_status` records what happened to it, and the dashboard can
    re-send from there.
    """
    existing = store.get_query(token)
    if not existing:
        raise HTTPException(404, "Query not found")
    resolution = request.resolution.strip()
    store.update_query(
        token,
        status="RESOLVED",
        resolved_at=time.time(),
        resolution=resolution or None,
    )

    sent_to: str | None = None
    # Nothing to send is not a failure: a query can be closed as a duplicate, or
    # answered by someone ringing the student directly.
    if resolution and notify:
        target = _whatsapp_msisdn(existing.get("phone") or "")
        if not target:
            store.update_query(token, whatsapp_status="no_number")
        elif not settings.whatsapp_details_configured:
            store.update_query(token, whatsapp_status="disabled")
        else:
            store.update_query(token, whatsapp_status="pending")
            _send_details_whatsapp(
                target,
                existing.get("query_text") or existing.get("category") or "aap ki query",
                resolution,
                call_id=existing.get("call_id"),
                token=token,
            )
            sent_to = mask_number(target)

    row = _serialise_query(store.get_query(token) or {})
    bus.publish("query.resolved", token=token, sentTo=sent_to)
    return {**row, "whatsapp_sent_to": sent_to}


@api.get("/calls/{call_id}")
async def get_call(call_id: str) -> dict[str, Any]:
    row = store.get(call_id)
    if not row:
        raise HTTPException(404, "Call not found")
    return _serialise(row)


@api.delete("/calls/{call_id}")
async def delete_call(call_id: str) -> dict[str, str]:
    """Erase a call and its audio.

    Deliberately permanent: the row goes, and so does the recording file, since
    a deleted call that still has its audio sitting on disk is not deleted in
    any sense the person clicking the button would recognise.

    Deleting a call that is still up hangs it up first. Refusing used to seem
    safer, but with several people sharing one dashboard the row someone wants
    gone is often exactly the one still ringing on a desk nobody is at — and
    dropping the line is what they meant by deleting it.
    """
    row = store.get(call_id)
    if not row:
        raise HTTPException(404, "Call not found")
    if not row.get("end_time") and row.get("status") in LIVE_STATUSES:
        log.info("delete on live call %s — hanging it up first", call_id)
        with contextlib.suppress(Exception):
            await terminate_call(call_id, outcome="ENDED_BY_OPERATOR")

    await recordings.delete(row)
    store.delete(call_id)
    # The words spoken on the call are as much a record of it as the audio, so
    # a delete that left them behind would not be a delete. Suppressed because
    # the row and the file are already gone: failing here would report a delete
    # that did not happen on a call that no longer exists.
    with contextlib.suppress(Exception):
        await asyncio.to_thread(store.delete_transcripts, call_id)
    # And the queries raised on it. The workbook deliberately keeps a query
    # whose call row has gone — somebody is still waiting on an answer — so
    # without this a deleted call carried on appearing in the Follow-Up Queries
    # sheet under the id of a call that no longer exists. Deleting a call has
    # to mean it is gone from the report too.
    removed = 0
    with contextlib.suppress(Exception):
        removed = await asyncio.to_thread(store.delete_queries, call_id)
    _transcript_seq.pop(call_id, None)
    log.info("deleted call %s (%d quer%s)", call_id, removed, "y" if removed == 1 else "ies")
    bus.publish("call.deleted", callId=call_id)
    return {"status": "deleted"}


@api.post("/calls/{call_id}/end")
async def end_call(call_id: str) -> dict[str, Any]:
    """Hang up, then close the row without waiting for the provider to say so.

    Hanging up leaves the call ENDING — the finish event is what turns that into
    a real end time and duration, and it may be seconds away or may never come.
    Everyone watching the dashboard sees a timer that is still running, on a
    call they just watched somebody end. So chase the provider's own record
    immediately instead: the row is normally closed before the click's toast has
    faded, and the reconcile loop remains the backstop if it is not.
    """
    if not store.get(call_id):
        raise HTTPException(404, "Call not found")
    await terminate_call(call_id, outcome="ENDED_BY_OPERATOR")
    await _settle_call(call_id)
    return {"status": "ok", "call": _serialise(store.get(call_id) or {})}


async def _settle_call(call_id: str, attempts: tuple[float, ...] = (0.5, 1.5, 3.0)) -> None:
    """Ask the provider how a just-ended call ended, briefly and then give up.

    The provider needs a moment to write its own history, so a single immediate
    lookup usually finds the call still open. These few retries cover that gap;
    anything slower is left to the reconcile loop rather than held open here,
    because the caller is a click waiting on a response.
    """
    for delay in attempts:
        await asyncio.sleep(delay)
        row = store.get(call_id)
        if not row or row.get("status") not in LIVE_STATUSES:
            return
        with contextlib.suppress(Exception):
            if await _reconcile_call(row):
                bus.publish("call.updated", call=_serialise(store.get(call_id) or {}))
                return


def _split_recording_files(payload: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pull the audio files out of an Infobip dialog-recording response.

    Shape is nested, not what the field names suggest, and sometimes wrapped:
        { dialogId, composedFiles: [...], callRecordings: [ { files: [...] } ] }
        { results: [ { …the above… } ] }
    A dialog records each leg separately, so callRecordings normally holds two
    entries — the caller and the agent. `composedFiles` is the mixed-down single
    file, and only exists once a composition has been requested.

    Returns (composed, per_leg) rather than one merged list: the caller has to
    know which it got, because handing back a single leg means a recording with
    one voice and silence where the other person spoke. Reading the wrapped
    shape here is what makes composition get requested at all — checking
    `payload["composedFiles"]` directly silently misses it on `results`
    responses and the download falls through to a half-empty leg file.
    """
    if isinstance(payload, dict) and "results" in payload:
        entries = payload.get("results") or []
    elif isinstance(payload, list):
        entries = payload
    else:
        entries = [payload]

    composed: list[dict[str, Any]] = []
    per_leg: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        composed.extend(entry.get("composedFiles") or [])
        for rec in entry.get("callRecordings") or []:
            per_leg.extend(rec.get("files") or [])
        # The per-call endpoint puts its files at the top level instead.
        per_leg.extend(entry.get("files") or [])
    return composed, per_leg


def _recording_files(payload: Any) -> list[dict[str, Any]]:
    composed, per_leg = _split_recording_files(payload)
    return composed or per_leg


RECORDINGS_DIR = (
    Path(settings.recordings_dir)
    if Path(settings.recordings_dir).is_absolute()
    else BASE_DIR / settings.recordings_dir
)

# Infobip file formats -> what a browser needs to play them.
_AUDIO_MIME = {"WAV": "audio/wav", "MP3": "audio/mpeg", "OGG": "audio/ogg"}


async def _to_mp3(audio: bytes) -> bytes | None:
    """Re-encode a browser recording as MP3, or None if that is not possible.

    MediaRecorder produces a *streaming* webm: no duration in the header and no
    seek index, because it is designed to be written while the data is still
    arriving. Such a file plays from the beginning and nothing else — no
    scrubbing, no skip forward, no rewind — in our own player and in whatever
    the operator opens the download with. Re-encoding writes a container with
    the length and frame index that seeking needs, and MP3 in particular plays
    on the machines admissions staff actually use.

    Runs ffmpeg over pipes rather than temporary files: this is a web request,
    and a container's disk is not somewhere to leave audio lying around.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-i", "pipe:0",
            "-vn",
            "-ac", "1",              # one channel: it is a phone call, not music
            "-ar", "24000",
            "-b:a", "48k",           # ample for speech, and small to store
            "-write_xing", "1",      # the header that makes the file seekable
            "-f", "mp3",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError):
        # No ffmpeg in this image. Keep the original rather than lose the call.
        log.warning("ffmpeg unavailable — storing the browser recording as-is")
        return None

    try:
        converted, errors = await asyncio.wait_for(
            process.communicate(audio), timeout=settings.transcode_timeout_seconds
        )
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        log.warning("transcode timed out — storing the browser recording as-is")
        return None

    if process.returncode != 0 or not converted:
        log.warning("transcode failed (%s): %s", process.returncode, errors[:200].decode("utf-8", "replace"))
        return None
    return converted


def _extension_for(mime: str) -> str:
    """A filename extension that matches the bytes.

    Provider recordings are WAV; browser-made ones are webm/opus. Naming a webm
    file .wav gives the operator a download their media player refuses to open.
    """
    mime = (mime or "").lower()
    for needle, extension in (
        ("webm", "webm"),
        ("ogg", "ogg"),
        ("mp4", "mp4"),
        ("mpeg", "mp3"),
        ("mp3", "mp3"),
    ):
        if needle in mime:
            return extension
    return "wav"


# Object keys written to Supabase carry this marker, so a row saved by the
# deployed service is still recognisable on a dev machine reading the same
# database — and a local file path stored by that dev machine is not mistaken
# for an object key in production.
_REMOTE_PREFIX = "sb://"


class RecordingStorage:
    """Where call audio lives. Supabase Storage if configured, else the disk.

    The disk is the wrong answer anywhere the filesystem is replaced on deploy —
    which is every free-tier host — but it is exactly right for a laptop, so
    both stay. `recording_path` holds whichever reference the active backend
    understands, and reads tolerate the other kind by returning nothing, which
    sends the caller down the re-fetch path rather than to an error.
    """

    def __init__(self) -> None:
        self.remote = bool(settings.supabase_url and settings.supabase_service_key)
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=f"{settings.supabase_url.rstrip('/')}/storage/v1",
                # The new-style secret keys are not JWTs, and Storage rejects
                # them outright in an Authorization header ("Invalid Compact
                # JWS"). The apikey header is the one that authenticates them.
                headers={"apikey": settings.supabase_service_key},
                timeout=60,
            )
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def put(self, call_id: str, data: bytes, mime: str, extension: str) -> str | None:
        """Store the audio and return the reference to record, or None."""
        if not self.remote:
            RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
            target = RECORDINGS_DIR / f"{call_id}.{extension}"
            target.write_bytes(data)
            stored = target
            with contextlib.suppress(ValueError):
                stored = target.relative_to(BASE_DIR)
            return stored.as_posix()

        # Too big for the bucket. Not an error and not a lost recording: the
        # provider's file id is kept, so playback streams from them instead.
        if len(data) > settings.supabase_max_upload_bytes:
            log.warning(
                "recording for %s is %.0f MB — over the bucket limit, leaving it with the provider",
                call_id,
                len(data) / 1_048_576,
            )
            return None

        key = f"calls/{call_id}.{extension}"
        try:
            response = await self._http().post(
                f"/object/{settings.supabase_bucket}/{key}",
                content=data,
                # Same call id overwrites rather than 409s, so re-saving a
                # recording is repeatable instead of a one-time operation.
                headers={"content-type": mime, "x-upsert": "true"},
            )
        except httpx.HTTPError as exc:
            log.warning("recording upload for %s failed: %s", call_id, exc)
            return None
        if response.status_code >= 400:
            log.warning(
                "recording upload for %s returned %s: %s",
                call_id,
                response.status_code,
                response.text[:200],
            )
            return None
        return f"{_REMOTE_PREFIX}{key}"

    async def signed_url(self, row: dict[str, Any], seconds: int = 3600) -> str | None:
        """A temporary direct link to the audio, or None if there is no object.

        Serving audio through the API means every byte crosses the network
        twice — storage to server, server to browser — and the player cannot
        start until the last byte of that arrives. A signed link lets the
        browser fetch from storage itself: playback begins on the first chunk,
        seeking works through range requests, and the server carries none of it.

        The bucket stays private. The link is signed, time-limited, and minted
        only for someone who already asked this API for this call.
        """
        path = row.get("recording_path")
        if not self.remote or not path or not path.startswith(_REMOTE_PREFIX):
            return None
        key = path[len(_REMOTE_PREFIX) :]
        try:
            response = await self._http().post(
                f"/object/sign/{settings.supabase_bucket}/{key}",
                json={"expiresIn": seconds},
            )
        except httpx.HTTPError as exc:
            log.warning("could not sign %s: %s", key, exc)
            return None
        if response.status_code >= 400:
            return None
        signed = (response.json() or {}).get("signedURL")
        return f"{settings.supabase_url.rstrip('/')}/storage/v1{signed}" if signed else None

    async def get(self, row: dict[str, Any]) -> bytes | None:
        """The stored audio, or None if this backend cannot see it."""
        path = row.get("recording_path")
        if not path:
            return None
        if path.startswith(_REMOTE_PREFIX):
            if not self.remote:
                return None
            key = path[len(_REMOTE_PREFIX) :]
            try:
                response = await self._http().get(f"/object/{settings.supabase_bucket}/{key}")
            except httpx.HTTPError as exc:
                log.warning("recording fetch for %s failed: %s", row.get("call_id"), exc)
                return None
            return response.content if response.status_code < 400 and response.content else None

        file = Path(path) if Path(path).is_absolute() else BASE_DIR / path
        if file.exists() and file.stat().st_size > 0:
            return file.read_bytes()
        return None

    async def delete(self, row: dict[str, Any]) -> None:
        """Erase the audio. Best-effort: a deleted call must still delete."""
        path = row.get("recording_path")
        if not path:
            return
        if path.startswith(_REMOTE_PREFIX):
            if self.remote:
                key = path[len(_REMOTE_PREFIX) :]
                with contextlib.suppress(httpx.HTTPError):
                    await self._http().delete(f"/object/{settings.supabase_bucket}/{key}")
            return
        file = Path(path) if Path(path).is_absolute() else BASE_DIR / path
        with contextlib.suppress(OSError):
            file.unlink()


recordings = RecordingStorage()


# =============================================================================
# Our own call recorder — raw media in, MP3 out, provider not involved
# =============================================================================
#
# The provider's recordings could not be trusted: composed files came back
# shorter than the call (65s of a 4:18 call, 311s of a 6:20 one) and contained
# only one side of the conversation, while their own metadata claimed the full
# length. Nothing on our side could fix that, and a recording that is silently
# wrong is worse than one that is missing.
#
# So the carrier streams us the raw audio of each leg and we assemble the file
# ourselves. What we write is exactly what we received: no composition step, no
# metadata to disagree with, and a length we can check against the call.


class LiveRecorder:
    """Audio arriving from the carrier for calls that are in progress."""

    # Infobip streams linear PCM. The sample rate is confirmed from the first
    # frames of a real call and logged, because guessing it wrong is the
    # difference between a recording and a chipmunk.
    SAMPLE_RATE = 8000

    def __init__(self) -> None:
        self._audio: dict[str, bytearray] = {}
        # Ayesha's own voice, when it reaches us over the realtime socket rather
        # than only over SIP. Kept apart from the carrier's track because the
        # two arrive at different sample rates and are mixed at the end.
        self._agent: dict[str, bytearray] = {}
        # When capture began, so a turn that starts thirty seconds in is written
        # thirty seconds in rather than at the top of the file.
        self._started: dict[str, float] = {}
        # Calls whose media socket has closed, and calls already encoded. A
        # finished call must never be reopened by a late frame: it would leave a
        # buffer behind that nothing ever saves.
        self._stream_ended: dict[str, asyncio.Event] = {}
        self._finished: set[str] = set()
        self._described = False

    def open(self, call_id: str) -> None:
        self._audio.setdefault(call_id, bytearray())
        self._started.setdefault(call_id, time.monotonic())
        self._stream_ended.setdefault(call_id, asyncio.Event())

    def feed(self, call_id: str, chunk: bytes) -> None:
        if call_id in self._finished:
            return
        buffer = self._audio.get(call_id)
        if buffer is None:
            self.open(call_id)
            buffer = self._audio[call_id]
        buffer.extend(chunk)

    def feed_agent(self, call_id: str, chunk: bytes, rate: int) -> None:
        """Ayesha's audio, padded with silence to sit where she actually spoke.

        Her voice only exists while she is talking, so appending deltas
        end-to-end would compress a five-minute call into ninety seconds of
        her talking over herself. Each delta is placed at the wall-clock
        offset it arrived at, and the gaps are silence.
        """
        if call_id in self._finished or call_id not in self._started:
            return
        track = self._agent.setdefault(call_id, bytearray())
        elapsed = time.monotonic() - self._started[call_id]
        want = int(elapsed * rate) * 2          # 16-bit mono
        if want > len(track):
            track.extend(b"\x00" * (want - len(track)))
        track.extend(chunk)

    def describe_once(self, detail: str) -> None:
        """Log the shape of the first thing the carrier sends, once."""
        if not self._described:
            self._described = True
            log.info("media stream frame format: %s", detail)

    def seconds(self, call_id: str) -> float:
        return len(self._audio.get(call_id, b"")) / (self.SAMPLE_RATE * 2)

    def stream_ended(self, call_id: str) -> None:
        """The carrier closed the media socket: everything has arrived."""
        event = self._stream_ended.get(call_id)
        if event:
            event.set()

    async def wait_for_stream(self, call_id: str, timeout: float) -> bool:
        """Give the carrier's last frames time to land before encoding.

        The call-ended webhook beats the final audio frames — the socket is
        still draining when it arrives. Encoding immediately cost the tail of
        every recording, which is the part with the goodbye in it.
        """
        event = self._stream_ended.get(call_id)
        if event is None:
            return True
        if event.is_set():
            return True
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(event.wait(), timeout)
            return True
        log.info("media stream for %s still open after %.0fs — encoding anyway", call_id, timeout)
        return False

    async def finish(self, call_id: str, agent_rate: int = 0) -> bytes | None:
        """Wrap what we captured as MP3 and hand it back."""
        self._finished.add(call_id)
        raw = self._audio.pop(call_id, None)
        agent = self._agent.pop(call_id, None)
        self._started.pop(call_id, None)
        self._stream_ended.pop(call_id, None)
        if not raw or len(raw) < self.SAMPLE_RATE:  # under half a second
            # Her half alone is still a recording worth keeping.
            if not (agent and agent_rate):
                return None
            raw = bytearray()

        carrier_seconds = len(raw) / (self.SAMPLE_RATE * 2)
        if agent and agent_rate:
            # The observed byte rate is the ground truth for what OpenAI sent.
            # If it disagrees with the configured rate, the mix is pitched
            # wrong, and this line is how anyone finds out.
            log.info(
                "call %s: carrier %.0fs, agent track %.0fs at the configured "
                "%d Hz (%.0f KB)",
                call_id, carrier_seconds,
                len(agent) / (agent_rate * 2), agent_rate, len(agent) / 1024,
            )
            mp3 = await _mix_to_mp3(
                bytes(raw), self.SAMPLE_RATE, bytes(agent), agent_rate
            )
        else:
            mp3 = await _pcm_to_mp3(bytes(raw), self.SAMPLE_RATE)
        if mp3:
            log.info(
                "recorded call %s ourselves: %.0fs of audio -> %.0f KB mp3",
                call_id, carrier_seconds, len(mp3) / 1024,
            )
        return mp3

    def drop(self, call_id: str) -> None:
        self._audio.pop(call_id, None)
        self._agent.pop(call_id, None)
        self._started.pop(call_id, None)
        self._stream_ended.pop(call_id, None)


live_recorder = LiveRecorder()

# Roughly a minute of 8 kHz frames. A media socket that never names its call is
# broken, and holding its audio forever would be a leak, but a few seconds of
# slack costs nothing and saves the opening of the call.
_MAX_UNATTRIBUTED_FRAMES = 3000


async def _mix_to_mp3(
    caller: bytes, caller_rate: int, agent: bytes, agent_rate: int
) -> bytes | None:
    """Both halves of the conversation into one MP3.

    The carrier's stream carries the caller; Ayesha's voice comes back over the
    realtime socket at a different rate. ffmpeg resamples and mixes them.
    `dropout_transition=0` and `normalize=0` keep amix from ducking one side
    whenever the other falls silent — on a conversation, where exactly one
    person is talking at a time, normalising makes both halves pump.
    """
    if not agent:
        return await _pcm_to_mp3(caller, caller_rate)
    with tempfile.TemporaryDirectory() as tmp:
        agent_path = Path(tmp) / "agent.raw"
        agent_path.write_bytes(agent)
        try:
            process = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "s16le", "-ar", str(caller_rate), "-ac", "1", "-i", "pipe:0",
                "-f", "s16le", "-ar", str(agent_rate), "-ac", "1", "-i", str(agent_path),
                "-filter_complex",
                "[0:a][1:a]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0[out]",
                "-map", "[out]",
                "-b:a", "48k", "-write_xing", "1", "-f", "mp3", "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, OSError):
            log.warning("ffmpeg unavailable — cannot mix the captured audio")
            return await _pcm_to_mp3(caller, caller_rate)
        try:
            out, err = await asyncio.wait_for(
                process.communicate(caller), timeout=settings.transcode_timeout_seconds
            )
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            log.warning("mixing timed out — keeping the caller's side only")
            return await _pcm_to_mp3(caller, caller_rate)
        if process.returncode != 0 or not out:
            log.warning(
                "mixing failed (%s) — keeping the caller's side only",
                (err or b"").decode("utf-8", "replace").strip()[:200],
            )
            return await _pcm_to_mp3(caller, caller_rate)
        return out


async def _pcm_to_mp3(pcm: bytes, rate: int) -> bytes | None:
    """Raw signed 16-bit little-endian mono PCM to MP3."""
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "s16le", "-ar", str(rate), "-ac", "1",
            "-i", "pipe:0",
            "-b:a", "48k", "-write_xing", "1", "-f", "mp3", "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError):
        log.warning("ffmpeg unavailable — cannot encode the captured audio")
        return None
    try:
        out, err = await asyncio.wait_for(
            process.communicate(pcm), timeout=settings.transcode_timeout_seconds
        )
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        return None
    if process.returncode != 0 or not out:
        log.warning("encoding captured audio failed: %s", err[:200].decode("utf-8", "replace"))
        return None
    return out


def _is_complete(call_id: str, audio: bytes, file: dict[str, Any], expect_seconds: int) -> bool:
    """Does this download actually contain the whole call?

    The provider lists a composed recording before it has finished writing it,
    so fetching the moment it appears can return a file that is still growing.
    A real 4:18 call was stored as 1:05 that way — and because the recording
    then looked saved, nothing ever went back for the rest of it.

    Two independent checks, because either can be missing: the byte count the
    provider claims, and how long the call is known to have lasted.
    """
    size = file.get("size")
    if isinstance(size, int) and size > 0 and len(audio) < size * 0.98:
        log.info("recording for %s is still being written (%d of %d bytes)",
                 call_id, len(audio), size)
        return False
    length = file.get("duration")
    if isinstance(length, (int, float)) and expect_seconds and length < expect_seconds - 10:
        log.info("recording for %s covers %.0fs of a %ds call", call_id, length, expect_seconds)
        return False
    return True


async def _save_recording(
    call_id: str,
    file: dict[str, Any],
    expect_seconds: int = 0,
    allow_partial: bool = False,
) -> bytes | None:
    """Download a recording once and keep it.

    Infobip expires recordings, so a dashboard that only ever proxies to them
    eventually plays nothing. Copying the composed file into our own storage at
    the end of the call is what makes it replayable indefinitely.

    Returns the audio itself rather than a path, because storage may not be a
    filesystem — and the caller usually wants to send these exact bytes back.
    Note the file id is recorded even when the copy fails: with it, the provider
    remains a fallback source, which is the whole reason a 90-minute call too
    large for the bucket is still playable.
    """
    file_id = file.get("id")
    if not file_id:
        return None

    fmt = str(file.get("fileFormat") or file.get("format") or "WAV").upper()
    extension = fmt.lower() if fmt in _AUDIO_MIME else "wav"

    try:
        response = await telephony.stream_file(file_id)
    except Exception as exc:  # noqa: BLE001 - a failed save must not lose the call
        log.warning("could not fetch recording for %s: %s", call_id, exc)
        return None
    if response.status_code >= 400 or not response.content:
        log.warning("recording download for %s returned %s", call_id, response.status_code)
        return None

    mime = response.headers.get("content-type") or _AUDIO_MIME.get(fmt, "audio/wav")
    audio = response.content

    # A partial recording that nothing retries is worse than none: it marks the
    # call as recorded and stops anyone looking for the rest.
    if not _is_complete(call_id, audio, file, expect_seconds) and not allow_partial:
        return None

    # Provider recordings arrive as WAV: a seven-minute call is 6.3MB, and every
    # one of those bytes crosses the network before a player can start. The same
    # call as MP3 is under a megabyte and sounds identical down a phone line.
    if settings.transcode_recordings and "wav" in mime.lower():
        converted = await _to_mp3(audio)
        if converted:
            log.info(
                "recording for %s: %.1f MB wav -> %.1f MB mp3",
                call_id,
                len(audio) / 1_048_576,
                len(converted) / 1_048_576,
            )
            audio, mime, extension = converted, "audio/mpeg", "mp3"

    reference = await recordings.put(call_id, audio, mime, extension)
    fields: dict[str, Any] = {
        "recording_mime": mime,
        "recording_file_id": file_id,
        "recording_ready": 1,
    }
    if reference:
        fields["recording_path"] = reference
    store.upsert(call_id, **fields)
    log.info(
        "saved recording for call %s (%.0f KB%s)",
        call_id,
        len(response.content) / 1024,
        "" if reference else ", provider-hosted only",
    )
    return response.content


@api.post("/calls/{call_id}/recording-problem", status_code=202)
async def report_recording_problem(call_id: str, request: Request) -> dict[str, str]:
    """Let the browser say why it could not record, so the reason is visible.

    A failure that only reaches the operator's developer console is a failure
    nobody sees. The browser is the only place that knows whether the
    microphone was busy, blocked or absent, and each of those has a different
    fix — so it says so here, and the answer lands in the server log next to
    the call it belongs to.
    """
    payload = await request.json() if await request.body() else {}
    reason = str(payload.get("reason") or "unspecified")[:300]
    # Success is reported too, and that is the point: three outcomes have to be
    # distinguishable in the log. A start line then no upload is a failure at
    # the end of the call; a failure line names its own cause; and *neither*
    # line means the browser is not running this code at all — a stale bundle
    # or a deploy that never landed, which no amount of provider settings fixes.
    if payload.get("ok"):
        log.info("browser started recording call %s (%s)", call_id, reason)
        return {"status": "noted"}
    log.warning("browser could not record call %s: %s", call_id, reason)
    store.upsert(call_id, error=f"recording: {reason}"[:300])
    return {"status": "noted"}


@api.post("/calls/{call_id}/recording", status_code=201)
async def upload_recording(call_id: str, request: Request) -> dict[str, Any]:
    """Accept a recording the browser made itself.

    Talk-tab calls run under Infobip's built-in WEBRTC calls configuration,
    which is not ours and has recording switched off — every attempt to start
    one is refused, and no setting we can reach changes that. The browser
    already holds both halves of the conversation though: the microphone it is
    sending and the stream it is receiving. So it records the call itself and
    posts the result here, and those calls stop depending on the provider
    entirely.

    Deliberately narrow: browser calls only, and only while the call is recent.
    Nothing else has any business posting audio, and this endpoint is as
    unauthenticated as the rest of the API.
    """
    row = store.get(call_id)
    if not row:
        raise HTTPException(404, "Call not found")
    if row.get("mode") != "MANUAL_BROWSER":
        raise HTTPException(409, "This call is recorded by the provider")
    started = row.get("start_time") or 0
    if started and time.time() - started > settings.reconcile_window_seconds:
        raise HTTPException(409, "Too late to attach a recording to this call")

    mime = (request.headers.get("content-type") or "audio/webm").split(";")[0].strip()
    if not (mime.startswith("audio/") or mime.startswith("video/")):
        raise HTTPException(415, "That is not audio")

    audio = await request.body()
    if not audio:
        raise HTTPException(400, "Empty recording")
    if len(audio) > settings.max_upload_bytes:
        raise HTTPException(413, "That recording is too large")

    # Convert to MP3 so the recording can be scrubbed, skipped and rewound —
    # the webm MediaRecorder produces has no duration or seek index and plays
    # straight through or not at all. Falling back to the original is
    # deliberate: an unseekable recording still beats no recording.
    converted = await _to_mp3(audio)
    if converted:
        log.info(
            "converted browser recording for %s: %.0f KB webm -> %.0f KB mp3",
            call_id,
            len(audio) / 1024,
            len(converted) / 1024,
        )
        audio, mime = converted, "audio/mpeg"
    extension = _extension_for(mime)
    reference = await recordings.put(call_id, audio, mime, extension)
    if not reference:
        raise HTTPException(502, "Could not store the recording")

    store.upsert(
        call_id,
        recording_path=reference,
        recording_mime=mime,
        recording_ready=1,
        recording_started=3,  # 3 = recorded in the browser, not by the provider
        recording_absent=0,
    )
    log.info("browser uploaded its own recording for %s (%.0f KB)", call_id, len(audio) / 1024)
    bus.publish("recording.ready", callId=call_id)
    bus.publish("call.updated", call=_serialise(store.get(call_id) or {}))
    return {"status": "stored", "bytes": len(audio)}


@api.get("/calls/{call_id}/recording")
async def get_recording(
    call_id: str, download: bool = False, meta: bool = False, link: bool = False
) -> Any:
    """The call audio. `?meta=true` returns the file list instead.

    Audio is the default because this URL is what the dashboard puts in an
    <audio src>: returning JSON there gave a player that silently never played.
    The provider key never reaches the browser — audio is served from disk, or
    proxied through here on the first request.
    """
    row = store.get(call_id)
    if not row:
        raise HTTPException(404, "Call not found")

    mime = row.get("recording_mime") or "audio/wav"
    extension = _extension_for(mime)

    # A direct link to storage, when the caller can use one. This is what makes
    # a long recording start playing at once instead of after the whole file has
    # crossed the network twice.
    if link:
        url = await recordings.signed_url(row)
        if url:
            return {"url": url, "mime": mime, "filename": f"call-{call_id}.{extension}"}
        # No link to give: the audio is on local disk, or not stored yet. Say so
        # plainly so the player falls back to asking for the bytes instead.
        raise HTTPException(404, "No direct link for this recording")

    # Fast path: the call ended, we already stored it, nothing to ask Infobip
    # about — and it still works after the provider expires the file.
    stored = await recordings.get(row)
    if stored and not meta:
        return _audio_response(stored, mime, f"{call_id}.{extension}", download)

    # Stored once, then lost — the container was replaced, or this row was
    # written by an instance whose storage this one cannot see. The provider
    # still has the file and we still know its id, so re-fetch it rather than
    # reporting a recording the dashboard has already promised the user.
    if row.get("recording_file_id") and not stored and not meta:
        restored = await _save_recording(call_id, {"id": row["recording_file_id"]})
        if restored:
            log.info("restored recording for %s from provider storage", call_id)
            return _audio_response(
                restored,
                (store.get(call_id) or {}).get("recording_mime") or mime,
                f"{call_id}.{extension}",
                download,
            )

    # Already established that there is nothing to fetch. Every player on every
    # open dashboard would otherwise re-ask the provider for a file we know does
    # not exist — the log fills with 404s and each one is a round trip.
    if row.get("recording_absent") and not meta:
        raise HTTPException(404, "This call was not recorded")

    try:
        if row.get("dialog_id"):
            payload = await telephony.recordings_for_dialog(row["dialog_id"])
        else:
            # No dialog id — the bridge event was missed. The leg may still have
            # been recorded, so ask about the call itself before giving up.
            payload = await telephony.recordings_for_call(call_id)
    except TelephonyError as exc:
        if exc.status == 404:
            raise HTTPException(404, "Recording not ready yet") from exc
        raise HTTPException(502, "Could not reach the recording service") from exc

    # Prefer the merged file. If only single legs exist, ask Infobip to compose
    # them — otherwise the caller downloads a recording containing one voice and
    # silence where the other person spoke.
    composed, per_leg = _split_recording_files(payload)
    if not composed and per_leg and row.get("dialog_id"):
        with contextlib.suppress(TelephonyError):
            await telephony.compose_dialog_recording(row["dialog_id"])
        for _ in range(6):
            await asyncio.sleep(3)
            payload = await telephony.recordings_for_dialog(row["dialog_id"])
            composed, per_leg = _split_recording_files(payload)
            if composed:
                break

    files = composed or per_leg
    if not files:
        raise HTTPException(404, "Recording not ready yet")

    if meta:
        return {"call_id": call_id, "files": files, "composed": bool(composed)}

    # Save on the way past, so every later play is served from our own storage.
    saved = await _save_recording(call_id, files[0])
    if saved:
        fresh = store.get(call_id) or {}
        return _audio_response(
            saved,
            fresh.get("recording_mime") or mime,
            f"{call_id}.{extension}",
            download,
        )

    response = await telephony.stream_file(files[0]["id"])
    if response.status_code >= 400:
        raise HTTPException(502, "Could not download the recording")
    return _audio_response(
        response.content,
        response.headers.get("content-type", "audio/wav"),
        f"{call_id}.wav",
        download,
    )


def _audio_response(data: bytes, mime: str, filename: str, download: bool) -> Response:
    """`inline` so the browser's audio element plays it; `attachment` to save."""
    disposition = "attachment" if download else "inline"
    return Response(
        content=data,
        media_type=mime,
        headers={
            "Content-Disposition": f'{disposition}; filename="{filename}"',
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, max-age=3600",
        },
    )


# =============================================================================
# Post-call summary
# =============================================================================
#
# What a call actually contained is otherwise locked inside an audio file. The
# desk needs to read it: who rang, what they wanted, what they were told, and
# above all what they were NOT told and are still waiting on. That is what this
# produces, once, when the call ends — and it is what the Excel report is built
# from.
#
# The one rule that matters here is the same one the agent works under: nothing
# is invented. A field the caller never mentioned stays empty. An empty cell in
# the report is a fact ("they did not say"); a plausible guess in that cell is a
# lie the desk will act on.

# Held for the same reason as the recording watchers: asyncio keeps only weak
# references to bare tasks, so an unheld one can be collected mid-flight.
_summary_tasks: set[asyncio.Task] = set()

# Sent as a strict schema rather than "reply with JSON", because the parsing has
# to succeed on every call without a retry loop, and because `required` plus
# nullable types is what makes "I don't know" expressible. Given only a prose
# instruction, the model fills unknown fields with something reasonable instead
# of leaving them out.
_SUMMARY_JSON_SCHEMA: dict[str, Any] = {
    "name": "call_record",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "Six to ten plain English sentences: who called (if they "
                    "identified themselves), why, what they asked, what they "
                    "were told, what was left unanswered, and how the call "
                    "ended. Only what is in the transcript."
                ),
            },
            "main_query": {
                "type": "string",
                "description": "The one thing the call was mainly about, in a short phrase.",
            },
            "questions_asked": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Each distinct question the caller asked, in English.",
            },
            "info_provided": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Each distinct fact the agent gave them, in English.",
            },
            "student_name": {"type": ["string", "null"]},
            "whatsapp_number": {
                "type": ["string", "null"],
                "description": (
                    "Only a number the caller gave FOR WhatsApp follow-up. Not "
                    "the number they rang from unless they said to use it."
                ),
            },
            "student_email": {"type": ["string", "null"]},
            "programme": {
                "type": ["string", "null"],
                "description": "Programme or department of interest, as they said it.",
            },
            "admission_interest": {
                "type": ["string", "null"],
                "description": "Intake, campus or admission stage they are at, if said.",
            },
            "city": {"type": ["string", "null"]},
            "caller_type": {
                "type": ["string", "null"],
                "description": "candidate, student, alumni or other — only if clear.",
            },
            "reg_no": {"type": ["string", "null"], "description": "Registration / student ID."},
            "unanswered_query": {
                "type": ["string", "null"],
                "description": (
                    "What the agent could not answer and the caller is still "
                    "waiting on. Null if everything asked was answered."
                ),
            },
            "follow_up_required": {"type": "boolean"},
            "follow_up_reason": {"type": ["string", "null"]},
            "notes": {
                "type": ["string", "null"],
                "description": "Anything else the desk would want to know. Null if nothing.",
            },
        },
        "required": [
            "summary", "main_query", "questions_asked", "info_provided",
            "student_name", "whatsapp_number", "student_email", "programme",
            "admission_interest", "city", "caller_type", "reg_no",
            "unanswered_query", "follow_up_required", "follow_up_reason", "notes",
        ],
    },
}

_SUMMARY_SYSTEM = (
    "You read transcripts of calls to the University call "
    "centre and turn them into a record for the follow-up desk.\n"
    "The transcript is the ONLY source. Callers speak Urdu, English or a mix; "
    "write your output in English.\n"
    "Absolute rule: never infer, guess, complete or invent anything. If the "
    "transcript does not contain a field, return null for it. A wrong name or "
    "an invented number is worse than an empty one, because somebody will ring "
    "it. Do not fill a field from what is 'usually' true, and do not turn the "
    "agent's offer to find something out into an answer she gave.\n"
    "Transcripts are machine-made and will contain mis-hearings. Where a "
    "number or a name is not clearly stated, treat it as not stated."
)


def _transcript_text(rows: list[dict[str, Any]], limit: int = 400) -> str:
    """The conversation as lines of dialogue, newest kept if it is very long.

    The tail is what a summary needs most — outcomes, contact details and
    promises all land at the end of a call — so an over-long transcript is
    trimmed from the front rather than the back.
    """
    lines = []
    for row in rows:
        text = (row.get("text") or "").strip()
        if not text:
            continue
        who = "Caller" if (row.get("role") or "") == "caller" else "Agent"
        lines.append(f"{who}: {text}")
    return "\n".join(lines[-limit:])


def _blank_to_none(value: Any) -> Any:
    """Empty strings and the model's stand-ins for "nothing" all become NULL.

    Asked for null, models still occasionally answer "N/A", "unknown" or "none".
    Stored as-is, those read on the report as real answers.
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text.lower() in {
        "n/a", "na", "none", "null", "unknown", "not provided", "not given",
        "not mentioned", "not stated", "-",
    }:
        return None
    return text


def _summary_fields(data: dict[str, Any], call_queries: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge what the model read out of the transcript with what was registered.

    Registered queries win wherever the two disagree. They are not a reading of
    the call — they are what the agent deliberately wrote down during it, with
    the caller confirming it back — so on follow-up, which is the part the desk
    acts on, they are the record and the summary is the commentary.
    """
    fields: dict[str, Any] = {
        "summary": _blank_to_none(data.get("summary")),
        "main_query": _blank_to_none(data.get("main_query")),
        "questions_asked": "\n".join(
            str(q).strip() for q in (data.get("questions_asked") or []) if str(q).strip()
        ) or None,
        "info_provided": "\n".join(
            str(i).strip() for i in (data.get("info_provided") or []) if str(i).strip()
        ) or None,
        "student_name": _blank_to_none(data.get("student_name")),
        "whatsapp_number": _blank_to_none(data.get("whatsapp_number")),
        "student_email": _blank_to_none(data.get("student_email")),
        "programme": _blank_to_none(data.get("programme")),
        "admission_interest": _blank_to_none(data.get("admission_interest")),
        "city": _blank_to_none(data.get("city")),
        "caller_type": _blank_to_none(data.get("caller_type")),
        "reg_no": _blank_to_none(data.get("reg_no")),
        "unanswered_query": _blank_to_none(data.get("unanswered_query")),
        "follow_up_required": 1 if data.get("follow_up_required") else 0,
        "follow_up_reason": _blank_to_none(data.get("follow_up_reason")),
        "notes": _blank_to_none(data.get("notes")),
        "summary_status": "DONE",
    }

    if call_queries:
        # A registered query IS a follow-up, whatever the summary concluded.
        fields["follow_up_required"] = 1
        first = call_queries[0]
        fields["unanswered_query"] = (
            _blank_to_none(first.get("query_text")) or fields["unanswered_query"]
        )
        fields["follow_up_reason"] = fields["follow_up_reason"] or (
            "Registered as query "
            + ", ".join(str(q.get("token")) for q in call_queries if q.get("token"))
        )
        for column, key in (
            ("student_name", "name"),
            ("programme", "programme"),
            ("caller_type", "caller_type"),
            ("reg_no", "reg_no"),
        ):
            fields[column] = _blank_to_none(first.get(key)) or fields[column]
        # Only a caller who actually opted in has a WhatsApp number on record.
        if first.get("whatsapp_opt_in"):
            fields["whatsapp_number"] = (
                _blank_to_none(first.get("phone")) or fields["whatsapp_number"]
            )

    return fields


def _summary_model() -> str:
    return settings.summary_model.strip() or settings.llm_model


def _summary_tuning() -> dict[str, Any]:
    """Sampling arguments for whichever model is doing the summarising.

    Same family split as _llm_tuning, but its own budget: a summary is longer
    than a chat reply and would be cut off mid-sentence at the chat endpoint's
    cap. Temperature is pinned at zero — this is extraction, and creativity in
    it is indistinguishable from fabrication.
    """
    if _summary_model().startswith(("gpt-5", "o1", "o3", "o4")):
        return {"max_completion_tokens": 1600, "reasoning_effort": "low"}
    return {"temperature": 0, "max_tokens": 1600}


def _extract_summary(transcript: str) -> dict[str, Any]:
    """One blocking call to the model. Always run in a worker thread."""
    client = _text_llm_client()
    completion = client.chat.completions.create(
        model=_summary_model(),
        **_summary_tuning(),
        response_format={"type": "json_schema", "json_schema": _SUMMARY_JSON_SCHEMA},
        messages=[
            {"role": "system", "content": _SUMMARY_SYSTEM},
            {"role": "user", "content": f"Call transcript:\n\n{transcript}"},
        ],
    )
    return json.loads(completion.choices[0].message.content or "{}")


async def summarise_call(call_id: str, *, force: bool = False) -> dict[str, Any] | None:
    """Summarise one finished call and write the result onto its row.

    Returns the stored fields, or None if there was nothing to summarise.

    Safe to call more than once for the same call — which matters, because a
    call can be finished by a webhook and again by the reconcile loop, and both
    of them end here. The status column is claimed before the model is called,
    so the second caller finds the work already taken and returns.
    """
    row = await asyncio.to_thread(store.get, call_id)
    if not row:
        return None
    status = (row.get("summary_status") or "").upper()
    if not force and status in ("PENDING", "DONE"):
        return None
    if not settings.summary_enabled or not _text_llm_ready():
        await asyncio.to_thread(store.upsert, call_id, summary_status="SKIPPED")
        return None

    transcript_rows = await asyncio.to_thread(store.list_transcripts, call_id)
    transcript = _transcript_text(transcript_rows)
    if len(transcript) < 40:
        # A call with no words in it — unanswered, or one that dropped before
        # anybody spoke. There is nothing to summarise, and given silence the
        # model would invent a call to fill it.
        await asyncio.to_thread(store.upsert, call_id, summary_status="SKIPPED")
        return None

    # Claim it before the slow part, so a concurrent finish path does not start
    # a second identical request against the same call.
    await asyncio.to_thread(store.upsert, call_id, summary_status="PENDING")

    try:
        data = await asyncio.to_thread(_extract_summary, transcript)
    except Exception:  # noqa: BLE001 - a missing summary must not lose the call
        log.exception("could not summarise call %s", call_id)
        # FAILED rather than left PENDING: a stuck PENDING would never be
        # retried, whereas this can be asked for again by hand.
        await asyncio.to_thread(store.upsert, call_id, summary_status="FAILED")
        return None

    call_queries = await asyncio.to_thread(store.queries_for, call_id)
    fields = _summary_fields(data, call_queries)
    await asyncio.to_thread(store.upsert, call_id, **fields)
    log.info("summarised call %s (follow-up: %s)", call_id, bool(fields["follow_up_required"]))
    bus.publish("call.summarised", callId=call_id, summary=fields.get("summary"))
    bus.publish("call.updated", call=_serialise(await asyncio.to_thread(store.get, call_id) or {}))
    return fields


def schedule_call_summary(call_id: str, *, delay: float = 3.0) -> None:
    """Kick off a summary in the background once a call has finished.

    The delay is for the transcript, not the model: the last few lines of a call
    are still working their way through the deferred-write queue when the finish
    event arrives, and summarising a second too early loses the end of the
    conversation — which is the part carrying the contact details.
    """
    if not settings.summary_enabled or not call_id:
        return

    async def _run() -> None:
        await asyncio.sleep(delay)
        try:
            await summarise_call(call_id)
        except Exception:  # noqa: BLE001 - background work, nothing to raise to
            log.exception("summary task failed for %s", call_id)
        finally:
            # The call is over; its line counter is dead weight from here.
            _transcript_seq.pop(call_id, None)

    task = asyncio.create_task(_run())
    _summary_tasks.add(task)
    task.add_done_callback(_summary_tasks.discard)



# =============================================================================
# Master Excel report
# =============================================================================

# Report timestamps are written in Pakistan Standard Time. The server may run
# anywhere; the calls did not. Fixed rather than read from a tz database
# because PKT has had no DST since 2009, so an offset is the whole truth here
# and needs no extra dependency in the image.
REPORT_TZ = timezone(timedelta(hours=5), "PKT")

# An upper bound on how much history one download carries, so a request can
# never try to hold an unbounded call log in memory at once. Far above any
# realistic volume for this desk — a hundred calls a day reaches it in nearly
# three years — and it exists to make the failure mode "the oldest calls are
# not in this file" rather than the process being killed mid-download.
_REPORT_ROW_CAP = 100_000

#
# ONE workbook, every call ever taken, three sheets keyed on Call ID.
#
# It is built from the database on each download rather than being a file that
# gets appended to. That is the whole design, and it is what makes the two
# rules that matter impossible to break rather than merely unlikely:
#
#   * no call can be lost, because nothing is ever written over — the workbook
#     is derived, and the store it is derived from is only ever inserted into;
#   * two administrators downloading at the same moment cannot corrupt anything,
#     because they are reading, not writing.
#
# A kept-on-disk workbook would have had to survive concurrent appends, a host
# that recycles its filesystem, and the first partial write during a deploy —
# and any one of those loses call history permanently. Regenerating costs a
# second on a few thousand calls and cannot.

# Sheet 1: one row per call, the whole log.
_SHEET_CALLS = "Call Summary"
# Sheet 2: what was learned about the person on the other end.
_SHEET_STUDENTS = "Student Details"
# Sheet 3: what they asked and did not get an answer to.
_SHEET_FOLLOWUPS = "Follow-Up Queries"

_CALL_HEADERS = [
    "Call ID", "Date", "Time", "Direction", "Caller Number", "Student Name",
    "WhatsApp Number", "Email", "Duration (mm:ss)", "Duration (seconds)",
    "Main Query", "Call Summary", "Outcome", "Call Status", "Handled By",
    "Follow-Up Required", "Follow-Up Reason", "Unanswered Query",
    "Recording Reference", "Recording Available", "Caller Turns",
    "Agent Turns", "Interruptions",
]

_STUDENT_HEADERS = [
    "Call ID", "Date", "Student Name", "Phone Number", "WhatsApp Number",
    "Email", "Caller Type", "Programme / Department", "Registration No",
    "Admission Interest", "City", "Questions Asked", "Information Provided",
    "Follow-Up Required", "Additional Notes",
]

_FOLLOWUP_HEADERS = [
    "Call ID", "Reference Token", "Date", "Student Name", "WhatsApp Number",
    "Phone Number", "Category", "Question", "Information Requested",
    "Reason Information Was Unavailable", "Follow-Up Required",
    "Follow-Up Status", "WhatsApp Status", "Due By", "Resolved At", "Notes",
]


def _stamp(value: Any) -> tuple[str, str]:
    """A stored epoch as (date, time) in the university's own timezone.

    Split into two columns because that is how the desk filters: everything
    from Tuesday, not everything after a particular instant. Reported in PKT
    rather than UTC — the server may sit anywhere, but a call at 9am was 9am in
    Lahore, and a report that says 4am invites somebody to "correct" it.
    """
    if not value:
        return "", ""
    try:
        when = datetime.fromtimestamp(float(value), REPORT_TZ)
    except (TypeError, ValueError, OSError):
        return "", ""
    return when.strftime("%Y-%m-%d"), when.strftime("%H:%M:%S")


def _clock(seconds: Any) -> str:
    """Talk time as mm:ss, which is how a duration is read at a glance."""
    try:
        total = max(int(seconds or 0), 0)
    except (TypeError, ValueError):
        return ""
    return f"{total // 60:02d}:{total % 60:02d}"


def _yes_no(value: Any) -> str:
    return "Yes" if value else "No"


def _cell(value: Any) -> Any:
    """Anything the store holds, as something Excel will accept.

    Long free text is capped: Excel refuses a cell over 32,767 characters and
    fails the whole workbook rather than that one cell, so a runaway transcript
    in a notes field would cost the administrator every call in the file.
    """
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return value
    text = str(value)
    return text if len(text) <= 32_000 else text[:32_000] + "…"


def _handled_by(row: dict[str, Any]) -> str:
    mode = (row.get("mode") or "").upper()
    if mode.startswith("MANUAL"):
        return "Counselor (manual)"
    return "Ayesha (AI agent)"


def _call_report_rows(
    calls: list[dict[str, Any]], queries_by_call: dict[str, list[dict[str, Any]]]
) -> tuple[list[list[Any]], list[list[Any]], list[list[Any]]]:
    """Turn the stored rows into the three sheets' worth of cells.

    Kept apart from the workbook building so the same shaping can be tested
    without openpyxl, and so the sheets provably agree: every row of all three
    comes from this one pass over the same calls, keyed by the same Call ID.
    """
    call_rows: list[list[Any]] = []
    student_rows: list[list[Any]] = []
    followup_rows: list[list[Any]] = []

    for row in calls:
        call_id = row.get("call_id") or ""
        date, clock = _stamp(row.get("start_time"))
        phone = _normalise(row.get("phone_number") or "")
        recorded = bool(row.get("recording_path") or row.get("recording_file_id"))
        follow_up = bool(row.get("follow_up_required"))
        call_queries = queries_by_call.get(call_id, [])
        if call_queries:
            follow_up = True

        call_rows.append([
            _cell(call_id), date, clock,
            _cell(row.get("direction")), _cell(phone),
            _cell(row.get("student_name")), _cell(row.get("whatsapp_number")),
            _cell(row.get("student_email")),
            _clock(row.get("duration")), _cell(row.get("duration") or 0),
            _cell(row.get("main_query")), _cell(row.get("summary")),
            _cell(row.get("outcome")), _cell(row.get("status")),
            _handled_by(row),
            _yes_no(follow_up), _cell(row.get("follow_up_reason")),
            _cell(row.get("unanswered_query")),
            # The provider's file id where there is one, else our own stored
            # path. Either way it is what identifies the audio in Supabase, and
            # it is what somebody chasing a recording quotes.
            _cell(row.get("recording_file_id") or row.get("recording_path")),
            _yes_no(recorded),
            _cell(row.get("user_turns") or 0), _cell(row.get("agent_turns") or 0),
            _cell(row.get("interruptions") or 0),
        ])

        # A student row only where the call actually taught us something about
        # the person. Rows of nothing but a call id and empty cells are not a
        # record of a student — they make the sheet look full while telling the
        # desk nothing, and they are exactly the fake data this must not create.
        known = any(
            row.get(field)
            for field in (
                "student_name", "whatsapp_number", "student_email", "programme",
                "reg_no", "admission_interest", "city", "caller_type",
                "questions_asked",
            )
        )
        if known:
            student_rows.append([
                _cell(call_id), date, _cell(row.get("student_name")), _cell(phone),
                _cell(row.get("whatsapp_number")), _cell(row.get("student_email")),
                _cell(row.get("caller_type")), _cell(row.get("programme")),
                _cell(row.get("reg_no")), _cell(row.get("admission_interest")),
                _cell(row.get("city")), _cell(row.get("questions_asked")),
                _cell(row.get("info_provided")), _yes_no(follow_up),
                _cell(row.get("notes")),
            ])

        for query in call_queries:
            q_date, _ = _stamp(query.get("created_at"))
            due_date, due_time = _stamp(query.get("due_at"))
            done_date, done_time = _stamp(query.get("resolved_at"))
            status = (query.get("status") or "OPEN").upper()
            followup_rows.append([
                _cell(call_id), _cell(query.get("token")), q_date or date,
                _cell(query.get("name") or row.get("student_name")),
                # The number they agreed to be messaged on, and only that. A
                # caller who did not opt in has no WhatsApp number here, however
                # well we know their phone number.
                _cell(query.get("phone") if query.get("whatsapp_opt_in")
                      else row.get("whatsapp_number")),
                _cell(query.get("phone") or phone),
                _cell(query.get("category")), _cell(query.get("query_text")),
                _cell(query.get("query_text")),
                "Not available in the knowledge base — registered for the department",
                _yes_no(status != "RESOLVED"), status,
                _cell(query.get("whatsapp_status")),
                f"{due_date} {due_time}".strip(),
                f"{done_date} {done_time}".strip(),
                _cell(query.get("resolution")),
            ])

        # A call the agent could not answer but never registered — no token, so
        # it is in no query row, and without this it would be in no follow-up
        # sheet at all. This is the one the desk most needs to see.
        if row.get("unanswered_query") and not call_queries:
            followup_rows.append([
                _cell(call_id), "", date, _cell(row.get("student_name")),
                _cell(row.get("whatsapp_number")), _cell(phone), "",
                _cell(row.get("unanswered_query")), _cell(row.get("unanswered_query")),
                _cell(row.get("follow_up_reason")
                      or "Information was not available on the call"),
                _yes_no(follow_up), "OPEN — no reference issued", "",
                "", "", _cell(row.get("notes")),
            ])

    return call_rows, student_rows, followup_rows


def build_master_workbook() -> bytes:
    """The whole call history as one .xlsx, in memory.

    Blocking (openpyxl is synchronous and the store is too), so callers run it
    in a worker thread rather than on the event loop.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    # Oldest first: a log is read downwards, and appending the newest call to
    # the bottom is what makes row order mean chronology. The store lists newest
    # first because that is what a dashboard wants.
    calls = list(reversed(store.list(limit=_REPORT_ROW_CAP)))

    queries_by_call: dict[str, list[dict[str, Any]]] = {}
    orphan_queries: list[dict[str, Any]] = []
    for query in store.list_queries(limit=_REPORT_ROW_CAP):
        call_id = query.get("call_id")
        if call_id:
            queries_by_call.setdefault(call_id, []).append(query)
        else:
            orphan_queries.append(query)

    call_rows, student_rows, followup_rows = _call_report_rows(calls, queries_by_call)

    # A query whose call row is gone — the call was deleted, or it was logged
    # before the call was. Still somebody waiting on an answer, so it belongs in
    # the sheet the desk works from, under whatever call id it remembers.
    known_ids = {row.get("call_id") for row in calls}
    for query in orphan_queries + [
        q for cid, qs in queries_by_call.items() if cid not in known_ids for q in qs
    ]:
        q_date, _ = _stamp(query.get("created_at"))
        due_date, due_time = _stamp(query.get("due_at"))
        status = (query.get("status") or "OPEN").upper()
        followup_rows.append([
            _cell(query.get("call_id")), _cell(query.get("token")), q_date,
            _cell(query.get("name")),
            _cell(query.get("phone") if query.get("whatsapp_opt_in") else None),
            _cell(query.get("phone")), _cell(query.get("category")),
            _cell(query.get("query_text")), _cell(query.get("query_text")),
            "Not available in the knowledge base — registered for the department",
            _yes_no(status != "RESOLVED"), status,
            _cell(query.get("whatsapp_status")),
            f"{due_date} {due_time}".strip(), "",
            _cell(query.get("resolution")),
        ])

    book = Workbook()
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", start_color="1F3864")
    header_align = Alignment(vertical="center", wrap_text=True)
    body_align = Alignment(vertical="top", wrap_text=True)

    sheets = (
        (_SHEET_CALLS, _CALL_HEADERS, call_rows),
        (_SHEET_STUDENTS, _STUDENT_HEADERS, student_rows),
        (_SHEET_FOLLOWUPS, _FOLLOWUP_HEADERS, followup_rows),
    )

    for index, (title, headers, rows) in enumerate(sheets):
        # Workbook() opens with one sheet already made; reusing it keeps the
        # book from carrying an empty "Sheet" alongside the three real ones.
        sheet = book.active if index == 0 else book.create_sheet()
        sheet.title = title
        sheet.append(headers)
        for row in rows:
            sheet.append(row)

        for cell in sheet[1]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_align
        # Headers stay put while the desk scrolls a few thousand calls, and each
        # column gets a filter — the two things that make a long log usable.
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = (
            f"A1:{get_column_letter(len(headers))}{max(sheet.max_row, 1)}"
        )

        # Width from the content, clamped: unbounded, one long summary makes a
        # column wider than the screen and pushes every other column off it.
        for column, name in enumerate(headers, start=1):
            longest = len(str(name))
            for row in rows:
                if column <= len(row):
                    longest = max(longest, len(str(row[column - 1] or "")))
            sheet.column_dimensions[get_column_letter(column)].width = min(
                max(longest + 2, 12), 60
            )
            for cell in sheet[get_column_letter(column)][1:]:
                cell.alignment = body_align

    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


# ---- Reports ----------------------------------------------------------------
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@api.get("/exports/calls.xlsx")
async def export_calls_xlsx() -> Response:
    """The master workbook: every call ever taken, in one file.

    This is what the dashboard's Download Excel button asks for. There is one
    workbook and it is this response — never a file per call, and never only the
    latest one. It is regenerated from the store on each request, so it is
    current as of the click and no previous call can have been overwritten to
    produce it.

    Built in a worker thread: openpyxl and the store are both synchronous, and
    a few thousand calls' worth of workbook takes long enough that doing it on
    the event loop would stall every live call for the duration.
    """
    try:
        workbook = await asyncio.to_thread(build_master_workbook)
    except ImportError as exc:
        # openpyxl missing means the image was built from an older
        # requirements.txt. Say so plainly rather than as a 500 — it is a
        # deployment step, not a fault the administrator can retry away.
        log.exception("openpyxl is not installed")
        raise HTTPException(
            503, "Excel export needs the openpyxl package; redeploy to install it."
        ) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("could not build the Excel report")
        raise HTTPException(500, "The Excel report could not be built") from exc

    return Response(
        content=workbook,
        media_type=XLSX_MIME,
        headers={
            "Content-Disposition": f'attachment; filename="{settings.excel_filename}"',
            # The workbook changes with every call taken, and a cached copy is
            # a download that silently omits today's.
            "Cache-Control": "no-store",
            # So the browser can read the filename off a cross-origin fetch —
            # without it the download saves as the URL path.
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
    )


@api.get("/calls/{call_id}/transcript")
async def get_transcript(call_id: str) -> dict[str, Any]:
    """The call as text, in the order it was spoken."""
    if not await asyncio.to_thread(store.get, call_id):
        raise HTTPException(404, "Call not found")
    rows = await asyncio.to_thread(store.list_transcripts, call_id)
    return {
        "call_id": call_id,
        "lines": [
            {
                "seq": r.get("seq"),
                "role": r.get("role"),
                "text": r.get("text"),
                "at": r.get("created_at"),
            }
            for r in rows
        ],
    }


@api.post("/calls/{call_id}/summary")
async def make_summary(call_id: str, force: bool = False) -> dict[str, Any]:
    """Summarise a call now, or re-summarise one.

    The summary normally happens by itself when the call ends. This is for the
    two cases where it did not: a call that finished while the model was
    unreachable, and a call recorded before summarising existed.
    """
    row = await asyncio.to_thread(store.get, call_id)
    if not row:
        raise HTTPException(404, "Call not found")
    if row.get("status") in LIVE_STATUSES:
        raise HTTPException(409, "That call is still in progress")
    fields = await summarise_call(call_id, force=force)
    if fields is None:
        current = await asyncio.to_thread(store.get, call_id) or {}
        status = (current.get("summary_status") or "").upper()
        if status == "DONE":
            return {"status": "already_done", "call": _serialise(current)}
        if status == "SKIPPED":
            # Nothing was said on the call. Not an error, and inventing a
            # summary for it is precisely what must not happen.
            return {"status": "nothing_to_summarise", "call": _serialise(current)}
        raise HTTPException(502, "The summary could not be generated")
    return {"status": "ok", "call": _serialise(await asyncio.to_thread(store.get, call_id) or {})}



# ---- Webhooks ---------------------------------------------------------------
webhooks = APIRouter(prefix="/webhooks")

# Places Infobip has been observed to put the call id, in preference order.
_ID_PATHS = (
    ("call", "id"),
    ("callId",),
    ("id",),
    ("dialog", "parentCall", "id"),
    ("dialog", "childCall", "id"),
)


def _extract_call_id(body: Any) -> str:
    """Find the call id wherever this event type happens to put it.

    Event payloads are not uniformly shaped — CALL_FINISHED in particular does
    not always carry `call.id` — and a missed id means the call is never closed
    and the dashboard shows it as active forever.
    """
    if not isinstance(body, dict):
        return ""
    for path in _ID_PATHS:
        node: Any = body
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, str) and node:
            return node
    return ""


def _shape(body: Any, depth: int = 2) -> str:
    """Compact key outline of a payload, for diagnosing unknown event shapes."""
    if not isinstance(body, dict) or depth <= 0:
        return type(body).__name__
    return "{" + ", ".join(f"{k}:{_shape(v, depth - 1)}" for k, v in list(body.items())[:8]) + "}"


@webhooks.post("/infobip")
async def infobip_events(request: Request) -> dict[str, str]:
    payload = await request.json()
    event = payload.get("type") or payload.get("event") or ""
    body = payload.get("properties", payload)
    call = body.get("call", {}) if isinstance(body, dict) else {}
    call_id = _extract_call_id(body)

    if not call_id and event:
        # Losing the id means the call never closes and shows as active forever,
        # so make the shape visible rather than silently dropping the event.
        log.warning("no call id in %s payload; keys=%s", event, _shape(body))

    log.info("infobip %s (%s)", event, call_id or "-")

    if event == "CALL_RECEIVED":
        await _on_inbound(call_id, call)
    elif event == "CALL_RINGING":
        # Outbound calls were stored as DIALING and stayed there for the whole
        # ring, because nothing ever moved them on: RINGING was a status the
        # dashboard knew how to show and the backend never wrote. Guarded on
        # DIALING so a late or duplicate event cannot pull an answered call
        # back to ringing.
        if (store.get(call_id) or {}).get("status") == "DIALING":
            store.upsert(call_id, status="RINGING")
            bus.publish("call.updated", call=_serialise(store.get(call_id) or {}))
    elif event == "CALL_ESTABLISHED":
        await _on_established(call_id, call)
    elif event in ("CALL_FINISHED", "CALL_FAILED"):
        _on_finished(call_id, event, body)
    elif event == "DIALOG_ESTABLISHED":
        dialog = body.get("dialog", {})
        parent = (dialog.get("parentCall") or {}).get("id")
        if parent:
            row = store.get(parent) or {}
            bridged: dict[str, Any] = {"dialog_id": dialog.get("id")}
            # Whether this event means "answered" depends on which leg was
            # ringing, and getting it wrong is what the dashboard shows.
            #
            #  * INBOUND: Infobip auto-answers the caller when this leg
            #    connects, so there is no CALL_ESTABLISHED and this IS the
            #    pickup — the moment recording starts and the clock starts.
            #  * OUTBOUND with pre-dial: the agent leg comes up while the
            #    student's phone is still ringing. Nobody has answered
            #    anything. Recording the dialog id is all this event may do:
            #    calling it BRIDGED would show a ringing call as picked up,
            #    start the timer on the ringtone, and — through the signal
            #    below — let Ayesha greet a phone nobody has lifted.
            #    CALL_ESTABLISHED is the pickup for these; see _on_established.
            #  * OUTBOUND without pre-dial: answer_time is already stamped by
            #    the time the dialog exists, so this takes the answered path
            #    exactly as it always did.
            predialled = (
                settings.predial_agent_leg
                and row.get("direction") == "OUTBOUND"
                and not row.get("answer_time")
                # Pre-dial only ever runs on the plain agent flow. The manual
                # modes reach this event by their own route, where the dialog
                # really does mean both parties are connected.
                and row.get("mode") not in ("MANUAL_PHONE", "MANUAL_BROWSER")
            )
            if not predialled:
                bridged["status"] = "BRIDGED"
                if not row.get("answer_time"):
                    bridged["answer_time"] = time.time()
                _signal_answered(parent)
                # Inbound legs are auto-answered by the carrier, so they never
                # raise CALL_ESTABLISHED and never reached the one place that
                # starts our own capture — every inbound call went unrecorded.
                # This event is their pickup, so it is where recording begins.
                if row.get("direction") == "INBOUND":
                    asyncio.create_task(start_our_recording(parent))
            store.upsert(parent, **bridged)
            bus.publish("call.updated", call=_serialise(store.get(parent) or {}))
    elif event in ("RECORDING_FINISHED", "DIALOG_RECORDING_FINISHED", "DIALOG_FINISHED"):
        dialog_id = (body.get("dialog") or {}).get("id") or body.get("dialogId")
        for row in store.list(limit=50):
            if row.get("dialog_id") != dialog_id:
                continue
            # The event only says the file exists provider-side. Fetching it is
            # what actually makes it playable, so start the saver too.
            if not row.get("recording_path"):
                _watch_recording(row["call_id"], dialog_id)
            store.upsert(row["call_id"], recording_ready=1)
            bus.publish("recording.ready", callId=row["call_id"])
            break

    return {"status": "ok"}


def _normalise(number: str) -> str:
    """Store numbers in one shape so the log does not mix +92…, 92… and 0092…."""
    digits = re.sub(r"\D", "", number or "")
    if not digits:
        return "unknown"
    digits = digits.lstrip("0")  # 0092… and local 03xx… both lose leading zeros
    return f"+{digits}"


def _is_browser_leg(call: dict[str, Any]) -> bool:
    """Did this call originate from the browser softphone?

    The softphone dials Infobip directly and bridges itself to the student; the
    server's only part is handing out a token. Its legs still arrive here as
    webhooks, and answering one by bridging would put Ayesha on a call whose
    entire purpose is that no AI is listening — a third party on a two-party
    line, re-injecting audio the counselor hears as an echo of themselves.

    Two markers, either one is enough: a WEBRTC endpoint type, or the identity
    the token endpoint minted (see the `ucp-console-` prefix in `rtc_token`).
    """
    endpoint = call.get("endpoint") or {}
    if isinstance(endpoint, dict) and str(endpoint.get("type", "")).upper() == "WEBRTC":
        return True
    for field in ("from", "to"):
        value = call.get(field)
        if isinstance(value, str) and value.startswith("ucp-console-"):
            return True
    return False


async def _on_inbound(call_id: str, call: dict[str, Any]) -> None:
    if _is_browser_leg(call):
        log.info("ignoring browser softphone leg %s — it bridges itself", call_id)
        return

    caller = _normalise(call.get("from") or "")
    store.upsert(
        call_id,
        session_id=str(uuid.uuid4()),
        phone_number=caller,
        direction="INBOUND",
        status="RINGING",
        start_time=time.time(),
    )
    bus.publish("call.created", call=_serialise(store.get(call_id) or {}))
    log.info("inbound call %s from %s", call_id, mask_number(caller))

    try:
        await telephony.bridge_to_agent(call_id, caller)
        store.upsert(call_id, status="CONNECTING_AGENT")
    except TelephonyError as exc:
        log.error("failed to bridge inbound %s: %s", call_id, exc)
        store.upsert(call_id, status="FAILED", error=str(exc)[:300])
    bus.publish("call.updated", call=_serialise(store.get(call_id) or {}))


# Set the instant a leg is answered, so the greeting starts on a signal rather
# than on the next turn of a polling loop. The poll below is kept as a backstop
# for the case where the webhook that would fire this never arrives.
_answer_signals: dict[str, asyncio.Event] = {}


def _answer_signal(call_id: str) -> asyncio.Event:
    signal = _answer_signals.get(call_id)
    if signal is None:
        signal = asyncio.Event()
        _answer_signals[call_id] = signal
    return signal


def _signal_answered(call_id: str | None) -> None:
    """Release any greeting waiting on this call. Safe to call more than once."""
    if call_id:
        _answer_signal(call_id).set()


async def _wait_until_answered(our_call_id: str | None) -> None:
    """Hold the greeting until there is a person on the other end.

    Only does anything when pre-dial is on, because only then can the realtime
    session exist before the phone is answered. `answer_time` is the single
    stamp both pickup paths set — CALL_ESTABLISHED for outbound, and
    DIALOG_ESTABLISHED for the inbound legs Infobip auto-answers — so waiting on
    it works whichever way the call arrived.

    Times out rather than waiting forever: a greeting that is a little early is
    recoverable, a call where she never speaks at all is not.
    """
    if not settings.predial_agent_leg or not our_call_id:
        return

    signal = _answer_signal(our_call_id)
    deadline = time.monotonic() + settings.call_connect_timeout_seconds
    while time.monotonic() < deadline:
        # Checked first and costing nothing: when the pickup webhook has already
        # landed this returns without touching the database at all.
        if signal.is_set():
            return
        row = await asyncio.to_thread(store.get, our_call_id)
        if not row:
            return
        if row.get("answer_time"):
            return
        if row.get("status") in ("FAILED", "COMPLETED", "NO_ANSWER", "BUSY"):
            # Nobody is coming. Let the session end on its own rather than
            # greeting a call that is already over.
            return
        # A second between database reads rather than a quarter, because the
        # signal is what actually ends this wait now - the query is only there
        # in case the webhook is lost. Waiting on the signal instead of sleeping
        # is what keeps pickup-to-greeting instant.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(signal.wait(), timeout=1.0)
            return
    log.warning("greeting released on timeout for %s — pickup never seen", our_call_id)


async def _settled(task: asyncio.Task) -> None:
    """Wait for a bookkeeping write, without letting it break the call.

    The write is ordering-critical but not call-critical: a later status must
    never overtake it, and a database that refused it must never drop the leg.
    Awaiting it more than once is fine, which is what lets every branch below
    call this before its own write.
    """
    with contextlib.suppress(Exception):
        await task


async def _on_established(call_id: str, call: dict[str, Any]) -> None:
    # Settle any pre-dial still in flight before reading the row. Its whole
    # purpose is to bring the agent leg up during the ring, so on a call that
    # was answered quickly it is very often still mid-request right now — and
    # every decision below reads `dialog_id`, which only that request can
    # supply this early. Reading first and asking later is what produced the
    # duplicate bridge.
    predial = _predial_tasks.pop(call_id, None)
    if predial is not None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(predial), timeout=8)

    row = store.get(call_id)
    if not row:
        return
    # Pickup. This is where the conversation, the dashboard timer and the
    # recording all begin; everything before it was ringing.
    #
    # The browser softphone bridges itself, so none of the dialling below
    # applies to it. Read here rather than at the bridge, because the status
    # this event writes depends on it too.
    browser = row.get("mode") == "MANUAL_BROWSER"

    answered: dict[str, Any] = {"status": "ANSWERED"}
    if not row.get("answer_time"):
        answered["answer_time"] = time.time()
    if row.get("status") == "BRIDGED" or (row.get("dialog_id") and not browser):
        # Pre-dial brought the agent leg up during the ring, so by the time
        # anyone picks up the two legs are already joined: this instant is when
        # they are actually talking. Writing ANSWERED here would move the call
        # backwards in the dashboard and then never advance, because the bridge
        # that would have set BRIDGED was skipped as already done. The same
        # guard covers the browser softphone, whose two events can arrive in
        # either order.
        answered["status"] = "BRIDGED"
    # Before the database write, not after: a greeting held for pre-dial should
    # be released by the pickup itself, not by a round trip to hosted Postgres.
    _signal_answered(call_id)

    # The AI leg is dialled below, and everything between this instant and that
    # dial is silence the caller is holding the phone through. Two blocking
    # database writes used to sit in front of it; they are bookkeeping nobody is
    # waiting on, so they run alongside the dial instead of ahead of it. The
    # answer_time above is already stamped, so nothing is lost by writing late.
    # Every later write must land after this one — a status set here arriving
    # on top of CONNECTING_AGENT would put the call back to ANSWERED — so the
    # task is awaited before any of them, never merely fired and forgotten.
    answered_write = asyncio.create_task(
        asyncio.to_thread(store.upsert, call_id, **answered)
    )

    # Start capturing our own copy the moment there is a conversation to
    # capture. Pickup, not dial: nobody needs a recording of a ringtone.
    asyncio.create_task(start_our_recording(call_id))

    # The connection to OpenAI is warmed when an outbound call is placed, but an
    # inbound one never rang from here — so warm it now, while the SIP leg is
    # being set up, rather than paying for the handshake after it connects.
    asyncio.create_task(warm_openai_connection())

    # Outbound: someone just picked up, so now add the second leg. Guarded by
    # dialog_id so a duplicate webhook cannot bridge twice, and by mode: the
    # browser softphone already bridged itself to the student, so a leg added
    # here would be a third party on a two-party call — audio arriving twice,
    # which the counselor hears as themselves repeating back.
    if browser:
        # Already bridged by the browser. The status decided above is the
        # whole of this update.
        pass
    elif row.get("direction") == "OUTBOUND" and not row.get("dialog_id"):
        # A manual call dials the counselor first and bridges the student to
        # them. Routing it through bridge_to_agent instead would put Ayesha on
        # the line, which is the one thing this mode exists to avoid.
        manual = row.get("mode") == "MANUAL_PHONE"
        student = row.get("phone_number") or ""
        if manual and not student:
            log.error("manual call %s has no number to bridge to", call_id)
            await _settled(answered_write)
            store.upsert(call_id, status="FAILED", error="No student number recorded")
        else:
            try:
                if manual:
                    await telephony.bridge_to_phone(call_id, student)
                else:
                    await telephony.bridge_to_agent(call_id, settings.infobip_phone_number)
                await _settled(answered_write)
                store.upsert(call_id, status="CONNECTING_AGENT")
            except TelephonyError as exc:
                await _settled(answered_write)
                if "already in" in exc.body and "dialog" in exc.body:
                    # Infobip is telling us the leg we were about to add is
                    # already attached — pre-dial won the race. That is the
                    # success case, not a failure, and marking it FAILED made a
                    # live call vanish from the dashboard mid-conversation.
                    log.info("outbound %s already bridged by pre-dial", call_id)
                    store.upsert(call_id, status="BRIDGED")
                else:
                    log.error("failed to bridge outbound %s: %s", call_id, exc)
                    store.upsert(call_id, status="FAILED", error=str(exc)[:300])
    await _settled(answered_write)
    bus.publish("call.updated", call=_serialise(store.get(call_id) or {}))


async def _await_recording(call_id: str, dialog_id: str) -> None:
    """Poll Infobip until the recording is processed, then flag it.

    Deliberately not driven by RECORDING_FINISHED: that event has to be selected
    when creating the Infobip subscription, and if it is missing the flag never
    flips and the download button never appears. Polling works regardless of how
    the subscription was configured.
    """
    requested_compose = False
    last_per_leg: list[dict[str, Any]] = []

    attempts = (5, 10, 15, 20, 30, 30, 60, 60)
    expect = int((store.get(call_id) or {}).get("duration") or 0)
    for index, delay in enumerate(attempts):
        await asyncio.sleep(delay)
        # Only the last pass accepts a short file; before that a short file is
        # assumed to be one the provider has not finished writing.
        final = index == len(attempts) - 1
        try:
            payload = await telephony.recordings_for_dialog(dialog_id)
        except TelephonyError as exc:
            if exc.status != 404:
                log.warning("recording lookup failed for %s: %s", call_id, exc)
            continue

        composed, per_leg = _split_recording_files(payload)
        last_per_leg = per_leg or last_per_leg

        # Ask for the merged file as soon as the legs exist. Without this the
        # only downloads available are single legs — one voice and silence
        # where the other person should be.
        if not composed and per_leg and not requested_compose:
            requested_compose = True
            with contextlib.suppress(TelephonyError):
                await telephony.compose_dialog_recording(dialog_id)
                log.info("requested composed recording for call %s", call_id)
            continue  # give the mixer a moment before checking again

        if composed and await _save_recording(call_id, composed[0], expect, final):
            bus.publish("recording.ready", callId=call_id)
            return

    # Composition never appeared. A single leg is a poor recording, but it is
    # far better than none — keep it and say so.
    if last_per_leg and await _save_recording(call_id, last_per_leg[0], expect, True):
        log.warning("call %s saved as a single leg — composition never completed", call_id)
        bus.publish("recording.ready", callId=call_id)
        return

    log.info("no recording appeared for call %s", call_id)


def _watch_recording(call_id: str, dialog_id: str) -> None:
    """Start (at most one) background poller that saves this call's audio."""
    if any(t.get_name() == f"rec:{call_id}" for t in _recording_watchers):
        return
    watcher = asyncio.create_task(
        _await_recording(call_id, dialog_id), name=f"rec:{call_id}"
    )
    _recording_watchers.add(watcher)
    watcher.add_done_callback(_recording_watchers.discard)


# ---- Making sure there is something to save ---------------------------------
# Calls the server dials carry their recording settings in the dialog request,
# so they are recorded from the first second. A browser call is not ours to
# configure: the softphone SDK creates the dialog, and whether its recording
# request is honoured depends on the WebRTC application's settings in the
# Infobip portal. When it is not honoured nothing says so — the call simply
# finishes with no audio, which is how a 141-second conversation was lost.
# Asking the Calls API to start recording removes that dependency.


def _dialog_id_of(dialog: dict[str, Any]) -> str | None:
    """Live dialogs and historical ones name the same field differently."""
    return dialog.get("dialogId") or dialog.get("id")


def _parent_id_of(dialog: dict[str, Any]) -> str | None:
    return dialog.get("parentCallId") or (dialog.get("parentCall") or {}).get("id")


async def _ensure_recording(call_id: str) -> None:
    """Switch recording on for a call dialled from the browser.

    The dialog does not exist at the moment the browser registers the call — it
    appears once the student's leg is added — so this polls rather than trying
    once and giving up.

    Two things are done rather than one, in this order, because a call can end
    before the better one becomes possible. A real 21-second call was lost
    exactly that way: the leg-level fallback only ran after the whole polling
    window, by which point the line was down and there was nothing to record.

      1. Record the near leg immediately. One voice, but captured from the first
         second and available on any call however short.
      2. Upgrade to recording the whole dialog as soon as the dialog exists,
         which is what yields both voices composed into one file.
      3. Failing that, record the student's leg separately. Two single-leg
         recordings can still be composed afterwards, and the student's half is
         the half worth having.
    """
    near_leg = False
    student_leg = False

    for delay in (0, 1, 1, 1, 2, 2, 3, 3, 4, 5, 6, 8, 10):
        if delay:
            await asyncio.sleep(delay)
        row = store.get(call_id) or {}
        # A call that is over cannot start recording; the reconciler will save
        # whatever the provider did manage to capture.
        if row.get("status") not in LIVE_STATUSES:
            break
        if row.get("recording_started") == 2:  # already recording the dialog
            return

        # Best outcome first: the dialog, which records both voices.
        dialog = await _find_live_dialog(call_id)
        dialog_id = row.get("dialog_id") or (dialog or {}).get("id")
        if dialog_id:
            try:
                await telephony.start_dialog_recording(dialog_id)
                store.upsert(call_id, dialog_id=dialog_id, recording_started=2)
                log.info("recording both legs of browser call %s (dialog %s)", call_id, dialog_id)
                return
            except TelephonyError as exc:
                store.upsert(call_id, dialog_id=dialog_id)
                if exc.status in (400, 409):  # already recording — job done
                    store.upsert(call_id, recording_started=2)
                    return
                log.warning("dialog recording for %s refused: %s", call_id, exc)
                # The dialog will not record, but its legs are separate calls and
                # may. The student's is the half most worth having.
                child = (dialog or {}).get("child")
                if child and not student_leg:
                    student_leg = await _record_leg(child, "student")
                if student_leg:
                    store.upsert(call_id, recording_started=1)
                    break

        # Retried on every tick rather than only at the start: the browser
        # registers its call the instant it dials, and the provider does not
        # necessarily know about that call yet — the first attempts can 404 on a
        # call that exists moments later. A three-second window missed it.
        if not near_leg:
            near_leg = await _record_leg(call_id, "counselor")
            if near_leg:
                # 1 = a leg is recording, 2 = the whole dialog is. Keep looping
                # either way: a leg now, upgraded to the dialog when it appears.
                store.upsert(call_id, recording_started=1)

    if student_leg:
        log.warning("browser call %s recording each leg separately — dialog refused", call_id)
    elif near_leg:
        log.warning(
            "browser call %s is recording the counselor's leg only — no dialog to record", call_id
        )
    else:
        log.error("browser call %s is not being recorded at all", call_id)


async def _find_live_dialog(call_id: str) -> dict[str, str] | None:
    """The dialog this call belongs to, asked for three different ways.

    Returns its id and the *other* leg's id, since recording that leg separately
    is the last thing worth trying when the dialog itself will not record.

    Which of these three lookups works for a browser-dialled call is not
    documented and was not discoverable without a live call to try it on — the
    live dialog list came back empty for a WEBRTC-configuration dialog. So ask
    all three, take the first answer, and log which one produced it so a single
    real call settles the question for good.
    """

    def pair(dialog: dict[str, Any], source: str) -> dict[str, str] | None:
        dialog_id = _dialog_id_of(dialog)
        if not dialog_id:
            return None
        parent = _parent_id_of(dialog)
        child = dialog.get("childCallId") or (dialog.get("childCall") or {}).get("id")
        # Whichever leg is not the one we already have an id for.
        other = child if parent == call_id else parent
        log.info("found dialog for %s via %s", call_id, source)
        return {"id": dialog_id, "child": str(other) if other else ""}

    # The call resource itself, which may name its dialog.
    with contextlib.suppress(TelephonyError, AttributeError):
        call = await telephony.call_detail(call_id)
        if isinstance(call, dict) and call.get("dialogId"):
            log.info("found dialog for %s via call detail", call_id)
            return {"id": str(call["dialogId"]), "child": ""}

    # Dialogs currently in progress.
    with contextlib.suppress(TelephonyError):
        for dialog in await telephony.live_dialogs():
            # Either leg may be the one we know about: the browser is the parent
            # of its own dialog, but nothing guarantees that stays true.
            if call_id in (
                _parent_id_of(dialog),
                dialog.get("childCallId"),
                (dialog.get("childCall") or {}).get("id"),
            ):
                return pair(dialog, "live dialogs")

    # History, which is proven to answer for this exact filter — and lists a
    # dialog from the moment it is created, not only once it has ended.
    with contextlib.suppress(TelephonyError):
        dialog = await telephony.dialog_history_for_call(call_id)
        if dialog:
            return pair(dialog, "dialog history")
    return None


_media_config_id: str | None = None


async def media_stream_config_id() -> str | None:
    """The carrier-side config that points at our own media socket.

    Created once and remembered. Named, so a config left behind by an earlier
    deploy is reused rather than duplicated on every restart.
    """
    global _media_config_id
    if _media_config_id or not settings.record_from_media_stream:
        return _media_config_id
    base = settings.public_base_url.strip().rstrip("/")
    if not base:
        log.warning("PUBLIC_BASE_URL is not set — cannot receive our own call audio")
        return None
    url = base.replace("https://", "wss://").replace("http://", "ws://") + "/ws/media"
    try:
        for config in await telephony.media_stream_configs():
            if config.get("name") == settings.media_stream_config_name:
                # The deployed URL can change; keep the config pointing at us.
                if config.get("url") == url:
                    _media_config_id = config.get("id")
                    return _media_config_id
                # Same name, wrong URL — a config left by an earlier deploy,
                # pointing at a host that no longer answers. Creating a second
                # one under the same name would leave two, with no way to tell
                # which the carrier picks, so the stale one goes first.
                stale = config.get("id")
                if stale:
                    log.info("replacing stale media stream config %s (%s)",
                             stale, config.get("url"))
                    with contextlib.suppress(TelephonyError):
                        await telephony.delete_media_stream_config(stale)
                break
        created = await telephony.create_media_stream_config(
            settings.media_stream_config_name, url
        )
        _media_config_id = created.get("id")
        log.info("media stream config %s -> %s", _media_config_id, url)
    except TelephonyError as exc:
        log.warning("could not set up our own recording stream: %s", exc)
    return _media_config_id


async def start_our_recording(call_id: str) -> None:
    """Ask the carrier to stream this call's audio to us."""
    if not settings.record_calls:
        return
    want_stream = settings.record_from_media_stream
    want_agent = settings.record_agent_audio
    if not (want_stream or want_agent):
        return

    # Opened before either source starts. Ayesha's own audio arrives on the
    # realtime socket and waits for nothing the carrier does, but feed_agent
    # drops audio for a call the recorder was never opened for — and the only
    # open() sat behind the media-stream check. Turning the media stream off
    # therefore turned agent capture off too, silently, which is how a
    # deployment ends up with no recording at all from either source.
    live_recorder.open(call_id)

    if not want_stream:
        # No media socket will open for this call, so there are no trailing
        # frames to wait for at the end — say so now rather than spending the
        # tail timeout on a stream that was never started.
        live_recorder.stream_ended(call_id)
        store.upsert(call_id, recording_started=2)  # 2 = agent audio only
        log.info("recording call %s from the agent audio alone", call_id)
        return

    config = await media_stream_config_id()
    if not config:
        live_recorder.drop(call_id)
        return
    try:
        await telephony.start_media_stream(call_id, config)
        store.upsert(call_id, recording_started=4)  # 4 = we are capturing it
        log.info("recording call %s from the media stream", call_id)
    except TelephonyError as exc:
        live_recorder.drop(call_id)
        log.warning("media stream refused for %s: %s — falling back to provider", call_id, exc)


async def finish_our_recording(call_id: str) -> bool:
    """Encode and store what we captured. True if we saved a recording.

    Waits for the carrier's media socket to finish draining first. The
    call-ended webhook arrives while the last frames are still in flight, so
    encoding on the webhook alone reliably lost the end of the call.
    """
    drained = await live_recorder.wait_for_stream(
        call_id, settings.recording_tail_wait_seconds
    )
    # Every failure below used to be a bare `return False`, so a call that ended
    # with no recording said nothing about which of four different things went
    # wrong. Report the state we are encoding from, then name the step that
    # failed — a missing recording is hard enough to chase without guessing.
    carrier_bytes = len(live_recorder._audio.get(call_id, b""))
    agent_bytes = len(live_recorder._agent.get(call_id, b""))
    log.info(
        "finishing recording for %s: carrier %d B, agent %d B, stream drained=%s, "
        "record_agent_audio=%s, record_from_media_stream=%s",
        call_id, carrier_bytes, agent_bytes, drained,
        settings.record_agent_audio, settings.record_from_media_stream,
    )

    audio = await live_recorder.finish(
        call_id,
        settings.agent_audio_sample_rate if settings.record_agent_audio else 0,
    )
    if not audio:
        if not carrier_bytes and not agent_bytes:
            reason = (
                "nothing was captured — no media-stream frames arrived and no "
                "agent audio was fed (is RECORD_AGENT_AUDIO set on this deploy?)"
            )
        elif not settings.record_agent_audio and not carrier_bytes:
            reason = "only agent audio was available but RECORD_AGENT_AUDIO is off"
        else:
            reason = "ffmpeg produced no output from the captured audio"
        log.warning("no recording for %s: %s", call_id, reason)
        store.upsert(call_id, recording_absent=1)
        return False

    reference = await recordings.put(call_id, audio, "audio/mpeg", "mp3")
    if not reference:
        log.error(
            "encoded %.0f KB for %s but storing it failed — check Supabase "
            "credentials and bucket", len(audio) / 1024, call_id,
        )
        store.upsert(call_id, recording_absent=1)
        return False
    store.upsert(
        call_id,
        recording_path=reference,
        recording_mime="audio/mpeg",
        recording_ready=1,
        recording_absent=0,
    )
    bus.publish("recording.ready", callId=call_id)
    bus.publish("call.updated", call=_serialise(store.get(call_id) or {}))
    return True


async def _record_leg(call_id: str, whose: str) -> bool:
    """Record one leg of a call, by that leg's own call id.

    One attempt: the caller retries on its own schedule. The provider's answer
    is logged in full rather than swallowed — the difference between "that call
    does not exist yet" and "this account may not record that kind of leg" is
    the whole diagnosis, and without it there is nothing to go on but guesses.
    """
    if not call_id:
        return False
    try:
        await telephony.start_call_recording(call_id)
    except TelephonyError as exc:
        if exc.status in (400, 409):  # already recording
            return True
        log.info("cannot record %s leg yet (call %s): %s", whose, call_id, exc)
        return False
    log.info("recording %s leg (call %s)", whose, call_id)
    return True


# ---- Reconciliation ---------------------------------------------------------
# Every path above this line assumes webhooks arrive. They do not always, and a
# dropped one is not visible: a missed CALL_FINISHED leaves the dashboard timer
# climbing for as long as the reaper's cutoff allows, and a missed
# DIALOG_ESTABLISHED means no dialog id, so no recording is ever fetched. The
# provider will always answer the same questions on request, so ask it — the
# call log then converges on the truth whether or not a webhook ever lands.

_TERMINAL_DIALOG_STATES = ("FINISHED", "FAILED", "CANCELLED")


def _provider_end(row: dict[str, Any], dialog: dict[str, Any]) -> dict[str, Any]:
    """Closing fields for a call, taken from the provider's own record."""
    ended = _parse_provider_time(dialog.get("endTime"))
    # The dialog's duration is talk time — measured from the moment both legs
    # were bridged, which is exactly what the dashboard means by duration. Fall
    # back to our own clock only when the provider gives no figure.
    duration = dialog.get("duration")
    if not isinstance(duration, int):
        started = row.get("answer_time") or row.get("start_time") or ended
        duration = int((ended or time.time()) - started)
    failed = str(dialog.get("state") or "").upper() != "FINISHED"
    return {
        "status": "FAILED" if failed and not duration else "COMPLETED",
        "end_time": ended or time.time(),
        "duration": max(int(duration), 0),
        # Our own outcome wins when we have one: ENDED_BY_OPERATOR says who hung
        # up, where the provider only reports the mechanical NORMAL_HANGUP.
        "outcome": row.get("outcome") or (dialog.get("errorCode") or {}).get("name"),
    }


def _parse_provider_time(value: Any) -> float | None:
    """`2026-08-12T12:47:09.793+0000` -> epoch seconds."""
    if not isinstance(value, str) or not value:
        return None
    text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", value)
    with contextlib.suppress(ValueError):
        return datetime.fromisoformat(text).timestamp()
    return None


async def _reconcile_legless_call(row: dict[str, Any]) -> bool:
    """Close a call that never became a dialog, from the leg's own record."""
    if row.get("status") not in LIVE_STATUSES:
        return False
    call_id = row["call_id"]
    try:
        leg = await telephony.call_history_for_call(call_id)
    except TelephonyError as exc:
        if exc.status != 404:
            log.warning("leg lookup failed for %s: %s", call_id, exc)
        return False
    if not leg or str(leg.get("state") or "").upper() not in _TERMINAL_DIALOG_STATES:
        return False

    store.upsert(call_id, **_provider_end(row, leg))
    log.info("closed call %s from provider leg history", call_id)
    return True


async def _reconcile_call(row: dict[str, Any]) -> bool:
    """Bring one call in line with what the provider says happened to it.

    Returns whether anything changed, so the caller knows to tell the dashboard.
    """
    call_id = row["call_id"]
    try:
        dialog = await telephony.dialog_history_for_call(call_id)
    except TelephonyError as exc:
        if exc.status != 404:
            log.warning("reconcile lookup failed for %s: %s", call_id, exc)
        return False
    if not dialog:
        # No dialog was ever created — the call rang out, or the bridge failed.
        # The leg's own record still says how it ended, and closing from it is
        # what stops the dashboard timer on a call that never connected.
        return await _reconcile_legless_call(row)

    changed = False
    dialog_id = _dialog_id_of(dialog)
    if dialog_id and not row.get("dialog_id"):
        store.upsert(call_id, dialog_id=dialog_id)
        changed = True

    live = row.get("status") in LIVE_STATUSES
    finished = str(dialog.get("state") or "").upper() in _TERMINAL_DIALOG_STATES
    if live and finished:
        store.upsert(call_id, **_provider_end(row, dialog))
        log.info("closed call %s from provider history (no finish webhook)", call_id)
        # This is the path a call takes when its finish webhook never arrived,
        # so nothing else has summarised it. Idempotent, so the webhook winning
        # a race with this costs one skipped call rather than two summaries.
        schedule_call_summary(call_id)
        changed = True

    # Audio, if the provider has any and we have not already saved it. The
    # per-leg file is taken only once composition has had its chance, since a
    # composed file replaces two half-conversations with the whole one.
    # `recording_path` rather than a storage read: this runs on a loop, and
    # asking object storage for the bytes of every recent call just to learn
    # that one exists is a request per call per tick.
    if finished and settings.record_calls and not row.get("recording_path"):
        composed, per_leg = _split_recording_files(dialog.get("recording") or {})
        expect = int(row.get("duration") or 0)
        ended = _parse_provider_time(dialog.get("endTime")) or row.get("end_time") or 0
        settled = bool(ended) and time.time() - ended > settings.recording_grace_seconds
        if composed and await _save_recording(call_id, composed[0], expect, settled):
            bus.publish("recording.ready", callId=call_id)
            changed = True
        elif len(per_leg) == 1 and not dialog.get("establishTime"):
            # One leg on a dialog that never bridged — the other party never
            # arrived, so there is no second voice and never will be. Nothing to
            # compose; take what there is instead of polling for four minutes.
            if await _save_recording(call_id, per_leg[0], expect, settled):
                bus.publish("recording.ready", callId=call_id)
                changed = True
        elif per_leg and dialog_id:
            # Two legs and no mix yet: ask for one, and let the next pass save
            # it. _await_recording does the same dance on the happy path.
            _watch_recording(call_id, dialog_id)
        elif not composed and not per_leg and not row.get("recording_absent"):
            # The provider has finished with this call and reports no audio for
            # it whatsoever. Recordings can appear slightly after the dialog
            # ends, so wait out a grace period before saying so — but once said,
            # the dashboard stops offering a retry that cannot succeed.
            if settled:
                store.upsert(call_id, recording_absent=1)
                log.info("call %s has no recording and the provider has none coming", call_id)
                changed = True
    return changed


async def _reconcile_loop() -> None:
    """Reconcile anything unfinished, forever.

    Deliberately cheap: only calls that are still open, or that ended recently
    without audio, are ever looked up. A finished call with its recording on
    disk is never asked about again.
    """
    live = False
    while True:
        # Fast while a call is up, because that is when someone is watching a
        # timer and waiting for it to stop; slow the rest of the time, because
        # then it is only chasing recordings and nobody is watching at all.
        await asyncio.sleep(
            settings.live_poll_seconds if live else settings.reconcile_interval_seconds
        )
        try:
            cutoff = time.time() - settings.reconcile_window_seconds
            updated = False
            live = False
            for row in store.list(limit=100):
                pending_audio = (
                    settings.record_calls
                    and not row.get("recording_path")
                    and (row.get("end_time") or row.get("start_time") or 0) > cutoff
                )
                if row.get("status") not in LIVE_STATUSES and not pending_audio:
                    continue
                if await _reconcile_call(row):
                    updated = True
                    bus.publish(
                        "call.updated", call=_serialise(store.get(row["call_id"]) or {})
                    )
                # Re-read: the call may have just been closed by the line above.
                if (store.get(row["call_id"]) or {}).get("status") in LIVE_STATUSES:
                    live = True
            if updated:
                bus.publish("calls.reconciled")
        except Exception:  # noqa: BLE001 - housekeeping must never kill the app
            log.exception("provider reconciliation failed")


def _on_finished(call_id: str, event: str, body: dict[str, Any]) -> None:
    # A call that never reached CALL_ESTABLISHED leaves its pre-dial task here.
    _predial_tasks.pop(call_id, None)
    row = store.get(call_id)
    if not row:
        return
    ended = time.time()
    # Answered calls are measured from pickup; a call that never connected keeps
    # its ringing time so a 40-second unanswered call does not log as zero.
    duration = int(ended - (row.get("answer_time") or row.get("start_time") or ended))
    # The carrier's reason lives in errorCode and is the only way to tell a
    # non-existent number from a busy one — surface it rather than dropping it.
    error = body.get("errorCode") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        error = (body.get("call") or {}).get("errorCode") if isinstance(body, dict) else None
    outcome = (error or {}).get("name") or (error or {}).get("description")

    store.upsert(
        call_id,
        status="COMPLETED" if event == "CALL_FINISHED" else "FAILED",
        outcome=outcome,
        end_time=ended,
        duration=max(duration, 0),
    )
    # Dropped, not set: a greeting still waiting on this call must learn that
    # nobody answered from the status check in its poll, never be released to
    # speak into a line that has already ended. Popping also keeps the table
    # from growing for calls that never had a realtime session to clear it.
    _answer_signals.pop(call_id, None)
    task = _sessions.pop(row.get("openai_call_id") or "", None)
    if task:
        task.cancel()

    # Our own capture first: it is already in memory, it is the whole call, and
    # it does not depend on anyone else finishing a composition. The provider's
    # copy is only chased if we have nothing.
    async def _keep_audio() -> None:
        if await finish_our_recording(call_id):
            return
        if settings.record_calls and row.get("dialog_id") and not row.get("recording_path"):
            _watch_recording(call_id, row["dialog_id"])

    asyncio.create_task(_keep_audio())

    # Read the call into a record the desk can act on. Deliberately after the
    # audio is secured and independent of it: a summary is worth having even
    # when the recording is not, and neither should wait on the other.
    schedule_call_summary(call_id)

    bus.publish("call.updated", call=_serialise(store.get(call_id) or {}))


@webhooks.post("/openai")
async def openai_events(request: Request) -> Response:
    raw = await request.body()
    if not settings.openai_webhook_secret:
        log.error("OPENAI_WEBHOOK_SECRET not set — refusing unverified webhooks")
        raise HTTPException(503, "Webhook secret not configured")

    # Only the secret verifies the signature. The SDK refuses to build without
    # an api_key, which an Azure-only deployment does not have.
    client = OpenAI(
        api_key=settings.openai_api_key or "unused-for-webhook-verification",
        webhook_secret=settings.openai_webhook_secret,
    )
    try:
        event = client.webhooks.unwrap(raw, dict(request.headers))
    except InvalidWebhookSignatureError:
        log.warning("rejected OpenAI webhook: bad signature")
        raise HTTPException(400, "Invalid signature") from None
    except ValueError as exc:
        # Missing headers or unparseable body — a probe. Never a 500.
        log.warning("rejected malformed OpenAI webhook: %s", exc)
        raise HTTPException(400, "Malformed webhook") from None

    if event.type != "realtime.call.incoming":
        return Response(status_code=200)

    openai_call_id = event.data.call_id

    # Correlation is two database round trips and the accept is a network round
    # trip, and neither needs the other's result. Doing them in sequence put the
    # database on the critical path of the one moment latency is audible: the
    # caller has picked up and is waiting in silence for Ayesha to speak. With a
    # hosted Postgres that was most of the delay before she said anything.
    # The lookup also goes to a thread, because a blocking query on the event
    # loop stalls every other call in flight, not only this one.
    correlating = asyncio.create_task(
        asyncio.to_thread(
            _correlate, openai_call_id, getattr(event.data, "sip_headers", None)
        )
    )

    try:
        await accept_realtime_call(openai_call_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("failed to accept realtime call %s", openai_call_id)
        failed_call_id = None
        with contextlib.suppress(Exception):
            failed_call_id = await correlating
        bus.publish("agent.error", callId=failed_call_id, message=str(exc)[:200])
        return Response(status_code=200)

    # Correlation only labels the call for the dashboard, so it has almost
    # certainly finished during the accept above; awaiting it here costs nothing
    # in the normal case and never blocks the pickup.
    try:
        our_call_id = await correlating
    except Exception:  # noqa: BLE001
        # A failed lookup must not cost us the call — she can talk to a caller
        # the dashboard has not labelled yet.
        log.exception("failed to correlate realtime call %s", openai_call_id)
        our_call_id = None

    _sessions[openai_call_id] = asyncio.create_task(run_session(openai_call_id, our_call_id))
    bus.publish("agent.accepted", callId=our_call_id)
    return Response(status_code=200)


def _correlate(openai_call_id: str, sip_headers: Any) -> str | None:
    """Tie OpenAI's call id to our Infobip leg.

    OpenAI knows nothing about Infobip ids, so match on the caller number in the
    SIP From header and fall back to the newest call still in flight.
    """
    from_value = ""
    for header in sip_headers or []:
        if (getattr(header, "name", "") or "").lower() == "from":
            from_value = getattr(header, "value", "") or ""
            break
    digits = re.sub(r"\D", "", from_value)

    live = [
        r for r in store.list(limit=25)
        if r.get("status") in ("DIALING", "RINGING", "ANSWERED", "CONNECTING_AGENT", "BRIDGED")
        and not r.get("openai_call_id")
    ]
    match = next(
        (r for r in live if digits and re.sub(r"\D", "", r.get("phone_number") or "") in digits),
        None,
    ) or (live[0] if live else None)

    if match:
        store.upsert(match["call_id"], openai_call_id=openai_call_id)
        return match["call_id"]
    return None


# ---- Live feed --------------------------------------------------------------
@app.websocket("/ws/media")
async def media_socket(socket: WebSocket) -> None:
    """Raw call audio from the carrier, for calls we are recording ourselves.

    The call id arrives either in the query string or in a JSON frame before the
    audio starts, depending on how the provider is feeling; both are accepted.
    Audio frames are binary and appended as they come.

    Deliberately tolerant: this socket must never raise. A recording is worth a
    great deal, but not a dropped call — and the carrier is holding this
    connection open alongside a live conversation.
    """
    await socket.accept()
    call_id = socket.query_params.get("callId") or socket.query_params.get("call_id") or ""
    frames = 0
    # Audio that arrives before the carrier tells us which call it belongs to.
    # Discarding it — which is what this did — cut the opening of the recording
    # off, and lost the whole call whenever the id only ever came in a control
    # frame. Hold it instead and flush it the moment the id is known.
    unattributed: list[bytes] = []
    try:
        while True:
            message = await socket.receive()
            if message.get("type") == "websocket.disconnect":
                break

            payload = message.get("bytes")
            if payload:
                frames += 1
                if frames == 1:
                    live_recorder.describe_once(f"binary, first frame {len(payload)} bytes")
                if call_id:
                    live_recorder.feed(call_id, payload)
                elif len(unattributed) < _MAX_UNATTRIBUTED_FRAMES:
                    unattributed.append(payload)
                continue

            text = message.get("text")
            if not text:
                continue
            # A control frame. The one thing worth reading out of it is the id
            # of the call this stream belongs to.
            with contextlib.suppress(json.JSONDecodeError):
                event = json.loads(text)
                live_recorder.describe_once(f"json control frame: {_shape(event)}")
                found = (
                    event.get("callId")
                    or event.get("call_id")
                    or (event.get("call") or {}).get("id")
                )
                if found and not call_id:
                    call_id = str(found)
                    live_recorder.open(call_id)
                    for held in unattributed:
                        live_recorder.feed(call_id, held)
                    if unattributed:
                        log.info(
                            "media stream attached to call %s (%d frames held from "
                            "before the id arrived)", call_id, len(unattributed)
                        )
                    else:
                        log.info("media stream attached to call %s", call_id)
                    unattributed.clear()
    except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
        pass
    except Exception:  # noqa: BLE001 - never let this socket disturb a call
        log.exception("media socket for %s failed", call_id or "unknown")
    finally:
        if call_id:
            log.info(
                "media stream for %s closed after %d frames (%.0fs captured)",
                call_id,
                frames,
                live_recorder.seconds(call_id),
            )
            # Everything the carrier had has now arrived, so the encoder may
            # stop waiting. Without this the call-ended webhook always pays the
            # full grace period, or worse, encodes before the tail lands.
            live_recorder.stream_ended(call_id)
        elif frames:
            log.warning(
                "media stream closed with %d audio frames but no call id — "
                "recording lost", frames
            )


@app.websocket("/ws/events")
async def events_socket(socket: WebSocket) -> None:
    await socket.accept()
    queue = bus.subscribe()
    try:
        await socket.send_json({
            "kind": "snapshot",
            "calls": [_serialise(r) for r in store.list(limit=25)],
            "stats": store.stats(),
        })
        for event in bus.recent():
            await socket.send_json(event)
        while True:
            await socket.send_json(await queue.get())
    except (WebSocketDisconnect, asyncio.CancelledError, RuntimeError):
        pass
    finally:
        bus.unsubscribe(queue)


# =============================================================================
# Inbound WhatsApp — the student replies, Ayesha answers
# =============================================================================
#
# The templates above are outbound only, and their wording is fixed by Meta.
# This is the other direction: a student replies to one of those messages, or
# messages the business number cold, and gets a real answer back from the same
# knowledge base the voice agent uses.
#
# The reason this can be conversational at all is the customer service window.
# Meta permits free-form text for 24 hours after a user messages the business,
# so a REPLY costs us no template and no approval — we can answer anything, at
# any length, in whatever words the answer needs. (Our phone calls run over
# Infobip, which Meta cannot see, so a call never opens that window. Only a
# WhatsApp message does. That asymmetry is the whole design.)
#
# Nothing here touches the voice path.

# Free-form replies are only legal inside the window, so the window is tracked
# rather than assumed: the last time each number messaged us. A reply attempted
# outside it is refused locally instead of being accepted by the API and
# silently dropped, which is the failure mode that looks like "it worked".
_WA_WINDOW_SECONDS = 24 * 3600

# Bounded, because this is memory on a long-lived process. Both are keyed by
# the student's number and both are lost on restart, which is survivable: the
# worst case is one reply without conversational context.
_wa_seen: "collections.OrderedDict[str, float]" = collections.OrderedDict()
_wa_threads: "collections.OrderedDict[str, list[dict[str, str]]]" = collections.OrderedDict()
_wa_window: "collections.OrderedDict[str, float]" = collections.OrderedDict()

_WA_SEEN_MAX = 2000
_WA_THREADS_MAX = 500
# How much of the conversation to carry into the next answer. Six turns is
# enough for "aur uski fees?" to resolve against the previous question without
# feeding the model a transcript it will start quoting.
_WA_HISTORY_TURNS = 6

_wa_inbound_tasks: set[asyncio.Task] = set()


def _bounded(store: Any, limit: int) -> None:
    while len(store) > limit:
        store.popitem(last=False)


def _wa_signature_ok(raw: bytes, header: str | None) -> bool:
    """Verify Meta actually sent this.

    The endpoint is public and answers questions using an LLM, so without this
    anyone who finds the URL can spend our tokens and put words in Ayesha's
    mouth. Meta signs every delivery with the app secret.

    Compared in constant time. If no app secret is configured the request is
    refused outright rather than waved through — an unauthenticated webhook is
    not a degraded mode, it is an open door.
    """
    if not settings.meta_app_secret.strip():
        log.error("META_APP_SECRET is not set — refusing unverified webhook")
        return False
    if not header or not header.startswith("sha256="):
        return False
    digest = hmac.new(
        settings.meta_app_secret.strip().encode(), raw, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(digest, header.split("=", 1)[1])


async def send_whatsapp_text(to_number: str, text: str) -> str:
    """Send a plain, non-template WhatsApp message. Returns the message id.

    Legal only inside the 24-hour customer service window. Outside it Meta
    accepts this call and never delivers the message, so callers must check the
    window first — see _wa_in_window.
    """
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": _whatsapp_msisdn(to_number),
        "type": "text",
        # Link previews turn a mentioned URL into a card that buries the answer
        # under a thumbnail. The answers here are text.
        "text": {"preview_url": False, "body": text[:4000]},
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=10.0)) as client:
        response = await client.post(
            settings.whatsapp_api_url,
            headers={
                "Authorization": f"Bearer {settings.whatsapp_access_token.strip()}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
    if response.status_code >= 400:
        raise RuntimeError(f"WhatsApp {response.status_code}: {response.text[:300]}")
    messages = (response.json() or {}).get("messages") or []
    return (messages[0].get("id") if messages else "") or ""


def _wa_in_window(wa_id: str) -> bool:
    opened = _wa_window.get(wa_id)
    return bool(opened and time.time() - opened < _WA_WINDOW_SECONDS)


def _kb_answer(question: str, history: list[dict[str, str]] | None = None) -> str:
    """Answer one question from the knowledge base. Blocking; run in a thread.

    Shares the persona and the retrieval with the voice agent and the chat
    endpoint, so a student gets the same answer whichever way they ask. The
    refusal when nothing is retrieved is deliberate and is the same one the
    prompt gives on a call: no verified information means say so and hand over
    the helpline, never improvise.
    """
    context, hits = kb.context_for(question)
    if not hits:
        return (
            "Maazrat, is baare mein mere paas verified maloomat nahi hain. "
            "Aap hamari admissions helpline par rabta kar sakte "
            "hain, ya call par mujh se poochh sakte hain."
        )

    messages: list[dict[str, str]] = [
        {"role": "system", "content": build_system_prompt(for_voice=False)},
        {
            "role": "system",
            "content": (
                "THIS IS WHATSAPP, NOT A PHONE CALL. Write to be read: short "
                "sentences, line breaks between points. "
                "ALL AMOUNTS, DATES AND PERCENTAGES AS DIGITS - PKR 310,710, "
                "15-08-2026, 85% - never the words for them. Dates are "
                "day-month-year with dashes and a four digit year, zero "
                "padded: 01-06-2026, not '1 June 2026'. The persona's "
                "lakh rules are for SPEAKING; the student is reading this and "
                "copying it onto a form. Take digits from the knowledge base, "
                "drop its '(=3.1 lakh)' tag, and never round or write "
                "'taqreeban'. "
                "No greeting unless they greeted you, and no sign-off — the "
                "student is mid-conversation. Keep it under 700 characters."
            ),
        },
    ]
    # Prior turns, so "aur uski fees?" resolves against what was just discussed.
    messages.extend(history or [])
    messages.append(
        {
            "role": "user",
            "content": (
                f"University knowledge base extracts:\n\n{context}\n\n"
                f"Student's question: {question}\n\n"
                "Answer only from the extracts above, in natural Pakistani Urdu "
                "written in Roman (English) letters."
            ),
        }
    )

    client = _text_llm_client()
    completion = client.chat.completions.create(
        model=settings.llm_model, **_llm_tuning(), messages=messages
    )
    return (completion.choices[0].message.content or "").strip()


async def _handle_wa_message(wa_id: str, text: str, name: str | None) -> None:
    """Answer one inbound message. Never raises — Meta must not see a failure.

    Runs detached from the webhook response on purpose. Meta expects a 200
    within seconds and retries anything slower, so answering inline would mean
    the same question answered two or three times while retrieval and the model
    finish.
    """
    try:
        history = list(_wa_threads.get(wa_id) or [])
        answer = await asyncio.to_thread(_kb_answer, text, history)
        if not answer:
            return

        if not _wa_in_window(wa_id):
            # Their message opened a window a moment ago, so this should be
            # unreachable; if it is not, sending would be accepted and dropped.
            log.warning("no service window for %s — not replying", mask_number(wa_id))
            return

        await send_whatsapp_text(wa_id, answer)

        thread = _wa_threads.setdefault(wa_id, [])
        thread.append({"role": "user", "content": text})
        thread.append({"role": "assistant", "content": answer})
        del thread[:-_WA_HISTORY_TURNS]
        _wa_threads.move_to_end(wa_id)
        _bounded(_wa_threads, _WA_THREADS_MAX)

        log.info("whatsapp replied to %s (%d chars)", mask_number(wa_id), len(answer))
        bus.publish(
            "whatsapp.inbound",
            from_=mask_number(wa_id), name=name, question=text[:200],
            answer=answer[:200],
        )
    except Exception:  # noqa: BLE001 - a failed reply must not kill the worker
        log.exception("failed to answer whatsapp message from %s", mask_number(wa_id))


@webhooks.get("/whatsapp")
async def whatsapp_verify(request: Request) -> Response:
    """Meta's one-time subscription handshake.

    Meta GETs this with a challenge when the webhook URL is saved, and expects
    the challenge echoed back as plain text. Anything else — including a JSON
    body containing the right number — fails the subscription.
    """
    params = request.query_params
    token = settings.whatsapp_webhook_verify_token.strip()
    if (
        params.get("hub.mode") == "subscribe"
        and token
        and params.get("hub.verify_token") == token
    ):
        log.info("whatsapp webhook verified")
        return Response(content=params.get("hub.challenge", ""), media_type="text/plain")
    log.warning("whatsapp webhook verification refused")
    return Response(status_code=403, content="forbidden")


@webhooks.post("/whatsapp")
async def whatsapp_events(request: Request) -> Response:
    """Inbound WhatsApp messages and delivery receipts.

    Always answers 200, whatever happens inside. A non-200 makes Meta retry the
    same delivery for hours, so an error here would turn one failed answer into
    a queue of them — and a message we could not parse is not a message Meta can
    fix by sending again.
    """
    raw = await request.body()
    if not _wa_signature_ok(raw, request.headers.get("X-Hub-Signature-256")):
        # 403 rather than 200: this one Meta should not retry, and a bad
        # signature is either a misconfiguration or somebody probing.
        return Response(status_code=403, content="bad signature")

    if not settings.whatsapp_inbound_enabled:
        return Response(status_code=200, content="disabled")

    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return Response(status_code=200, content="ok")

    for entry in body.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            # Delivery and read receipts arrive on the same subscription. They
            # are not questions; ignore them without noise.
            if not value.get("messages"):
                continue

            names = {
                c.get("wa_id"): (c.get("profile") or {}).get("name")
                for c in (value.get("contacts") or [])
            }

            for message in value["messages"]:
                wa_id = message.get("from") or ""
                message_id = message.get("id") or ""
                if not wa_id or not message_id:
                    continue

                # Meta redelivers on any hiccup, and answering twice is worse
                # than not answering: the student gets the same paragraph again.
                if message_id in _wa_seen:
                    log.info("ignoring duplicate whatsapp message %s", message_id)
                    continue
                _wa_seen[message_id] = time.time()
                _bounded(_wa_seen, _WA_SEEN_MAX)

                # Their message is what opens the 24-hour window, so record it
                # before anything can fail.
                _wa_window[wa_id] = time.time()
                _wa_window.move_to_end(wa_id)
                _bounded(_wa_window, _WA_THREADS_MAX)

                kind = message.get("type")
                if kind == "text":
                    text = ((message.get("text") or {}).get("body") or "").strip()
                elif kind == "button":
                    text = ((message.get("button") or {}).get("text") or "").strip()
                elif kind == "interactive":
                    interactive = message.get("interactive") or {}
                    reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
                    text = (reply.get("title") or "").strip()
                else:
                    # Voice notes, images and documents. Saying so is better
                    # than silence, which reads as the number being dead.
                    text = ""
                    task = asyncio.create_task(
                        send_whatsapp_text(
                            wa_id,
                            "Maazrat, main abhi sirf likhe hue paighaam samajh "
                            "sakti hoon. Apna sawal type kar dijiye, ya hamari "
                            "admissions helpline par call kar lijiye.",
                        )
                    )
                    _wa_inbound_tasks.add(task)
                    task.add_done_callback(_wa_inbound_tasks.discard)

                if not text:
                    continue

                log.info("whatsapp from %s: %s", mask_number(wa_id), text[:80])
                task = asyncio.create_task(
                    _handle_wa_message(wa_id, text, names.get(wa_id))
                )
                _wa_inbound_tasks.add(task)
                task.add_done_callback(_wa_inbound_tasks.discard)

    return Response(status_code=200, content="ok")


app.include_router(api)
app.include_router(webhooks)


# =============================================================================
# Lifecycle
# =============================================================================


"""Startup and shutdown live in the lifespan handler defined above the app."""


if __name__ == "__main__":
    # Render injects $PORT; never hardcode it for production.
    port = int(os.environ.get("PORT", settings.port))
    reload = settings.app_env == "development"
    uvicorn.run(
        "app:app",
        host=settings.host,
        port=port,
        reload=reload,
        # Without reload_dirs the reloader watches the working directory, which
        # is the repo root when started as `python backend/app.py`.
        reload_dirs=[str(BASE_DIR)] if reload else None,
        # Watch source only. The app writes its SQLite log and embedding cache
        # inside this same directory, so watching everything made every call
        # event restart the server that produced it — dropping live WebSockets
        # and reloading the embedding model in the middle of a phone call.
        reload_includes=["*.py"] if reload else None,
        reload_excludes=["data/*", ".cache/*", "*.db", "*.db-journal", "*.npy", "*.json"]
        if reload
        else None,
        app_dir=str(BASE_DIR),
        log_level=settings.log_level.lower(),
    )