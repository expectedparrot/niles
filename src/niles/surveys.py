from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from .store import NilesError


SURVEY_SCHEMA_VERSION = "niles.survey.v1"
ROUTE_ACTIONS = {"set_field", "set_trait", "append_note", "create_task", "task_due", "task_assignee", "add_tag", "archive", "noop"}


TEMPLATES: dict[str, dict[str, Any]] = {
    "debrief": {
        "description": "Capture an interaction and its next step.",
        "questions": [
            {"name": "summary", "text": "What happened?", "type": "text", "required": True},
            {"name": "sentiment", "text": "How did it go?", "type": "choice", "options": ["positive", "neutral", "negative"]},
            {"name": "next_step", "text": "What is the next step?", "type": "text"},
            {"name": "next_by", "text": "When is it due?", "type": "text"},
            {"name": "owner", "text": "Who owns it?", "type": "text"},
        ],
        "routes": {
            "summary": {"action": "append_note", "kind": "debrief"},
            "sentiment": {"action": "set_trait", "trait": "last_sentiment"},
            "next_step": {"action": "create_task"},
            "next_by": {"action": "task_due", "binds": "next_step"},
            "owner": {"action": "task_assignee", "binds": "next_step"},
        },
    },
    "review": {
        "description": "Triage a stale relationship.",
        "questions": [
            {"name": "outcome", "text": "What should happen?", "type": "choice", "options": ["follow_up", "archive", "keep"]},
            {"name": "next_step", "text": "What follow-up is needed?", "type": "text"},
        ],
        "routes": {"outcome": {"action": "noop"}, "next_step": {"action": "create_task"}},
    },
    "intake-basic": {
        "description": "Basic third-party contact intake.",
        "questions": [
            {"name": "name", "text": "Name", "type": "text", "required": True},
            {"name": "email", "text": "Email", "type": "text"},
            {"name": "company", "text": "Company", "type": "text"},
            {"name": "message", "text": "How can we help?", "type": "text"},
        ],
        "routes": {"message": {"action": "append_note", "kind": "intake"}},
    },
}


def template_definition(name: str) -> dict[str, Any]:
    if name not in TEMPLATES:
        raise NilesError("unknown_survey", f"No survey matched '{name}'.")
    definition = deepcopy(TEMPLATES[name])
    definition.update({"schema_version": SURVEY_SCHEMA_VERSION, "name": name, "template": True, "version": 1})
    return definition


def validate_survey(definition: dict[str, Any]) -> dict[str, Any]:
    if definition.get("schema_version") != SURVEY_SCHEMA_VERSION:
        raise NilesError("unsupported_survey_schema", "Survey schema is not supported.")
    questions = definition.get("questions")
    if not isinstance(questions, list) or not questions:
        raise NilesError("invalid_survey", "Survey needs at least one question.")
    names = [question.get("name") for question in questions]
    if any(not name for name in names) or len(names) != len(set(names)):
        raise NilesError("invalid_survey", "Question names must be present and unique.")
    for question_name, route in definition.get("routes", {}).items():
        if question_name not in names:
            raise NilesError("invalid_route", f"Route references missing question '{question_name}'.")
        if route.get("action") not in ROUTE_ACTIONS:
            raise NilesError("invalid_route", f"Unknown route action '{route.get('action')}'.")
        if route.get("binds") and route["binds"] not in names:
            raise NilesError("invalid_route", f"Route binding references missing question '{route['binds']}'.")
    return definition


def loads_answers(path_text: str) -> dict[str, Any]:
    try:
        value = json.loads(path_text)
    except json.JSONDecodeError as exc:
        raise NilesError("invalid_answers", "Answers are not valid JSON.") from exc
    if not isinstance(value, dict):
        raise NilesError("invalid_answers", "Answers must be a JSON object.")
    return value
