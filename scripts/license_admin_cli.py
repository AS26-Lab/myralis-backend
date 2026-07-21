from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.license_admin_service import LicenseAdminService  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Myralis AI license admin CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_user = subparsers.add_parser("create-user", help="Create a user")
    create_user.add_argument("--email", required=True)
    create_user.add_argument("--role", default="client")

    create_license = subparsers.add_parser("create-license", help="Create a license")
    create_license.add_argument("--email", required=True)
    create_license.add_argument("--client-name", required=True)
    create_license.add_argument("--plan-name", required=True)
    create_license.add_argument("--credits", required=True, type=float)
    create_license.add_argument("--status", required=True)
    create_license.add_argument("--expires-at", default=None)
    create_license.add_argument(
        "--developer-mode-allowed",
        action="store_true",
        help="Allow developer mode for this license",
    )

    add_credits = subparsers.add_parser("add-credits", help="Add credits")
    add_credits.add_argument("--key", required=True)
    add_credits.add_argument("--amount", required=True, type=float)
    add_credits.add_argument("--reason", required=True)

    remove_credits = subparsers.add_parser("remove-credits", help="Remove credits")
    remove_credits.add_argument("--key", required=True)
    remove_credits.add_argument("--amount", required=True, type=float)
    remove_credits.add_argument("--reason", required=True)

    change_role = subparsers.add_parser("change-role", help="Change a role")
    change_role.add_argument("--email", required=True)
    change_role.add_argument("--role", required=True)

    set_status = subparsers.add_parser("set-status", help="Change license status")
    set_status.add_argument("--key", required=True)
    set_status.add_argument("--status", required=True)

    subparsers.add_parser("list-users", help="List users")
    subparsers.add_parser("list-licenses", help="List licenses")

    get_license = subparsers.add_parser("get-license", help="Get a license")
    get_license.add_argument("--key", required=True)

    get_user = subparsers.add_parser("get-user", help="Get a user")
    get_user.add_argument("--email", required=True)

    create_code = subparsers.add_parser("create-code", help="Create activation code")
    create_code.add_argument("--code", default=None)
    create_code.add_argument("--credits", default=100, type=float)
    create_code.add_argument("--plan-name", default="Beta")
    create_code.add_argument("--client-name", default=None)
    create_code.add_argument("--expires-at", default=None)

    subparsers.add_parser("list-codes", help="List activation codes")

    get_code = subparsers.add_parser("get-code", help="Get activation code")
    get_code.add_argument("--code", required=True)

    disable_code = subparsers.add_parser("disable-code", help="Disable activation code")
    disable_code.add_argument("--code", required=True)

    activate_code = subparsers.add_parser("activate-code", help="Activate code")
    activate_code.add_argument("--code", required=True)
    activate_code.add_argument("--email", required=True)
    activate_code.add_argument("--name", required=True)

    return parser


def _pretty_print(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def _print_error(message: str) -> int:
    print(f"Error: {message}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    service = LicenseAdminService()

    try:
        if args.command == "create-user":
            _pretty_print(service.create_user(args.email, args.role))
            return 0

        if args.command == "create-license":
            result = service.create_license(
                email=args.email,
                client_name=args.client_name,
                plan_name=args.plan_name,
                credits=args.credits,
                status=args.status,
                developer_mode_allowed=bool(args.developer_mode_allowed),
                expires_at=args.expires_at,
            )
            _pretty_print(result)
            return 0

        if args.command == "add-credits":
            _pretty_print(
                service.add_credits(
                    license_key=args.key,
                    amount=args.amount,
                    reason=args.reason,
                )
            )
            return 0

        if args.command == "remove-credits":
            _pretty_print(
                service.remove_credits(
                    license_key=args.key,
                    amount=args.amount,
                    reason=args.reason,
                )
            )
            return 0

        if args.command == "change-role":
            _pretty_print(service.change_role(email=args.email, role=args.role))
            return 0

        if args.command == "set-status":
            _pretty_print(service.set_license_status(license_key=args.key, status=args.status))
            return 0

        if args.command == "list-users":
            _pretty_print(service.list_users())
            return 0

        if args.command == "list-licenses":
            _pretty_print(service.list_licenses())
            return 0

        if args.command == "get-license":
            license_record = service.get_license(args.key)
            if license_record is None:
                return _print_error("license_not_found")
            _pretty_print(license_record)
            return 0

        if args.command == "get-user":
            user = service.get_user(args.email)
            if user is None:
                return _print_error("user_not_found")
            _pretty_print(user)
            return 0

        if args.command == "create-code":
            _pretty_print(
                service.create_activation_code(
                    code=args.code,
                    credits=args.credits,
                    plan_name=args.plan_name,
                    client_name=args.client_name,
                    expires_at=args.expires_at,
                )
            )
            return 0

        if args.command == "list-codes":
            _pretty_print(service.list_activation_codes())
            return 0

        if args.command == "get-code":
            code_record = service.get_activation_code(args.code)
            if code_record is None:
                return _print_error("activation_code_not_found")
            _pretty_print(code_record)
            return 0

        if args.command == "disable-code":
            _pretty_print(service.disable_activation_code(args.code))
            return 0

        if args.command == "activate-code":
            _pretty_print(
                service.activate_code(
                    code=args.code,
                    email=args.email,
                    name=args.name,
                )
            )
            return 0

        return _print_error("unknown command")
    except Exception as exc:
        return _print_error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
