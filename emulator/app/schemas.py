"""API contract v1: the JSON document that 1C:Fitness Club sends to accounting."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

ObjectType = Literal["ПродажаАбонемента", "Возврат", "Оплата"]
Operation = Literal["upsert", "cancel"]

# Rounding tolerance between the document total and the sum of its lines.
AMOUNT_TOLERANCE = Decimal("0.01")


class Item(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    line: int | None = Field(default=None, ge=1)
    name: str = Field(min_length=1, max_length=255)
    quantity: Decimal = Field(gt=0, max_digits=15, decimal_places=3)
    price: Decimal = Field(ge=0, max_digits=15, decimal_places=2)
    amount: Decimal = Field(ge=0, max_digits=15, decimal_places=2)


class SyncDocument(BaseModel):
    # Unknown fields are ignored so 1C can add fields without breaking the receiver.
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    # Required by the contract
    document_id: UUID
    client_name: str = Field(min_length=1, max_length=255)
    amount: Decimal = Field(gt=0, max_digits=15, decimal_places=2)
    items: list[Item] = Field(min_length=1, max_length=1000)
    object_type: ObjectType

    # Optional metadata
    schema_version: str = "1.0"
    message_id: UUID | None = None
    source: str | None = None
    operation: Operation = "upsert"
    version: int | None = Field(default=None, ge=0)
    document_number: str | None = None
    document_date: datetime | None = None
    client_id: str | None = None
    currency: str = Field(default="KZT", pattern=r"^[A-Z]{3}$")

    @model_validator(mode="after")
    def amount_matches_items(self) -> SyncDocument:
        total = sum((item.amount for item in self.items), Decimal("0"))
        if abs(total - self.amount) > AMOUNT_TOLERANCE:
            raise ValueError(
                f"amount ({self.amount}) does not match the sum of items.amount ({total})"
            )
        return self


class SyncResponse(BaseModel):
    status: Literal["accepted", "updated", "cancelled", "duplicate", "stale_ignored"]
    document_id: UUID
    accounting_entry_id: str
    received_at: datetime


class ValidationErrorItem(BaseModel):
    field: str
    message: str
    type: str


class ErrorResponse(BaseModel):
    status: Literal["rejected", "error"]
    error: str
    detail: list[ValidationErrorItem] | str


class ChaosSettings(BaseModel):
    failure_rate: float = Field(ge=0.0, le=1.0)
