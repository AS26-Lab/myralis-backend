-- Myralis AI licensing schema for Supabase
-- Source of truth for users, licenses, usage, and credit ledger.
-- This SQL is intended to be run in the Supabase SQL Editor.

create extension if not exists pgcrypto;

create table if not exists public.app_users (
    id uuid primary key default gen_random_uuid(),
    email text unique not null,
    role text not null default 'client',
    created_at timestamptz default now(),
    constraint app_users_role_check check (role in ('admin', 'client', 'beta_tester'))
);

create table if not exists public.licenses (
    id uuid primary key default gen_random_uuid(),
    license_key text unique not null,
    user_id uuid references public.app_users(id) on delete cascade,
    client_name text,
    status text not null default 'active',
    credits_balance numeric not null default 0,
    plan_name text,
    expires_at timestamptz,
    developer_mode_allowed boolean default false,
    created_at timestamptz default now(),
    updated_at timestamptz default now(),
    constraint licenses_status_check check (status in ('active', 'expired', 'suspended', 'beta')),
    constraint licenses_credits_balance_check check (credits_balance >= 0)
);

create table if not exists public.usage_events (
    id uuid primary key default gen_random_uuid(),
    license_id uuid references public.licenses(id) on delete set null,
    user_id uuid references public.app_users(id) on delete set null,
    event_type text not null,
    question_text text,
    question_length int,
    answer_length int,
    openai_tokens int default 0,
    elevenlabs_chars int default 0,
    deepgram_seconds numeric default 0,
    credits_spent numeric default 0,
    model_used text,
    language text,
    metadata jsonb default '{}'::jsonb,
    created_at timestamptz default now()
);

create table if not exists public.credit_ledger (
    id uuid primary key default gen_random_uuid(),
    license_id uuid references public.licenses(id) on delete cascade,
    change_amount numeric not null,
    reason text not null,
    balance_after numeric not null,
    created_at timestamptz default now()
);

create table if not exists public.activation_codes (
    id uuid primary key default gen_random_uuid(),
    code text unique not null,
    status text not null default 'available',
    credits numeric not null default 0,
    plan_name text not null default 'Beta',
    client_name text,
    expires_at timestamptz,
    used_by_email text,
    used_by_name text,
    used_license_key text,
    used_at timestamptz,
    created_at timestamptz default now(),
    updated_at timestamptz default now(),
    constraint activation_codes_status_check check (status in ('available', 'used', 'expired', 'disabled')),
    constraint activation_codes_credits_check check (credits >= 0)
);

create or replace function public.set_licenses_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

create or replace function public.set_activation_codes_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists trg_licenses_updated_at on public.licenses;
create trigger trg_licenses_updated_at
before update on public.licenses
for each row
execute function public.set_licenses_updated_at();

drop trigger if exists trg_activation_codes_updated_at on public.activation_codes;
create trigger trg_activation_codes_updated_at
before update on public.activation_codes
for each row
execute function public.set_activation_codes_updated_at();

create index if not exists idx_app_users_email on public.app_users (email);
create index if not exists idx_licenses_license_key on public.licenses (license_key);
create index if not exists idx_licenses_user_id on public.licenses (user_id);
create index if not exists idx_usage_events_license_id on public.usage_events (license_id);
create index if not exists idx_usage_events_created_at on public.usage_events (created_at);
create index if not exists idx_credit_ledger_license_id on public.credit_ledger (license_id);
create index if not exists idx_activation_codes_code on public.activation_codes (code);
create index if not exists idx_activation_codes_status on public.activation_codes (status);

insert into public.app_users (email, role)
values ('mcstokerrap@gmail.com', 'admin')
on conflict (email) do nothing;

insert into public.licenses (
    license_key,
    user_id,
    client_name,
    status,
    credits_balance,
    plan_name,
    expires_at,
    developer_mode_allowed
)
values (
    'BETA-MYRALIS-001',
    (
        select id
        from public.app_users
        where email = 'mcstokerrap@gmail.com'
        limit 1
    ),
    'Myralis Internal Beta',
    'beta',
    100,
    'Beta',
    '2026-09-01',
    true
)
on conflict (license_key) do nothing;
