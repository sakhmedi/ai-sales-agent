from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Response body for the health check endpoint."""

    status: str
    version: str
    dry_run: bool
