from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.license_manager import LicenseManager  # noqa: E402


def _mask_license_key(value: str | None) -> str:
    """Mask a license key for status output without exposing the full value."""

    clean = str(value or "").strip()
    if not clean:
        return "-"
    parts = clean.split("-")
    if len(parts) >= 3:
        return f"{parts[0]}-****-{parts[-1]}"
    if len(clean) <= 8:
        return "*" * len(clean)
    return f"{clean[:4]}****{clean[-4:]}"


def _print_kv(label: str, value: object) -> None:
    print(f"{label}: {value}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Myralis AI local license CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    activate = subparsers.add_parser(
        "activate",
        help="Validate and save a local license state",
    )
    activate.add_argument("--email", required=True, help="License email")
    activate.add_argument("--key", required=True, help="License key")

    subparsers.add_parser(
        "status",
        help="Show the current saved license state",
    )

    subparsers.add_parser(
        "clear",
        help="Remove the saved local license state",
    )

    return parser


def _handle_activate(manager: LicenseManager, email: str, key: str) -> int:
    result = manager.validate_license(email, key)
    manager.save_local_license_state(email, key, result)

    _print_kv("can_launch", result.can_launch)
    _print_kv("reason", result.reason)
    _print_kv("plan_name", result.plan_name)
    _print_kv("credits_balance", result.credits_balance)
    if not result.can_launch:
        print(
            "note: license saved for diagnosis only; this state will not permit enforcement"
        )
    return 0


def _handle_status(manager: LicenseManager) -> int:
    cached = manager.load_local_license_state()
    result = manager.validate_saved_license()

    _print_kv("email", cached.get("email") or "-")
    _print_kv("license_key", _mask_license_key(cached.get("license_key")))
    _print_kv("can_launch", result.can_launch)
    _print_kv("reason", result.reason)
    _print_kv("plan_name", result.plan_name)
    _print_kv("credits_balance", result.credits_balance)
    _print_kv("debug_allowed", manager.is_debug_allowed(result))
    return 0


def _handle_clear(manager: LicenseManager) -> int:
    manager.clear_local_license_state()
    print("Local license state cleared.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    manager = LicenseManager()

    try:
        if args.command == "activate":
            return _handle_activate(manager, args.email, args.key)
        if args.command == "status":
            return _handle_status(manager)
        if args.command == "clear":
            return _handle_clear(manager)
        parser.error("Unknown command")
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
