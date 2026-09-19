"""Ayesha on Gemini, talking to the carrier over a raw audio socket.

The OpenAI path never carries audio: Infobip dials OpenAI over a SIP trunk and
the two talk directly, while app.py only sends instructions over a control
socket. Gemini has no phone number, so this module is the missing middle — it
holds both sides of the call and moves every 20ms of audio between them:

    caller -> Infobip -> /ws/phone -> this module -> Gemini Live
    caller <- Infobip <- /ws/phone <- this module <- Gemini Live

Everything here was learned on live calls in the sister project:

  * Only binary frames of exactly 640 bytes (20ms of 16kHz PCM16) may go to
    Infobip. A text frame - even a well-formed control message - makes the
    platform hang up the audio leg within seconds.
  * Nothing may be sent when there is nothing to say. A steady stream of
    silence also makes Infobip drop the leg.
  * Audio must leave at real-time pace, one frame per 20ms against the wall
    clock. Sent as fast as the model produces it, a reply sits in the carrier's
    buffer where a barge-in can no longer stop it, and the caller keeps hearing
    an agent that has already been told to stop.

The call's own logic - who it belongs to, what the knowledge base says, when to
hang up - stays in app.py and reaches this module as callbacks, so nothing here
needs to import it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import numpy as np

log = logging.getLogger("ucp.gemini")

# The carrier's side of the socket: 16kHz mono PCM16, 20ms per frame.
PHONE_RATE = 16000
FRAME_MS = 20
FRAME_BYTES = int(PHONE_RATE * FRAME_MS / 1000) * 2  # 640

# Gemini speaks at 24kHz and listens at 16kHz, which is the rate the phone
# already gives us - so only the reply needs converting.
GEMINI_OUT_RATE = 24000
GEMINI_IN_RATE = 16000

# How much of her reply to hold before playing it. Her audio arrives in bursts
# and the line wants it evenly; without a little in hand, every burst boundary
# is a gap the caller hears as a stutter.
PREROLL_FRAMES = 10


def _low_pass_taps(cutoff: float, length: int = 121) -> np.ndarray:
    """A windowed-sinc low pass, as a symmetric odd-length kernel."""
    n = length if length % 2 else length + 1
    k = np.arange(n) - (n - 1) / 2
    with np.errstate(invalid="ignore"):
        sinc = np.where(k == 0, 2 * cutoff, np.sin(2 * np.pi * cutoff * k) / (np.pi * k))
    window = np.hamming(n)
    taps = sinc * window
    return taps / taps.sum()


class RateConverter:
    """24kHz to 16kHz by 2:3, keeping its filter state across frames.

    Upsample, filter, then decimate - in that order, because a filter applied
    after decimation cannot remove aliasing the decimation has already folded
    in. The state carries over so joining 20ms frames does not click, and the
    phase is remembered so the pattern resumes on the right sample rather than
    shifting pitch every frame.
    """

    def __init__(self, up: int, down: int, keep: float = 0.9) -> None:
        self.up = up
        self.down = down
        self.taps = _low_pass_taps(0.5 * keep / max(up, down))
        self.tail = np.zeros(len(self.taps) - 1, dtype=np.float32)
        self.phase = 0

    def process(self, pcm: bytes) -> bytes:
        if not pcm:
            return b""
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        stuffed = np.zeros(len(samples) * self.up, dtype=np.float32)
        stuffed[:: self.up] = samples * self.up

        joined = np.concatenate([self.tail, stuffed])
        filtered = np.convolve(joined, self.taps, mode="valid")
        self.tail = joined[-(len(self.taps) - 1):] if len(self.taps) > 1 else joined[:0]

        out = filtered[self.phase :: self.down]
        used = self.phase + self.down * len(out)
        self.phase = used - len(filtered)
        clipped = np.clip(out, -1.0, 1.0)
        return (clipped * 32767).astype(np.int16).tobytes()


@dataclass
class GeminiCallbacks:
    """What the call around this bridge wants to know, all optional."""

    on_caller_audio: Callable[[bytes], None] | None = None
    on_agent_audio: Callable[[bytes, int], None] | None = None
    on_transcript: Callable[[str, str], None] | None = None
    on_tool: Callable[[str, dict[str, Any]], Awaitable[str]] | None = None
    on_hangup: Callable[[str], Awaitable[None]] | None = None
    on_connected: Callable[[], None] | None = None


@dataclass
class GeminiConfig:
    api_key: str
    model: str
    voice: str
    instructions: str
    greeting: str
    tools: list[dict[str, Any]] = field(default_factory=list)
    language: str = "ur-PK"
    # Less eager than the default on purpose: on a phone line her own voice
    # leaks back through the caller's handset, and a sensitive detector reads
    # that as the caller speaking and cuts her off mid-sentence.
    vad_prefix_ms: int = 300
    vad_silence_ms: int = 700
    interruptions: bool = True
    # Long enough for her goodbye to finish playing out of the pacer before
    # the carrier is told to hang up.
    hangup_grace_seconds: float = 2.5


class GeminiPhoneCall:
    """One call: the carrier's socket on one side, a Gemini session on the other."""

    def __init__(
        self,
        call_id: str,
        socket: Any,
        config: GeminiConfig,
        callbacks: GeminiCallbacks | None = None,
    ) -> None:
        self.call_id = call_id
        self.socket = socket
        self.config = config
        self.cb = callbacks or GeminiCallbacks()

        self._to_phone = RateConverter(2, 3)
        self._queue: list[bytes] = []
        self._pending = b""
        self._session: Any = None
        self._closed = False
        self._greeted = False
        self._greet_requested = False
        self._speaking = False
        self._tasks: list[asyncio.Task] = []

    # ---- carrier side ----------------------------------------------------

    def feed_caller(self, pcm16: bytes) -> None:
        """A 20ms frame from the phone, already 16kHz PCM16."""
        if self._closed or not pcm16:
            return
        if self.cb.on_caller_audio:
            self.cb.on_caller_audio(pcm16)
        session = self._session
        if session is None:
            return  # the model is not up yet; the opening of a call is silence anyway
        self._tasks.append(asyncio.create_task(self._send_audio(session, pcm16)))

    async def _send_audio(self, session: Any, pcm16: bytes) -> None:
        from google.genai import types  # imported late: the SDK is optional

        try:
            await session.send_realtime_input(
                audio=types.Blob(data=pcm16, mime_type=f"audio/pcm;rate={GEMINI_IN_RATE}")
            )
        except Exception as exc:  # noqa: BLE001 - a dropped frame must not end the call
            if not self._closed:
                log.debug("call %s: audio frame not sent (%s)", self.call_id, exc)

    def greet(self) -> None:
        """Speak the opening line, once the caller can actually hear it.

        Left to itself the model waits for the caller to speak first, which on
        an answered call is two people listening to each other in silence.
        """
        if self._greeted or self._closed:
            return
        if self._session is None:
            self._greet_requested = True
            return
        self._greeted = True
        self._tasks.append(asyncio.create_task(self._send_greeting()))

    async def _send_greeting(self) -> None:
        from google.genai import types

        line = (self.config.greeting or "").strip()
        prompt = (
            f'Greet the caller now. Say exactly this, word for word: "{line}"'
            if line
            else "Greet the caller now."
        )
        try:
            await self._session.send_client_content(
                turns=types.Content(role="user", parts=[types.Part(text=prompt)]),
                turn_complete=True,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("call %s: greeting not sent (%s)", self.call_id, exc)

    # ---- the session ------------------------------------------------------

    async def run(self) -> None:
        """Hold the call open until either side ends it."""
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=self.config.api_key)
        config: dict[str, Any] = {
            "response_modalities": ["AUDIO"],
            "system_instruction": self.config.instructions,
            # Both sides transcribed, which is what makes a call reviewable
            # afterwards - without it only the recording survives.
            "input_audio_transcription": {},
            "output_audio_transcription": {},
            "realtime_input_config": {
                "automatic_activity_detection": {
                    "start_of_speech_sensitivity": "START_SENSITIVITY_LOW",
                    "end_of_speech_sensitivity": "END_SENSITIVITY_LOW",
                    "prefix_padding_ms": self.config.vad_prefix_ms,
                    "silence_duration_ms": self.config.vad_silence_ms,
                }
            },
        }
        if self.config.voice:
            config["speech_config"] = {
                "voice_config": {"prebuilt_voice_config": {"voice_name": self.config.voice}}
            }
        if self.config.tools:
            config["tools"] = [{"function_declarations": self.config.tools}]

        try:
            async with client.aio.live.connect(model=self.config.model, config=config) as session:
                self._session = session
                log.info("call %s: connected to %s", self.call_id, self.config.model)
                if self.cb.on_connected:
                    self.cb.on_connected()
                if self._greet_requested:
                    self.greet()

                pacer = asyncio.create_task(self._pace_to_phone())
                try:
                    await self._read_from_model(session, types)
                finally:
                    pacer.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - never take the call down with a stack trace
            log.warning("call %s: Gemini session ended (%s)", self.call_id, exc)
        finally:
            self._session = None
            self._closed = True

    async def _read_from_model(self, session: Any, types: Any) -> None:
        while not self._closed:
            async for message in session.receive():
                if self._closed:
                    return
                content = getattr(message, "server_content", None)

                # The caller cut in. The model has already stopped; what is
                # still queued here has to go too, or she talks over them.
                if content is not None and getattr(content, "interrupted", False):
                    dropped = len(self._queue)
                    self._queue.clear()
                    self._pending = b""
                    self._speaking = False
                    if dropped:
                        log.info("call %s: interrupted, dropped %d frames", self.call_id, dropped)

                if content is not None:
                    turn = getattr(content, "model_turn", None)
                    for part in getattr(turn, "parts", None) or []:
                        blob = getattr(part, "inline_data", None)
                        data = getattr(blob, "data", None)
                        if data:
                            self._queue_agent_audio(data)

                    heard = getattr(content, "input_transcription", None)
                    said = getattr(content, "output_transcription", None)
                    if self.cb.on_transcript:
                        if heard is not None and getattr(heard, "text", ""):
                            self.cb.on_transcript("caller", heard.text)
                        if said is not None and getattr(said, "text", ""):
                            self.cb.on_transcript("agent", said.text)

                    if getattr(content, "turn_complete", False):
                        self._speaking = False

                tool_call = getattr(message, "tool_call", None)
                calls = getattr(tool_call, "function_calls", None) if tool_call else None
                if calls:
                    await self._answer_tools(session, types, calls)

    def _queue_agent_audio(self, pcm24: bytes) -> None:
        """Her reply, 24kHz from the model, cut into frames the phone accepts."""
        if self.cb.on_agent_audio:
            self.cb.on_agent_audio(pcm24, GEMINI_OUT_RATE)
        self._speaking = True
        converted = self._to_phone.process(pcm24)
        buffer = self._pending + converted
        while len(buffer) >= FRAME_BYTES:
            self._queue.append(buffer[:FRAME_BYTES])
            buffer = buffer[FRAME_BYTES:]
        self._pending = buffer

    async def _answer_tools(self, session: Any, types: Any, calls: list[Any]) -> None:
        responses = []
        hang_up_reason: str | None = None

        for call in calls:
            name = getattr(call, "name", "") or ""
            args = dict(getattr(call, "args", None) or {})

            if name == "end_call":
                hang_up_reason = str(args.get("reason") or "nothing_further")
                result = "Call will end once your goodbye has played."
            elif self.cb.on_tool:
                try:
                    result = await self.cb.on_tool(name, args)
                except Exception as exc:  # noqa: BLE001
                    log.exception("call %s: tool %s failed", self.call_id, name)
                    result = f"That lookup failed ({exc}). Tell the caller you cannot confirm it."
            else:
                result = "That tool is not available on this call."

            responses.append(
                types.FunctionResponse(
                    id=getattr(call, "id", None), name=name, response={"result": result}
                )
            )

        if responses:
            try:
                await session.send_tool_response(function_responses=responses)
            except Exception as exc:  # noqa: BLE001
                log.warning("call %s: tool response not sent (%s)", self.call_id, exc)

        if hang_up_reason and self.cb.on_hangup:
            log.info("call %s: agent is ending the call (%s)", self.call_id, hang_up_reason)
            self._tasks.append(asyncio.create_task(self._hang_up_after_goodbye(hang_up_reason)))

    async def _hang_up_after_goodbye(self, reason: str) -> None:
        # Her goodbye is still in the pacer: hanging up the moment the tool
        # fires cuts her off mid-word.
        await asyncio.sleep(self.config.hangup_grace_seconds)
        if self.cb.on_hangup:
            await self.cb.on_hangup(reason)

    # ---- playing her back -------------------------------------------------

    async def _pace_to_phone(self) -> None:
        """One frame per 20ms against the wall clock, and nothing when idle."""
        next_at = time.monotonic()
        buffering = True
        waited = 0

        while not self._closed:
            now = time.monotonic()
            if now - next_at > FRAME_MS * 10 / 1000:
                next_at = now - FRAME_MS / 1000

            while next_at <= now:
                next_at += FRAME_MS / 1000
                frame: bytes | None = None

                if buffering and self._queue:
                    waited += 1
                    if len(self._queue) >= PREROLL_FRAMES or waited > PREROLL_FRAMES:
                        buffering = False
                        waited = 0
                if not buffering:
                    if self._queue:
                        frame = self._queue.pop(0)
                    else:
                        buffering = True
                        waited = 0

                if frame:
                    try:
                        await self.socket.send_bytes(frame)
                    except Exception:  # noqa: BLE001 - the carrier closed the socket
                        self._closed = True
                        return

            await asyncio.sleep(0.005)

    async def close(self) -> None:
        self._closed = True
        self._queue.clear()
        self._pending = b""
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()


def configured(api_key: str) -> bool:
    """Whether a Gemini call could run: a key, and the SDK actually installed."""
    if not (api_key or "").strip():
        return False
    try:
        import google.genai  # noqa: F401
    except ImportError:
        log.warning("GEMINI_API_KEY is set but google-genai is not installed")
        return False
    return True
