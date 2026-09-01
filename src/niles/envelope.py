from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


SCHEMA_VERSION = "niles.envelope.v1"


@dataclass
class NextStep:
    label: str
    command: str
    mutates: bool
    network: bool = False
    requires_approval: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "command": self.command,
            "mutates": self.mutates,
            "network": self.network,
            "requires_approval": self.requires_approval,
        }


@dataclass
class Envelope:
    status: str
    command: str
    argv: list[str]
    data: dict[str, Any] = field(default_factory=dict)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    next_steps: list[NextStep] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": self.status,
            "command": self.command,
            "argv": self.argv,
            "data": self.data,
            "warnings": self.warnings,
            "errors": self.errors,
            "next_steps": [step.to_dict() for step in self.next_steps],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=False) + "\n"


def ok(
    command: str,
    argv: list[str],
    data: dict[str, Any] | None = None,
    warnings: list[dict[str, Any]] | None = None,
    next_steps: list[NextStep] | None = None,
) -> Envelope:
    return Envelope(
        status="ok",
        command=command,
        argv=argv,
        data=data or {},
        warnings=warnings or [],
        errors=[],
        next_steps=next_steps or [],
    )


def error(
    command: str,
    argv: list[str],
    code: str,
    message: str,
    data: dict[str, Any] | None = None,
    next_steps: list[NextStep] | None = None,
) -> Envelope:
    return Envelope(
        status="error",
        command=command,
        argv=argv,
        data=data or {},
        warnings=[],
        errors=[{"code": code, "message": message}],
        next_steps=next_steps or [],
    )
