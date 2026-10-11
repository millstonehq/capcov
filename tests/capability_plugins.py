"""Plugin callables the capability adapter tests load by `module:callable`."""
from __future__ import annotations


def map_health(surfaces: list[dict], config: dict) -> list[dict]:
    return [{"capability": "ops/health", "surface": s["id"]} for s in surfaces if s["id"] == "GET /healthz"]


def parse_journey(line: str, source: dict):
    parts = line.split()
    if len(parts) != 2:
        return None
    result, name = parts
    return {"capability": f"canary/{name}", "result": {"ok": "pass", "fail": "fail"}.get(result, result)}
