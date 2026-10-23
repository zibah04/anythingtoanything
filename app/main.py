import asyncio
import logging
import mimetypes
import os
import re
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.formparsers import MultiPartException
from starlette.types import ASGIApp, Receive, Scope, Send

from app.conversion import ConversionError, convert_bytes
from app.formats import normalize, registry

STATIC = Path(__file__).parent / "static"
MAX_UPLOAD = 25 * 1024 * 1024
app = FastAPI(title="Anything to Anything", version="0.1.0", docs_url=None, redoc_url=None)
conversion_workers = int(os.environ.get("CONVERSION_WORKERS", "2"))
if not 1 <= conversion_workers <= 4:
    raise ValueError("CONVERSION_WORKERS must be between 1 and 4.")
slots = asyncio.Semaphore(conversion_workers)
logger = logging.getLogger(__name__)


class UploadTooLarge(MultiPartException):
    pass


class UploadLimitMiddleware:
    """Limit bytes before multipart parsing, including chunked requests."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http" or scope["path"] != "/api/convert":
            return await self.app(scope, receive, send)
        received = 0
        too_large = False

        async def limited_receive():
            nonlocal received, too_large
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_UPLOAD + 1024 * 1024:
                    too_large = True
                    raise UploadTooLarge("Each file must be 25 MB or smaller.")
            return message

        async def limited_send(message):
            if not too_large:
                await send(message)

        try:
            await self.app(scope, limited_receive, limited_send)
        except UploadTooLarge:
            pass
        if too_large:
            response = JSONResponse(
                {"detail": "Each file must be 25 MB or smaller."}, status_code=413
            )
            await response(scope, receive, send)


app.add_middleware(UploadLimitMiddleware)


@app.middleware("http")
async def request_guards(request: Request, call_next):
    length = request.headers.get("content-length")
    if length:
        try:
            oversized = int(length) > MAX_UPLOAD + 1024 * 1024
        except ValueError:
            return JSONResponse({"detail": "Invalid content length."}, status_code=400)
        if oversized:
            return JSONResponse({"detail": "Each file must be 25 MB or smaller."}, status_code=413)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    )
    if response.headers.get("content-type", "").startswith(
        ("text/html", "text/css", "text/javascript", "application/javascript", "image/svg+xml")
    ):
        # Revalidate the unversioned frontend files after a deployment or edit.
        response.headers.setdefault("Cache-Control", "no-cache")
    if request.url.path.startswith("/api/convert"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/health")
def health():
    return {"status": "ok", "version": "0.1.0"}


@app.get("/api/formats")
def formats():
    return {**registry(), "server_conversion_workers": conversion_workers}


@app.post("/api/convert")
async def convert(
    file: Annotated[UploadFile, File()],
    target: Annotated[str, Form()],
    pdf_mode: Annotated[str, Form()] = "editable",
):
    try:
        async with slots:
            chunks, size = [], 0
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise HTTPException(413, "Each file must be 25 MB or smaller.")
                chunks.append(chunk)
            if not size:
                raise HTTPException(400, "This file is empty. Choose a file with content.")
            filename = (file.filename or "document").replace("\\", "/").rsplit("/", 1)[-1]
            source_ext = normalize(Path(filename).suffix)
            target_ext = normalize(target)
            try:
                result = await run_in_threadpool(
                    convert_bytes, b"".join(chunks), source_ext, target_ext, pdf_mode
                )
            except ConversionError as exc:
                raise HTTPException(422, str(exc)) from exc
            except Exception as exc:
                logger.error(
                    "Conversion failed (%s → %s): %s", source_ext, target_ext, type(exc).__name__
                )
                raise HTTPException(
                    422,
                    "The file could not be converted. It may be damaged or use an unsupported feature.",
                ) from exc
            stem = re.sub(r"[^\w .()-]", "_", Path(filename).stem)[:100] or "converted"
            output_name = f"{stem}.{target_ext}"
            ascii_name = output_name.encode("ascii", "ignore").decode() or f"converted.{target_ext}"
            mime = mimetypes.guess_type(output_name)[0] or "application/octet-stream"
            return Response(
                result,
                media_type=mime,
                headers={
                    "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(output_name)}",
                    "Cache-Control": "no-store",
                },
            )
    finally:
        await file.close()


app.mount("/static", StaticFiles(directory=STATIC, html=True), name="static")
# Keep the HTML and its relative assets together at both supported entry points.
# API routes must remain above this final frontend mount.
app.mount("/", StaticFiles(directory=STATIC, html=True), name="frontend")
