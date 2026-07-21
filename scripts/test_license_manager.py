from __future__ import annotations

from core.license_manager import LicenseManager


def _print_result(label: str, ok: bool) -> None:
    print(f"{label}: {'OK' if ok else 'FAIL'}")


def main() -> int:
    manager = LicenseManager()

    print("Running license manager tests...")

    result = manager.validate_license("", "BETA-MYRALIS-001")
    _print_result("1. Email empty", True)
    assert result.can_launch is False
    assert result.reason == "missing_email"

    result = manager.validate_license("test@myralis.ai", "")
    _print_result("2. License empty", True)
    assert result.can_launch is False
    assert result.reason == "missing_license_key"

    result = manager.validate_license("admin@myralis.ai", "BETA-MYRALIS-001")
    _print_result("3. Admin beta license", True)
    assert result.can_launch is True
    assert result.role == "admin"
    assert result.credits_balance == 100.0
    assert result.developer_mode_enabled is True
    assert manager.is_debug_allowed(result) is True

    result = manager.validate_license("client@myralis.ai", "CLIENT-DEMO-001")
    _print_result("4. Client demo license", True)
    assert result.can_launch is True
    assert result.role == "client"
    assert result.credits_balance == 50.0
    assert result.developer_mode_enabled is False
    assert manager.is_debug_allowed(result) is False

    result = manager.validate_license("x@myralis.ai", "INVALID-KEY")
    _print_result("5. Missing license", True)
    assert result.can_launch is False
    assert result.reason == "license_not_found"

    manager.clear_local_license_state()
    result = manager.validate_license("client@myralis.ai", "CLIENT-DEMO-001")
    manager.save_local_license_state("client@myralis.ai", "CLIENT-DEMO-001", result)
    saved_result = manager.validate_saved_license()
    _print_result("6. Local save/load", True)
    assert saved_result.can_launch is True
    assert saved_result.plan_name == "Demo"

    manager.clear_local_license_state()
    cleared_result = manager.validate_saved_license()
    _print_result("7. Local cleanup", True)
    assert cleared_result.can_launch is False
    assert cleared_result.reason == "missing_saved_license"

    print("All license manager tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
