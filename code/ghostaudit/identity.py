"""Identity resolution for the Ghost Data deletion audit.

The challenge guarantees that request emails resolve to either a current or a
historical identity.  We keep the full identifier set for each user so that
evidence can be linked through old email addresses, phone numbers and email
hashes as well as through the stable user_id.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Optional, Set


def norm_email(value: object) -> str:
    return str(value or "").strip().lower()


def norm_phone(value: object) -> str:
    return re.sub(r"\D", "", str(value or ""))


def norm_hash(value: object) -> str:
    return str(value or "").strip().lower()


@dataclass
class Identity:
    user_id: str
    name: str
    email: str
    phone: str
    email_hash: str
    aliases: Set[str] = field(default_factory=set)

    @property
    def key_values(self) -> Set[str]:
        return set(self.aliases)


class IdentityIndex:
    def __init__(self, dataset_dir: Path):
        self.dataset_dir = Path(dataset_dir)
        self.identities: Dict[str, Identity] = {}
        self._email_to_user: Dict[str, str] = {}
        self._phone_to_user: Dict[str, str] = {}
        self._hash_to_user: Dict[str, str] = {}
        self._load()

    def _add_email(self, value: str, user_id: str) -> None:
        value = norm_email(value)
        if value:
            self._email_to_user.setdefault(value, user_id)

    def _add_phone(self, value: str, user_id: str) -> None:
        value = norm_phone(value)
        if value:
            self._phone_to_user.setdefault(value, user_id)

    def _add_hash(self, value: str, user_id: str) -> None:
        value = norm_hash(value)
        if value:
            self._hash_to_user.setdefault(value, user_id)

    def _load(self) -> None:
        with (self.dataset_dir / "user_identities.csv").open(
            encoding="utf-8", newline=""
        ) as fh:
            for row in csv.DictReader(fh):
                ident = Identity(
                    user_id=row["user_id"],
                    name=row["name"],
                    email=row["email"],
                    phone=row["phone"],
                    email_hash=row["email_hash"],
                )
                ident.aliases.add(norm_email(ident.email))
                ident.aliases.add(norm_phone(ident.phone))
                ident.aliases.add(norm_hash(ident.email_hash))
                self.identities[ident.user_id] = ident
                self._add_email(ident.email, ident.user_id)
                self._add_phone(ident.phone, ident.user_id)
                self._add_hash(ident.email_hash, ident.user_id)

        audit_path = self.dataset_dir / "user_audit_log.csv"
        with audit_path.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                user_id = row["user_id"]
                ident = self.identities.get(user_id)
                if not ident:
                    continue
                field_name = row["field_name"]
                for value in (row.get("old_value", ""), row.get("new_value", "")):
                    if not value:
                        continue
                    if field_name == "email":
                        ident.aliases.add(norm_email(value))
                        self._add_email(value, user_id)
                    elif field_name == "phone":
                        ident.aliases.add(norm_phone(value))
                        self._add_phone(value, user_id)
                    elif field_name == "email_hash":
                        ident.aliases.add(norm_hash(value))
                        self._add_hash(value, user_id)

    def resolve_email(self, email: str) -> Optional[str]:
        return self._email_to_user.get(norm_email(email))

    def resolve_phone(self, phone: str) -> Optional[str]:
        return self._phone_to_user.get(norm_phone(phone))

    def resolve_hash(self, email_hash: str) -> Optional[str]:
        return self._hash_to_user.get(norm_hash(email_hash))

    def resolve_text(self, value: object) -> Optional[str]:
        """Resolve a raw identifier value without relying on field names."""
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        if "@" in text:
            return self.resolve_email(text)
        if text.lower().startswith("user_"):
            return text if text in self.identities else None
        if re.fullmatch(r"[0-9a-fA-F]{64}", text):
            return self.resolve_hash(text)
        return self.resolve_phone(text)

    def resolve_any(self, *values: object) -> Optional[str]:
        for value in values:
            if isinstance(value, dict):
                for nested in value.values():
                    user_id = self.resolve_any(nested)
                    if user_id:
                        return user_id
            elif isinstance(value, (list, tuple, set)):
                for nested in value:
                    user_id = self.resolve_any(nested)
                    if user_id:
                        return user_id
            else:
                user_id = self.resolve_text(value)
                if user_id:
                    return user_id
        return None

    def values_for(self, user_id: str) -> Set[str]:
        ident = self.identities.get(user_id)
        return set(ident.aliases) if ident else set()
