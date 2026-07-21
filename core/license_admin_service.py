from __future__ import annotations

import json
import logging
import os
import secrets
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

from core.runtime_paths import get_runtime_paths


LOGGER = logging.getLogger(__name__)

ALLOWED_ROLES = {"client", "beta_tester", "admin"}
ALLOWED_LICENSE_STATUSES = {"active", "beta", "expired", "suspended"}
ALLOWED_ACTIVATION_CODE_STATUSES = {"available", "used", "expired", "disabled"}


class LicenseAdminService:
    """Internal administrative API for users, licenses, credits, and roles.

    This service is intended for future use by:

    - Admin Panel
    - Web Dashboard
    - Internal scripts
    - Support tooling

    It is not meant for direct use by end users. The implementation supports
    both the existing mock mode and a Supabase-backed mode, but it should only
    be used from trusted internal tooling.
    """

    def __init__(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.config_dir = get_runtime_paths(self.root).config_root
        self.mock_state_path = self.config_dir / "license_admin_mock_state.json"
        self._mock_users: dict[str, dict[str, Any]] = {}
        self._mock_licenses: dict[str, dict[str, Any]] = {}
        self._mock_credit_ledger: list[dict[str, Any]] = []
        self._mock_activation_codes: dict[str, dict[str, Any]] = {}
        self._load_mock_state()

    def create_user(self, email: str, role: str = "client") -> dict[str, Any]:
        """Create a user in mock memory or Supabase."""

        clean_email = self._require_email(email)
        clean_role = self._validate_role(role)

        if self._use_supabase():
            return self._create_user_supabase(clean_email, clean_role)
        result = self._create_user_mock(clean_email, clean_role)
        self._save_mock_state()
        return result

    def create_license(
        self,
        *,
        email: str,
        client_name: str,
        plan_name: str,
        credits: float,
        status: str,
        developer_mode_allowed: bool,
        expires_at: str | None,
    ) -> dict[str, Any]:
        """Create a user if needed and issue a new license key."""

        clean_email = self._require_email(email)
        clean_status = self._validate_license_status(status)
        clean_credits = self._validate_credits(credits)
        clean_plan_name = str(plan_name or "").strip() or None
        clean_client_name = str(client_name or "").strip() or None
        clean_expires_at = self._normalize_expires_at(expires_at)

        user = self.get_user(clean_email)
        if user is None:
            user = self.create_user(clean_email)

        license_key = self._generate_license_key()
        if self._use_supabase():
            self._create_license_supabase(
                license_key=license_key,
                user_id=str(user["id"]),
                client_name=clean_client_name,
                plan_name=clean_plan_name,
                credits=clean_credits,
                status=clean_status,
                developer_mode_allowed=bool(developer_mode_allowed),
                expires_at=clean_expires_at,
            )
        else:
            self._create_license_mock(
                license_key=license_key,
                user_id=str(user["id"]),
                email=clean_email,
                client_name=clean_client_name,
                plan_name=clean_plan_name,
                credits=clean_credits,
                status=clean_status,
                developer_mode_allowed=bool(developer_mode_allowed),
                expires_at=clean_expires_at,
            )
            self._save_mock_state()

        return {
            "license_key": license_key,
            "credits": clean_credits,
            "plan": clean_plan_name,
            "status": clean_status,
        }

    def add_credits(self, *, license_key: str, amount: float, reason: str) -> dict[str, Any]:
        """Increase credits and write a credit ledger entry."""

        clean_license_key = self._require_license_key(license_key)
        delta = self._validate_positive_amount(amount)
        clean_reason = self._require_reason(reason)

        license_record = self.get_license(clean_license_key)
        if license_record is None:
            raise ValueError("license_not_found")

        new_balance = float(license_record["credits_balance"]) + delta
        if self._use_supabase():
            self._update_license_balance_supabase(clean_license_key, new_balance)
            self._insert_credit_ledger_supabase(
                license_id=str(license_record["id"]),
                change_amount=delta,
                reason=clean_reason,
                balance_after=new_balance,
            )
        else:
            license_record["credits_balance"] = new_balance
            self._mock_credit_ledger.append(
                {
                    "id": self._new_id(),
                    "license_key": clean_license_key,
                    "license_id": license_record["id"],
                    "change_amount": delta,
                    "reason": clean_reason,
                    "balance_after": new_balance,
                    "created_at": self._now(),
                }
            )
            self._save_mock_state()
        return self.get_license(clean_license_key) or {}

    def remove_credits(
        self,
        *,
        license_key: str,
        amount: float,
        reason: str,
    ) -> dict[str, Any]:
        """Decrease credits without allowing a negative balance."""

        clean_license_key = self._require_license_key(license_key)
        delta = self._validate_positive_amount(amount)
        clean_reason = self._require_reason(reason)

        license_record = self.get_license(clean_license_key)
        if license_record is None:
            raise ValueError("license_not_found")

        current_balance = float(license_record["credits_balance"])
        new_balance = max(0.0, current_balance - delta)
        if self._use_supabase():
            self._update_license_balance_supabase(clean_license_key, new_balance)
            self._insert_credit_ledger_supabase(
                license_id=str(license_record["id"]),
                change_amount=-delta,
                reason=clean_reason,
                balance_after=new_balance,
            )
        else:
            license_record["credits_balance"] = new_balance
            self._mock_credit_ledger.append(
                {
                    "id": self._new_id(),
                    "license_key": clean_license_key,
                    "license_id": license_record["id"],
                    "change_amount": -delta,
                    "reason": clean_reason,
                    "balance_after": new_balance,
                    "created_at": self._now(),
                }
            )
            self._save_mock_state()
        return self.get_license(clean_license_key) or {}

    def change_role(self, *, email: str, role: str) -> dict[str, Any]:
        """Change a user's role to a valid allowed value."""

        clean_email = self._require_email(email)
        clean_role = self._validate_role(role)
        user = self.get_user(clean_email)
        if user is None:
            raise ValueError("user_not_found")

        if self._use_supabase():
            return self._change_role_supabase(clean_email, clean_role)

        user["role"] = clean_role
        self._save_mock_state()
        return user

    def set_license_status(self, *, license_key: str, status: str) -> dict[str, Any]:
        """Set a license status using the supported status enum."""

        clean_license_key = self._require_license_key(license_key)
        clean_status = self._validate_license_status(status)

        license_record = self.get_license(clean_license_key)
        if license_record is None:
            raise ValueError("license_not_found")

        if self._use_supabase():
            return self._set_license_status_supabase(clean_license_key, clean_status)

        license_record["status"] = clean_status
        self._save_mock_state()
        return license_record

    def list_users(self) -> list[dict[str, Any]]:
        """Return all users."""

        if self._use_supabase():
            return self._list_users_supabase()
        return list(self._mock_users.values())

    def list_licenses(self) -> list[dict[str, Any]]:
        """Return all licenses."""

        if self._use_supabase():
            return self._list_licenses_supabase()
        return list(self._mock_licenses.values())

    def get_license(self, license_key: str) -> dict[str, Any] | None:
        """Find a license by key."""

        clean_license_key = self._require_license_key(license_key)
        if self._use_supabase():
            return self._get_license_supabase(clean_license_key)
        return self._mock_licenses.get(clean_license_key)

    def get_user(self, email: str) -> dict[str, Any] | None:
        """Find a user by email."""

        clean_email = self._require_email(email)
        if self._use_supabase():
            return self._get_user_supabase(clean_email)
        return self._mock_users.get(clean_email)

    def create_activation_code(
        self,
        code: str | None = None,
        credits: float = 100,
        plan_name: str = "Beta",
        client_name: str | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        """Create an activation code for beta onboarding."""

        clean_code = self._normalize_activation_code(code)
        clean_credits = self._validate_credits(credits)
        clean_plan_name = str(plan_name or "").strip() or "Beta"
        clean_client_name = str(client_name or "").strip() or None
        clean_expires_at = self._normalize_expires_at(expires_at)

        if self._use_supabase():
            return self._create_activation_code_supabase(
                code=clean_code,
                credits=clean_credits,
                plan_name=clean_plan_name,
                client_name=clean_client_name,
                expires_at=clean_expires_at,
            )

        activation_code = self._mock_activation_codes.get(clean_code)
        if activation_code is None:
            activation_code = {
                "id": self._new_id(),
                "code": clean_code,
                "status": "available",
                "credits": clean_credits,
                "plan_name": clean_plan_name,
                "client_name": clean_client_name,
                "expires_at": clean_expires_at,
                "used_by_email": None,
                "used_by_name": None,
                "used_license_key": None,
                "used_at": None,
                "created_at": self._now(),
                "updated_at": self._now(),
            }
            self._mock_activation_codes[clean_code] = activation_code
        else:
            activation_code.update(
                {
                    "status": "available",
                    "credits": clean_credits,
                    "plan_name": clean_plan_name,
                    "client_name": clean_client_name,
                    "expires_at": clean_expires_at,
                    "updated_at": self._now(),
                }
            )
        self._save_mock_state()
        return activation_code

    def list_activation_codes(self) -> list[dict[str, Any]]:
        """Return all activation codes."""

        if self._use_supabase():
            return self._list_activation_codes_supabase()
        return list(self._mock_activation_codes.values())

    def get_activation_code(self, code: str) -> dict[str, Any] | None:
        """Find an activation code by code."""

        clean_code = self._require_activation_code(code)
        if self._use_supabase():
            return self._get_activation_code_supabase(clean_code)
        return self._mock_activation_codes.get(clean_code)

    def disable_activation_code(
        self,
        code: str,
        reason: str = "manual",
    ) -> dict[str, Any]:
        """Disable an activation code."""

        clean_code = self._require_activation_code(code)
        _ = self._require_reason(reason)
        if self._use_supabase():
            return self._disable_activation_code_supabase(clean_code)

        activation_code = self._mock_activation_codes.get(clean_code)
        if activation_code is None:
            raise ValueError("activation_code_not_found")
        activation_code["status"] = "disabled"
        activation_code["updated_at"] = self._now()
        self._save_mock_state()
        return activation_code

    def activate_code(
        self,
        *,
        code: str,
        email: str,
        name: str,
    ) -> dict[str, Any]:
        """Activate a beta code, create a user, and issue a license."""

        clean_code = self._require_activation_code(code)
        clean_email = self._require_email(email)
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("name is required")

        if self._use_supabase():
            return self._activate_code_supabase(
                code=clean_code,
                email=clean_email,
                name=clean_name,
            )
        return self._activate_code_mock(
            code=clean_code,
            email=clean_email,
            name=clean_name,
        )

    def _use_supabase(self) -> bool:
        provider = str(os.getenv("MYRALIS_LICENSE_PROVIDER", "") or "").strip().lower()
        return provider == "supabase"

    def _generate_license_key(self) -> str:
        while True:
            key = f"MYR-{secrets.token_hex(4).upper()}-{secrets.token_hex(4).upper()}"
            if self.get_license(key) is None:
                return key

    def _create_user_mock(self, email: str, role: str) -> dict[str, Any]:
        existing = self._mock_users.get(email)
        if existing is not None:
            existing["role"] = role
            return existing
        user = {
            "id": self._new_id(),
            "email": email,
            "role": role,
            "created_at": self._now(),
        }
        self._mock_users[email] = user
        return user

    def _create_license_mock(
        self,
        *,
        license_key: str,
        user_id: str,
        email: str,
        client_name: str | None,
        plan_name: str | None,
        credits: float,
        status: str,
        developer_mode_allowed: bool,
        expires_at: str | None,
    ) -> dict[str, Any]:
        license_record = {
            "id": self._new_id(),
            "license_key": license_key,
            "user_id": user_id,
            "email": email,
            "client_name": client_name,
            "plan_name": plan_name,
            "credits_balance": float(credits),
            "status": status,
            "developer_mode_allowed": bool(developer_mode_allowed),
            "expires_at": expires_at,
            "created_at": self._now(),
            "updated_at": self._now(),
        }
        self._mock_licenses[license_key] = license_record
        return license_record

    def _load_mock_state(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        if not self.mock_state_path.exists():
            return
        try:
            loaded = json.loads(self.mock_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOGGER.warning(
                "Could not load mock license admin state; starting with empty state."
            )
            return

        if not isinstance(loaded, dict):
            LOGGER.warning(
                "Mock license admin state is invalid; starting with empty state."
            )
            return

        users = loaded.get("users", {})
        licenses = loaded.get("licenses", {})
        credit_ledger = loaded.get("credit_ledger", [])
        activation_codes = loaded.get("activation_codes", {})
        if isinstance(users, dict):
            self._mock_users = {
                str(email): dict(user)
                for email, user in users.items()
                if isinstance(user, dict)
            }
        if isinstance(licenses, dict):
            self._mock_licenses = {
                str(license_key): dict(license_record)
                for license_key, license_record in licenses.items()
                if isinstance(license_record, dict)
            }
        if isinstance(credit_ledger, list):
            self._mock_credit_ledger = [
                dict(entry) for entry in credit_ledger if isinstance(entry, dict)
            ]
        if isinstance(activation_codes, dict):
            self._mock_activation_codes = {
                str(code): dict(entry)
                for code, entry in activation_codes.items()
                if isinstance(entry, dict)
            }

    def _save_mock_state(self) -> None:
        if self._use_supabase():
            return
        self.config_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "users": self._mock_users,
            "licenses": self._mock_licenses,
            "credit_ledger": self._mock_credit_ledger,
            "activation_codes": self._mock_activation_codes,
        }
        temp_path = self.mock_state_path.with_suffix(self.mock_state_path.suffix + ".tmp")
        temp_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(self.mock_state_path)

    def _create_user_supabase(self, email: str, role: str) -> dict[str, Any]:
        client = self._supabase_client()
        try:
            existing = (
                client.table("app_users")
                .select("id,email,role,created_at")
                .eq("email", email)
                .limit(1)
                .execute()
            )
            row = self._first_row(existing)
            if row is not None:
                return row
            response = (
                client.table("app_users")
                .insert({"email": email, "role": role})
                .execute()
            )
            row = self._first_row(response)
            if row is None:
                raise RuntimeError("user_insert_failed")
            return row
        except Exception:
            LOGGER.exception("Failed to create Supabase user")
            raise

    def _create_license_supabase(
        self,
        *,
        license_key: str,
        user_id: str,
        client_name: str | None,
        plan_name: str | None,
        credits: float,
        status: str,
        developer_mode_allowed: bool,
        expires_at: str | None,
    ) -> dict[str, Any]:
        client = self._supabase_client()
        payload = {
            "license_key": license_key,
            "user_id": user_id,
            "client_name": client_name,
            "status": status,
            "credits_balance": credits,
            "plan_name": plan_name,
            "expires_at": expires_at,
            "developer_mode_allowed": developer_mode_allowed,
        }
        response = client.table("licenses").insert(payload).execute()
        row = self._first_row(response)
        if row is None:
            raise RuntimeError("license_insert_failed")
        return row

    def _create_activation_code_supabase(
        self,
        *,
        code: str,
        credits: float,
        plan_name: str,
        client_name: str | None,
        expires_at: str | None,
    ) -> dict[str, Any]:
        client = self._supabase_client()
        response = (
            client.table("activation_codes")
            .insert(
                {
                    "code": code,
                    "status": "available",
                    "credits": credits,
                    "plan_name": plan_name,
                    "client_name": client_name,
                    "expires_at": expires_at,
                }
            )
            .execute()
        )
        row = self._first_row(response)
        if row is None:
            raise RuntimeError("activation_code_insert_failed")
        return row

    def _update_license_balance_supabase(self, license_key: str, credits: float) -> None:
        client = self._supabase_client()
        client.table("licenses").update({"credits_balance": credits}).eq(
            "license_key",
            license_key,
        ).execute()

    def _insert_credit_ledger_supabase(
        self,
        *,
        license_id: str,
        change_amount: float,
        reason: str,
        balance_after: float,
    ) -> None:
        client = self._supabase_client()
        client.table("credit_ledger").insert(
            {
                "license_id": license_id,
                "change_amount": change_amount,
                "reason": reason,
                "balance_after": balance_after,
            }
        ).execute()

    def _change_role_supabase(self, email: str, role: str) -> dict[str, Any]:
        client = self._supabase_client()
        response = (
            client.table("app_users")
            .update({"role": role})
            .eq("email", email)
            .execute()
        )
        row = self._first_row(response)
        if row is None:
            raise RuntimeError("user_update_failed")
        return row

    def _set_license_status_supabase(self, license_key: str, status: str) -> dict[str, Any]:
        client = self._supabase_client()
        response = (
            client.table("licenses")
            .update({"status": status})
            .eq("license_key", license_key)
            .execute()
        )
        row = self._first_row(response)
        if row is None:
            raise RuntimeError("license_update_failed")
        return row

    def _list_activation_codes_supabase(self) -> list[dict[str, Any]]:
        client = self._supabase_client()
        response = client.table("activation_codes").select("*").order("created_at").execute()
        return self._rows(response)

    def _get_activation_code_supabase(self, code: str) -> dict[str, Any] | None:
        client = self._supabase_client()
        response = (
            client.table("activation_codes")
            .select("*")
            .eq("code", code)
            .limit(1)
            .execute()
        )
        return self._first_row(response)

    def _disable_activation_code_supabase(self, code: str) -> dict[str, Any]:
        client = self._supabase_client()
        response = (
            client.table("activation_codes")
            .update({"status": "disabled"})
            .eq("code", code)
            .execute()
        )
        row = self._first_row(response)
        if row is None:
            raise RuntimeError("activation_code_update_failed")
        return row

    def _activate_code_supabase(
        self,
        *,
        code: str,
        email: str,
        name: str,
    ) -> dict[str, Any]:
        try:
            activation_code = self._get_activation_code_supabase(code)
            if activation_code is None:
                return {"ok": False, "reason": "activation_code_not_found"}

            status = self._as_optional_str(activation_code.get("status")) or ""
            if status != "available":
                return {"ok": False, "reason": "activation_code_not_available"}

            expires_at = self._parse_timestamp(activation_code.get("expires_at"))
            if expires_at is not None and expires_at <= datetime.now(timezone.utc):
                return {"ok": False, "reason": "activation_code_expired"}

            user = self._get_user_supabase(email)
            if user is None:
                user = self._create_user_supabase(email, "client")

            license_key = self._generate_license_key()
            license_row = self._create_license_supabase(
                license_key=license_key,
                user_id=str(user["id"]),
                client_name=name,
                plan_name=self._as_optional_str(activation_code.get("plan_name")) or "Beta",
                credits=self._as_float(activation_code.get("credits")),
                status="beta",
                developer_mode_allowed=False,
                expires_at=self._as_optional_str(activation_code.get("expires_at")),
            )
            self._mark_activation_code_used_supabase(
                code=code,
                email=email,
                name=name,
                license_key=license_key,
            )
            self._insert_credit_ledger_supabase(
                license_id=str(license_row["id"]),
                change_amount=self._as_float(activation_code.get("credits")),
                reason="activation_code",
                balance_after=self._as_float(activation_code.get("credits")),
            )
            return {
                "ok": True,
                "license_key": license_key,
                "email": email,
                "name": name,
                "credits": self._as_float(activation_code.get("credits")),
                "plan_name": self._as_optional_str(activation_code.get("plan_name")) or "Beta",
            }
        except Exception:
            LOGGER.exception("Supabase activation code flow failed")
            return {"ok": False, "reason": "supabase_error"}

    def _mark_activation_code_used_supabase(
        self,
        *,
        code: str,
        email: str,
        name: str,
        license_key: str,
    ) -> None:
        client = self._supabase_client()
        client.table("activation_codes").update(
            {
                "status": "used",
                "used_by_email": email,
                "used_by_name": name,
                "used_license_key": license_key,
                "used_at": self._now(),
            }
        ).eq("code", code).execute()

    def _activate_code_mock(
        self,
        *,
        code: str,
        email: str,
        name: str,
    ) -> dict[str, Any]:
        activation_code = self._mock_activation_codes.get(code)
        if activation_code is None:
            return {"ok": False, "reason": "activation_code_not_found"}

        status = self._as_optional_str(activation_code.get("status")) or ""
        if status != "available":
            return {"ok": False, "reason": "activation_code_not_available"}

        expires_at = self._parse_timestamp(activation_code.get("expires_at"))
        if expires_at is not None and expires_at <= datetime.now(timezone.utc):
            return {"ok": False, "reason": "activation_code_expired"}

        user = self.get_user(email)
        if user is None:
            user = self.create_user(email, "client")

        license_key = self._generate_license_key()
        license_row = self._create_license_mock(
            license_key=license_key,
            user_id=str(user["id"]),
            email=email,
            client_name=name,
            plan_name=self._as_optional_str(activation_code.get("plan_name")) or "Beta",
            credits=self._as_float(activation_code.get("credits")),
            status="beta",
            developer_mode_allowed=False,
            expires_at=self._normalize_expires_at(activation_code.get("expires_at")),
        )
        self._mark_activation_code_used_mock(
            code=code,
            email=email,
            name=name,
            license_key=license_key,
        )
        credits = self._as_float(activation_code.get("credits"))
        self._mock_credit_ledger.append(
            {
                "id": self._new_id(),
                "license_key": license_key,
                "license_id": license_row["id"],
                "change_amount": credits,
                "reason": "activation_code",
                "balance_after": credits,
                "created_at": self._now(),
            }
        )
        self._save_mock_state()
        return {
            "ok": True,
            "license_key": license_key,
            "email": email,
            "name": name,
            "credits": credits,
            "plan_name": self._as_optional_str(activation_code.get("plan_name")) or "Beta",
        }

    def _mark_activation_code_used_mock(
        self,
        *,
        code: str,
        email: str,
        name: str,
        license_key: str,
    ) -> None:
        activation_code = self._mock_activation_codes.get(code)
        if activation_code is None:
            return
        activation_code.update(
            {
                "status": "used",
                "used_by_email": email,
                "used_by_name": name,
                "used_license_key": license_key,
                "used_at": self._now(),
                "updated_at": self._now(),
            }
        )
        self._save_mock_state()

    def _list_users_supabase(self) -> list[dict[str, Any]]:
        client = self._supabase_client()
        response = client.table("app_users").select("*").order("created_at").execute()
        return self._rows(response)

    def _list_licenses_supabase(self) -> list[dict[str, Any]]:
        client = self._supabase_client()
        response = client.table("licenses").select("*").order("created_at").execute()
        return self._rows(response)

    def _get_license_supabase(self, license_key: str) -> dict[str, Any] | None:
        client = self._supabase_client()
        response = (
            client.table("licenses")
            .select("*")
            .eq("license_key", license_key)
            .limit(1)
            .execute()
        )
        return self._first_row(response)

    def _get_user_supabase(self, email: str) -> dict[str, Any] | None:
        client = self._supabase_client()
        response = (
            client.table("app_users")
            .select("*")
            .eq("email", email)
            .limit(1)
            .execute()
        )
        return self._first_row(response)

    def _supabase_client(self) -> Any:
        supabase_url = str(os.getenv("SUPABASE_URL", "") or "").strip()
        supabase_service_role_key = str(
            os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or ""
        ).strip()
        if not supabase_url or not supabase_service_role_key:
            raise RuntimeError("supabase_not_configured")
        try:
            from supabase import create_client  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError("supabase_package_missing") from exc
        return create_client(supabase_url, supabase_service_role_key)

    def _first_row(self, response: Any) -> dict[str, Any] | None:
        data = getattr(response, "data", None)
        if isinstance(data, list) and data:
            first = data[0]
            return first if isinstance(first, dict) else None
        if isinstance(data, dict):
            return data
        return None

    def _rows(self, response: Any) -> list[dict[str, Any]]:
        data = getattr(response, "data", None)
        if not isinstance(data, list):
            return []
        return [row for row in data if isinstance(row, dict)]

    def _as_optional_str(self, value: Any) -> str | None:
        if value is None:
            return None
        return str(value)

    def _as_str(self, value: Any, default: str = "") -> str:
        if value is None:
            return default
        return str(value)

    def _as_float(self, value: Any, default: float = 0.0) -> float:
        if value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _as_bool(self, value: Any, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "y", "on")
        return bool(value)

    def _parse_timestamp(self, value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                normalized = value.replace("Z", "+00:00")
                return datetime.fromisoformat(normalized)
            except ValueError:
                return None
        return None

    def _require_email(self, email: str) -> str:
        clean = str(email or "").strip()
        if not clean:
            raise ValueError("email is required")
        return clean

    def _require_license_key(self, license_key: str) -> str:
        clean = str(license_key or "").strip()
        if not clean:
            raise ValueError("license_key is required")
        return clean

    def _validate_role(self, role: str) -> str:
        clean = str(role or "").strip()
        if clean not in ALLOWED_ROLES:
            raise ValueError("invalid role")
        return clean

    def _validate_license_status(self, status: str) -> str:
        clean = str(status or "").strip()
        if clean not in ALLOWED_LICENSE_STATUSES:
            raise ValueError("invalid license status")
        return clean

    def _validate_positive_amount(self, amount: float) -> float:
        try:
            clean = float(amount)
        except (TypeError, ValueError) as exc:
            raise ValueError("amount must be numeric") from exc
        if clean <= 0:
            raise ValueError("amount must be greater than zero")
        return clean

    def _validate_credits(self, credits: float) -> float:
        try:
            clean = float(credits)
        except (TypeError, ValueError) as exc:
            raise ValueError("credits must be numeric") from exc
        if clean < 0:
            raise ValueError("credits cannot be negative")
        return clean

    def _require_reason(self, reason: str) -> str:
        clean = str(reason or "").strip()
        if not clean:
            raise ValueError("reason is required")
        return clean

    def _normalize_expires_at(self, expires_at: str | None) -> str | None:
        clean = str(expires_at or "").strip()
        return clean or None

    def _normalize_activation_code(self, code: str | None) -> str:
        clean_code = str(code or "").strip().upper()
        if clean_code:
            return clean_code
        return f"BETA-{secrets.token_hex(4).upper()}"

    def _require_activation_code(self, code: str) -> str:
        clean = str(code or "").strip().upper()
        if not clean:
            raise ValueError("activation code is required")
        return clean

    def _new_id(self) -> str:
        return secrets.token_hex(16)

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()
