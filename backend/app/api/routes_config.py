"""Config endpoints — read and update the bounded, operator-tunable keeper config."""
from __future__ import annotations

from fastapi import APIRouter

from app.core.models import KeeperConfig
from app.deps import apply_config, get_container

router = APIRouter(prefix="/config", tags=["config"])


@router.get("", response_model=KeeperConfig)
async def get_config() -> KeeperConfig:
    return get_container().config


@router.put("", response_model=KeeperConfig)
async def put_config(new: KeeperConfig) -> KeeperConfig:
    """Apply a config update to the live service (poll interval takes effect on worker restart)."""
    return apply_config(new)
