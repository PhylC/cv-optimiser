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
