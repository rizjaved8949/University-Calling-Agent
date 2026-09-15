"""Put recovered call recordings back into the call log, permanently.

Audio can outlive its call row: Infobip keeps its own copy of every call the
server dialled, and a browser recording may be sitting on someone's disk long
after the container that held it was recycled. This restores either kind into
Supabase — the audio into the bucket, the call into Postgres — so it appears in
the History tab and stays there.

Run it from UCP_Backend with the Supabase settings in .env:

    python restore_recordings.py data/recovered/*.wav
    python restore_recordings.py "C:/path/to/talk-call.webm" --number +923068222219 \
        --started "2026-08-12T18:38:21+0000" --duration 145 --mode MANUAL_BROWSER

Files named like `2026-08-12T18-44-30_970461fb_410s.wav` — as recovered from
Infobip — carry their own metadata and need no flags: the timestamp, call id and
duration are read straight off the name, and everything else is looked up.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import app

MIME = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".webm": "audio/webm", ".ogg": "audio/ogg"}
# 2026-08-12T18-44-30_970461fb_410s.wav
FROM_NAME = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})_([0-9a-f]+)_(\d+)s")


def parse_name(path: Path) -> dict[str, object] | None:
    match = FROM_NAME.search(path.stem)
    if not match:
        return None
    stamp, call_id, duration = match.groups()
    started = datetime.strptime(stamp, "%Y-%m-%dT%H-%M-%S").replace(tzinfo=timezone.utc)
    return {"call_id": call_id, "start": started.timestamp(), "duration": int(duration)}


async def restore(path: Path, args: argparse.Namespace) -> bool:
    if not path.exists():
        print(f"  missing: {path}")
        return False

    meta = parse_name(path) or {}
    # A partial call id from a filename is not enough to update the right row,
    # so anything without full metadata gets a fresh id and the flags supplied.
    call_id = str(meta.get("call_id") or "")
    if len(call_id) < 36:
        call_id = args.call_id or str(uuid.uuid4())
    started = float(args.started_epoch or meta.get("start") or 0)
    duration = int(args.duration or meta.get("duration") or 0)
    if not started:
        print(f"  {path.name}: need --started, the filename does not carry a time")
        return False

    audio = path.read_bytes()
    mime = MIME.get(path.suffix.lower(), "audio/wav")

    # Browser recordings are webm, which cannot be seeked. Convert on the way in
    # so a restored call behaves like a freshly recorded one.
    if "webm" in mime or "ogg" in mime:
        converted = await app._to_mp3(audio)
        if converted:
            print(f"  {path.name}: converted to mp3 ({len(audio)//1024} -> {len(converted)//1024} KB)")
            audio, mime = converted, "audio/mpeg"

    reference = await app.recordings.put(call_id, audio, mime, app._extension_for(mime))
    if not reference:
        print(f"  {path.name}: upload failed")
        return False

    app.store.upsert(
        call_id,
        session_id=call_id[:8],
        phone_number=args.number or "unknown",
        direction="OUTBOUND",
        status="COMPLETED",
        outcome=args.outcome,
        start_time=started,
        answer_time=started,
        end_time=started + duration,
        duration=duration,
        mode=args.mode,
        recording_path=reference,
        recording_mime=mime,
        recording_ready=1,
        recording_absent=0,
    )
    when = datetime.fromtimestamp(started, timezone.utc).strftime("%b %d %H:%M")
    print(f"  restored {call_id[:8]}  {when} UTC  {duration}s  {len(audio)//1024} KB -> {reference}")
    return True


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", help="audio files to restore")
    parser.add_argument("--number", default="", help="the other party's number")
    parser.add_argument("--started", dest="started_iso", default="", help="ISO start time")
    parser.add_argument("--duration", type=int, default=0, help="seconds")
    parser.add_argument("--mode", default=None, help="MANUAL_BROWSER, MANUAL_PHONE, or omit for an AI call")
    parser.add_argument("--outcome", default="RESTORED")
    parser.add_argument("--call-id", default="", help="attach to an existing call id")
    args = parser.parse_args()

    args.started_epoch = 0.0
    if args.started_iso:
        text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", args.started_iso)
        args.started_epoch = datetime.fromisoformat(text).timestamp()

    if not app.recordings.remote:
        print("Supabase is not configured — set SUPABASE_URL and SUPABASE_SERVICE_KEY in .env")
        return 1
    if not isinstance(app.store, app.PostgresStore):
        print("DATABASE_URL is not reaching Postgres — restoring now would write to a local file")
        return 1

    print(f"restoring into {app.settings.supabase_bucket} + Postgres\n")
    done = 0
    for name in args.files:
        # Absolute paths cannot be globbed relative to the working directory,
        # and a shell that already expanded the pattern passes real paths here.
        candidates = [Path(name)]
        if not Path(name).exists() and not Path(name).is_absolute():
            candidates = sorted(Path().glob(name))
        for path in candidates:
            if await restore(path, args):
                done += 1
    print(f"\n{done} recording(s) restored. They are now in the History tab and survive redeploys.")
    await app.recordings.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
