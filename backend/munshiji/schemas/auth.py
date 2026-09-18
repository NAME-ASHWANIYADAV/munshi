"""Login DTOs.

The shops list is public by design — it *is* the login screen's account picker, and these are
seeded demo shops whose one shared password is printed beside them. Nothing here ever carries
a hash.
"""

from __future__ import annotations

from pydantic import Field

from munshiji.schemas.common import ApiModel
from munshiji.schemas.merchant import MerchantOut

__all__ = ["LoginIn", "LoginOut", "ShopCard", "ShopsOut"]


class LoginIn(ApiModel):
    phone: str = Field(min_length=6, max_length=20, description="With or without +91/spaces.")
    password: str = Field(min_length=1, max_length=72)


class LoginOut(ApiModel):
    merchant: MerchantOut
    token: str


class ShopCard(ApiModel):
    """One row of the account picker — identity only, no secrets."""

    merchant_id: str
    shop_name: str
    owner_name: str
    category: str
    city: str
    locality: str
    phone: str


class ShopsOut(ApiModel):
    shops: list[ShopCard]
    #: The one demo password, so the picker can prefill it. A real deployment deletes this field.
    demo_password: str = ""
