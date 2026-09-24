"""Errors the UI can translate.

Every refusal the operator can see carries three things in its JSON body:

    {"detail": "...", "code": "device.not_found", "params": {"sn": "..."}}

``detail`` stays the English sentence it always was — logs, curl users and
anything that predates the code read it unchanged. ``code`` is the stable key
the frontend looks up in its own translation table, and ``params`` are the
values that sentence interpolates. A code the frontend does not know falls
back to ``detail``, so adding a new error never needs the two to ship in step.

Codes are ``area.what`` in snake_case and never change once shipped: they are
the contract, the English text is not.
"""

import json
from typing import Any, Dict, Optional

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse


class AppError(HTTPException):
    """An HTTPException with a translation code and its parameters."""

    def __init__(
        self,
        code: str,
        status_code: int,
        detail: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.code = code
        self.params = params or {}

    @classmethod
    def wrap(cls, exc: Exception, status_code: int, fallback_code: Optional[str] = None) -> "AppError":
        """Re-raise a service-layer error over HTTP, keeping its code if it has one."""
        return cls(
            getattr(exc, "code", None) or fallback_code or "",
            status_code,
            str(exc),
            getattr(exc, "params", None),
        )


class CodedError(ValueError):
    """A service-layer refusal that already knows its translation code.

    Services raise this (or subclass it) so a router can hand the code on
    through ``AppError.wrap`` without re-deciding what the error means.
    """

    def __init__(self, code: str, message: str, **params: Any) -> None:
        super().__init__(message)
        self.code = code
        self.params = params


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    body: Dict[str, Any] = {"detail": exc.detail}
    if exc.code:
        body["code"] = exc.code
        body["params"] = exc.params
    return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # FastAPI's own 422 puts a list in `detail`; the code lets the UI say
    # something readable instead of printing that list.
    response = await request_validation_exception_handler(request, exc)
    body = json.loads(response.body)
    body["code"] = "common.validation"
    body["params"] = {}
    return JSONResponse(body, status_code=response.status_code)


def install(app) -> None:
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)


# Success messages follow the same contract as errors: the English `message`
# stays, and `message_code` / `message_params` are what the UI translates
# (under messages.* rather than errors.*). A param may itself be a fragment —
# {"code": ..., "params": ...} — for a sentence built from parts, such as the
# "what" of a queued command; the UI translates those first.


def message(code: str, **params: Any) -> Dict[str, Any]:
    """Fields to merge into a response body: ``{**message("x", n=1), ...}``."""
    return {"message_code": code, "message_params": params}


def fragment(code: str, **params: Any) -> Dict[str, Any]:
    """A translatable piece of a larger message; empty code means no text."""
    return {"code": code, "params": params}
