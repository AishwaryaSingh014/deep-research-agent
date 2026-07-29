"""Reading reports already written to outputs/."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from deepresearch import config
from deepresearch.report import slugify

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/{slug}")
def get_saved_report(slug: str) -> dict:
    """Read a report previously written to outputs/ by the CLI or an earlier run."""
    safe = slugify(slug)
    path = config.OUTPUT_DIR / f"{safe}.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"No saved report for {safe!r}")
    return {"slug": safe, "markdown": path.read_text(encoding="utf-8")}
