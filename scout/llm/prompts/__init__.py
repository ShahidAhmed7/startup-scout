"""Prompts and JSON schemas for every LLM job. Rubric = CLAUDE.md section 9."""
from __future__ import annotations

SYSTEM = (
    "You are the analyst behind Startup Scout, a private research tool for one founder in Bangladesh "
    "whose field is AI (they cannot build frontier AI labs, chips, hardware or data centres). "
    "Write in plain, short English with normal capitalisation: numbers as digits, no hype words, sentence case. "
    "Every output must be in English, even when the source text is in Chinese, Arabic or another language. "
    "Never invent facts. Facts (funding, what a company does, competitors, market facts) may only come from "
    "the material you are given. If something is not in the material, write \"unknown\"."
)

# ---------------------------------------------------------------- extraction (Flash-Lite)

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {"articles": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "item_id": {"type": "integer"},
            "kind": {"type": "string", "enum": ["funding", "launch", "other"]},
            "companies": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "website": {"type": "string"},
                    "country": {"type": "string"},
                    "stage": {"type": "string"},
                    "amount_text": {"type": "string"},
                    "amount_usd": {"type": "number"},
                    "investors": {"type": "array", "items": {"type": "string"}},
                    "what_they_do": {"type": "string"},
                },
                "required": ["name", "website", "country", "stage", "amount_text", "amount_usd", "investors",
                             "what_they_do"],
            }},
        },
        "required": ["item_id", "kind", "companies"],
    }}},
    "required": ["articles"],
}


def extract_prompt(articles: list[dict]) -> str:
    lines = [
        "Read these news items and pull out startup facts. Rules:",
        "- Use ONLY what each item's text says. No facts from memory. Unknown text fields = \"\", unknown "
        "amount_usd = 0, unknown investors = [].",
        "- Translate everything to English. Keep company names as written (add the Latin-script name if the "
        "item gives one).",
        "- kind: \"funding\" = a startup raised money (pre-seed, seed, Series A..., venture debt, grant); "
        "\"launch\" = a young startup launching its first product or coming out of stealth (YC, Launch HN, new "
        "company); \"other\" = anything else: VC funds closing, IPOs, acquisitions, big or established companies "
        "(OpenAI, Google, banks, telecoms) releasing products, media outlets launching sections, policy, opinion, "
        "events. For \"other\" return no companies.",
        "- companies: only the startup(s) that raised money or launched, never the investors.",
        "- country: where the startup is based, only if the item says so (e.g. \"Lagos-based\" = Nigeria).",
        "- stage: Pre-seed, Seed, Series A, Series B..., Bridge, Debt, Grant, or \"\" if not stated.",
        "- amount_text: the money raised in this round (never the valuation), exactly as written (\"₹50 crore\", \"$4M\"). amount_usd: a number only if the "
        "item states a US-dollar amount (e.g. 4000000), else 0.",
        "- website: only if the item text contains it.",
        "- what_they_do: what the company sells or does (not the news event), one plain sentence, at most 20 words, "
        "from the item text.",
        "Return one entry per item id.",
        "",
    ]
    for a in articles:
        lines.append(f"ITEM {a['id']} (source: {a['source']})\nTitle: {a['title']}\nText: {a['summary']}\n")
    return "\n".join(lines)


# ---------------------------------------------------------------- scoring + skip filter (Hydrogen)

SCORE_KEYS = ["bangladesh", "global", "pain", "why_now", "feasibility", "fits_you"]

# Broad sectors, so the daily picks can be spread across the economy (not five flavours of AI software).
SECTORS = ["fintech", "commerce and retail", "logistics and mobility", "health", "education", "agriculture and food",
           "travel and hospitality", "real estate and construction", "jobs and HR", "media and creators",
           "marketing and sales", "productivity and business software", "developer tools and AI infrastructure",
           "security", "legal and government", "climate and energy", "manufacturing and industry",
           "consumer and social", "other"]

SCORE_SCHEMA = {
    "type": "object",
    "properties": {"startups": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "startup_id": {"type": "integer"},
            "category": {"type": "string"},
            "sector": {"type": "string", "enum": SECTORS},
            "ai_native": {"type": "boolean"},
            "search_phrase": {"type": "string"},
            "skip": {"type": "boolean"},
            "skip_reason": {"type": "string"},
            "scores": {"type": "object", "properties": {k: {"type": "integer"} for k in SCORE_KEYS},
                       "required": SCORE_KEYS},
            "money_to_start": {"type": "string", "enum": ["low", "medium", "high"]},
            "reasons": {"type": "object",
                        "properties": {k: {"type": "string"} for k in SCORE_KEYS + ["money_to_start"]},
                        "required": SCORE_KEYS + ["money_to_start"]},
            "ai_angle": {"type": "string"},
            "ai_angle_strong": {"type": "boolean"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        },
        "required": ["startup_id", "category", "sector", "ai_native", "search_phrase", "skip", "skip_reason", "scores",
                     "money_to_start", "reasons", "ai_angle", "ai_angle_strong", "confidence"],
    }}},
    "required": ["startups"],
}

RUBRIC = """Score each startup twice. Each score is an integer 1-10 with a one-line reason.
Missing information lowers confidence, not the score: set "confidence": "low".
The owner is a 2-3 person team in Bangladesh with these skills: {skills}. Score the VERSION that team could
build, not the funded startup's own market: a US tool for dentists can still be a good Bangladesh idea if Dhaka
clinics have the same problem. Be honest and calibrated: most startups land around 3-6; reserve 8-10 for ideas
the owner should seriously consider this month.

Shared sub-scores:
- pain: how badly the customer needs this, as evidenced by the material.
- why_now: what changed recently that makes this possible or urgent.
- feasibility: can 2-3 people build a first version in about 2 months, with no special hardware, licences,
  physical operations (warehouses, fleets, farms, factories) or big data deals? 8-10 = pure software they could
  ship; 4-6 = possible but hard (deep integrations, trust, data access); 1-3 = needs capital, hardware, licences
  or physical operations.
- money_to_start: low / medium / high, with a short reason.
- fits_you: can a small software team with the owner's skills build it? The owner can build any software product
  (web apps, mobile-friendly sites, marketplaces, booking/ordering/payment tools, dashboards); AI is a plus, not a
  requirement. A non-AI marketplace or fintech tool can fit just as well as an AI product.

bangladesh = could this small team build a version for the Bangladeshi market?
- 8-10: a common, painful problem in Bangladesh; buyers will pay in taka via bKash, Nagad or cash on delivery;
  no licence needed; software-first; a first version in 2 months. Typical examples: software or AI tools for
  Facebook-commerce sellers, small shops, clinics, schools, students, freelancers or exporters, sold for a few
  hundred to a few thousand taka a month.
- 5-7: real demand but a clear hurdle (strong local incumbents likely, a partner needed, logistics, slow buyers).
- 1-4: needs a Bangladesh Bank / BSEC / BTRC / DGDA licence, big capital, hardware, physical operations, credit
  cards, credit bureaus, reliable addresses or APIs Bangladesh lacks; or Bangladeshis don't have the problem.
- Think about low-end Android, Bangla language and Facebook-commerce habits.

global = could this small team, based in Bangladesh, build a version and sell it to customers abroad?
- 8-10 (score the version a Bangladesh team would sell): online, self-serve, sold to small businesses, creators,
  developers or consumers; paid through a merchant
  of record (Paddle, Lemon Squeezy) because Stripe and PayPal don't operate in Bangladesh; light compliance; a
  clear niche the funded startup ignores (smaller customers, another region, one industry).
- 5-7: sellable online but crowded, or needs some sales effort or trust-building.
- 1-4: enterprise sales (banks, hospitals, governments, law firms, large companies), security reviews,
  regulated data (medical, financial), on-site work, or infrastructure that needs big capital.

Hard rules: if the material does not say what the company sells or does, every score is at most 3 and confidence
is "low" (you cannot judge an unknown product). bangladesh and global may each be at most feasibility + 2. If money_to_start is high, neither may
be above 6. An idea is not good just because the funded company is impressive.

AI is ONE angle on every idea, not a filter: never score an idea higher just because it uses AI. Marketplaces,
logistics, fintech, commerce, education, health, agriculture, travel and consumer businesses can score just as high.

ai_angle: one line: could AI make this cheaper or better, and could an open model do the job?
ai_angle_strong: true only if AI is central to a cheaper/better version a small team could build.
ai_native: true if the funded product itself IS an AI product (AI agent, AI model, AI-first software); false for
businesses that merely use AI somewhere.
sector: the ONE broad sector from the allowed list that best describes the customer and market (not the technology):
an AI tool for clinics is "health", an AI tool for shops is "commerce and retail".

skip = true if the startup is in any of these categories: {skip_list}. Then give skip_reason (the category).
Still fill every other field (scores may be rough).

category: 1-3 words (e.g. "fintech", "health software", "logistics", "dev tools").
search_phrase: 3-6 plain words describing the product type for a web search
(e.g. "clinic appointment booking app"). No company name."""


def score_prompt(startups: list[dict], skills: list[str], skip_list: list[str]) -> str:
    out = [RUBRIC.format(skills=", ".join(skills), skip_list="; ".join(skip_list)), "",
           "Return one entry per startup_id. Use only the material below.", ""]
    for s in startups:
        out.append(f"STARTUP {s['id']}: {s['name']}")
        for k in ("country", "stage", "amount", "investors", "what_they_do", "yc_batch"):
            if s.get(k):
                out.append(f"  {k}: {s[k]}")
        for src in s["sources"]:
            out.append(f"  source: {src}")
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------- cards (Hydrogen)

CARD_SCHEMA = {
    "type": "object",
    "properties": {
        "what_they_do": {"type": "string"},
        "why_funded": {"type": "string"},
        "bangladesh_version": {"type": "string"},
        "global_angle": {"type": "string"},
        "ai_angle": {"type": "string"},
        "already_in_bangladesh": {"type": "string"},
        "competitors": {"type": "array", "items": {
            "type": "object", "properties": {"name": {"type": "string"}, "url": {"type": "string"}},
            "required": ["name", "url"]}},
        "test_this_week": {"type": "string"},
        "score_check": {"type": "object", "properties": {
            "bangladesh": {"type": "integer"}, "global": {"type": "integer"}, "note": {"type": "string"}},
            "required": ["bangladesh", "global", "note"]},
        "source_urls": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["what_they_do", "why_funded", "bangladesh_version", "global_angle", "ai_angle",
                 "already_in_bangladesh", "competitors", "test_this_week", "score_check", "source_urls"],
}


def card_prompt(p: dict) -> str:
    return f"""Write a startup idea card for the owner. Market focus: {p['market']}.

STARTUP FACTS (from news extraction; use as given):
{p['facts']}

SCORES (already decided, do not change): {p['scores']}

NEWS SOURCES (title + text):
{p['news']}

COMPANY HOMEPAGE TEXT ({p['homepage_url'] or 'not available'}):
{p['homepage'] or 'unknown'}

WEB SEARCH RESULTS (for "already in Bangladesh"; competitor names may ONLY come from here):
{p['search']}

Write each field in plain English, short:
- what_they_do: 1-2 sentences, from the homepage/news only.
- why_funded: 1-2 sentences: what the sources say about traction, team or market. If they say nothing, "unknown".
- bangladesh_version: 2-4 sentences: how a 2-3 person team could build a version for Bangladesh (payments via
  bKash/Nagad/cash on delivery, Bangla, low-end Android, licences needed, who pays in taka).
- global_angle: 2-3 sentences: how a Bangladesh-based team could sell a version abroad: which customers, which
  niche the funded startup ignores, how they'd find buyers online, and payment via Paddle or Lemon Squeezy as
  merchant of record. Be honest if the global version is weak.
- ai_angle: 1-2 sentences: how AI (ideally an open model) makes it cheaper or better.
- already_in_bangladesh: 1-2 sentences summarising who already does this in Bangladesh, based ONLY on the web
  search results. If the results show nobody, say "No one found in a quick search."
- competitors: only companies the search results show are active in Bangladesh, each with the exact result URL.
  Global tools that merely appear in the results are not Bangladeshi competitors. [] if none.
- test_this_week: 2-4 concrete steps to test demand in Bangladesh within 7 days (who to talk to, what to post,
  what number counts as a yes).
- score_check: after reading the homepage and search results, would you change the scores? Give the Bangladesh
  and global scores you now think are right (same rules: strict, small team, licences, local competitors) and a
  one-line note on what changed. If nothing changed, repeat the scores and set note to "".
- source_urls: the URLs (from the material above) that support the facts you used.
Never add facts that are not in the material."""


# ---------------------------------------------------------------- deep dive (Hydrogen)

DEEP_SCHEMA = {
    "type": "object",
    "properties": {
        "competitors_bd": {"type": "string"},
        "competitors": {"type": "array", "items": {
            "type": "object", "properties": {"name": {"type": "string"}, "url": {"type": "string"}},
            "required": ["name", "url"]}},
        "failure_check": {"type": "string"},
        "how_it_makes_money": {"type": "string"},
        "risks": {"type": "string"},
        "first_7_days": {"type": "string"},
        "source_urls": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["competitors_bd", "competitors", "failure_check", "how_it_makes_money", "risks",
                 "first_7_days", "source_urls"],
}


def deep_dive_prompt(p: dict) -> str:
    return f"""Write a deep dive on this startup idea for a 2-3 person team in Bangladesh.

THE CARD THE OWNER ALREADY READ:
{p['card']}

WEB SEARCH: competitors in Bangladesh (names may ONLY come from here):
{p['search_bd']}

WEB SEARCH: similar startups that shut down or failed:
{p['search_fail']}

Fields (plain English, short, digits for numbers):
- competitors_bd: 2-4 sentences on who already does this in Bangladesh and how, ONLY from the search results.
- competitors: [{{name, url}}] only companies the search results show are active in Bangladesh; [] if none.
- failure_check: 1-3 sentences: what the shut-down search shows (who failed and why). If nothing relevant, say so.
- how_it_makes_money: 2-4 sentences with example prices in taka (৳) for Bangladesh, and in US dollars if
  there is a global version. Mark prices as "to test", not facts.
- risks: 3 short risks.
- first_7_days: a numbered 7-day plan, one line per day.
- source_urls: URLs from the search results you relied on."""


# ---------------------------------------------------------------- validation kit (Hydrogen; Qwen if notes)

KIT_SCHEMA = {
    "type": "object",
    "properties": {
        "interview": {"type": "array", "items": {
            "type": "object", "properties": {"bangla": {"type": "string"}, "english": {"type": "string"}},
            "required": ["bangla", "english"]}},
        "headline": {"type": "string"},
        "subhead": {"type": "string"},
        "call_to_action": {"type": "string"},
        "prices": {"type": "string"},
        "plan_7_days": {"type": "string"},
    },
    "required": ["interview", "headline", "subhead", "call_to_action", "prices", "plan_7_days"],
}


def kit_prompt(card: str, notes: str, market: str, shorter: bool, all_bangla: bool) -> str:
    style = []
    if shorter:
        style.append("Make everything much shorter: 3 interview questions, a 4-word headline, a 3-day plan.")
    if all_bangla:
        style.append("Write EVERY field in Bangla (Bengali script), including the plan; english glosses may stay.")
    return f"""Build a validation kit so a 2-3 person team can test this idea with real customers this week.

THE IDEA (card):
{card}

THE OWNER'S PRIVATE NOTES (may be empty):
{notes or '(none)'}

Market: {market}. {' '.join(style)}
Fields:
- interview: 5 questions to ask potential customers, in natural spoken Bangla, each with an English gloss.
  Ask about past behaviour and real spending, not opinions about the idea.
- headline, subhead, call_to_action: landing-page copy{' in Bangla' if market == 'Bangladesh' else ''}.
- prices: 2-3 price points to test, in taka (৳) for Bangladesh{' and US dollars for the global version' if market != 'Bangladesh' else ''}, marked "to test".
- plan_7_days: a numbered 7-day plan, one line per day, with a number that counts as a yes.
Use only the idea and notes above; no invented market facts."""


# ---------------------------------------------------------------- ask (Hydrogen; Qwen if private notes)

ASK_SCHEMA = {
    "type": "object",
    "properties": {"on_topic": {"type": "boolean"}, "answer": {"type": "string"}},
    "required": ["on_topic", "answer"],
}


def ask_prompt(question: str, context: str) -> str:
    return f"""The owner asked a question. You may ONLY answer questions about the startup card / saved idea below
(the idea, its market, how to build or test it, its competitors and risks). If the question is about anything
else (general knowledge, coding help, chit-chat, other topics), set on_topic to false and answer "".

CONTEXT:
{context}

QUESTION: {question}

Answer in at most 120 words, plain English, using only the context plus general reasoning about building and
testing the idea. If a fact is not in the context, say it is unknown."""


# ---------------------------------------------------------------- assumptions from private notes (Qwen only)

ASSUMPTIONS_SCHEMA = {
    "type": "object",
    "properties": {"assumptions": {"type": "array", "items": {
        "type": "object",
        "properties": {"text": {"type": "string"},
                       "status": {"type": "string", "enum": ["holding", "at_risk", "untested"]},
                       "evidence_for": {"type": "integer"}, "evidence_against": {"type": "integer"}},
        "required": ["text", "status", "evidence_for", "evidence_against"]}},
        "summary": {"type": "string"}},
    "required": ["assumptions", "summary"],
}


def assumptions_prompt(card: str, notes: list[str], current: list[dict]) -> str:
    cur = "\n".join(f"- {a['text']} [{a['status']}, +{a['evidence_for']}/-{a['evidence_against']}]" for a in current)
    nts = "\n".join(f"- {n}" for n in notes)
    return f"""Track the key assumptions behind this startup idea using the owner's private notes from customer tests.

THE IDEA (card):
{card[:2500]}

CURRENT ASSUMPTIONS:
{cur or '(none yet: create 4-6, e.g. "Clinic owners will pay ৳1,000 a month")'}

ALL NOTES (oldest first):
{nts}

Return the full updated list (keep wording stable; add new ones only if the notes raise them). For each:
status = holding (evidence supports it), at_risk (evidence against it), untested (no evidence yet);
evidence_for / evidence_against = how many notes support / contradict it.
summary: 1-2 sentences on what changed with the latest note."""


# ---------------------------------------------------------------- card fact gate (Flash-Lite)

FIX_SCHEMA = {
    "type": "object",
    "properties": {
        "unsupported_claims": {"type": "array", "items": {"type": "string"}},
        "what_they_do": {"type": "string"},
        "why_funded": {"type": "string"},
        "already_in_bangladesh": {"type": "string"},
        "score_check_note": {"type": "string"},
    },
    "required": ["unsupported_claims", "what_they_do", "why_funded", "already_in_bangladesh", "score_check_note"],
}


def fix_prompt(fields: dict, material: str) -> str:
    import json as _json

    return f"""You are the fact gate for a startup card. Below are the card's FACTUAL fields and ALL the material the
writer had. A fact is supported only if the material states it (funding, investors, accelerators like Y Combinator,
traction, team, what the company does, which companies operate in Bangladesh, market facts like regulations or
payment habits). Do not use your own knowledge.

1. unsupported_claims: list every claim in these fields that the material does not support ([] if none).
2. Return each field rewritten with ONLY the unsupported parts removed or replaced by "unknown"; keep everything
   else word for word. If nothing is unsupported, return the fields unchanged.

FIELDS:
{_json.dumps(fields, ensure_ascii=False, indent=1)}

MATERIAL:
{material}"""
