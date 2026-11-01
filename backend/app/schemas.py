from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProductInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    category: str = Field(default="General", min_length=1, max_length=80)
    sku: str | None = Field(default=None, max_length=50)
    quantity: int = Field(ge=0)
    min_stock: int = Field(default=5, ge=0)
    unit: str = Field(default="unidad", min_length=1, max_length=24)
    price: Decimal = Field(ge=0, max_digits=10, decimal_places=2)

    @field_validator("name", "category", "unit")
    @classmethod
    def trim_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Este campo no puede quedar vacío")
        return value

    @field_validator("sku")
    @classmethod
    def trim_sku(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None


class Product(ProductInput):
    id: str
    warehouse_id: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ProductPage(BaseModel):
    items: list[Product]
    total: int
    page: int
    page_size: int
    pages: int


class MovementInput(BaseModel):
    type: str = Field(pattern="^(entrada|salida|ajuste)$")
    quantity: int = Field(ge=0)
    note: str | None = Field(default=None, max_length=300)

    @field_validator("quantity")
    @classmethod
    def validate_movement_quantity(cls, value: int, info):
        if info.data.get("type") in {"entrada", "salida"} and value == 0:
            raise ValueError("La cantidad debe ser mayor a cero")
        return value


class Movement(BaseModel):
    id: str
    user_id: str
    product_id: str
    type: str
    quantity: int
    previous_stock: int
    new_stock: int
    note: str | None
    created_at: datetime


class WarehouseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def trim_warehouse_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("El nombre de la bodega no puede quedar vacío")
        return value


class WarehouseMemberInput(BaseModel):
    user_id: UUID
    role: Literal["admin", "member", "viewer"] = "member"
