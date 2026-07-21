from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT_DIR / ".env"

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def _load_env_file_if_available() -> None:
    try:
        from dotenv import load_dotenv  # type: ignore[import-not-found]
    except ImportError:
        return
    if ENV_PATH.exists():
        load_dotenv(dotenv_path=ENV_PATH)


def _require_supabase_env() -> None:
    provider = str(os.getenv("MYRALIS_LICENSE_PROVIDER", "") or "").strip().lower()
    supabase_url = str(os.getenv("SUPABASE_URL", "") or "").strip()
    supabase_service_role_key = str(
        os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or ""
    ).strip()

    if provider != "supabase":
        print("MYRALIS_LICENSE_PROVIDER must be set to supabase")
        raise SystemExit(1)
    if not supabase_url or not supabase_service_role_key:
        print("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
        raise SystemExit(1)


def _print_ok(label: str) -> None:
    print(f"{label}: OK")


def _timestamp_token() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def main() -> int:
    _load_env_file_if_available()
    _require_supabase_env()

    from core.license_admin_service import LicenseAdminService  # noqa: E402
    from core.license_manager import LicenseManager  # noqa: E402

    token = _timestamp_token()
    activation_code_value = f"BETA-TEST-{token}"
    email = f"supabase-test+{token}@myralis.ai"
    name = "Supabase Test"

    admin_service = LicenseAdminService()
    manager = LicenseManager()

    activation_code = admin_service.create_activation_code(
        code=activation_code_value,
        credits=100,
        plan_name="BetaTest",
        client_name="Supabase Test",
        expires_at="2026-09-01",
    )
    assert activation_code["code"] == activation_code_value
    _print_ok("1. Create activation code")

    activation_result = admin_service.activate_code(
        code=activation_code_value,
        email=email,
        name=name,
    )
    assert bool(activation_result.get("ok")) is True
    license_key = str(activation_result.get("license_key") or "").strip()
    assert license_key
    assert float(activation_result.get("credits", 0)) == 100.0
    _print_ok("2. Activate code")

    validation_result = manager.validate_license(email, license_key)
    assert validation_result.can_launch is True
    assert validation_result.status == "beta"
    assert validation_result.plan_name == "BetaTest"
    assert float(validation_result.credits_balance) == 100.0
    _print_ok("3. Validate created license")

    deduction_result = manager.deduct_credits(
        license_key,
        10,
        "supabase_test_usage",
    )
    assert bool(deduction_result.get("ok")) is True
    assert float(deduction_result.get("credits_balance", -1)) == 90.0
    _print_ok("4. Deduct credits")

    validation_after_deduction = manager.validate_license(email, license_key)
    assert validation_after_deduction.can_launch is True
    assert float(validation_after_deduction.credits_balance) == 90.0
    _print_ok("5. Revalidate after deduction")

    activation_code_after = admin_service.get_activation_code(activation_code_value)
    assert activation_code_after is not None
    assert str(activation_code_after.get("status")) == "used"
    assert str(activation_code_after.get("used_by_email")) == email
    assert str(activation_code_after.get("used_license_key")) == license_key
    _print_ok("6. Verify activation code usage")

    print("All Supabase licensing tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
