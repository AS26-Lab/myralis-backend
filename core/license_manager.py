from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass
from typing import Any

from core.runtime_paths import get_runtime_paths


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class LicenseValidationResult:
    """Represents the outcome of a local/mock license validation.

    This object is intentionally simple so the licensing flow can be exercised
    without any external services yet. In the next stage, this result can be
    populated from a remote source such as Supabase.
    """

    can_launch: bool
    reason: str
    email: str | None
    license_key: str | None
    user_id: str | None
    license_id: str | None
    role: str
    status: str | None
    credits_balance: float
    plan_name: str | None
    developer_mode_enabled: bool


class LicenseManager:
    """Central license manager using mock local validation for now.

    This module is the foundation for the future Supabase-backed licensing
    system. Current behavior is deterministic and local-only so the license
    flow can be tested without network access or external dependencies.

    The local state file is only a convenience cache for the user experience.
    It is not a security boundary and must never be treated as authoritative
    for permissions, admin access, or developer mode. Those checks will come
    from the remote Supabase-backed validation in a later stage.
    """

    BETA_LICENSE_KEY = "BETA-MYRALIS-001"
    DEMO_LICENSE_KEY = "CLIENT-DEMO-001"

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path(__file__).resolve().parents[1]
        self.config_dir = get_runtime_paths(self.root).config_root
        self.local_state_path = self.config_dir / "license_state.json"

    def validate_license(self, email: str, license_key: str) -> LicenseValidationResult:
        """Validate a license using the configured provider.

        Provider selection is controlled by ``MYRALIS_LICENSE_PROVIDER``.
        ``mock`` and the empty/default value keep the current local behavior.
        ``supabase`` enables a lazy, best-effort remote validation path.
        """

        provider = self._license_provider()
        if provider == "supabase":
            return self._validate_license_supabase(email, license_key)
        return self._validate_license_mock(email, license_key)

    def _validate_license_mock(
        self,
        email: str,
        license_key: str,
    ) -> LicenseValidationResult:
        """Validate a license using local mock rules.

        This path preserves the existing deterministic behavior and is the
        default when no Supabase configuration is present.
        """

        clean_email = str(email or "").strip()
        clean_license_key = str(license_key or "").strip()

        if not clean_email:
            return LicenseValidationResult(
                can_launch=False,
                reason="missing_email",
                email=None,
                license_key=None,
                user_id=None,
                license_id=None,
                role="guest",
                status=None,
                credits_balance=0.0,
                plan_name=None,
                developer_mode_enabled=False,
            )

        if not clean_license_key:
            return LicenseValidationResult(
                can_launch=False,
                reason="missing_license_key",
                email=clean_email,
                license_key=None,
                user_id=None,
                license_id=None,
                role="guest",
                status=None,
                credits_balance=0.0,
                plan_name=None,
                developer_mode_enabled=False,
            )

        persistent_result = self._validate_mock_persistent_license(
            clean_email,
            clean_license_key,
        )
        if persistent_result is not None:
            return persistent_result

        if clean_license_key == self.BETA_LICENSE_KEY:
            return LicenseValidationResult(
                can_launch=True,
                reason="ok",
                email=clean_email,
                license_key=clean_license_key,
                user_id="mock-user-admin",
                license_id="mock-license-beta",
                role="admin",
                status="beta",
                credits_balance=100.0,
                plan_name="Beta",
                developer_mode_enabled=True,
            )

        if clean_license_key == self.DEMO_LICENSE_KEY:
            return LicenseValidationResult(
                can_launch=True,
                reason="ok",
                email=clean_email,
                license_key=clean_license_key,
                user_id="mock-user-client",
                license_id="mock-license-demo",
                role="client",
                status="active",
                credits_balance=50.0,
            plan_name="Demo",
            developer_mode_enabled=False,
        )

        return LicenseValidationResult(
            can_launch=False,
            reason="license_not_found",
            email=clean_email,
            license_key=clean_license_key,
            user_id=None,
            license_id=None,
            role="guest",
            status=None,
            credits_balance=0.0,
            plan_name=None,
            developer_mode_enabled=False,
        )

    def _validate_mock_persistent_license(
        self,
        email: str,
        license_key: str,
    ) -> LicenseValidationResult | None:
        """Validate mock licenses persisted by LicenseAdminService.

        The local admin service stores users and licenses in
        ``config/license_admin_mock_state.json``. This file is convenience-only
        and may be missing or corrupt; in that case we silently fall back to the
        built-in legacy mock keys.
        """

        state = self._load_mock_admin_state()
        if not state:
            return None

        users = state.get("users", {})
        licenses = state.get("licenses", {})
        if not isinstance(users, dict) or not isinstance(licenses, dict):
            return None

        license_data = self._find_mock_license_by_key(licenses, license_key)
        if license_data is None:
            return None

        user_data = self._find_mock_user_by_email(users, email)
        if user_data is None:
            return self._inactive_result(
                can_launch=False,
                reason="user_not_found",
                email=email,
                license_key=license_key,
            )

        license_user_id = self._as_optional_str(license_data.get("user_id"))
        user_id = self._as_optional_str(user_data.get("id"))
        if not user_id or license_user_id != user_id:
            return self._inactive_result(
                can_launch=False,
                reason="license_does_not_belong_to_user",
                email=email,
                license_key=license_key,
                user_id=user_id,
                license_id=self._as_optional_str(license_data.get("id")),
                role=self._as_optional_str(user_data.get("role")) or "client",
                status=self._as_optional_str(license_data.get("status")),
                plan_name=self._as_optional_str(license_data.get("plan_name")),
                credits_balance=self._as_float(license_data.get("credits_balance")),
            )

        status = (self._as_optional_str(license_data.get("status")) or "").lower()
        if status == "expired":
            return self._inactive_result(
                can_launch=False,
                reason="license_expired",
                email=email,
                license_key=license_key,
                user_id=user_id,
                license_id=self._as_optional_str(license_data.get("id")),
                role=self._as_optional_str(user_data.get("role")) or "client",
                status=status,
                plan_name=self._as_optional_str(license_data.get("plan_name")),
                credits_balance=self._as_float(license_data.get("credits_balance")),
            )
        if status == "suspended":
            return self._inactive_result(
                can_launch=False,
                reason="license_suspended",
                email=email,
                license_key=license_key,
                user_id=user_id,
                license_id=self._as_optional_str(license_data.get("id")),
                role=self._as_optional_str(user_data.get("role")) or "client",
                status=status,
                plan_name=self._as_optional_str(license_data.get("plan_name")),
                credits_balance=self._as_float(license_data.get("credits_balance")),
            )
        if status not in {"active", "beta"}:
            return self._inactive_result(
                can_launch=False,
                reason="license_inactive",
                email=email,
                license_key=license_key,
                user_id=user_id,
                license_id=self._as_optional_str(license_data.get("id")),
                role=self._as_optional_str(user_data.get("role")) or "client",
                status=status or None,
                plan_name=self._as_optional_str(license_data.get("plan_name")),
                credits_balance=self._as_float(license_data.get("credits_balance")),
            )

        expires_at = self._parse_timestamp(license_data.get("expires_at"))
        if expires_at is not None and expires_at <= datetime.now(timezone.utc):
            return self._inactive_result(
                can_launch=False,
                reason="license_expired",
                email=email,
                license_key=license_key,
                user_id=user_id,
                license_id=self._as_optional_str(license_data.get("id")),
                role=self._as_optional_str(user_data.get("role")) or "client",
                status=status,
                plan_name=self._as_optional_str(license_data.get("plan_name")),
                credits_balance=self._as_float(license_data.get("credits_balance")),
            )

        credits_balance = self._as_float(license_data.get("credits_balance"))
        if credits_balance <= 0.0:
            return self._inactive_result(
                can_launch=False,
                reason="no_credits",
                email=email,
                license_key=license_key,
                user_id=user_id,
                license_id=self._as_optional_str(license_data.get("id")),
                role=self._as_optional_str(user_data.get("role")) or "client",
                status=status,
                plan_name=self._as_optional_str(license_data.get("plan_name")),
                credits_balance=credits_balance,
            )

        role = self._as_optional_str(user_data.get("role")) or "client"
        developer_mode_enabled = role in {"admin", "developer"}
        return LicenseValidationResult(
            can_launch=True,
            reason="ok",
            email=email,
            license_key=license_key,
            user_id=user_id,
            license_id=self._as_optional_str(license_data.get("id")),
            role=role,
            status=status,
            credits_balance=credits_balance,
            plan_name=self._as_optional_str(license_data.get("plan_name")),
            developer_mode_enabled=developer_mode_enabled,
        )

    def _validate_license_supabase(
        self,
        email: str,
        license_key: str,
    ) -> LicenseValidationResult:
        """Validate a license against Supabase when configured.

        This method is intentionally lazy-loaded and tolerant of missing
        configuration or package dependencies. It returns structured failure
        results instead of raising so startup can remain safe in development.
        """

        clean_email = str(email or "").strip()
        clean_license_key = str(license_key or "").strip()

        if not clean_email:
            return self._missing_email_result()

        if not clean_license_key:
            return self._missing_license_key_result(clean_email)

        supabase_url = str(os.getenv("SUPABASE_URL", "") or "").strip()
        supabase_service_role_key = str(
            os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or ""
        ).strip()
        if not supabase_url or not supabase_service_role_key:
            return self._inactive_result(
                can_launch=False,
                reason="supabase_not_configured",
                email=clean_email,
                license_key=clean_license_key,
            )

        try:
            from supabase import create_client  # type: ignore[import-not-found]
        except ImportError:
            return self._inactive_result(
                can_launch=False,
                reason="supabase_package_missing",
                email=clean_email,
                license_key=clean_license_key,
            )

        try:
            client = create_client(supabase_url, supabase_service_role_key)
            user_rows = (
                client.table("app_users")
                .select("id,email,role")
                .eq("email", clean_email)
                .limit(1)
                .execute()
            )
            user_data = self._first_row(user_rows)
            if not user_data:
                return self._inactive_result(
                    can_launch=False,
                    reason="user_not_found",
                    email=clean_email,
                    license_key=clean_license_key,
                )

            license_rows = (
                client.table("licenses")
                .select(
                    "id,license_key,user_id,client_name,status,credits_balance,plan_name,"
                    "expires_at,developer_mode_allowed,created_at,updated_at"
                )
                .eq("license_key", clean_license_key)
                .limit(1)
                .execute()
            )
            license_data = self._first_row(license_rows)
            if not license_data:
                return self._inactive_result(
                    can_launch=False,
                    reason="license_not_found",
                    email=clean_email,
                    license_key=clean_license_key,
                    user_id=self._as_optional_str(user_data.get("id")),
                )

            user_id = self._as_optional_str(user_data.get("id"))
            license_user_id = self._as_optional_str(license_data.get("user_id"))
            if not user_id or license_user_id != user_id:
                return self._inactive_result(
                    can_launch=False,
                    reason="license_does_not_belong_to_user",
                    email=clean_email,
                    license_key=clean_license_key,
                    user_id=user_id,
                    license_id=self._as_optional_str(license_data.get("id")),
                    role=self._as_optional_str(user_data.get("role")) or "client",
                    status=self._as_optional_str(license_data.get("status")),
                    plan_name=self._as_optional_str(license_data.get("plan_name")),
                    credits_balance=self._as_float(license_data.get("credits_balance")),
                )

            status = (self._as_optional_str(license_data.get("status")) or "").lower()
            if status == "expired":
                return self._inactive_result(
                    can_launch=False,
                    reason="license_expired",
                    email=clean_email,
                    license_key=clean_license_key,
                    user_id=user_id,
                    license_id=self._as_optional_str(license_data.get("id")),
                    role=self._as_optional_str(user_data.get("role")) or "client",
                    status=status,
                    plan_name=self._as_optional_str(license_data.get("plan_name")),
                    credits_balance=self._as_float(license_data.get("credits_balance")),
                )
            if status == "suspended":
                return self._inactive_result(
                    can_launch=False,
                    reason="license_suspended",
                    email=clean_email,
                    license_key=clean_license_key,
                    user_id=user_id,
                    license_id=self._as_optional_str(license_data.get("id")),
                    role=self._as_optional_str(user_data.get("role")) or "client",
                    status=status,
                    plan_name=self._as_optional_str(license_data.get("plan_name")),
                    credits_balance=self._as_float(license_data.get("credits_balance")),
                )
            if status not in {"active", "beta"}:
                return self._inactive_result(
                    can_launch=False,
                    reason="license_inactive",
                    email=clean_email,
                    license_key=clean_license_key,
                    user_id=user_id,
                    license_id=self._as_optional_str(license_data.get("id")),
                    role=self._as_optional_str(user_data.get("role")) or "client",
                    status=status or None,
                    plan_name=self._as_optional_str(license_data.get("plan_name")),
                    credits_balance=self._as_float(license_data.get("credits_balance")),
                )

            expires_at = self._parse_timestamp(license_data.get("expires_at"))
            if expires_at is not None and expires_at <= datetime.now(timezone.utc):
                return self._inactive_result(
                    can_launch=False,
                    reason="license_expired",
                    email=clean_email,
                    license_key=clean_license_key,
                    user_id=user_id,
                    license_id=self._as_optional_str(license_data.get("id")),
                    role=self._as_optional_str(user_data.get("role")) or "client",
                    status=status,
                    plan_name=self._as_optional_str(license_data.get("plan_name")),
                    credits_balance=self._as_float(license_data.get("credits_balance")),
                )

            credits_balance = self._as_float(license_data.get("credits_balance"))
            if credits_balance <= 0.0:
                return self._inactive_result(
                    can_launch=False,
                    reason="no_credits",
                    email=clean_email,
                    license_key=clean_license_key,
                    user_id=user_id,
                    license_id=self._as_optional_str(license_data.get("id")),
                    role=self._as_optional_str(user_data.get("role")) or "client",
                    status=status,
                    plan_name=self._as_optional_str(license_data.get("plan_name")),
                    credits_balance=credits_balance,
                )

            role = self._as_optional_str(user_data.get("role")) or "client"
            developer_mode_enabled = role in {"admin", "developer"}
            return LicenseValidationResult(
                can_launch=True,
                reason="ok",
                email=clean_email,
                license_key=clean_license_key,
                user_id=user_id,
                license_id=self._as_optional_str(license_data.get("id")),
                role=role,
                status=status,
                credits_balance=credits_balance,
                plan_name=self._as_optional_str(license_data.get("plan_name")),
                developer_mode_enabled=developer_mode_enabled,
            )
        except Exception:
            LOGGER.exception("Supabase license validation failed")
            return self._inactive_result(
                can_launch=False,
                reason="supabase_error",
                email=clean_email,
                license_key=clean_license_key,
            )

    def is_debug_allowed(self, result: LicenseValidationResult) -> bool:
        """Return True only when launch is allowed and debug is permitted."""

        return bool(
            result.can_launch
            and str(result.role or "").strip().lower() in {"admin", "developer"}
        )

    def has_credits(self, result: LicenseValidationResult) -> bool:
        """Return True when the license has a positive credit balance."""

        return float(result.credits_balance) > 0.0

    def deduct_credits(
        self,
        license_key: str,
        amount: float,
        reason: str = "usage",
    ) -> dict[str, Any]:
        """Deduct credits from a license.

        This will later be used by UsageManager and ConversationManager. In
        production, Supabase must remain the source of truth. The local mock
        path only updates persistent mock admin state for internal testing.
        """

        clean_license_key = str(license_key or "").strip()
        clean_reason = str(reason or "").strip() or "usage"
        try:
            clean_amount = float(amount)
        except (TypeError, ValueError):
            return {"ok": False, "reason": "invalid_amount"}
        if clean_amount <= 0.0:
            return {"ok": False, "reason": "invalid_amount"}
        if not clean_license_key:
            return {"ok": False, "reason": "license_not_found_or_not_persistent"}

        provider = self._license_provider()
        if provider == "supabase":
            return self._deduct_credits_supabase(
                license_key=clean_license_key,
                amount=clean_amount,
                reason=clean_reason,
            )
        return self._deduct_credits_mock(
            license_key=clean_license_key,
            amount=clean_amount,
            reason=clean_reason,
        )

    def _license_provider(self) -> str:
        provider = str(os.getenv("MYRALIS_LICENSE_PROVIDER", "") or "").strip().lower()
        return provider if provider == "supabase" else "mock"

    def _deduct_credits_mock(
        self,
        *,
        license_key: str,
        amount: float,
        reason: str,
    ) -> dict[str, Any]:
        """Deduct credits from persistent mock admin state only."""

        state = self._load_mock_admin_state()
        if not state:
            return {"ok": False, "reason": "license_not_found_or_not_persistent"}

        licenses = state.get("licenses", {})
        if not isinstance(licenses, dict):
            return {"ok": False, "reason": "license_not_found_or_not_persistent"}

        license_data = self._find_mock_license_by_key(licenses, license_key)
        if license_data is None:
            return {"ok": False, "reason": "license_not_found_or_not_persistent"}

        current_balance = self._as_float(license_data.get("credits_balance"))
        if current_balance < amount:
            return {
                "ok": False,
                "reason": "insufficient_credits",
                "credits_balance": current_balance,
            }

        new_balance = round(current_balance - amount, 4)
        license_data["credits_balance"] = new_balance
        license_data["updated_at"] = self._now()
        credit_ledger = state.get("credit_ledger", [])
        if not isinstance(credit_ledger, list):
            credit_ledger = []
        credit_ledger.append(
            {
                "id": self._new_id(),
                "license_key": license_key,
                "license_id": self._as_optional_str(license_data.get("id")),
                "change_amount": -amount,
                "reason": reason,
                "balance_after": new_balance,
                "created_at": self._now(),
            }
        )
        state["credit_ledger"] = credit_ledger
        self._save_mock_admin_state(state)
        return {"ok": True, "credits_balance": new_balance}

    def _deduct_credits_supabase(
        self,
        *,
        license_key: str,
        amount: float,
        reason: str,
    ) -> dict[str, Any]:
        """Deduct credits in Supabase when the provider is enabled."""

        supabase_url = str(os.getenv("SUPABASE_URL", "") or "").strip()
        supabase_service_role_key = str(
            os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or ""
        ).strip()
        if not supabase_url or not supabase_service_role_key:
            return {"ok": False, "reason": "supabase_not_configured"}

        try:
            from supabase import create_client  # type: ignore[import-not-found]
        except ImportError:
            return {"ok": False, "reason": "supabase_package_missing"}

        try:
            client = create_client(supabase_url, supabase_service_role_key)
            response = (
                client.table("licenses")
                .select("id,license_key,credits_balance")
                .eq("license_key", license_key)
                .limit(1)
                .execute()
            )
            license_data = self._first_row(response)
            if not license_data:
                return {"ok": False, "reason": "license_not_found_or_not_persistent"}

            current_balance = self._as_float(license_data.get("credits_balance"))
            if current_balance < amount:
                return {
                    "ok": False,
                    "reason": "insufficient_credits",
                    "credits_balance": current_balance,
                }

            new_balance = round(current_balance - amount, 4)
            client.table("licenses").update({"credits_balance": new_balance}).eq(
                "license_key",
                license_key,
            ).execute()
            client.table("credit_ledger").insert(
                {
                    "license_id": license_data.get("id"),
                    "change_amount": -amount,
                    "reason": reason,
                    "balance_after": new_balance,
                }
            ).execute()
            return {"ok": True, "credits_balance": new_balance}
        except Exception:
            LOGGER.exception("Supabase credit deduction failed")
            return {"ok": False, "reason": "supabase_error"}

    def _inactive_result(
        self,
        *,
        can_launch: bool,
        reason: str,
        email: str | None,
        license_key: str | None,
        user_id: str | None = None,
        license_id: str | None = None,
        role: str = "guest",
        status: str | None = None,
        credits_balance: float = 0.0,
        plan_name: str | None = None,
        developer_mode_enabled: bool = False,
    ) -> LicenseValidationResult:
        return LicenseValidationResult(
            can_launch=can_launch,
            reason=reason,
            email=email,
            license_key=license_key,
            user_id=user_id,
            license_id=license_id,
            role=role,
            status=status,
            credits_balance=credits_balance,
            plan_name=plan_name,
            developer_mode_enabled=developer_mode_enabled,
        )

    def _missing_email_result(self) -> LicenseValidationResult:
        return self._inactive_result(
            can_launch=False,
            reason="missing_email",
            email=None,
            license_key=None,
        )

    def _missing_license_key_result(self, email: str) -> LicenseValidationResult:
        return self._inactive_result(
            can_launch=False,
            reason="missing_license_key",
            email=email,
            license_key=None,
        )

    def _first_row(self, response: Any) -> dict[str, Any] | None:
        data = getattr(response, "data", None)
        if isinstance(data, list) and data:
            first = data[0]
            return first if isinstance(first, dict) else None
        if isinstance(data, dict):
            return data
        return None

    def _as_optional_str(self, value: Any) -> str | None:
        clean = str(value or "").strip()
        return clean or None

    def _as_float(self, value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _parse_timestamp(self, value: Any) -> datetime | None:
        clean = self._as_optional_str(value)
        if not clean:
            return None
        try:
            normalized = clean.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _load_mock_admin_state(self) -> dict[str, Any]:
        """Load mock admin state persisted by LicenseAdminService.

        This is a best-effort convenience cache. Corruption or absence must not
        break licensing; callers should fall back to the legacy hardcoded mock.
        """

        state_path = self.config_dir / "license_admin_mock_state.json"
        if not state_path.exists():
            return {}

        try:
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOGGER.warning("Could not load mock admin state; falling back to legacy mock.")
            return {}

        return loaded if isinstance(loaded, dict) else {}

    def _save_mock_admin_state(self, state: dict[str, Any]) -> None:
        """Persist mock admin state after a mutation."""

        self.config_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "users": state.get("users", {}),
            "licenses": state.get("licenses", {}),
            "credit_ledger": state.get("credit_ledger", []),
        }
        temp_path = self.config_dir / "license_admin_mock_state.json.tmp"
        temp_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(self.config_dir / "license_admin_mock_state.json")

    def _find_mock_user_by_email(
        self,
        users: dict[str, Any],
        email: str,
    ) -> dict[str, Any] | None:
        clean_email = str(email or "").strip()
        candidate = users.get(clean_email)
        if isinstance(candidate, dict):
            return candidate
        for user in users.values():
            if isinstance(user, dict) and self._as_optional_str(user.get("email")) == clean_email:
                return user
        return None

    def _find_mock_license_by_key(
        self,
        licenses: dict[str, Any],
        license_key: str,
    ) -> dict[str, Any] | None:
        clean_license_key = str(license_key or "").strip()
        candidate = licenses.get(clean_license_key)
        if isinstance(candidate, dict):
            return candidate
        for license_record in licenses.values():
            if (
                isinstance(license_record, dict)
                and self._as_optional_str(license_record.get("license_key")) == clean_license_key
            ):
                return license_record
        return None

    def load_local_license_state(self) -> dict[str, Any]:
        """Load cached license credentials from disk.

        This file is only a convenience layer for the user. If the file is
        missing or corrupted, the app should continue without failing. The
        returned data must not be trusted as a source of permissions.
        """

        if not self.local_state_path.exists():
            return {}

        try:
            loaded = json.loads(self.local_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOGGER.warning("Could not load local license state; ignoring cache.")
            return {}

        if not isinstance(loaded, dict):
            return {}
        return loaded

    def save_local_license_state(
        self,
        email: str,
        license_key: str,
        result: LicenseValidationResult,
    ) -> None:
        """Persist local license credentials for convenience only.

        The saved file is not a secure source of truth. It intentionally omits
        role and developer-mode state, because those permissions must never be
        trusted from local storage.
        """

        self.config_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "email": str(email or "").strip(),
            "license_key": str(license_key or "").strip(),
            "last_validation_reason": result.reason,
            "last_can_launch": bool(result.can_launch),
            "last_plan_name": result.plan_name,
            "last_credits_balance": float(result.credits_balance),
        }
        temp_path = self.local_state_path.with_suffix(self.local_state_path.suffix + ".tmp")
        temp_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(self.local_state_path)

    def validate_saved_license(self) -> LicenseValidationResult:
        """Validate cached credentials using the mock validator.

        This only replays the locally cached email and license key. It does not
        imply trust in any stored permission-related fields.
        """

        cached = self.load_local_license_state()
        email = str(cached.get("email", "") or "").strip()
        license_key = str(cached.get("license_key", "") or "").strip()

        if not email or not license_key:
            return LicenseValidationResult(
                can_launch=False,
                reason="missing_saved_license",
                email=email or None,
                license_key=license_key or None,
                user_id=None,
                license_id=None,
                role="guest",
                status=None,
                credits_balance=0.0,
                plan_name=None,
                developer_mode_enabled=False,
            )

        return self.validate_license(email, license_key)

    def clear_local_license_state(self) -> None:
        """Remove the cached local license file if it exists.

        This only clears convenience state and should never be needed to revoke
        access, since the local file is not considered secure authority.
        """

        try:
            self.local_state_path.unlink()
        except FileNotFoundError:
            return
