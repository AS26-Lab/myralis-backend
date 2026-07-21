# Licensing Schema

This directory documents the initial Supabase schema for Myralis AI licensing.

## What each table is for

- `app_users`: canonical users for the licensing system.
- `licenses`: one or more licenses attached to a user, including status, credits, plan, and developer-mode permission.
- `usage_events`: usage tracking for conversations and consumable actions.
- `credit_ledger`: immutable credit changes for auditing and reconciliation.

## Source of truth

Supabase is the source of truth for:

- user identity
- license identity
- license status
- credit balance
- developer-mode permission
- usage history
- billing/audit ledger

The local file `config/license_state.json` is only a cache for convenience. It must not be treated as secure authority.

## Important rule

`role` and any admin-like permission must never be decided from the launcher alone. The launcher can store local convenience state, but the authoritative decision must come from Supabase in a later stage.

## How to run the SQL

1. Open your Supabase project.
2. Go to the SQL Editor.
3. Paste the contents of `supabase_schema.sql`.
4. Run the script.
5. Verify the tables, indexes, trigger, and example inserts were created.

## How to create more beta licenses manually

1. Insert or reuse an `app_users` row for the customer email.
2. Insert a `licenses` row with:
   - `status = 'beta'`
   - `credits_balance = 100` or your chosen amount
   - `plan_name = 'Beta'`
   - `developer_mode_allowed = true` only for trusted internal accounts
3. Keep `license_key` unique.
4. Link the license to the user with `user_id`.

## Verify Supabase schema

PowerShell:

```powershell
$env:MYRALIS_LICENSE_PROVIDER="supabase"
python scripts\verify_supabase_schema.py
```

Your `.env` should contain:

```env
SUPABASE_URL=...
SUPABASE_SERVICE_ROLE_KEY=...
MYRALIS_LICENSE_PROVIDER=supabase
```

## What is missing for the next stage

The next step is to build the Python Supabase client layer that:

- validates a license against Supabase
- reads canonical user and license state
- synchronizes local cache from remote truth
- records usage events into Supabase
- updates credits through the ledger
