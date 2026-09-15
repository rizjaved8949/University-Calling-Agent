# WhatsApp template — query reference token

> **Note for the generic build.** Everything else in this project now uses
> neutral "University" wording, but the two templates below cannot follow:
> their text is registered and approved by Meta, and the sending code may only
> pass the placeholder values, never the surrounding wording. Any WhatsApp
> message the agent sends therefore still carries the original institution's
> name and footer. Fixing that means registering new templates under generic
> names, waiting for Meta's approval, then pointing WHATSAPP_TEMPLATE and
> WHATSAPP_DETAILS_TEMPLATE at them. The wording recorded here is what Meta
> actually approved, so it is left unchanged on purpose.

Sending goes direct to Meta's Cloud API — Infobip is voice only and is not
involved. Submit this template in **Meta Business Manager → WhatsApp Manager →
Message Templates → Create**. Nothing here needs changing; the code already
expects this shape.

## Submission fields

| Field | Value |
|---|---|
| **Name** | `ucp_query_reference` |
| **Category** | **UTILITY** — not Marketing, not Authentication |
| **Language** | **English** (`en`) — see note below |
| **Header** | none |
| **Footer** | `University of Central Punjab, Lahore` |
| **Buttons** | none |

## Body

```
Assalam-o-Alaikum! University of Central Punjab, Lahore mein aap ki query register kar li gayi hai aur us ka reference number {{1}} hai.

Hamari admissions team is par kaam kar rahi hai aur maximum 72 hours ke andar aap se rabta karegi. Baraye meherbani yeh reference follow-up ke liye mehfooz rakhein.

Shukriya.
```

**Sample value for {{1}}** (Meta asks for one at submission): `482103`

### Why the number is inside a sentence

An earlier draft put it on its own line — `Aap ka reference number hai: {{1}}` —
and Meta's classifier rejected the template before submission with *"Category
does not match ... this message template will be rejected"*, insisting it was
**Authentication**.

It was right to be suspicious: a bare six-digit number alone on a line is
exactly the shape of a one-time password, and every OTP template isolates its
code that way. Accepting the Authentication category would have been worse than
a rejection — that category has its own required format and pricing, and it is
not what this message is.

So the number stays **inline, inside a full sentence**, surrounded by words about
a query being registered and a team following up. That is what makes it read as a
support-ticket reference rather than a login code. Keep it that way: moving
`{{1}}` back onto its own line will fail the same check.

## Why these choices

- **Exactly one placeholder.** `_send_token_whatsapp` sends a single body
  placeholder and nothing else. Adding a second variable to the template means
  Infobip rejects every send until the code is changed to match.
- **Language `en`, not `ur`.** The text is Roman Urdu — Urdu words in Latin
  letters. Meta has no Roman Urdu locale, and `ur` expects Urdu script, so a
  template registered under `ur` with this body reads as broken. `en` is the
  correct registration for Latin-script content.
- **UTILITY category.** This message is a service update about something the
  caller asked for. Registering it as Marketing costs more per message and
  invites rejection, because there is no offer or promotion in it.
- **No buttons.** A "Reply" or "Call us" button changes the approval path and
  buys nothing — the caller already has the phone number they rang.
- **{{1}} is not at the start or the end of the body.** Meta rejects templates
  whose body begins or ends with a variable.

## Why this name and not `ucp_query_token`

Deleting a template does not free its name immediately - Meta holds it while
the delete propagates, and refuses a new template of the same name with
*"Message template language is being deleted"*. The first submission had to be
deleted to correct a footer, and the name stayed locked afterwards, so the
template was created under a new one rather than waiting on Meta.

Nothing in the code cares what it is called - the name is read from
`WHATSAPP_TEMPLATE` - but it must match the approved template **exactly**.

## After approval

Set in `.env` (and on Render):

```ini
WHATSAPP_TEMPLATE=ucp_query_reference
WHATSAPP_LANGUAGE=en
```

The credentials are already set. The moment the template name is filled in,
`whatsapp_configured` flips true and Ayesha starts offering to send the token —
so do not set it before the template is actually approved, or she will promise
messages that fail.

The language value must match the registration **exactly**. If WhatsApp Manager
shows the template as `en_US`, put `en_US` here; `en` will be rejected.

## What the caller receives

> Assalam-o-Alaikum! Aap ki query University of Central Punjab, Lahore mein register ho gayi hai.
>
> Aap ka reference number hai: 482103
>
> Is number ko future follow-up ke liye save kar lein. Aap ko maximum 72 hours ke andar response provide kar diya jaye ga.
>
> Shukriya.
>
> *University of Central Punjab, Lahore*


---

# Second template — the details themselves

The template above sends a *reference number*. This one sends the *information*:
either read out on the call and sent as a written copy ("WhatsApp par bhej
dein"), or the department's answer once a query is resolved in the dashboard.

Submit it the same way: **WhatsApp Manager → Message Templates → Create**.

## Submission fields

| Field | Value |
|---|---|
| **Name** | `ucp_query_details` |
| **Category** | **UTILITY** |
| **Language** | **English** (`en`) — same reasoning as above |
| **Header** | none |
| **Footer** | `University of Central Punjab, Lahore` |
| **Buttons** | none |

## Body

```
Assalam-o-Alaikum! Aap ne hamari call par jo maloomat talab ki thi, wo darj zail hai.

Aap ka sawal: {{1}}

Jawab: {{2}}

Mazeed wazahat ke liye admissions helpline 0800-00827 par rabta karein. Shukriya.
```

**Sample value for {{1}}**: `Admission ke zaroori documents`
**Sample value for {{2}}**: `CNIC, matric aur intermediate ki marksheet, aur do passport size tasveerein darkar hain.`

### Why it is worded as a reply, not an announcement

The first draft opened *"University of Central Punjab, Lahore se aap ke liye
maloomat"* — information for you from UCP. Meta's classifier refused it before
submission with *"Category does not match"*, recommending **Marketing**, and
warned it would be rejected.

That was a fair reading. Utility means a message about something the customer
**already** did; an unprompted "here is information about us" is promotional
however useful it is. The fee figure in the old sample made it worse — a price
quote out of context looks like an offer.

So the body now opens by naming the interaction it belongs to — *"Aap ne hamari
call par jo maloomat talab ki thi"* — and frames the two variables as
`Aap ka sawal` / `Jawab`, a reply to a question they asked. The sample is a
document list rather than a price.

Keep that framing if the wording is ever revised. Anything that reads as UCP
announcing something, rather than answering something, will be pushed back to
Marketing — which costs more per message and is throttled differently.

The text is **Roman Urdu** — Urdu words in English letters — because that is how
Pakistani students read WhatsApp. Urdu script renders inconsistently on older
Android phones and cannot be typed back in reply by most callers.

## One template covers every topic

There is deliberately **one** details template, not one per subject. `{{1}}` is
whatever the caller asked about and `{{2}}` is the answer, so the same approved
template carries every case the agent handles:

| Caller asked about | `{{1}}` | `{{2}}` |
|---|---|---|
| Entry test | `Entry test ki tafseel` | `Entry test 15 August 2026 ko hai. Test mein English, Maths aur Analytical Reasoning shamil hain. Registration 5 August tak open hai.` |
| Admission process | `Admission ka tareeqa kar` | `Pehle online portal par form bhar dein. Uske baad entry test aur interview ki tareekh aap ko email par mil jaye gi.` |
| Fees | `BS Computer Science ki fees` | `Per semester fee PKR 223,979 hai. Admission fee PKR 25,000 alag se, sirf pehle semester mein.` |
| Documents | `Admission ke zaroori documents` | `CNIC ya B-Form ki copy, matric aur intermediate ki marksheet, aur do passport size tasveerein darkar hain.` |
| Scholarships | `Merit scholarship ki sharait` | `85 percent ya us se zyada marks par merit scholarship milti hai. Darkhwast admission ke waqt hi jama karwani hoti hai.` |
| Deadlines | `Admission ki aakhri tareekh` | `Fall 2026 ke liye darkhwast 20 August 2026 tak jama karwai ja sakti hai.` |
| Query answered later | `Sibling discount policy` | `Ji haan, dusre bhai behan par 25 percent discount milta hai. Admissions office se darkhwast form le lijiye.` |

Adding a template per subject would mean seven Meta approvals, seven ways for a
send to fail, and a code change every time the university adds a topic. The
variable does that work already.

Note the last row: the **same** template is used when the follow-up desk resolves
a query days later, with `{{1}}` taken from the recorded question and `{{2}}` from
what the department answered. One approval covers both paths.

## The rules this shape obeys

- **Exactly two placeholders, in this order.** `send_whatsapp_details` sends
  `[topic, details]` and nothing else. A third variable, or the two swapped,
  means every send fails until the code matches.
- **Neither `{{1}}` nor `{{2}}` starts or ends the body.** Meta rejects those.
- **No newlines, tabs, or 4+ spaces reach a placeholder.** Meta rejects the
  whole message, not the offending parameter — so `_template_param()` flattens
  line breaks into sentence breaks, strips bullet characters, and caps the
  length before anything is sent. Nothing else in the code may bypass it.
- **Lengths are capped** at 120 characters for `{{1}}` and 600 for `{{2}}`
  (`WHATSAPP_TOPIC_MAX_CHARS`, `WHATSAPP_DETAILS_MAX_CHARS`). The template body
  may not exceed 1024 characters once rendered, and the fixed wording above
  takes roughly 200 of them. Over-long text is cut at a sentence boundary and
  marked with `…` rather than mid-word.

## After approval

```ini
WHATSAPP_DETAILS_TEMPLATE=ucp_query_details
```

Until that value is set, `whatsapp_details_configured` is false and:

- the `send_whatsapp_details` tool is **not offered to the agent at all**, so
  she cannot promise a message that will not arrive — she is told instead to
  give the information out loud;
- resolving a query still closes it, recording `whatsapp_status=disabled`
  rather than failing.

Approve the template first, then set the value. Never the other way round.

## Why not just send a normal message?

Free-form WhatsApp messages are only allowed inside a 24-hour customer service
window, which opens when the customer messages **or WhatsApp-calls** you. Our
calls run over the normal phone network via Infobip, which Meta cannot see — so
no window is ever open, and a free-form send is accepted by the API and then
silently dropped. A template works from anywhere at any time, at the cost of
fixed wording. That is the whole reason this file exists.
