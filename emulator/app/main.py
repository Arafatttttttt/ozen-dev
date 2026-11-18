"""Ozen-Sync accounting emulator: pretends to be 1C:Accounting / an external ERP.

Run:  uvicorn app.main:app --port 8000
Env:  OZEN_FAILURE_RATE  share of requests answered with 500 (default 0.2)
      OZEN_SEED          RNG seed for reproducible chaos (optional)
"""

from __future__ import annotations

import logging
import os
import random
from datetime import datetime, timezone
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .schemas import (
    ChaosSettings,
    ErrorResponse,
    SyncDocument,
    SyncResponse,
    ValidationErrorItem,
)
from .store import DocumentStore

log = logging.getLogger("ozen.emulator")

SYNC_PATH = "/api/v1/accounting/sync"


def create_app(failure_rate: float | None = None, seed: int | None = None) -> FastAPI:
    if failure_rate is None:
        failure_rate = float(os.getenv("OZEN_FAILURE_RATE", "0.2"))
    if seed is None and os.getenv("OZEN_SEED"):
        seed = int(os.environ["OZEN_SEED"])

    app = FastAPI(
        title="Ozen-Sync Accounting Emulator",
        version="1.0.0",
        description="Receiving side for 1C:Fitness Club documents. Validates the "
        "contract, deduplicates by Idempotency-Key and randomly fails to test retries.",
    )
    app.state.failure_rate = failure_rate
    app.state.rng = random.Random(seed)
    app.state.store = DocumentStore()

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        # FastAPI answers 422 by default; the contract promises 400 for a bad packet.
        app.state.store.count("rejected")
        errors = [
            ValidationErrorItem(
                field=".".join(str(p) for p in err["loc"] if p != "body") or "body",
                message=err["msg"],
                type=err["type"],
            )
            for err in exc.errors()
        ]
        log.warning("400 rejected: %s", "; ".join(f"{e.field}: {e.message}" for e in errors))
        body = ErrorResponse(status="rejected", error="validation_error", detail=errors)
        return JSONResponse(status_code=400, content=body.model_dump(mode="json"))

    @app.post(
        SYNC_PATH,
        response_model=SyncResponse,
        responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
        summary="Receive a document from 1C",
    )
    def sync_document(
        doc: SyncDocument,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        attempt: str | None = Header(default=None, alias="X-Ozen-Attempt"),
    ):
        store: DocumentStore = app.state.store
        store.count("requests")

        # Chaos happens before anything is stored: a crash "before commit".
        if app.state.rng.random() < app.state.failure_rate:
            store.count("simulated_failures")
            log.warning("500 simulated failure: doc=%s attempt=%s", doc.document_id, attempt)
            body = ErrorResponse(status="error", error="internal_error", detail="Simulated failure")
            return JSONResponse(status_code=500, content=body.model_dump(mode="json"))

        key = idempotency_key or (str(doc.message_id) if doc.message_id else None)
        result = store.apply(doc, key)
        log.info(
            "200 %s: doc=%s type=%s amount=%s attempt=%s",
            result.status, doc.document_id, doc.object_type, doc.amount, attempt,
        )
        return SyncResponse(
            status=result.status,
            document_id=doc.document_id,
            accounting_entry_id=result.document.accounting_entry_id,
            received_at=datetime.now(timezone.utc),
        )

    @app.post(
        "/api/v1/accounting/validate",
        summary="Dry-run: validate a packet without storing it and without chaos",
    )
    def validate_document(doc: SyncDocument):
        return {"status": "valid", "document_id": str(doc.document_id), "items": len(doc.items)}

    @app.get("/api/v1/accounting/documents", summary="Documents received so far")
    def list_documents():
        docs = app.state.store.documents.values()
        return [
            {
                "document_id": str(d.document_id),
                "accounting_entry_id": d.accounting_entry_id,
                "object_type": d.object_type,
                "operation": d.operation,
                "version": d.version,
                "amount": d.payload["amount"],
                "client_name": d.payload["client_name"],
                "deliveries": d.deliveries,
                "received_at": d.received_at.isoformat(),
            }
            for d in docs
        ]

    @app.get("/api/v1/accounting/documents/{document_id}")
    def get_document(document_id: UUID):
        stored = app.state.store.documents.get(document_id)
        if stored is None:
            raise HTTPException(status_code=404, detail="Document not found")
        return stored.payload | {"accounting_entry_id": stored.accounting_entry_id}

    @app.get("/api/v1/admin/stats", summary="Counters for resilience tests")
    def stats():
        return app.state.store.snapshot_stats() | {"failure_rate": app.state.failure_rate}

    @app.put("/api/v1/admin/chaos", summary="Change the simulated failure rate at runtime")
    def set_chaos(settings: ChaosSettings):
        app.state.failure_rate = settings.failure_rate
        return {"failure_rate": app.state.failure_rate}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = create_app()
