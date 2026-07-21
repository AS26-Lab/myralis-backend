from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT_DIR / ".env"


def _load_env_file_if_available() -> None:
    try:
        from dotenv import load_dotenv  # type: ignore[import-not-found]
    except ImportError:
        return
    if ENV_PATH.exists():
        load_dotenv(dotenv_path=ENV_PATH)


def _short_error(exc: Exception) -> str:
    message = str(exc).strip()
    if not message:
        message = exc.__class__.__name__
    return message.splitlines()[0][:200]


def _get_supabase_client() -> Any:
    try:
        from supabase import create_client  # type: ignore[import-not-found]
    except ImportError:
        print("Missing dependency: supabase. Run: pip install supabase")
        raise SystemExit(1)

    supabase_url = str(os.getenv("SUPABASE_URL", "") or "").strip()
    supabase_service_role_key = str(
        os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or ""
    ).strip()
    if not supabase_url or not supabase_service_role_key:
        print("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
        raise SystemExit(1)

    return create_client(supabase_url, supabase_service_role_key)


def _check_table(client: Any, table_name: str, failures: list[str]) -> None:
    try:
        client.table(table_name).select("*").limit(1).execute()
        print(f"OK table {table_name}")
    except Exception as exc:
        error = _short_error(exc)
        print(f"FAIL table {table_name}: {error}")
        failures.append(f"table {table_name}: {error}")


def _first_row(response: Any) -> dict[str, Any] | None:
    data = getattr(response, "data", None)
    if isinstance(data, list) and data:
        first = data[0]
        return first if isinstance(first, dict) else None
    if isinstance(data, dict):
        return data
    return None


def main() -> int:
    _load_env_file_if_available()

    client = _get_supabase_client()
    failures: list[str] = []

    for table_name in (
        "app_users",
        "licenses",
        "usage_events",
        "credit_ledger",
        "activation_codes",
    ):
        _check_table(client, table_name, failures)

    try:
        admin_response = (
            client.table("app_users")
            .select("email,role")
            .eq("email", "mcstokerrap@gmail.com")
            .limit(1)
            .execute()
        )
        admin_row = _first_row(admin_response)
        if not admin_row:
            failures.append("admin seed missing")
            print("FAIL admin seed: missing")
        elif str(admin_row.get("role", "")).strip() != "admin":
            failures.append("admin seed role mismatch")
            print("FAIL admin seed: role mismatch")
        else:
            print("OK admin seed")
    except Exception as exc:
        error = _short_error(exc)
        failures.append(f"admin seed: {error}")
        print(f"FAIL admin seed: {error}")

    try:
        license_response = (
            client.table("licenses")
            .select("license_key,status,credits_balance,developer_mode_allowed")
            .eq("license_key", "BETA-MYRALIS-001")
            .limit(1)
            .execute()
        )
        license_row = _first_row(license_response)
        if not license_row:
            failures.append("beta seed missing")
            print("FAIL beta seed: missing")
        else:
            status_ok = str(license_row.get("status", "")).strip() == "beta"
            credits_ok = float(license_row.get("credits_balance", 0)) == 100.0
            dev_ok = bool(license_row.get("developer_mode_allowed")) is True
            if status_ok and credits_ok and dev_ok:
                print("OK beta seed")
            else:
                failures.append("beta seed mismatch")
                print("FAIL beta seed: mismatch")
    except Exception as exc:
        error = _short_error(exc)
        failures.append(f"beta seed: {error}")
        print(f"FAIL beta seed: {error}")

    if failures:
        print("Schema verification failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("All Supabase schema checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
