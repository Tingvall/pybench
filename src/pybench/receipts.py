from __future__ import annotations

from typing import Any


def success_receipt(outputs: list[dict[str, Any]], warnings: list[str] | None = None, timings: dict[str, float] | None = None) -> dict[str, Any]:
    return {
        "status": "ok",
        "outputs": outputs,
        "warnings": warnings or [],
        "timings": timings or {},
    }


def error_receipt(message: str, error_class: str = "RuntimeError", traceback: str | None = None) -> dict[str, Any]:
    return {
        "status": "error",
        "error_class": error_class,
        "message": message,
        "traceback": traceback,
    }
