"""GET /camera: 휴대폰에서 여는 카메라 페이지."""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(tags=["mobile-camera"])

CAMERA_HTML = Path(__file__).resolve().parent.parent / "static" / "camera" / "index.html"


@router.get("/camera", include_in_schema=False)
async def camera_page() -> FileResponse:
    # session 값은 페이지 JS가 URL에서 직접 읽는다
    return FileResponse(CAMERA_HTML, headers={"Cache-Control": "no-cache"})
