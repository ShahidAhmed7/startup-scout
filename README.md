# Startup Scout

**Turn the world's startup funding news into ready-to-test business ideas for Bangladesh — delivered to WhatsApp
every morning.**

Every funding round is a signal: an investor has just validated that a problem is real and worth paying to solve.
Startup Scout captures those signals from around the world, filters out what a small team can't build, and
translates the rest into concrete opportunities — for the Bangladeshi market and for selling globally from
Bangladesh.

---

## Why it matters

The best early-stage opportunities are often proven elsewhere first. The information exists, but it is scattered
across many publications, countries and languages, and none of it is framed for local realities such as mobile
wallet payments, Bangla-first users and Facebook-driven commerce.

Startup Scout does that research and adaptation automatically, so you start each day with a short, curated list of
ideas instead of an endless news feed.

---

## Key features

### Daily intelligence briefing
A morning WhatsApp digest with the strongest opportunities for Bangladesh, the strongest opportunities to build in
Bangladesh and sell abroad, additional ideas from other industries, and a snapshot of what was funded worldwide
overnight.

### Opportunity cards
Each pick opens a concise brief:
- What the company does and why investors backed it, with sources
- How a small local team could build a Bangladeshi version (payments, language, devices, licences)
- How the same idea could be sold internationally from Bangladesh
- Where AI can make it cheaper or better
- Who already operates in Bangladesh, found through live web research
- A practical plan to test demand within a week
- A transparent explanation of every score

### Dual-market scoring
Every startup is evaluated twice — for the local market and for global sales from Bangladesh — against clear
criteria: customer pain, timing, feasibility for a small team, cost to start and fit with your skills. Ideas that
require heavy capital, hardware, special licences or physical operations are flagged and filtered out early.

### Global coverage, balanced results
Coverage spans Y Combinator and the US, Europe, India, Pakistan, the Middle East, Africa, Southeast Asia, China and
Bangladesh, with non-English sources translated automatically. Daily picks are drawn from different industries so the
list reflects the whole market, not a single trend.

### Verified facts only
Every factual statement must come from a source the system has actually read. An automated fact check removes any
name or figure it cannot verify, and anything unknown is labelled as unknown.

### On-demand deep dives
One tap produces a full analysis: local competitors, comparable startups that failed and why, revenue model and
pricing in taka, key risks and a first-week action plan.

### Idea notebook and validation kits
Save promising ideas and track them from exploration to testing to decision. Generate a customer interview script in
Bangla with English translations, landing-page copy, price points to test and a launch plan.

### Private field notes
Record what customers tell you. Notes are processed only by a self-hosted open-source model and never sent to
third-party AI providers. The system tracks which of your assumptions are holding and which are at risk.

### Focused Q&A
Ask questions about any card or saved idea and get direct answers grounded in that material.

### Watchlists and alerts
Follow a sector, stage, country or specific company and receive an alert only when something new happens.

### Personalised ranking
Your saves and skips refine future recommendations over time, within limits that keep the results broad.

### Weekly and monthly reports
A weekly review of new Y Combinator companies and the most relevant ones, and a monthly view of what is being
funded in markets similar to Bangladesh.

### Private web archive
A secure, searchable archive of every card with filters for market, country, sector and score, plus a board view of
your notebook.

### Built for reliability
Multiple AI models back each other up, including a self-hosted fallback, so the daily briefing continues even when a
provider is unavailable. Built-in budget controls keep the system on free tiers.

---

## Architecture

### System overview

One serverless application on Modal runs the schedule, the daily pipeline, the WhatsApp webhook and a self-hosted fallback model; Supabase holds the state.

```text
  DATA SOURCES
  ┌────────────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────────┐
  │ Regional funding   │ │ YC directory │ │ Hacker News  │ │ Google News  │ │ TrustMRR         │
  │ feeds, worldwide   │ │ new batches  │ │ launches     │ │ by region    │ │ verified revenue │
  └─────────┬──────────┘ └──────┬───────┘ └──────┬───────┘ └──────┬───────┘ └────────┬─────────┘
            └───────────────────┴────────────────┼────────────────┴──────────────────┘
                                                 │  Firecrawl reads sites that block crawlers
                                                 │
  MODAL · SERVERLESS APPLICATION                 ▼
  ┌────────────────────┐  spawns  ┌──────────────────────────────┐  calls   ┌───────────────────────────┐
  │ tick               ├─────────►│ pipeline                     ├─────────►│ AI providers              │
  │ scheduler          │          │ collect · extract · merge    │          │ Gemini 4 Hydrogen: judge  │
  │ digest timing,     │          │ score · shortlist · research │          │ Gemini Flash-Lite: read   │
  │ reports, budget    │          │ cards · fact gate · digest   │          │ Gemma 4: fallback         │
  └─────────┬──────────┘          └──────────────┬───────────────┘          └─────────────┬─────────────┘
            │                                    │ reads / writes                         │ last resort
            │                                    ▼                                        ▼
            │                     ┌──────────────────────────────┐          ┌───────────────────────────┐
            │                     │ Supabase Postgres            │          │ Qwen on dedicated GPU     │
            │                     │ private schema · row-level   │          │ self-hosted · vLLM        │
            │                     │ security · pgvector          │          │ the only model for notes  │
            │                     └──────────────▲───────────────┘          └─────────────▲─────────────┘
            │                                    │ reads / writes                         │ private notes
            │                     ┌──────────────┴───────────────┐  spawns  ┌─────────────┴─────────────┐
            │                     │ web · FastAPI                ├─────────►│ message worker            │
            │                     │ signed webhook               │          │ commands · buttons ·      │
            │                     │ private web archive          │          │ notebook · watches        │
            │                     └──────────────▲───────────────┘          └─────────────┬─────────────┘
            │ morning digest                     │ signed events                          │ replies
            ▼                                    │                                        ▼
  ┌──────────────────────────────────────────────┴────────────────────────────────────────────────────┐
  │                        WHATSAPP CLOUD API  ◄──────────────►  OWNER'S PHONE                        │
  └───────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### Daily pipeline

Every stage is automated; AI is used only where judgement is needed, and two safety checks run before anything is sent.

```text
  ┌──────────────────────────────────────────────────────────────────────┐
  │ COLLECT      polite RSS and API fetching · conditional requests ·    │
  │              per-host rate limits · Firecrawl for blocked sites      │
  └───────────────────────────────────┬──────────────────────────────────┘
                                      ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │ PRE-FILTER   multilingual funding keywords, no AI cost               │
  └───────────────────────────────────┬──────────────────────────────────┘
                                      ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │ EXTRACT      Flash-Lite reads articles in batches on both keys,      │
  │              translates to English, pulls company, round, investors  │
  └───────────────────────────────────┬──────────────────────────────────┘
                                      ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │ MERGE        same website or name = one startup, one round per raise │
  └───────────────────────────────────┬──────────────────────────────────┘
                                      ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │ SCORE        Hydrogen scores every startup twice: Bangladesh and     │
  │              global · hard rubric caps · skip list · taste nudge     │
  └───────────────────────────────────┬──────────────────────────────────┘
                                      ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │ SHORTLIST    minimum score and feasibility · one pick per sector ·   │
  │              cap on AI-first picks · thin-information penalty        │
  └───────────────────────────────────┬──────────────────────────────────┘
                                      ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │ RESEARCH     live search for local competitors and past failures,    │
  │              company homepages                                       │
  └───────────────────────────────────┬──────────────────────────────────┘
                                      ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │ WRITE CARDS  Hydrogen, one card per pick, in parallel                │◄─────────────────┐
  └───────────────────────────────────┬──────────────────────────────────┘                  │
                                      ▼                                                     │
  ┌──────────────────────────────────────────────────────────────────────┐                  │
  │ FACT GATE    every name and figure must appear in the sources,       │                  │
  │              otherwise the sentence is removed                       │                  │
  └───────────────────────────────────┬──────────────────────────────────┘                  │
                                      ▼                                                     │
  ┌──────────────────────────────────────────────────────────────────────┐  score dropped   │
  │ RE-CHECK     re-score after research                                 ├──────────────────┘
  │                                                                      │  swap in reserve
  └───────────────────────────────────┬──────────────────────────────────┘
                                      ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │ DELIVER      digest · explore rows · worldwide funding snapshot ·    │
  │              watch alerts · quality audit                            │
  └──────────────────────────────────────────────────────────────────────┘
```

### AI routing and fallback

Private data never reaches a third-party provider, and the briefing keeps running when a provider is unavailable.

```text
                                ┌────────────────────────┐
                                │     AI job request     │
                                └────────────┬───────────┘
                                             ▼
                                ┌────────────────────────┐    yes    ┌──────────────────────────────┐
                                │ uses private notes?    ├──────────►│ Qwen on our own GPU          │
                                └────────────┬───────────┘           │ data never leaves our infra  │
                                             │ no                    └──────────────▲───────────────┘
                                             ▼                                      │
  ┌────────────────────────────────────────────────────────────────┐                │
  │ ASSIGNED MODEL                                                 │                │
  │ ┌──────────────────────┐   in parallel  ┌───────────────────┐  │                │
  │ │ main key             │◄──────────────►│ backup key        │  │                │
  │ └──────────────────────┘                └───────────────────┘  │                │
  │ daily caps · rate spacing · retries · schema validation · log  │                │
  └────────────────────────────────┬───────────────────────────────┘                │
                                   │ both keys fail                                 │
                                   ▼                                                │
                      ┌────────────────────────┐                                    │
                      │ Gemini Flash-Lite      │                                    │
                      └────────────┬───────────┘                                    │
                                   │ both keys fail                                 │
                                   ▼                                                │
                      ┌────────────────────────┐                                    │
                      │ Gemma 4                │                                    │
                      └────────────┬───────────┘                                    │
                                   │ both keys fail                                 │
                                   ▼                                                │
                      ┌────────────────────────┐    within budget                   │
                      │ budget guard           ├────────────────────────────────────┘
                      └────────────┬───────────┘
                                   │ over budget
                                   ▼
                      ┌────────────────────────┐
                      │ "skipped today" notice │
                      └────────────────────────┘
```

### WhatsApp message lifecycle

The webhook answers within seconds; the real work happens in an asynchronous worker, exactly once per message.

```text
Owner        WhatsApp API     web · FastAPI      Postgres          worker          AI router
  │                │                │                │                │                │
  │  message or button tap          │                │                │                │
  ├────────────────►                │                │                │                │
  │                │  POST /webhook, signed          │                │                │
  │                ├────────────────►                │                │                │
  │                │                ├─ verify signature, ignore other numbers          │
  │                │                │                │                │                │
  │                │                │  store once, by message id      │                │
  │                │                ├────────────────►                │                │
  │                │                │  move the 24-hour window        │                │
  │                │                ├────────────────►                │                │
  │                │  200 OK, within seconds         │                │                │
  │                ◄────────────────┤                │                │                │
  │                │                │  spawn async job                │                │
  │                │                ├─────────────────────────────────►                │
  │                │                │                │  claim the message exactly once │
  │                │                │                ◄────────────────┤                │
  │                │  deliver queued digest first    │                │                │
  │                ◄──────────────────────────────────────────────────┤                │
  │                │                │                │                │  deep dive, kit or answer
  │                │                │                │                ├────────────────►
  │                │                │                │                │  validated structured output
  │                │                │                │                ◄────────────────┤
  │                │                │                │  save card, idea, note or signal
  │                │                │                ◄────────────────┤                │
  │                │  reply: text, buttons or list   │                │                │
  │                ◄──────────────────────────────────────────────────┤                │
  │  delivered     │                │                │                │                │
  ◄────────────────┤                │                │                │                │
  │                │                │                │                │                │
```

**Technology:** Google Gemini for analysis, with Gemma and a self-hosted Qwen model as fallbacks; a serverless
application on Modal; PostgreSQL on Supabase with row-level security and pgvector; the WhatsApp Cloud API.

**Safeguards:** messages go only to the owner, webhooks are signature-verified and idempotent, WhatsApp's 24-hour
messaging window is enforced with a pending queue, private notes stay on self-hosted infrastructure, a budget guard
caps spending, and a daily quality audit checks scoring rules, duplicates, facts and links.

---

## Getting started

Requirements: free accounts with Google AI Studio, Meta for Developers (WhatsApp Cloud API), Supabase and Modal.

```bash
git clone https://github.com/ShahidAhmed7/startup-scout.git && cd startup-scout
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env                      # add your credentials
.venv/bin/python -m scout.db.migrate
.venv/bin/modal secret create scout-secrets --from-dotenv .env
.venv/bin/modal deploy app.py
.venv/bin/python -m scout.status          # verify the setup
```

Preview a full run without sending any messages:

```bash
.venv/bin/python -m scout.run --dry --dev
```

Customise the target market, skills, excluded industries and scoring weights in `config.yaml`, and the news sources
in `sources.yaml`.
