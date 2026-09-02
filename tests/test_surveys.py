from __future__ import annotations

import pytest

from niles.store import NilesError
from niles.surveys import SURVEY_SCHEMA_VERSION, loads_answers, template_definition, validate_survey


def assert_error(code, callable_):
    with pytest.raises(NilesError) as exc:
        callable_()
    assert exc.value.code == code


def test_template_and_answer_validation_errors():
    assert_error("unknown_survey", lambda: template_definition("missing"))
    assert_error("invalid_answers", lambda: loads_answers("not-json"))
    assert_error("invalid_answers", lambda: loads_answers("[]"))
    assert loads_answers('{"summary": "ok"}') == {"summary": "ok"}


@pytest.mark.parametrize(
    ("definition", "code"),
    [
        ({}, "unsupported_survey_schema"),
        ({"schema_version": SURVEY_SCHEMA_VERSION, "questions": []}, "invalid_survey"),
        ({"schema_version": SURVEY_SCHEMA_VERSION, "questions": [{"name": "q"}, {"name": "q"}]}, "invalid_survey"),
        ({"schema_version": SURVEY_SCHEMA_VERSION, "questions": [{"name": "q"}], "routes": {"missing": {"action": "noop"}}}, "invalid_route"),
        ({"schema_version": SURVEY_SCHEMA_VERSION, "questions": [{"name": "q"}], "routes": {"q": {"action": "execute_shell"}}}, "invalid_route"),
        ({"schema_version": SURVEY_SCHEMA_VERSION, "questions": [{"name": "q"}], "routes": {"q": {"action": "noop", "binds": "missing"}}}, "invalid_route"),
    ],
)
def test_survey_definition_validation_errors(definition, code):
    assert_error(code, lambda: validate_survey(definition))


def test_valid_survey_definition_round_trips():
    definition = template_definition("debrief")
    assert validate_survey(definition) is definition
