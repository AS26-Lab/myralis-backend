from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

os.environ["MYRALIS_LICENSE_PROVIDER"] = "mock"

from core.license_admin_service import LicenseAdminService  # noqa: E402


STATE_PATH = ROOT_DIR / "config" / "license_admin_mock_state.json"


def _print_ok(label: str) -> None:
    print(f"{label}: OK")


def main() -> int:
    STATE_PATH.unlink(missing_ok=True)

    print("Running license admin service persistence tests...")

    service_a = LicenseAdminService()
    user_a = service_a.create_user("persist@test.com", "client")
    assert user_a["email"] == "persist@test.com"
    assert user_a["role"] == "client"
    _print_ok("1. Create user")

    license_a = service_a.create_license(
        email="persist@test.com",
        client_name="Museo Persistente",
        plan_name="Beta",
        credits=100,
        status="beta",
        developer_mode_allowed=False,
        expires_at="2026-09-01",
    )
    license_key = str(license_a["license_key"])
    assert license_key
    _print_ok("2. Create license")

    service_b = LicenseAdminService()
    user_b = service_b.get_user("persist@test.com")
    license_b = service_b.get_license(license_key)
    assert user_b is not None
    assert license_b is not None
    assert float(license_b["credits_balance"]) == 100.0
    assert str(license_b["status"]) == "beta"
    _print_ok("3. Reload state in new instance")

    service_b.add_credits(
        license_key=license_key,
        amount=25,
        reason="test topup",
    )
    _print_ok("4. Add credits")

    service_c = LicenseAdminService()
    license_c = service_c.get_license(license_key)
    assert license_c is not None
    assert float(license_c["credits_balance"]) == 125.0
    _print_ok("5. Verify added credits in new instance")

    service_c.remove_credits(
        license_key=license_key,
        amount=20,
        reason="test deduct",
    )
    _print_ok("6. Remove credits")

    service_d = LicenseAdminService()
    license_d = service_d.get_license(license_key)
    assert license_d is not None
    assert float(license_d["credits_balance"]) == 105.0
    _print_ok("7. Verify deducted credits in new instance")

    negative_failed = False
    try:
        service_d.remove_credits(
            license_key=license_key,
            amount=9999,
            reason="too much",
        )
    except Exception:
        negative_failed = True
    final_after_large_deduct = float(service_d.get_license(license_key)["credits_balance"])
    assert negative_failed or final_after_large_deduct == 0.0
    assert final_after_large_deduct >= 0.0
    _print_ok("8. Prevent negative balance")

    users = service_d.list_users()
    licenses = service_d.list_licenses()
    assert any(user.get("email") == "persist@test.com" for user in users)
    assert any(item.get("license_key") == license_key for item in licenses)
    _print_ok("9. List users and licenses")

    print("All license admin persistence tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
