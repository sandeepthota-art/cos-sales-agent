from typing import Any

from fastapi import APIRouter, Depends

from app.api.dependencies import get_db, get_settings_dependency, require_auth
from app.config.settings import Settings
from app.mcp.tools import whats_on_my_table
from app.ui.data import dashboard_metrics

router = APIRouter(prefix="/api/v1", tags=["overview"], dependencies=[Depends(require_auth)])


@router.get("/overview")
def get_overview(db=Depends(get_db), settings: Settings = Depends(get_settings_dependency)) -> dict[str, Any]:
    """Reuses app.ui.data.dashboard_metrics (same counts the Streamlit
    Dashboard tab shows) and app.mcp.tools.whats_on_my_table (the same
    combined "what needs my attention" read the whats_on_my_table MCP tool
    already assembles) -- no new metric logic, no fabricated numbers."""
    return {
        "metrics": dashboard_metrics(db),
        "attention": whats_on_my_table(db, settings),
    }
