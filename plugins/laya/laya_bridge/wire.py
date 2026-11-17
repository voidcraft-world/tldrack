"""The `/v1/systemone` request this plugin will forward, checked at the boundary.

A port of `schemas/src/decide.ts` (`decideRequestSchema`) — that file owns the contract; this is
its stdlib twin, because a plugin cannot import Zod. It is deliberately STRICTER than Laya's own
validation: only the documented fields are forwarded, so a caller cannot reach a `laya-serve`
knob (token budgets, batch routes, language overrides) through the bridge.

Stdlib only: owned by plugins/laya/README.md — see "Pure modules".
"""

from __future__ import annotations

import re
from typing import Any

from laya_bridge.files import utf16_len

#: `DECIDE_QUESTION_NAME` in schemas/src/decide.ts. fullmatch, never match.
QUESTION_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
QUESTION_TYPES = ("noul", "choice", "score")

#: Model names a caller may pin: Laya's built-ins plus registered fine-tunes (`paths.py`).
MODEL_NAME = re.compile(r"[a-z][a-z0-9-]{0,63}")
BUILTIN_MODELS = ("english", "multilingual")

MAX_QUESTIONS = 32
MAX_INSTRUCTIONS_CHARS = 2_000
MAX_OPTIONS = 64
MAX_OPTION_CHARS = 500
#: An option NAME is what the ledger records as `choice` (`schemas/src/decide.ts` `LAYA_CHOICE_NAME_MAX`, 80 UTF-16 units).
MAX_OPTION_NAME_CHARS = 80
#: A state far past Laya's context is truncated by Laya anyway; this only bounds what we buffer.
MAX_STATE_CHARS = 64_000


class WireError(ValueError):
    """A request this plugin refuses — always a 400, and the message names the field."""


def _check_criteria(name: str, qtype: str, criteria: Any) -> Any:
    if criteria is None:
        if qtype in ("choice", "score"):
            raise WireError(f"question {name!r}: a {qtype} needs criteria")
        return None
    if qtype == "choice":
        if not isinstance(criteria, dict) or not criteria:
            raise WireError(f"question {name!r}: choice criteria must be a non-empty {{option: description}}")
        items = criteria.items()
    elif qtype == "score":
        if not isinstance(criteria, list) or len(criteria) < 2:
            raise WireError(f"question {name!r}: score criteria must list at least two levels")
        items = ((str(i), level) for i, level in enumerate(criteria))
    else:
        raise WireError(f"question {name!r}: a noul takes no criteria")
    count = 0
    for key, text in items:
        count += 1
        if not isinstance(key, str) or not key or not isinstance(text, str) or len(text) > MAX_OPTION_CHARS:
            raise WireError(f"question {name!r}: every option needs a name and a description under {MAX_OPTION_CHARS} chars")
        if utf16_len(key) > MAX_OPTION_NAME_CHARS:
            raise WireError(f"question {name!r}: an option name is at most {MAX_OPTION_NAME_CHARS} chars")
    if count > MAX_OPTIONS:
        raise WireError(f"question {name!r}: at most {MAX_OPTIONS} options")
    return criteria


def clean_request(body: Any, known_models: frozenset[str]) -> dict[str, Any]:
    """The forwardable body, or `WireError`. `known_models` = built-ins + registered fine-tunes."""
    if not isinstance(body, dict):
        raise WireError("the body must be a JSON object")

    state = body.get("state")
    if isinstance(state, str):
        if not state.strip():
            raise WireError("state must not be empty")
        size = len(state)
    elif isinstance(state, dict) and state:
        # A rough bound — the serialized size Laya will see is what matters, not exactness.
        size = len(repr(state))
    else:
        raise WireError("state must be a non-empty string or object")
    if size > MAX_STATE_CHARS:
        raise WireError(f"state is over {MAX_STATE_CHARS} chars")

    questions = body.get("questions")
    if not isinstance(questions, dict) or not questions:
        raise WireError("questions must be a non-empty object")
    if len(questions) > MAX_QUESTIONS:
        raise WireError(f"at most {MAX_QUESTIONS} questions per request")

    clean_questions: dict[str, dict[str, Any]] = {}
    for name, question in questions.items():
        if not isinstance(name, str) or not QUESTION_NAME.fullmatch(name):
            raise WireError(f"question name {name!r} must be lowercase snake_case")
        if not isinstance(question, dict):
            raise WireError(f"question {name!r} must be an object")
        qtype = question.get("type")
        if qtype not in QUESTION_TYPES:
            raise WireError(f"question {name!r}: type must be one of {', '.join(QUESTION_TYPES)}")
        instructions = question.get("instructions")
        if not isinstance(instructions, str) or not instructions.strip() or len(instructions) > MAX_INSTRUCTIONS_CHARS:
            raise WireError(f"question {name!r}: instructions must be 1–{MAX_INSTRUCTIONS_CHARS} chars")
        cleaned: dict[str, Any] = {"type": qtype, "instructions": instructions}
        criteria = _check_criteria(name, qtype, question.get("criteria"))
        if criteria is not None:
            cleaned["criteria"] = criteria
        clean_questions[name] = cleaned

    out: dict[str, Any] = {"state": state, "questions": clean_questions}
    model = body.get("model")
    if model is not None:
        if not isinstance(model, str) or not MODEL_NAME.fullmatch(model) or model not in known_models:
            raise WireError(f"model {model!r} is not served here; omit it to let Laya route")
        out["model"] = model
    return out
