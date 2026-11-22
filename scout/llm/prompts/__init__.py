"""Neutral, evidence-grounded prompts. Outputs are Pydantic models."""

import json
from scout.llm.schemas import (
    Extraction as EXTRACT_SCHEMA,
    Classification as CLASSIFY_SCHEMA,
    Scoring as SCORE_SCHEMA,
)
from scout.llm.schemas import (
    Card as CARD_SCHEMA,
    DeepDive as DEEP_SCHEMA,
    Kit as KIT_SCHEMA,
    Ask as ASK_SCHEMA,
    Assumptions as ASSUMPTIONS_SCHEMA,
)
from scout.llm.schemas import Sector
from typing import get_args

SECTORS = list(get_args(Sector))

SYSTEM = 'You are a senior venture analyst at a startup studio. The studio adapts business models that have been proven\nelsewhere to Bangladesh, and builds companies in Bangladesh that sell to customers abroad. You assess\nopportunities in every sector (finance, logistics, health, education, agriculture, commerce, industry, climate,\nsoftware and consumer) with no preference for any technology, business model or company size.\n\nWrite plain, short English with normal capitalisation, numbers as digits and no hype. Write in English even\nwhen the sources are in another language.\n\nFacts (funding, what a company does, traction, competitors, regulations, market figures) may only come from the\nmaterial you are given. If a fact is not in the material, write "unknown". Your own estimates and reasoning are\nwelcome when they are clearly marked as estimates.'


def extract_prompt(articles: list[dict], today: str | None = None) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    today = today or datetime.now(ZoneInfo("Asia/Dhaka")).date().isoformat()
    lines = [
        "Read these news items and pull out startup facts. Rules:",
        '- Use ONLY what each item\'s text says. No facts from memory. Unknown text fields = "", unknown '
        "amount_usd = 0, unknown investors = [].",
        "- Translate everything to English. Keep company names as written (add the Latin-script name if the "
        "item gives one).",
        '- kind: "funding" = a startup raised money (pre-seed, seed, Series A..., venture debt, grant); '
        '"launch" = a young startup launching its first product or coming out of stealth (YC, Launch HN, new '
        'company); "other" = anything else: VC funds closing, IPOs, acquisitions, big or established companies '
        "(OpenAI, Google, banks, telecoms) releasing products, media outlets launching sections, policy, opinion, "
        'events. For "other" return no companies.',
        "- companies: only the startup(s) that raised money or launched, never the investors.",
        '- country: where the startup is based, only if the item says so (e.g. "Lagos-based" = Nigeria).',
        '- stage: Pre-seed, Seed, Series A, Series B..., Bridge, Debt, Grant, or "" if not stated.',
        '- amount_text: the money raised in this round (never the valuation), exactly as written ("₹50 crore", "$4M"). amount_usd: a number only if the '
        "item states a US-dollar amount (e.g. 4000000), else 0.",
        "- website: only if the item text contains it.",
        "- what_they_do: what the company sells or does (not the news event), one plain sentence, at most 20 words, "
        "from the item text.",
        "- business_model_hint: how it makes money, only if stated; else empty. customer_hint: who pays, only if stated; else empty. traction: stated users, revenue or growth only; else empty.",
        f"- Today is {today}. If an item reports an event from an earlier year (for example it calls a past year "
        '"upcoming", as in "eyes going public in 2022"), it is old news: kind = "other".',
        "Return one entry per item id.",
        "",
    ]
    for a in articles:
        lines.append(
            f"ITEM {a['id']} (source: {a['source']})\nTitle: {a['title']}\nText: {a['summary']}\n"
        )
    return "\n".join(lines)


CLASSIFY = 'Classify each startup using only the material. Do not judge whether it is a good idea.\n\n- product_known: true if the material names what kind of product or service it is, even in two words\n  ("e-scooter startup", "blood-testing startup", "battery maker", "flexible office provider"). false only if\n  it gives no category at all, or only an empty label ("a digital product", "a SaaS", "an app", "an AI\n  platform") that does not say what it does.\n- one_line: what the company sells or does, max 15 words, plain words; "" if unknown.\n- sector: the ONE sector that best describes the customer and market, not the technology. An AI tool for\n  clinics is "health"; a lending app for farmers is "fintech".\n- business_model:\n  software_subscription = software sold by subscription; marketplace = connects two sides (buyers and sellers,\n  workers and clients); financial_product = lending, payments, insurance, investing, banking; commerce = sells\n  physical goods (online shops, B2B distribution, groceries); tech_enabled_service = a service delivered by\n  people with software (logistics operators, clinics, tutoring, agencies); hardware_light = devices built from\n  existing parts (sensors, terminals); deep_tech = new chips, robots, biotech, energy or space technology;\n  infrastructure_api = APIs, developer tools, data or cloud infrastructure; media_content = media, content,\n  communities, games.\n- customer: who pays (consumers, small_businesses, large_businesses, government, developers, mixed).\n- ai_native: true only if the product itself IS AI (an AI agent, model or AI-first software). False if it\n  merely uses AI somewhere.\n- skip: true if the startup belongs to any of these categories: {skip_list}. skip_reason = the category.\n- search_phrase: 3-6 plain words for a web search for this kind of product (no company name).'
RUBRIC = 'Assess each startup as a source of business ideas for three tracks: Bangladesh, global and service. You are not rating the funded company. You\nare rating the best realistic adaptation of its business model.\n\nStep 1: write the concepts.\n- concept_bd: one sentence: the best Bangladesh version. Who pays, for what, and how they pay.\n- concept_global: one sentence: the best version a company based in Bangladesh could sell to customers abroad.\n  It is a PRODUCT the customer buys and uses themselves (software, an app, a subscription, goods).\n- concept_service: one sentence: done-for-you WORK a team in Bangladesh does for clients every month, delivered\n  entirely online through software (the team runs the software and the AI; the client receives the result).\n  Clients can be anywhere, mostly abroad. Who the client is, what job is done for them, and the monthly package.\n  Any industry. Write "none" if the work cannot be delivered online through software (on-site work, physical\n  goods, licensed professional sign-off that must be local).\n- If a market has no sensible version, write "none".\n\nStep 2: score every dimension from 1 to 10, each with a one-line reason. Base reasons on the material and on\nwell-known structural facts about the market (payment habits, regulation, infrastructure). Use the whole scale;\nin a typical batch most dimensions fall between 3 and 7.\n\nBangladesh:\n- demand: how many people or businesses in Bangladesh have this problem, and how badly. 9-10: a large group\n  with an acute, frequent problem who already pay for poor substitutes. 5-6: real but narrow or occasional.\n  1-3: rare in Bangladesh.\n- readiness: can customers adopt and pay today? Consider bKash, Nagad, bank transfer and cash on delivery;\n  smartphone and internet reach of this customer group; addresses, logistics and data. 9-10: fits today\'s\n  habits. 1-3: needs infrastructure or habits that do not exist yet.\n- competition_gap: room for a new entrant. Name competitors only if they appear in the material. 9-10: nobody\n  credible serves it. 5: some players with clear gaps. 1-3: strong incumbents with lock-in. With no search\n  results, score 5 and write "not checked yet".\n- regulatory_path: 10: nothing beyond a trade licence. 7: simple registrations. 5: workable through a licensed\n  partner (bank, mobile financial service, NBFI, hospital, insurer). 3: needs its own licence from Bangladesh\n  Bank, BTRC, BSEC, DGDA, IDRA or similar, with long timelines. 1: effectively not allowed.\n- economics: can it make money at Bangladeshi price levels? Compare what customers would pay with the cost to\n  acquire and serve them. 9-10: clear margins at local prices. 1-3: only works at rich-country prices.\n\nGlobal, sold from Bangladesh:\n- demand_abroad: evidence that customers abroad pay for this. The funding round is some evidence; stated\n  traction is stronger evidence.\n- remote_delivery: can it be sold and delivered from Bangladesh without staff in the customer\'s country?\n  Software, online sales and remote services score high; on-site work scores low.\n- cost_advantage: does a Bangladesh base give a real edge? Examples: labour-intensive delivery, engineering\n  cost, language fluency, garment and manufacturing networks. 1-3: no edge.\n- trust_burden: how easy it is to win and keep foreign customers. 10: self-serve purchase, light compliance.\n  5: needs sales calls, references or certifications. 1: regulated data, security audits, long procurement.\n- competition_gap: room abroad for a focused new entrant (a niche, region or customer size the funded company\n  ignores).\n\nService from Bangladesh (done-for-you work delivered through software; score the concept_service version):\n- buyer_breadth: how many potential clients worldwide have this need. Judge from the kind of client (for example\n  "every small e-commerce store" is broad, "satellite operators" is narrow). Do not guess prices. 9-10: hundreds of\n  thousands of businesses or more. 5-6: thousands. 1-3: a few hundred.\n- pain_budget: do these clients already pay people, agencies or tools to get this job done? 9-10: a standing\n  monthly cost today. 1-3: they rarely spend on it.\n- cost_advantage: can a Bangladesh team do this work well for much less than local providers?\n- reachability: can a remote team find and contact the buyers (directories, marketplaces, app stores,\n  associations, online communities, cold email)? 1-3: buyers are hard to identify or only buy through local\n  networks.\n- repeatable_revenue: is it the same job every month for many clients, sold as a fixed package or retainer?\n  9-10: identical monthly deliverable for every client. 1-3: one-off custom projects.\n- remote_delivery: can the whole service be delivered online through software, with no staff on site?\n- trust_burden: 10: clients buy on a short call or trial. 5: needs references, contracts or a pilot. 1: regulated\n  data, security audits or licensed sign-off.\n- competition_gap: room for a focused newcomer (a client size, region or job that current providers ignore).\n\nAll markets:\n- timing: what changed recently (technology, regulation, behaviour, cost) that makes this work now.\n- traction: numbers stated in the material about users, customers, revenue, orders or growth, else "unknown".\n  A funding round, its amount or a valuation is NOT traction. investor_signal: named lead investors or\n  accelerator, else "unknown".\n- what_it_takes: build_type, capital (bootstrap under $50k, seed $50k-$1M, venture over $1M), team size to\n  reach first paying customers, licence ("none" or which), time to first revenue. These describe the effort;\n  they are not scores.\n- service_what_it_takes: the same labels for the concept_service version (what it takes to start the service\n  from Bangladesh, usually tech_enabled_service). If concept_service is "none", copy what_it_takes.\n- ai_angle: one line only if AI clearly changes the cost or quality of the business; otherwise "".\n- confidence: high = clear product and market facts; medium = clear product, little else; low = thin material.\n\nRules:\n- Rate the adaptation, not the company\'s prestige. A famous investor or a big round is evidence of demand,\n  nothing more.\n- No preference for any sector, technology or company size. AI is neither a plus nor a minus.\n- Effort is not a penalty. An idea that needs a warehouse, a fleet, a licensed partner or $2M can still score 9\n  on demand and economics. Describe the effort in what_it_takes.\n- If the material does not say what the company sells, every dimension is at most 3 and confidence is low.\n\nReturn one entry per startup_id.'
CARD = 'Write an opportunity card. Market focus: {market}. Use only the material below for facts.\n\nSTARTUP FACTS / CLASSIFICATION / SCORES (already decided) / NEWS / COMPANY HOMEPAGE / WEB SEARCH RESULTS\n(competitor names may ONLY come from the search results)\n\nFields:\n- one_liner: the opportunity for this market in at most 15 words (the Bangladesh version for a Bangladesh card,\n  the version sold abroad for a global card). Say who it serves and what it offers there; do not copy the\n  company description.\n- what_they_do: 1-2 sentences from the homepage and news only.\n- evidence: 1-2 sentences: traction, investors and why it was funded, from the material only. "unknown" if\n  nothing is stated.\n- bangladesh_play: 2-4 sentences: target customer, the offer, a price in taka marked "to test", how customers\n  pay, how to reach the first 100 customers, and the licence route if one is needed.\n- global_play: 2 or 3 sentences: which customers abroad, the niche the funded company ignores, how to sell and\n  deliver from Bangladesh, and how to get paid (a merchant of record such as Paddle or Lemon Squeezy for\n  software, since Stripe and PayPal do not operate in Bangladesh; invoicing for services). Say plainly if the\n  global version is weak.\n- service_play: 2 or 3 sentences for the service version (or "" if the scores say concept_service is "none"):\n  which clients, the job the Bangladesh team does for them each month and the software it runs on, the monthly\n  package with a price in US dollars marked "to test", and how to reach the first clients remotely.\n- main_risk: the single biggest reason this could fail in this market, one sentence.\n- already_here: who already does this in Bangladesh, only from the search results. If none: "No one found in a\n  quick search."\n- competitors: only companies the search results show are active in Bangladesh, each with the exact result URL.\n  [] if none.\n- ai_angle: one line only if AI clearly changes the economics; otherwise "".\n- update: after reading the homepage and search results, give competition_gap and regulatory_path for this\n  market again (1-10, same scale as before) and a one-line note on what changed, or "" if nothing changed.\n  competition_gap is the room left for a new entrant: if the search results show more or stronger competitors\n  than "not checked yet" assumed, the number goes DOWN; it goes up only if the results show the market is empty.\n- source_urls: URLs from the material that support the facts you used.\nNever add facts that are not in the material.'
DEEP = 'Write a deep dive on this opportunity. THE CARD / SCORES WITH REASONS / SEARCH: competitors in Bangladesh /\nSEARCH: similar companies that shut down / SEARCH: the same model abroad.\n\nFields:\n- landscape: 2-4 sentences on who does this in Bangladesh and abroad, only from the material and search results.\n- competitors: [{name, url}] only companies the search results show; [] if none.\n- failure_check: 1-3 sentences on who tried and failed and why, from the search results; say so if nothing.\n- economics: 2-4 sentences: price points in taka (and US dollars for a global version) marked "to test", the\n  main costs, and a rough path to break-even. Every number here is an estimate; say so.\n- regulatory_route: the licence or registration needed and the realistic route (own licence or a licensed\n  partner), or "none needed".\n- first_customers: how to reach the first 100 paying customers (channels, partners, communities).\n- validation_30_days: a numbered plan, week 1 to week 4, each with one measurable target, ending with the\n  number that counts as a yes.\n- risks: 3 short risks.\n- source_urls: URLs from the search results you used.'


def classify_prompt(startups, skip_list):
    return (
        CLASSIFY.replace("{skip_list}", "; ".join(skip_list))
        + "\nAllowed sectors: "
        + ", ".join(SECTORS)
        + "\n"
        + json.dumps(startups, default=str)
    )


def score_prompt(startups):
    return (
        RUBRIC
        + "\nEvidence rules: Last-30-day revenue is not MRR or ARR unless the source explicitly says so. A funding round, a universal need or a few paying users alone does not justify demand_abroad 8-10; those need substantial evidence of paying demand. Without traction numbers, demand_abroad is at most 7. competition_gap and trust_burden are about the adaptation sold from Bangladesh: enterprise, finance, health or security buyers abroad mean sales calls, references and security reviews (trust_burden 3-5), even for software. Product-only material means medium confidence, thin material low. Never claim an estimate or regulatory route was verified unless the material verifies it.\nMATERIAL:\n"
        + json.dumps(startups, default=str)
    )


def card_prompt(p):
    return (
        CARD.replace("{market}", p["market"])
        + "\nProposed pricing, channels, niches and regulatory routes are estimates to validate. Do not assert that the funded company ignores a niche unless a source says so; describe it as a proposed niche. Keep local retail registration separate from any export permits needed by the global concept. Last-30-day revenue is not MRR. Search results showing several direct substitutes or free options must be reflected in the competition assessment; name the new evidence in the update note rather than claiming the market was verified.\nMATERIAL:\n"
        + json.dumps(p, default=str)
    )


def deep_dive_prompt(p):
    return (
        DEEP
        + "\nThe card plays, effort labels and prices are hypotheses, not independent evidence. Describe regulatory routes as expected routes to validate unless official search material confirms them. Competitor and failure names must appear in the supplied searches. Last-30-day revenue is not MRR.\nMATERIAL:\n"
        + json.dumps(p, default=str)
    )


def kit_prompt(
    card: str, notes: str, market: str, shorter: bool, all_bangla: bool
) -> str:
    style = []
    if shorter:
        style.append(
            "Make everything much shorter: 3 interview questions, a 4-word headline, a 3-day plan."
        )
    if all_bangla:
        style.append(
            "Write EVERY field in Bangla (Bengali script), including the plan; english glosses may stay."
        )
    return f"""Build a validation kit to test this idea with real customers this week.

THE IDEA (card):
{card}

THE OWNER'S PRIVATE NOTES (may be empty):
{notes or "(none)"}

Market: {market}. {" ".join(style)}
Fields:
- interview: 5 questions to ask potential customers, in natural spoken Bangla, each with an English gloss.
  Ask about past behaviour and real spending, not opinions about the idea.
- headline, subhead, call_to_action: landing-page copy{" in Bangla" if market == "Bangladesh" else ""}.
- prices: 2 or 3 price points to test, in taka (৳) for Bangladesh{" and US dollars for the global version" if market != "Bangladesh" else ""}, marked "to test".
- plan_7_days: a numbered 7-day plan, one line per day, with a number that counts as a yes.
Use only the idea and notes above; no invented market facts."""


# ---------------------------------------------------------------- ask (Hydrogen; Qwen if private notes)


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


def assumptions_prompt(card: str, notes: list[str], current: list[dict]) -> str:
    cur = "\n".join(
        f"- {a['text']} [{a['status']}, +{a['evidence_for']}/-{a['evidence_against']}]"
        for a in current
    )
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
