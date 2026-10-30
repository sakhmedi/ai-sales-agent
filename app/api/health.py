from fastapi import APIRouter

from app import __version__
from app.config import get_settings
from app.models.health import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Liveness check. Also reports whether the app runs in dry-run mode."""
    settings = get_settings()
    return HealthResponse(status="ok", version=__version__, dry_run=settings.dry_run)
