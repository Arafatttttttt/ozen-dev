"""In-memory "accounting ledger" with idempotency and stale-version protection."""

from __future__ import annotations

import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID, uuid4

from .schemas import SyncDocument


@dataclass
class StoredDocument:
    document_id: UUID
    accounting_entry_id: str
    object_type: str
    operation: str
    version: int | None
    payload: dict
    received_at: datetime
    deliveries: int = 1


@dataclass
class ApplyResult:
    status: str
    document: StoredDocument


@dataclass
class DocumentStore:
    documents: dict[UUID, StoredDocument] = field(default_factory=dict)
    processed_keys: dict[str, ApplyResult] = field(default_factory=dict)
    stats: Counter = field(default_factory=Counter)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def apply(self, doc: SyncDocument, idempotency_key: str | None) -> ApplyResult:
        """Upsert a document. Safe to call many times with the same key."""
        with self._lock:
            if idempotency_key and idempotency_key in self.processed_keys:
                previous = self.processed_keys[idempotency_key]
                previous.document.deliveries += 1
                self.stats["duplicate"] += 1
                return ApplyResult("duplicate", previous.document)

            now = datetime.now(timezone.utc)
            existing = self.documents.get(doc.document_id)

            if existing and _is_stale(doc.version, existing.version):
                # An older snapshot arrived after a newer one (e.g. it was stuck in retries).
                result = ApplyResult("stale_ignored", existing)
            else:
                stored = StoredDocument(
                    document_id=doc.document_id,
                    accounting_entry_id=existing.accounting_entry_id
                    if existing
                    else f"ACC-{uuid4().hex[:10].upper()}",
                    object_type=doc.object_type,
                    operation=doc.operation,
                    version=doc.version,
                    payload=doc.model_dump(mode="json"),
                    received_at=now,
                    deliveries=existing.deliveries + 1 if existing else 1,
                )
                self.documents[doc.document_id] = stored
                if doc.operation == "cancel":
                    status = "cancelled"
                else:
                    status = "updated" if existing else "accepted"
                result = ApplyResult(status, stored)

            if idempotency_key:
                self.processed_keys[idempotency_key] = result
            self.stats[result.status] += 1
            return result

    def count(self, key: str) -> None:
        with self._lock:
            self.stats[key] += 1

    def snapshot_stats(self) -> dict:
        with self._lock:
            return {**self.stats, "documents": len(self.documents)}


def _is_stale(incoming: int | None, current: int | None) -> bool:
    return incoming is not None and current is not None and incoming < current
