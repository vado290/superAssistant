"""Device-facing API: a PC executor authenticates, long-polls for a read-only operation and reports its result."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from assistant_kernel.core import host_operations as ops


class Report(BaseModel):
    operation_token: str
    state: str
    result: dict = {}


def build_router(settings) -> APIRouter:
    router = APIRouter(prefix="/api/v1/host")

    def auth(device_id: str, authorization: str | None) -> None:
        token = (authorization or "").removeprefix("Bearer ").strip()
        try:
            ops.authenticate(settings, device_id, token)
        except PermissionError:
            raise HTTPException(401, "unauthorized") from None

    @router.post("/{device_id}/claim")
    async def claim(device_id: str, authorization: str | None = Header(default=None)) -> dict:
        auth(device_id, authorization)
        for _ in range(10):   # at most ~20 s, short database checks in between
            item = await ops.claim(settings, device_id)
            if item:
                return {"operation": item}
            await asyncio.sleep(2)
        return {"operation": None}

    @router.post("/{device_id}/operations/{operation_id}/result")
    async def result(device_id: str, operation_id: str, body: Report,
                     authorization: str | None = Header(default=None)) -> dict:
        auth(device_id, authorization)
        try:
            return await ops.report(settings, device_id, operation_id, body.operation_token, body.state, body.result)
        except PermissionError:
            raise HTTPException(403, "bad operation token") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @router.post("/{device_id}/operations/{operation_id}/artifact")
    async def artifact(device_id: str, operation_id: str, request: Request,
                       authorization: str | None = Header(default=None),
                       x_operation_token: str = Header(default=""),
                       x_content_sha256: str = Header(default="")) -> dict:
        auth(device_id, authorization)
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > ops.MAX_ARTIFACT_BYTES:
                raise HTTPException(413, "artifact exceeds size limit")
            content.extend(chunk)
        try:
            return await ops.import_artifact(settings, device_id, operation_id, x_operation_token,
                                             bytes(content), x_content_sha256)
        except PermissionError:
            raise HTTPException(403, "bad operation token") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    return router
