create table if not exists profiles (
  id uuid primary key,
  email text,
  updated_at timestamptz default now()
);

create table if not exists subscriptions (
  id bigint generated always as identity primary key,
  user_id uuid not null,
  email text,
  stripe_subscription_id text unique,
  status text not null default 'inactive',
  updated_at timestamptz default now()
);

alter table subscriptions add column if not exists stripe_customer_id text;
alter table subscriptions add column if not exists updated_at timestamptz default now();

create index if not exists subscriptions_user_id_idx on subscriptions(user_id);

create table if not exists usage_events (
  id bigint generated always as identity primary key,
  user_id uuid not null,
  event_type text not null default 'analysis',
  created_at timestamptz not null default now()
);

create index if not exists usage_events_user_created_idx on usage_events(user_id, created_at desc);

create table if not exists analysis_history (
  id bigint generated always as identity primary key,
  user_id uuid not null,
  job_title text,
  score integer,
  result_json jsonb not null,
  created_at timestamptz not null default now()
);

create index if not exists analysis_history_user_created_idx on analysis_history(user_id, created_at desc);

create table if not exists report_purchases (
  id bigint generated always as identity primary key,
  user_id uuid not null,
  email text,
  stripe_checkout_session_id text not null unique,
  stripe_customer_id text,
  stripe_payment_intent_id text,
  status text not null default 'paid',
  created_at timestamptz not null default now(),
  consumed_at timestamptz
);

alter table report_purchases enable row level security;

create index if not exists report_purchases_user_available_idx
on report_purchases(user_id, consumed_at, created_at desc);

create table if not exists analytics_events (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  user_id uuid null,
  email text null,
  event_name text not null,
  metadata jsonb not null default '{}'::jsonb
);

create index if not exists analytics_events_created_at_idx
on analytics_events(created_at desc);

create index if not exists analytics_events_event_name_idx
on analytics_events(event_name);

create index if not exists analytics_events_user_id_idx
on analytics_events(user_id);

create table if not exists lead_captures (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  email text not null,
  page_path text,
  page_type text,
  role_page text,
  source text,
  metadata jsonb not null default '{}'::jsonb
);

alter table lead_captures enable row level security;

create index if not exists lead_captures_created_at_idx
on lead_captures(created_at desc);

create index if not exists lead_captures_email_idx
on lead_captures(lower(email));

create index if not exists lead_captures_role_page_idx
on lead_captures(role_page);

alter table profiles
add column if not exists password_ready boolean not null default false;
