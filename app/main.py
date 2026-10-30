import logging

from fastapi import FastAPI

from app import __version__
from app.api import health
from app.config import get_settings
from app.logging_config import setup_logging

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    settings = get_settings()
    setup_logging(settings.log_level)

    app = FastAPI(title="AI Sales Agent", version=__version__)
    app.include_router(health.router)

    logger.info("Application created (dry_run=%s)", settings.dry_run)
    return app


app = create_app()
