# ─────────────────────────────────────────────────────────────────────────────
# app/middleware/upload_limit.py
#
# Rejects requests whose Content-Length exceeds MAX_UPLOAD_SIZE_MB before
# the body is read into memory. This prevents OOM crashes from large uploads.
#
# HOW TO INTEGRATE
# ────────────────
# In main.py, after `app = FastAPI(...)`:
#
#     from app.middleware.upload_limit import UploadSizeLimitMiddleware
#     app.add_middleware(
#         UploadSizeLimitMiddleware,
#         max_bytes=settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024,
#     )
#
# Add the middleware BEFORE CORSMiddleware so it runs first (outermost).
# ─────────────────────────────────────────────────────────────────────────────

import logging
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)


class UploadSizeLimitMiddleware(BaseHTTPMiddleware):
    """
    Reject multipart uploads that exceed max_bytes before reading the body.

    Uses the Content-Length header for a fast pre-check. Chunked-encoded
    requests without a Content-Length header are allowed through (FastAPI's
    form parsing will handle them, and they're rare for file uploads).
    """

    def __init__(self, app, max_bytes: int):
        super().__init__(app)
        self.max_bytes = max_bytes
        self.max_mb    = round(max_bytes / (1024 * 1024), 0)

    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                size = int(content_length)
            except ValueError:
                size = 0
            if size > self.max_bytes:
                logger.warning(
                    "Upload rejected: Content-Length %d bytes exceeds limit of %d bytes (%d MB) — path=%s",
                    size, self.max_bytes, self.max_mb, request.url.path,
                )
                return JSONResponse(
                    status_code=413,
                    content={
                        "detail": (
                            f"File too large. Maximum allowed size is {int(self.max_mb)} MB. "
                            f"Received {round(size / (1024 * 1024), 1)} MB."
                        )
                    },
                )
        return await call_next(request)
