-- Startup Scout schema. Safe to run many times (python -m scout.db.migrate).
-- Tables live in a private schema "scout" that Supabase's public REST API does not expose.
-- Row level security is also switched on with no policies, so the anon/authenticated API
-- roles can never read anything. Our own connection (the postgres owner role) is unaffected.

create schema if not exists scout;
set search_path to scout, public;

create table if not exists sources (
  id               text primary key,          -- same id as in sources.yaml
  name             text not null,
  url              text not null,
  kind             text not null,
  region           text,
  status           text not null default 'active',
  last_ok_at       timestamptz,
  last_nonempty_at timestamptz,
  fail_count       int not null default 0,
  etag             text,
  last_modified    text,
  updated_at       timestamptz not null default now()
);

create table if not exists items (
  id           bigserial primary key,
  source_id    text references sources(id),
  url          text not null unique,
  title        text,
  summary      text,
  lang         text,
  published_at timestamptz,
  fetched_at   timestamptz not null default now(),
  raw_json     jsonb
);
create index if not exists items_fetched_at_idx on items(fetched_at);

create table if not exists startups (
  id            bigserial primary key,
  name          text not null,
  norm_name     text not null,
  domain        text,
  country       text,
  description   text,
  category      text,
  first_seen_at timestamptz not null default now()
);
create unique index if not exists startups_domain_uq on startups(domain) where domain is not null;
create index if not exists startups_norm_name_idx on startups(norm_name);

create table if not exists rounds (
  id           bigserial primary key,
  startup_id   bigint not null references startups(id) on delete cascade,
  stage        text,
  amount_usd   numeric,
  investors    text[] not null default '{}',
  announced_at date,
  source_urls  text[] not null default '{}',
  created_at   timestamptz not null default now()
);

create table if not exists scores (
  id             bigserial primary key,
  startup_id     bigint not null references startups(id) on delete cascade,
  model_id       text not null,
  scores_json    jsonb not null,
  reasons_json   jsonb not null,
  money_to_start text,
  ai_angle       text,
  confidence     text,
  created_at     timestamptz not null default now()
);

create table if not exists skipped (
  id         bigserial primary key,
  startup_id bigint references startups(id) on delete cascade,
  category   text,
  reason     text,
  created_at timestamptz not null default now()
);

create table if not exists cards (
  id          bigserial primary key,
  startup_id  bigint references startups(id) on delete cascade,
  kind        text not null check (kind in ('card', 'deep_dive', 'kit')),
  body        text not null,
  source_urls text[] not null default '{}',
  model_id    text,
  created_at  timestamptz not null default now()
);

create table if not exists digests (
  id        bigserial primary key,
  date      date not null unique,
  card_ids  bigint[] not null default '{}',
  body_json jsonb,
  status    text not null check (status in ('prepared', 'sent', 'pending', 'skipped')),
  sent_at   timestamptz
);

create table if not exists messages (
  id            bigserial primary key,
  direction     text not null check (direction in ('in', 'out')),
  wa_message_id text unique,                 -- idempotency: each WhatsApp message handled once
  type          text,
  body          text,
  created_at    timestamptz not null default now()
);

create table if not exists owner_state (
  id                    int primary key default 1 check (id = 1),   -- exactly one row
  last_owner_message_at timestamptz,
  last_opened_card_id   bigint,
  pending_queue_json    jsonb not null default '[]'
);
insert into owner_state (id) values (1) on conflict (id) do nothing;

create table if not exists settings (
  key        text primary key,
  value      jsonb not null,
  updated_at timestamptz not null default now()
);

create table if not exists ideas (
  id          bigserial primary key,
  startup_id  bigint references startups(id) on delete set null,
  status      text not null default 'exploring',
  drop_reason text,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

-- Private: only ever sent to Qwen on Modal, never to Gemini.
create table if not exists notes (
  id         bigserial primary key,
  idea_id    bigint not null references ideas(id) on delete cascade,
  text       text not null,
  created_at timestamptz not null default now()
);

create table if not exists assumptions (
  id               bigserial primary key,
  idea_id          bigint not null references ideas(id) on delete cascade,
  text             text not null,
  status           text not null default 'untested',
  evidence_for     int not null default 0,
  evidence_against int not null default 0
);

create table if not exists watches (
  id         bigserial primary key,
  query      text not null,
  kind       text not null,
  created_at timestamptz not null default now(),
  active     boolean not null default true
);

create table if not exists signals (
  id         bigserial primary key,
  card_id    bigint references cards(id) on delete cascade,
  kind       text not null check (kind in ('save', 'skip', 'more')),
  created_at timestamptz not null default now()
);

create table if not exists usage (
  id          bigserial primary key,
  provider    text not null,                 -- gemini | qwen
  model_id    text,
  key_label   text,                          -- main | backup (which Gemini key served the call)
  kind        text,                          -- extract | score | card | ask | test ...
  tokens_in   int,
  tokens_out  int,
  gpu_seconds numeric,
  status      text not null,                 -- ok | quota | auth | server | network | bad_output | bad_request | cap
  created_at  timestamptz not null default now()
);
create index if not exists usage_created_at_idx on usage(created_at);

-- One row per pipeline run / cron / health ping, for `python -m scout.status`.
create table if not exists runs (
  id          bigserial primary key,
  kind        text not null,
  started_at  timestamptz not null default now(),
  finished_at timestamptz,
  status      text not null default 'running',
  stats_json  jsonb not null default '{}',
  error       text
);
create index if not exists runs_kind_started_idx on runs(kind, started_at desc);

-- YC companies already seen, to find new ones by diffing.
create table if not exists yc_seen (
  slug          text primary key,
  name          text,
  batch         text,
  first_seen_at timestamptz not null default now()
);

-- Lock down: RLS on every table, and no access for Supabase's API roles.
do $$
declare t text;
begin
  for t in select tablename from pg_tables where schemaname = 'scout' loop
    execute format('alter table scout.%I enable row level security', t);
  end loop;
  if exists (select 1 from pg_roles where rolname = 'anon') then
    execute 'revoke all on schema scout from anon, authenticated';
    execute 'revoke all on all tables in schema scout from anon, authenticated';
  end if;
end $$;

-- ---------- Phase 1 additions ----------
alter table items    add column if not exists kind text;              -- funding | launch | other | prefiltered | yc
alter table items    add column if not exists extracted_at timestamptz;
alter table startups add column if not exists website text;
alter table startups add column if not exists yc_batch text;
alter table startups add column if not exists last_seen_at timestamptz not null default now();
alter table rounds   add column if not exists amount_text text;
alter table scores   add column if not exists run_date date;
alter table scores   add column if not exists category text;
alter table scores   add column if not exists skip boolean not null default false;
alter table scores   add column if not exists rank_bd numeric;
alter table scores   add column if not exists rank_global numeric;
alter table cards    add column if not exists run_date date;
alter table cards    add column if not exists market text;            -- bangladesh | global
alter table cards    add column if not exists title text;
alter table cards    add column if not exists data_json jsonb;

-- Which items mention which startup (sources + buzz).
create table if not exists startup_items (
  startup_id bigint not null references startups(id) on delete cascade,
  item_id    bigint not null references items(id) on delete cascade,
  primary key (startup_id, item_id)
);

-- Cached ddgs searches and fetched homepages (be polite: don't repeat requests).
create table if not exists search_cache (
  query        text primary key,
  results_json jsonb not null,
  fetched_at   timestamptz not null default now()
);

do $$
declare t text;
begin
  for t in select tablename from pg_tables where schemaname = 'scout' loop
    execute format('alter table scout.%I enable row level security', t);
  end loop;
  if exists (select 1 from pg_roles where rolname = 'anon') then
    execute 'revoke all on all tables in schema scout from anon, authenticated';
  end if;
end $$;

-- ---------- source health ----------
alter table sources add column if not exists first_seen_at timestamptz not null default now();
alter table sources add column if not exists notified_at timestamptz;   -- when the owner was told it is down

-- ---------- phase 5 ----------
create table if not exists lane_seen (
  lane          text not null,
  key           text not null,
  first_seen_at timestamptz not null default now(),
  primary key (lane, key)
);
create extension if not exists vector with schema extensions;
alter table startups add column if not exists embedding extensions.vector(768);
alter table ideas    add column if not exists competitor_urls jsonb not null default '[]';
alter table ideas    add column if not exists last_scores jsonb;
alter table scores   add column if not exists taste numeric;

do $$
declare t text;
begin
  for t in select tablename from pg_tables where schemaname = 'scout' loop
    execute format('alter table scout.%I enable row level security', t);
  end loop;
  if exists (select 1 from pg_roles where rolname = 'anon') then
    execute 'revoke all on all tables in schema scout from anon, authenticated';
  end if;
end $$;
