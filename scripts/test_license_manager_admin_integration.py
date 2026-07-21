from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

os.environ["MYRALIS_LICENSE_PROVIDER"] = "mock"

from core.license_admin_service import LicenseAdminService  # noqa: E402
from core.license_manager import LicenseManager  # noqa: E402


STATE_PATH = ROOT_DIR / "config" / "license_admin_mock_state.json"


def _print_ok(label: str) -> None:
    print(f"{label}: OK")


def main() -> int:
    STATE_PATH.unlink(missing_ok=True)

    print("Running license manager/admin integration tests...")

    admin_service = LicenseAdminService()
    manager = LicenseManager()

    user = admin_service.create_user("integration@test.com", "client")
    assert user["email"] == "integration@test.com"
    assert user["role"] == "client"
    _print_ok("1. Create user")

    license_info = admin_service.create_license(
        email="integration@test.com",
        client_name="Museo Integration",
        plan_name="Beta",
        credits=100,
        status="beta",
        developer_mode_allowed=False,
        expires_at="2026-09-01",
    )
    license_key = str(license_info["license_key"])
    assert license_key
    _print_ok("2. Create license")

    result = manager.validate_license("integration@test.com", license_key)
    assert result.can_launch is True
    assert result.email == "integration@test.com"
    assert result.credits_balance == 100.0
    assert result.status == "beta"
    assert result.plan_name == "Beta"
    assert result.role == "client"
    assert result.developer_mode_enabled is False
    _print_ok("3. Validate created license")

    admin_service.change_role(email="integration@test.com", role="admin")
    result_admin = manager.validate_license("integration@test.com", license_key)
    assert result_admin.can_launch is True
    assert result_admin.developer_mode_enabled is True
    assert manager.is_debug_allowed(result_admin) is True
    _print_ok("4. Validate admin role upgrade")

    admin_service.set_license_status(license_key=license_key, status="suspended")
    result_suspended = manager.validate_license("integration@test.com", license_key)
    assert result_suspended.can_launch is False
    assert result_suspended.reason == "license_suspended"
    _print_ok("5. Validate suspended license")

    legacy_result = manager.validate_license("admin@myralis.ai", "BETA-MYRALIS-001")
    assert legacy_result.can_launch is True
    assert legacy_result.developer_mode_enabled is True
    _print_ok("6. Validate legacy fallback license")

    print("All license manager/admin integration tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
