# 🚀 Startup Scout

## Wake up to tomorrow's big startup ideas — before anyone in your city builds them.

**While you sleep, an AI analyst reads the world's startup funding news, throws away everything you can't build,
and drops the best ideas for Bangladesh straight into your WhatsApp. Every. Single. Morning.**

> Venture capital firms pay full-time analysts to do this. Startup Scout does it on autopilot — for free.

---

## 🤯 The secret hiding in plain sight

Every time an investor funds a startup, they're handing you **free market research**: proof that a problem is real,
painful and worth paying for.

The catch? That proof is buried across news sites in different countries and languages — and none of it tells you
whether the idea works **here**, where customers pay with bKash, read Bangla and shop on Facebook.

**Startup Scout digs it out, translates it, judges it and hands you the winners. Automatically.**

---

## ✨ Features that feel like having a VC analyst in your pocket

### ☀️ The Morning Drop
Wake up to a WhatsApp digest with your top picks for Bangladesh, your top picks to sell to the world, extra
"More to explore" ideas from other industries, and a snapshot of what got funded around the globe overnight.
One tap opens any idea.

### 🃏 Instant Idea Cards
Every pick becomes a one-page battle plan:
- **What they do & why investors bet on it** — straight from the sources, with links
- **🇧🇩 The Bangladesh version** — how a tiny team could build it here with bKash, Nagad, Bangla and low-cost phones
- **🌍 The global angle** — how to sell it abroad from Dhaka, no Stripe required
- **🤖 The AI angle** — how AI makes it cheaper, faster or smarter
- **🔍 Who's already doing it in Bangladesh** — real local competitors found by live web search
- **🧪 Test it this week** — concrete steps to find out if people will actually pay
- **📊 Why these scores** — every score explained, then honestly re-checked after research

### 🎯 Double Scoring That Thinks Like a Local Founder
Every startup is judged twice: *Could you build it for Bangladesh?* and *Could you build it in Bangladesh and sell
it to the world?* It knows which ideas need a central bank licence, medical approval or a telecom permit — and which
need factories, robots or chips — and kills them before they waste your time.

### 🌐 The Whole World, Not One Bubble
Y Combinator and Silicon Valley first — plus Europe, India, Pakistan, the Middle East, Africa, Southeast Asia, China
(translated automatically) and Bangladesh. Picks are spread across fintech, commerce, health, logistics, education
and more, so you never get the same idea in five disguises.

### 🔎 One-Tap Deep Dives
Tap **More** and get the full investigation: who already competes locally, who tried this before and failed, how it
could make money in taka, the biggest risks and a day-by-day launch plan for your first week.

### 🔒 Zero-Hallucination Fact Gate
Every fact must come from a source the bot actually read. A built-in fact checker verifies names and figures against
the sources and deletes anything it can't prove. When it doesn't know, it says **"unknown"** — never a guess.

### 📓 Your Idea Notebook
Save the ideas you love and move them through **Exploring → Testing → Promising → Dropped**, with the reasons
remembered.

### 🧪 Validation Kits on Demand
Get a ready-to-use customer interview script in Bangla (with English translations), landing-page headlines, prices to
test and a launch plan — in one tap.

### 🛡️ Private Notes That Never Leave Your Control
Log what customers tell you. Your notes are processed only by an open-source model running on your own cloud server —
never sent to big tech — and the bot tracks which of your assumptions are holding up and which are at risk.

### 💬 Ask Anything About Your Ideas
Question any card or saved idea and get a straight answer. (It stays focused: it only talks about your ideas.)

### 👀 Watch Alerts
Tell it what to watch — a sector, a stage, a country or a single company — and it pings you only when something new
happens. No noise.

### 🧠 It Learns Your Taste
Every Save and Skip teaches it what you like, and it quietly sharpens future picks — without trapping you in a bubble.

### 📅 Weekly & Monthly Intelligence Reports
Every Friday: the newest Y Combinator startups and the ones worth your attention. Every month: what's getting
funded in markets like Bangladesh and what the bot has learned about you.

### 🗂️ Your Private Idea Vault
A password-protected web archive of every card ever sent — searchable and filterable by market, country, sector and
score — plus your notebook as a visual board. Sign in from your phone with one tap.

### ⚙️ Control It by Chat
Change your delivery time, the mix of local and global picks, the industries to skip and your skills — just by
sending a WhatsApp message.

### 🔁 Never-Miss-a-Morning Reliability
Multiple AI models back each other up. If one goes down, the next takes over — all the way to a self-hosted model on
your own server — so the digest keeps coming.

### 💸 Free to Run
Built entirely on free tiers, with a built-in budget guard so it can never surprise you with a bill.

---

## 🏗️ How the magic works

```
 Funding news from around the world
        │
        ▼
 Collect ──► Translate & extract ──► Remove duplicates ──► Score for 🇧🇩 and 🌍
                                                                  │
                                                                  ▼
 WhatsApp ◄── Morning digest ◄── Fact gate ◄── Idea cards ◄── Live research
```

- **The brains:** Google Gemini for judgement and reading, with open-source Gemma and Qwen as backups.
- **The body:** one serverless app on Modal, a Postgres database on Supabase and the WhatsApp Cloud API.
- **The guard rails:** messages only to the owner, verified webhooks, WhatsApp's free-messaging rules respected,
  a budget guard and a daily quality audit.

---

## 🚀 Run your own Startup Scout

You'll need free accounts on Google AI Studio, Meta for Developers (WhatsApp), Supabase and Modal.

```bash
git clone https://github.com/ShahidAhmed7/startup-scout.git && cd startup-scout
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env                      # add your keys
.venv/bin/python -m scout.db.migrate
.venv/bin/modal secret create scout-secrets --from-dotenv .env
.venv/bin/modal deploy app.py
.venv/bin/python -m scout.status          # everything should be green
```

Try a full run without sending anything:

```bash
.venv/bin/python -m scout.run --dry --dev
```

Make it yours: pick your market, skills, industries to skip and scoring weights in `config.yaml`, and your news
sources in `sources.yaml`.

---

### ⭐ The next big startup in Bangladesh is being funded somewhere else today.
### Startup Scout makes sure you're the first to know.
