from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.license_manager import LicenseValidationResult


def mask_license_key(value: str | None) -> str:
    clean = str(value or "").strip()
    if not clean:
        return "-"
    parts = clean.split("-")
    if len(parts) >= 3:
        return f"{parts[0]}-****-{parts[-1]}"
    if len(clean) <= 8:
        return "*" * len(clean)
    return f"{clean[:4]}****{clean[-4:]}"


@dataclass(frozen=True)
class BackendAuthorizationContext:
    authenticated: bool = False
    email: str | None = None
    license_key_masked: str = "-"
    license_status: str | None = None
    plan_name: str | None = None
    role: str = "guest"
    developer_mode_allowed: bool = False
    admin_authorized: bool = False
    validated_at: str | None = None

    @property
    def authorized_session(self) -> bool:
        return bool(self.authenticated)

    @property
    def can_open_technical_panel(self) -> bool:
        role = str(self.role or "").strip().lower()
        return bool(self.admin_authorized or role == "developer")

    @classmethod
    def from_license_result(
        cls,
        result: LicenseValidationResult | None,
        *,
        validated_at: str | None = None,
    ) -> BackendAuthorizationContext:
        if result is None:
            return cls()
        role = str(result.role or "guest").strip().lower() or "guest"
        return cls(
            authenticated=bool(result.can_launch),
            email=result.email,
            license_key_masked=mask_license_key(result.license_key),
            license_status=result.status,
            plan_name=result.plan_name,
            role=role,
            developer_mode_allowed=bool(result.developer_mode_enabled),
            admin_authorized=role == "admin",
            validated_at=validated_at,
        )

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any] | None,
        *,
        validated_at: str | None = None,
    ) -> BackendAuthorizationContext:
        if not isinstance(payload, dict):
            return cls()
        role = str(payload.get("role", "guest") or "guest").strip().lower() or "guest"
        return cls(
            authenticated=bool(payload.get("authenticated", False)),
            email=_as_optional_str(payload.get("email")),
            license_key_masked=mask_license_key(payload.get("license_key")),
            license_status=_as_optional_str(payload.get("license_status")),
            plan_name=_as_optional_str(payload.get("plan_name")),
            role=role,
            developer_mode_allowed=bool(payload.get("developer_mode_allowed", False)),
            admin_authorized=bool(payload.get("admin_authorized", False)) or role == "admin",
            validated_at=validated_at,
        )


def _as_optional_str(value: Any) -> str | None:
    clean = str(value or "").strip()
    return clean or None

