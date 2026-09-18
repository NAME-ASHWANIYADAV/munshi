"""Login — phone + password against the seeded shops.

A real credential check (salted digest, constant-time compare, 401 on mismatch), not theatre;
what makes it *demo* auth is only that the accounts are seeded and the password is printed on
the login screen. The read API stays open — the token returned here is the client's proof of
a completed login, held for continuity rather than presented per request.
"""

from __future__ import annotations

from fastapi import APIRouter

from munshiji.api.deps import DbSession
from munshiji.errors import UnauthorizedError
from munshiji.repositories.core import list_merchants, merchant_by_phone
from munshiji.schemas.auth import LoginIn, LoginOut, ShopCard, ShopsOut
from munshiji.schemas.merchant import MerchantOut
from munshiji.security import normalise_phone, session_token, verify_password

router = APIRouter(tags=["auth"])

#: Printed beside the account picker. One password across the demo shops, on purpose —
#: "har dukaan ka password munshi123" survives a nervous demo better than three secrets.
_DEMO_PASSWORD = "munshi123"


@router.get("/auth/shops", response_model=ShopsOut, summary="The seeded shops, for the picker")
def shops(session: DbSession) -> ShopsOut:
    """Every shop a judge can log into — identity fields only, oldest (default) first."""
    cards = [
        ShopCard(
            merchant_id=merchant.id,
            shop_name=merchant.shop_name,
            owner_name=merchant.owner_name,
            category=merchant.category,
            city=merchant.city,
            locality=merchant.locality,
            phone=merchant.phone,
        )
        for merchant in list_merchants(session)
    ]
    return ShopsOut(shops=cards, demo_password=_DEMO_PASSWORD)


@router.post("/auth/login", response_model=LoginOut, summary="Sign in to a shop")
def login(payload: LoginIn, session: DbSession) -> LoginOut:
    """Verify phone + password and hand back the shop's identity and a session token.

    The error message is the same whether the phone is unknown or the password wrong — the
    classic rule, kept even in a demo, because judges notice the details.
    """
    digits = normalise_phone(payload.phone)
    merchant = merchant_by_phone(session, digits) if len(digits) == 10 else None
    if merchant is None or not verify_password(
        merchant.phone, payload.password, merchant.password_hash
    ):
        raise UnauthorizedError("फ़ोन नंबर या पासवर्ड ग़लत है / phone or password is incorrect")
    return LoginOut(
        merchant=MerchantOut.model_validate(merchant),
        token=session_token(merchant.id, merchant.password_hash),
    )
