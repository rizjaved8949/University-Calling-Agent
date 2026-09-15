"""Create the WhatsApp message templates from code instead of the dashboard.

    python manage_templates.py            # show what exists vs what is defined
    python manage_templates.py --create   # submit anything missing for review

The templates the agent sends are defined here, in one place, next to the code
that sends them. Clicking them into WhatsApp Manager works exactly once and then
lives only in a dashboard: the wording cannot be reviewed in a diff, a footer
pasted into the wrong box is invisible until a student receives it, and
recreating one after a mistake is a wizard rather than a command. All three of
those actually happened while setting this up.

What this does NOT do is skip Meta's review. A template created here is queued
and classified exactly as one created by hand -- if the category does not match
the content, the API says so in the same words the dashboard does. The value is
that the definition is version-controlled and resubmitting is one command.

Deliberately create-only. It never edits or deletes a template: an approved
template is a live thing students receive, and a script that can silently
replace one is a script that will. Deletions stay a human decision in the
dashboard.
"""
from __future__ import annotations

import json
import os
import re
import sys

os.environ.setdefault("DATABASE_URL", "")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx  # noqa: E402

from app import settings  # noqa: E402

# The exact bodies, samples and footers, as submitted. Changing a body here does
# NOT change an already-approved template -- Meta versions them by name, and an
# approved template can only be edited in the dashboard. This is the source of
# truth for creating them, and the record of what was created.
#
# Both are UTILITY. See WHATSAPP_TEMPLATE.md for why the reference number sits
# inside a sentence and why the details template is worded as a reply: each
# phrasing is what stopped Meta's classifier reclassifying it as Authentication
# or Marketing respectively.
FOOTER = "University of Central Punjab, Lahore"

TEMPLATES: list[dict] = [
    {
        "name": "ucp_query_reference",
        "language": "en",
        "category": "UTILITY",
        "body": (
            "Assalam-o-Alaikum! University of Central Punjab, Lahore mein aap ki "
            "query register kar li gayi hai aur us ka reference number {{1}} hai."
            "\n\n"
            "Hamari admissions team is par kaam kar rahi hai aur maximum 72 hours "
            "ke andar aap se rabta karegi. Baraye meherbani yeh reference "
            "follow-up ke liye mehfooz rakhein."
            "\n\n"
            "Shukriya."
        ),
        "examples": ["482103"],
        "footer": FOOTER,
    },
    {
        "name": "ucp_query_details",
        "language": "en",
        "category": "UTILITY",
        "body": (
            "Assalam-o-Alaikum! Aap ne hamari call par jo maloomat talab ki thi, "
            "wo darj zail hai."
            "\n\n"
            "Aap ka sawal: {{1}}"
            "\n\n"
            "Jawab: {{2}}"
            "\n\n"
            "Mazeed wazahat ke liye admissions helpline 0800-00827 par rabta "
            "karein. Shukriya."
        ),
        "examples": [
            "Admission ke zaroori documents",
            "CNIC, matric aur intermediate ki marksheet, aur do passport size "
            "tasveerein darkar hain.",
        ],
        "footer": FOOTER,
    },
]

# Meta's ceiling on a rendered body. Checked before submitting rather than after
# a caller gets a truncated message.
BODY_LIMIT = 1024


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.whatsapp_access_token.strip()}",
        "Content-Type": "application/json",
    }


def _base() -> str:
    return (
        f"https://graph.facebook.com/{settings.meta_graph_api_version}"
        f"/{settings.whatsapp_business_account_id.strip()}/message_templates"
    )


def _placeholders(text: str) -> list[str]:
    return sorted(set(re.findall(r"\{\{(\d+)\}\}", text)))


def _validate(spec: dict) -> list[str]:
    """Everything Meta would reject, caught before spending a review on it."""
    faults = []
    body = spec["body"]
    found = _placeholders(body)
    expected = [str(i + 1) for i in range(len(spec["examples"]))]
    if found != expected:
        faults.append(f"placeholders {found} do not match {len(spec['examples'])} sample(s)")
    if re.match(r"^\s*\{\{", body) or re.search(r"\}\}\s*$", body):
        faults.append("body starts or ends with a variable, which Meta rejects")

    # Worst case is every sample replaced by the longest value the code can send.
    caps = [settings.whatsapp_topic_max_chars, settings.whatsapp_details_max_chars]
    worst = body
    for i, _ in enumerate(spec["examples"]):
        worst = worst.replace("{{%d}}" % (i + 1), "x" * caps[min(i, len(caps) - 1)])
    if len(worst) > BODY_LIMIT:
        faults.append(f"worst-case body is {len(worst)} chars, over Meta's {BODY_LIMIT}")

    if len(spec["footer"]) > 60:
        faults.append(f"footer is {len(spec['footer'])} chars, over Meta's 60")
    return faults


def _existing() -> dict[str, dict]:
    response = httpx.get(
        _base(),
        params={"fields": "name,status,category,language,components", "limit": 200},
        headers=_headers(),
        timeout=30,
    )
    if response.status_code >= 400:
        error = (response.json() or {}).get("error", {})
        print(f"could not list templates: {error.get('message', response.text[:200])}")
        raise SystemExit(1)
    return {t["name"]: t for t in (response.json() or {}).get("data", [])}


def _create(spec: dict) -> bool:
    payload = {
        "name": spec["name"],
        "language": spec["language"],
        "category": spec["category"],
        "components": [
            {
                "type": "BODY",
                "text": spec["body"],
                "example": {"body_text": [spec["examples"]]},
            },
            {"type": "FOOTER", "text": spec["footer"]},
        ],
    }
    response = httpx.post(_base(), headers=_headers(), json=payload, timeout=30)
    body = response.json() if response.content else {}
    if response.status_code >= 400:
        error = body.get("error", {})
        print(f"  FAILED: {error.get('error_user_msg') or error.get('message')}")
        # The classifier's verdict arrives here, in the same words the dashboard
        # shows. Reword and rerun rather than resubmitting the same text.
        if error.get("error_user_title"):
            print(f"          ({error['error_user_title']})")
        return False
    print(f"  submitted for review (id {body.get('id', '?')}, status "
          f"{body.get('status', 'PENDING')})")
    return True


def main() -> int:
    create = "--create" in sys.argv

    if not settings.whatsapp_access_token.strip():
        print("WHATSAPP_ACCESS_TOKEN is not set.")
        return 1

    existing = _existing()
    missing = []

    print("Defined templates")
    print("=" * 66)
    for spec in TEMPLATES:
        faults = _validate(spec)
        found = existing.get(spec["name"])
        state = found["status"] if found else "MISSING"
        print(f"{spec['name']:<24} {state:<10} {spec['category']:<8} "
              f"{spec['language']}")
        for fault in faults:
            print(f"    INVALID: {fault}")
        if faults:
            # Never submit something already known to be malformed.
            continue
        if not found:
            missing.append(spec)

    if not missing:
        print("\nNothing to create - every defined template already exists.")
        print("Note: an approved template cannot be edited from here. Change its")
        print("wording in WhatsApp Manager, or create one under a new name.")
        return 0

    if not create:
        print(f"\n{len(missing)} template(s) missing. Re-run with --create to submit:")
        for spec in missing:
            print(f"  - {spec['name']}")
        return 0

    print()
    failures = 0
    for spec in missing:
        print(f"creating {spec['name']}...")
        if not _create(spec):
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
