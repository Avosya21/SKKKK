from fastapi import APIRouter, Depends, Response

from app.app.config import Settings, get_settings
from app.app.services.acme_challenge import proxy_acme_challenge

router = APIRouter(tags=["acme"])


@router.get("/.well-known/acme-challenge/{token}")
def acme_challenge(token: str, settings: Settings = Depends(get_settings)) -> Response:
    return proxy_acme_challenge(settings, token)
