<div align="center">

# Startup Scout

### Turn the world's startup funding news into ready-to-test business ideas for Bangladesh

**Delivered to WhatsApp every morning, researched, scored and fact-checked.**

<img alt="Python" src="https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white"> <img alt="Google Gemini" src="https://img.shields.io/badge/Google%20Gemini-8E75B2?style=for-the-badge&logo=googlegemini&logoColor=white"> <img alt="Modal" src="https://img.shields.io/badge/Modal-000000?style=for-the-badge&logo=modal&logoColor=white"> <img alt="Supabase" src="https://img.shields.io/badge/Supabase-3FCF8E?style=for-the-badge&logo=supabase&logoColor=white"> <img alt="WhatsApp Cloud API" src="https://img.shields.io/badge/WhatsApp%20Cloud%20API-25D366?style=for-the-badge&logo=whatsapp&logoColor=white"> <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white"> <img alt="vLLM" src="https://img.shields.io/badge/vLLM-FF6F00?style=for-the-badge&logo=nvidia&logoColor=white">

</div>

<br>

> [!IMPORTANT]
> **Every funding round is a signal.** An investor has just validated that a problem is real and worth paying to solve.
> Startup Scout captures those signals from around the world, filters out what a small team can't build, and turns
> the rest into concrete opportunities for the Bangladeshi market and for selling globally from Bangladesh.

## Why it matters

> [!NOTE]
> The best early-stage opportunities are often proven somewhere else first. The evidence exists, but it is scattered
> across many publications, countries and languages, and none of it accounts for local realities: mobile-wallet
> payments, Bangla-first users and Facebook-driven commerce.
>
> Startup Scout does the research and the local adaptation automatically, so each day starts with a short, curated
> list of ideas instead of an endless news feed.

## What it does

### Discover
<table>
<tr><td width="50%" valign="top"><b>Daily intelligence briefing</b><br>A morning WhatsApp digest with the strongest local opportunities, the strongest opportunities to sell abroad, extra ideas from other industries and a snapshot of what was funded worldwide overnight.</td><td width="50%" valign="top"><b>Global coverage</b><br>Y Combinator and the US, Europe, India, Pakistan, the Middle East, Africa, Southeast Asia, China and Bangladesh. Non-English sources are translated automatically.</td></tr>
<tr><td width="50%" valign="top"><b>Balanced results</b><br>Picks come from different industries, with a cap on AI-first companies, so the list reflects the whole market rather than one trend.</td><td width="50%" valign="top"><b>Watchlists and alerts</b><br>Follow a sector, stage, country or company and get notified only when something new happens.</td></tr>
</table>

### Decide
<table>
<tr><td width="50%" valign="top"><b>Opportunity cards</b><br>A concise brief for every pick: what the company does, why investors backed it, the Bangladesh version, the global angle, the AI angle and a one-week test plan.</td><td width="50%" valign="top"><b>Dual-market scoring</b><br>Every startup is scored for the Bangladeshi market and for global sales from Bangladesh: pain, timing, feasibility, cost to start and fit with your skills.</td></tr>
<tr><td width="50%" valign="top"><b>On-demand deep dives</b><br>Local competitors, comparable startups that failed and why, a revenue model with pricing in taka, key risks and a first-week plan.</td><td width="50%" valign="top"><b>Verified facts only</b><br>Every name and figure is checked against the sources the system actually read. Anything unverified is removed or marked unknown.</td></tr>
</table>

### Validate
<table>
<tr><td width="50%" valign="top"><b>Idea notebook</b><br>Save ideas and move them from exploring to testing to a decision, with your reasons recorded.</td><td width="50%" valign="top"><b>Validation kits</b><br>A customer interview script in Bangla with English translations, landing-page copy, price points to test and a launch plan.</td></tr>
<tr><td width="50%" valign="top"><b>Private field notes</b><br>Notes are processed only by a self-hosted open-source model. The system tracks which assumptions are holding and which are at risk.</td><td width="50%" valign="top"><b>Focused Q&A</b><br>Ask questions about any card or saved idea and get answers grounded in that material.</td></tr>
</table>

### Operate
<table>
<tr><td width="50%" valign="top"><b>Personalised ranking</b><br>Saves and skips refine future recommendations, within limits that keep results broad.</td><td width="50%" valign="top"><b>Weekly and monthly reports</b><br>New Y Combinator companies worth a look every week; funding trends in markets like Bangladesh every month.</td></tr>
<tr><td width="50%" valign="top"><b>Private web archive</b><br>A secure, searchable archive of every card with filters, plus a board view of your notebook. Sign in from your phone with one tap.</td><td width="50%" valign="top"><b>Built for reliability</b><br>Multiple AI models back each other up, including a self-hosted fallback, with budget controls that keep it on free tiers.</td></tr>
</table>


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

## Technology and safeguards

<table>
<tr><td width="50%" valign="top"><b>Technology</b><br>
Google Gemini for analysis, with Gemma and a self-hosted Qwen model as fallbacks · serverless application on Modal ·
PostgreSQL on Supabase with row-level security and pgvector · WhatsApp Cloud API · FastAPI · vLLM</td>
<td width="50%" valign="top"><b>Safeguards</b><br>
Messages go only to the owner · signed, idempotent webhooks · WhatsApp's messaging window enforced with a pending
queue · private notes stay on self-hosted infrastructure · budget guard · daily quality audit</td></tr>
</table>

## Getting started

> [!TIP]
> You need free accounts with **Google AI Studio**, **Meta for Developers** (WhatsApp Cloud API), **Supabase** and **Modal**.

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

> [!TIP]
> Customise the target market, skills, excluded industries and scoring weights in `config.yaml`, and the news
> sources in `sources.yaml`.
