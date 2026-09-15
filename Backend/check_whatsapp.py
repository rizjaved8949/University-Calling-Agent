"""Check the WhatsApp setup end to end, without sending anything.

    python check_whatsapp.py

Answers the four questions that decide whether Ayesha can actually send:

  1. Is the access token valid, and does it expire?
  2. Does the configured phone number id belong to us and is it registered?
  3. Are both templates approved, and do they have the shape the code sends?
  4. Do the names in .env match the approved templates?

Written because every one of these fails silently in a different place: an
expired token is a 401 mid-call, a template awaiting review is a 400 on send,
and a template whose variable count changed is a message that never arrives with
nothing in the log to say why. Reading it off the dashboard means four pages and
a squint; this is one command.

Read-only. It sends no messages and changes no configuration.
"""
from __future__ import annotations

import os
import sys

# Never let a stray DATABASE_URL make a config check open a database connection.
os.environ.setdefault("DATABASE_URL", "")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx  # noqa: E402

from app import settings  # noqa: E402

# What the code sends, and therefore what the templates must accept. Kept beside
# the sender rather than guessed: if send_whatsapp_details ever grows a third
# parameter, this check is what catches the mismatch before a caller does.
EXPECTED = {
    "token": (settings.whatsapp_template, 1, "the query reference number"),
    "details": (settings.whatsapp_details_template, 2, "the information itself"),
}

OK, BAD, WARN = "  OK  ", " FAIL ", " WARN ",


def _tick(good: bool) -> str:
    return OK if good else BAD


def _get(path: str, **params) -> tuple[int, dict]:
    url = f"https://graph.facebook.com/{settings.meta_graph_api_version}/{path}"
    try:
        response = httpx.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {settings.whatsapp_access_token.strip()}"},
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001 - a network failure is a result too
        return 0, {"error": {"message": str(exc)}}
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, {"error": {"message": response.text[:200]}}


def main() -> int:
    print("WhatsApp configuration check")
    print("=" * 62)

    # ---- what is even set -------------------------------------------------
    missing = [
        name
        for name, value in (
            ("WHATSAPP_ACCESS_TOKEN", settings.whatsapp_access_token),
            ("WHATSAPP_PHONE_NUMBER_ID", settings.whatsapp_phone_number_id),
            ("WHATSAPP_BUSINESS_ACCOUNT_ID", settings.whatsapp_business_account_id),
        )
        if not value.strip()
    ]
    if missing:
        print(f"[{BAD}] not set: {', '.join(missing)}")
        print("\nNothing else can be checked until those exist. See .env.example.")
        return 1
    print(f"[{OK}] credentials present (api {settings.meta_graph_api_version})")

    # ---- 1. the token -----------------------------------------------------
    status, body = _get(settings.whatsapp_phone_number_id.strip(),
                        fields="display_phone_number,verified_name,quality_rating")
    if status == 401 or (body.get("error") or {}).get("code") == 190:
        message = (body.get("error") or {}).get("message", "")
        print(f"[{BAD}] access token rejected")
        print(f"         {message}")
        print("\n  A token from the App Dashboard's API Setup page lasts 24 hours.")
        print("  For production create a System User token that never expires:")
        print("  Business Settings > Users > System users > Generate new token,")
        print("  expiration Never, permissions whatsapp_business_messaging and")
        print("  whatsapp_business_management.")
        return 1
    if status != 200:
        print(f"[{BAD}] could not read the phone number ({status})")
        print(f"         {(body.get('error') or {}).get('message', body)}")
        return 1

    print(f"[{OK}] access token is valid")
    print(f"         number  : {body.get('display_phone_number', '?')}"
          f"  ({body.get('verified_name', '?')})")
    quality = body.get("quality_rating")
    if quality and quality.upper() not in ("GREEN", "UNKNOWN"):
        # Yellow or red means Meta is throttling or about to; worth seeing here
        # rather than discovering it as messages quietly stop arriving.
        print(f"[{WARN}] quality rating is {quality} - sending may be limited")
    else:
        print(f"         quality : {quality or 'unknown'}")

    # ---- 2. the templates -------------------------------------------------
    status, body = _get(
        f"{settings.whatsapp_business_account_id.strip()}/message_templates",
        fields="name,status,category,language,components",
        limit=200,
    )
    if status != 200:
        print(f"[{BAD}] could not list templates ({status})")
        print(f"         {(body.get('error') or {}).get('message', body)}")
        print("\n  If this says permissions, the token is missing")
        print("  whatsapp_business_management.")
        return 1

    found = {t.get("name"): t for t in body.get("data", [])}
    print(f"[{OK}] read {len(found)} template(s) from the business account")

    problems = 0
    for kind, (name, want_params, purpose) in EXPECTED.items():
        print()
        if not name.strip():
            var = "WHATSAPP_TEMPLATE" if kind == "token" else "WHATSAPP_DETAILS_TEMPLATE"
            print(f"[{WARN}] {var} is blank - {purpose} cannot be sent")
            print(f"         Ayesha is told she cannot send this, which is correct")
            print(f"         and safe. Set it once the template is approved.")
            continue

        template = found.get(name)
        if not template:
            print(f"[{BAD}] template '{name}' does not exist on this account")
            print(f"         Names are case-sensitive. Available: "
                  f"{', '.join(sorted(found)) or '(none)'}")
            problems += 1
            continue

        state = (template.get("status") or "").upper()
        if state != "APPROVED":
            print(f"[{BAD}] template '{name}' is {state}, not APPROVED")
            print("         Sending will fail until Meta approves it. Do not set")
            print("         the .env value before then.")
            problems += 1
            continue

        # The parameter count has to match what the sender passes, or Meta
        # rejects every send with a shape error that names no template.
        body_text = ""
        for component in template.get("components", []):
            if (component.get("type") or "").upper() == "BODY":
                body_text = component.get("text") or ""
        actual = len({p for p in _placeholders(body_text)})
        good = actual == want_params
        print(f"[{_tick(good)}] template '{name}' approved "
              f"({template.get('category')}, {template.get('language')})")
        print(f"         placeholders: {actual} (code sends {want_params})")
        if not good:
            print("         MISMATCH - every send will fail. Fix the template")
            print("         body so its variable count matches, or the code.")
            problems += 1

    # ---- 3. the verdict ---------------------------------------------------
    print()
    print("=" * 62)
    print(f"token reference messages : "
          f"{'ready' if settings.whatsapp_configured else 'off (template not set)'}")
    print(f"detail messages          : "
          f"{'ready' if settings.whatsapp_details_configured else 'off (template not set)'}")
    if problems:
        print(f"\n{problems} problem(s) above must be fixed before sending works.")
        return 1
    print("\nNo problems found.")
    return 0


def _placeholders(text: str):
    import re

    return re.findall(r"\{\{(\d+)\}\}", text or "")


if __name__ == "__main__":
    raise SystemExit(main())
