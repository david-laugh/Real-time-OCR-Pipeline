"""GET /pair: standalone page that shows only the <qr-pairing> widget (for testing)."""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(tags=["qr-pairing"])

PAIR_HTML = Path(__file__).resolve().parent.parent / "static" / "pair" / "index.html"


@router.get("/pair", include_in_schema=False)
async def pair_page() -> FileResponse:
    return FileResponse(PAIR_HTML, headers={"Cache-Control": "no-cache"})
