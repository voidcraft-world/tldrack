"""A JSON canvas: validated, pruned, its questions found, its answers checked (pure).

The vocabulary is owned by `schemas/src/agent-canvas.ts` — see the note there.
This module reads its generated twin, `assets/canvas-schema.json`, and never
restates a block's shape: the validator below is a generic interpreter of the
JSON Schema subset Zod emits, so a block added to the registry is validated
here the moment the export is re-run.

── WHY A BAD BLOCK IS DROPPED, NOT THE CANVAS ──────────────────────────────
A canvas is a live pane, and an agent that misspells one key should lose that
one block, not the whole picture. So an issue is pinned to the DEEPEST block
holding it and that block is cut; the rest renders, and every issue is reported
(on the page, and to the agent by `hooks/check.sh` beside this package). The
same call as OpenUI's runtime, which skips an invalid action and runs the rest.
Only a doc whose ROOT is wrong (not JSON, no `blocks`) renders nothing.
"""

from __future__ import annotations

import functools
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from agent_canvas.delivery import QUESTION_MAX, REPLY_MAX, one_line

SCHEMA_PATH = Path(__file__).resolve().parent / "assets" / "canvas-schema.json"

#: Issues reported per canvas. Past this the agent has a bigger problem than a list fixes.
MAX_ISSUES = 20
#: Prune → re-validate rounds. Each round cuts at least one block, and only a
#: cut that empties a container can cause the next.
MAX_PASSES = 8
#: A free-text ("other…") answer, as the kit's input bounds it.
OTHER_MAX = 1500
FORM_TEXT_MAX = 2000

Path_ = tuple  # segments: str keys, int indexes


@dataclass(frozen=True)
class Issue:
    path: Path_
    code: str
    message: str

    def as_json(self) -> dict:
        return {"path": format_path(self.path), "code": self.code, "message": self.message}


@dataclass(frozen=True)
class Question:
    id: str
    kind: str  # "choice" · "form" · "html" (a legacy .choice)
    q: str
    block: dict = field(default_factory=dict, compare=False)

    def as_json(self) -> dict:
        return {"id": self.id, "q": self.q}


@dataclass
class LoadedDoc:
    doc: dict | None
    issues: list[Issue]
    questions: list[Question]


def format_path(path: Path_) -> str:
    out = ""
    for segment in path:
        out += f"[{segment}]" if isinstance(segment, int) else (f".{segment}" if out else segment)
    return out or "(root)"


@functools.lru_cache(maxsize=1)
def contract() -> dict:
    """`{schema, question_types, containers}` from the generated twin."""
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


# ── the validator: the JSON Schema subset `z.toJSONSchema` emits ──────────────


def _json_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _same(value: object, const: object) -> bool:
    return _json_type(value) == _json_type(const) and value == const


class _Validator:
    def __init__(self, root: dict) -> None:
        self._root = root

    def _resolve(self, schema: dict) -> dict:
        ref = schema.get("$ref")
        if not isinstance(ref, str):
            return schema
        node: object = self._root
        for part in ref.removeprefix("#/").split("/"):
            node = node[part]  # type: ignore[index]
        return node  # type: ignore[return-value]

    def check(self, value: object, schema: object, path: Path_) -> list[Issue]:
        if schema is False:
            return [Issue(path, "unknown-key", "no value allowed here")]
        if not isinstance(schema, dict):
            return []
        schema = self._resolve(schema)
        union = schema.get("oneOf") or schema.get("anyOf")
        if union:
            return self._union(value, union, path)
        issues: list[Issue] = []
        expected = schema.get("type")
        actual = _json_type(value)
        if expected and actual != expected:
            return [Issue(path, "type-mismatch", f"expected {expected}, got {actual}")]
        if actual == "number" and not math.isfinite(value):  # type: ignore[arg-type]
            return [Issue(path, "type-mismatch", "expected a finite number")]
        if "const" in schema and not _same(value, schema["const"]):
            return [Issue(path, "bad-value", f"expected {json.dumps(schema['const'])}")]
        if "enum" in schema and not any(_same(value, item) for item in schema["enum"]):
            return [Issue(path, "bad-value", f"expected one of {', '.join(map(str, schema['enum']))}")]
        if actual == "string":
            issues += self._string(value, schema, path)  # type: ignore[arg-type]
        elif actual == "number":
            issues += self._number(value, schema, path)  # type: ignore[arg-type]
        elif actual == "array":
            issues += self._array(value, schema, path)  # type: ignore[arg-type]
        elif actual == "object":
            issues += self._object(value, schema, path)  # type: ignore[arg-type]
        return issues

    def _union(self, value: object, members: list, path: Path_) -> list[Issue]:
        members = [self._resolve(member) for member in members]
        tags = [((member.get("properties") or {}).get("type") or {}).get("const") for member in members]
        if all(isinstance(tag, str) for tag in tags):
            # A discriminated union (blocks, form fields): judge by the named branch only.
            if not isinstance(value, dict):
                return [Issue(path, "type-mismatch", f"expected an object, got {_json_type(value)}")]
            kind = value.get("type")
            if kind not in tags:
                known = ", ".join(tags)
                if kind is None:
                    return [Issue(path, "missing-required", f'needs "type" — one of {known}')]
                return [Issue(path, "unknown-block", f"unknown type {json.dumps(kind)} — one of {known}")]
            return self.check(value, members[tags.index(kind)], path)
        attempts = [self.check(value, member, path) for member in members]
        if any(not found for found in attempts):
            return []
        # No branch fits. Report the branch of the value's own JSON type, when
        # exactly one has it ("too long" beats "expected text | number").
        actual = _json_type(value)
        typed = [found for member, found in zip(members, attempts) if member.get("type") == actual]
        if len(typed) == 1:
            return typed[0]
        wanted = sorted({str(member.get("type") or "value") for member in members})
        return [Issue(path, "type-mismatch", f"expected {' | '.join(wanted)}, got {actual}")]

    @staticmethod
    def _string(value: str, schema: dict, path: Path_) -> list[Issue]:
        length = len(value)  # code points — what Zod 4 counts, so an emoji is 1
        if "minLength" in schema and length < schema["minLength"]:
            return [
                Issue(
                    path,
                    "too-short",
                    "must not be empty" if schema["minLength"] == 1 else f"at least {schema['minLength']} characters",
                )
            ]
        if "maxLength" in schema and length > schema["maxLength"]:
            return [Issue(path, "too-long", f"{length} characters, max {schema['maxLength']}")]
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and not re.search(pattern, value):
            what = schema.get("description") or f"pattern {pattern}"
            return [Issue(path, "bad-value", f"must match {what}")]
        return []

    @staticmethod
    def _number(value: float, schema: dict, path: Path_) -> list[Issue]:
        if "minimum" in schema and value < schema["minimum"]:
            return [Issue(path, "out-of-range", f"must be ≥ {schema['minimum']}")]
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            return [Issue(path, "out-of-range", f"must be > {schema['exclusiveMinimum']}")]
        if "maximum" in schema and value > schema["maximum"]:
            return [Issue(path, "out-of-range", f"must be ≤ {schema['maximum']}")]
        return []

    def _array(self, value: list, schema: dict, path: Path_) -> list[Issue]:
        if "minItems" in schema and len(value) < schema["minItems"]:
            return [Issue(path, "too-few", f"{len(value)} items, at least {schema['minItems']}")]
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            return [Issue(path, "too-many", f"{len(value)} items, max {schema['maxItems']}")]
        prefix = schema.get("prefixItems") or []
        issues: list[Issue] = []
        for index, item in enumerate(value):
            item_schema = prefix[index] if index < len(prefix) else schema.get("items", True)
            issues += self.check(item, item_schema, (*path, index))
        return issues

    def _object(self, value: dict, schema: dict, path: Path_) -> list[Issue]:
        props: dict = schema.get("properties") or {}
        issues = [
            Issue((*path, key), "missing-required", f'needs "{key}"') for key in schema.get("required") or [] if key not in value
        ]
        for key, item in value.items():
            if key in props:
                issues += self.check(item, props[key], (*path, key))
            elif schema.get("additionalProperties") is False:
                known = ", ".join(props) or "none"
                issues.append(Issue((*path, key), "unknown-key", f'unknown key "{key}" — keys: {known}'))
        return issues


# ── walking blocks ───────────────────────────────────────────────────────────


def _children(block: dict) -> list[tuple[Path_, list]]:
    """`(relative path, child list)` for each list of blocks a container holds."""
    key = contract()["containers"].get(block.get("type"))
    if key == "blocks" and isinstance(block.get("blocks"), list):
        return [(("blocks",), block["blocks"])]
    if key == "tabs" and isinstance(block.get("tabs"), list):
        return [
            (("tabs", index, "blocks"), tab["blocks"])
            for index, tab in enumerate(block["tabs"])
            if isinstance(tab, dict) and isinstance(tab.get("blocks"), list)
        ]
    return []


def _walk(blocks: object, path: Path_ = ("blocks",)):
    """`(path, block)` for every block, depth-first in document order — the same
    order as `agent_canvas_question_ids` in the TS source, so ids agree."""
    if not isinstance(blocks, list):
        return
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        here = (*path, index)
        yield here, block
        for relative, children in _children(block):
            yield from _walk(children, (*here, *relative))


def _option_risk_issues(option: object) -> list[str]:
    """Port of `option_risk_issues` (schemas/src/agent-canvas.ts owns why)."""
    if not isinstance(option, dict) or not option.get("risk"):
        return []
    label, risk = option.get("label"), option["risk"]
    issues = [] if option.get("does") else [f'"{label}" is {risk}: say what it runs in "does"']
    if risk == "destructive":
        if not option.get("targets"):
            issues.append(f'"{label}" deletes: list every target in "targets"')
        if not option.get("undo"):
            issues.append(f'"{label}" deletes: say how to undo it in "undo"')
    return issues


def collect_questions(doc: dict) -> tuple[list[Question], list[Issue]]:
    """The questions in document order, and an issue per duplicate id (the later
    block is the one blamed) or per under-described risky option. Port of
    `agent_canvas_question_ids` + its refine."""
    kinds = set(contract()["question_types"])
    questions: list[Question] = []
    issues: list[Issue] = []
    seen: set[str] = set()
    for path, block in _walk(doc.get("blocks")):
        if block.get("type") not in kinds:
            continue
        qid = block.get("id") if isinstance(block.get("id"), str) else f"q{len(questions) + 1}"
        if qid in seen:
            issues.append(Issue(path, "duplicate-id", f'duplicate question id "{qid}"'))
        seen.add(qid)
        if block.get("type") == "choice":
            for index, option in enumerate(block.get("options") or []):
                issues += [
                    Issue((*path, "options", index), "risk-undescribed", message) for message in _option_risk_issues(option)
                ]
        questions.append(Question(qid, block["type"], one_line(block.get("q"), QUESTION_MAX) or "question", block))
    return questions, issues


# ── pruning ──────────────────────────────────────────────────────────────────


def _block_path(path: Path_) -> Path_ | None:
    """The deepest block an issue sits in: the prefix ending at the last `blocks[i]`."""
    for index in range(len(path) - 2, -1, -1):
        if path[index] == "blocks" and isinstance(path[index + 1], int):
            return path[: index + 2]
    return None


def _prune(doc: dict, issues: list[Issue]) -> bool:
    """Cut what each issue blames, in place. False when the ROOT is at fault."""
    cuts: set[Path_] = set()
    for issue in issues:
        block = _block_path(issue.path)
        if block is not None:
            cuts.add(block)
        elif len(issue.path) == 1 and issue.path[0] != "blocks":
            doc.pop(issue.path[0], None)  # a bad title, an unknown root key
        else:
            return False
    # Deepest-and-last first, so cutting one never shifts another's index; a
    # cut inside a block already being cut is skipped.
    for path in sorted(cuts, reverse=True):
        if any(other != path and path[: len(other)] == other for other in cuts):
            continue
        parent: object = doc
        for segment in path[:-1]:
            parent = parent[segment]  # type: ignore[index]
        del parent[path[-1]]  # type: ignore[union-attr]
    return True


def validate(doc: object) -> list[Issue]:
    issues = _Validator(contract()["schema"]).check(doc, contract()["schema"], ())
    if isinstance(doc, dict):
        issues += collect_questions(doc)[1]
    return issues


def _reject_constant(name: str):
    raise ValueError(f"{name} is not JSON")


def load_doc(text: str) -> LoadedDoc:
    """Parse, validate, prune. Issues are the FIRST pass's — later passes only
    clear up containers the first cut emptied, at paths that have shifted."""
    try:
        doc = json.loads(text, parse_constant=_reject_constant)
    except ValueError as exc:
        return LoadedDoc(None, [Issue((), "parse-failed", f"not JSON: {exc}")], [])
    reported: list[Issue] = []
    for attempt in range(MAX_PASSES):
        issues = validate(doc)
        if attempt == 0:
            reported = issues[:MAX_ISSUES]
        if not issues:
            questions, _ = collect_questions(doc)
            return LoadedDoc(doc, reported, questions)
        if not isinstance(doc, dict) or not _prune(doc, issues):
            break
    return LoadedDoc(None, reported or [Issue((), "parse-failed", "nothing valid left")], [])


def summary(questions: list[Question]) -> str | None:
    """What the 🙋 chip and the `attention` message say: the first question, and how many more."""
    if not questions:
        return None
    first = questions[0].q
    return first if len(questions) == 1 else one_line(f"{first} (+{len(questions) - 1})", QUESTION_MAX)


# ── answers ──────────────────────────────────────────────────────────────────


def _option_replies(block: dict) -> list[str]:
    replies = []
    for option in block.get("options") or []:
        if isinstance(option, str):
            replies.append(option)
        elif isinstance(option, dict):
            replies.append(option.get("reply") or option.get("label") or "")
    return replies


def _check_choice(block: dict, value: object) -> str | None:
    replies = _option_replies(block)
    free = block.get("other", True) is not False

    def one(item: object) -> str | None:
        if not isinstance(item, str):
            return "expected text"
        if not item.strip():
            return "an empty answer"
        if item in replies:
            return None
        if not free:
            return f'"{item[:40]}" is not an option'
        return None if len(item) <= OTHER_MAX else "a free-text answer is too long"

    if block.get("multi"):
        if not isinstance(value, list) or not value or len(value) > len(replies) + 1:
            return "expected a list of picks"
        if len(set(map(str, value))) != len(value):
            return "a pick is repeated"
        return next((why for why in map(one, value) if why), None)
    return one(value)


def _check_form(block: dict, value: object) -> str | None:
    if not isinstance(value, dict):
        return "expected an object of fields"
    fields = {spec.get("name"): spec for spec in block.get("fields") or [] if isinstance(spec, dict)}
    for name, entry in value.items():
        spec = fields.get(name)
        if spec is None:
            return f'unknown field "{name}"'
        kind = spec.get("type")
        if kind == "text" and not (isinstance(entry, str) and len(entry) <= FORM_TEXT_MAX):
            return f'"{name}" expects text'
        if kind == "select" and entry not in (spec.get("options") or []):
            return f'"{name}" is not one of its options'
        if kind == "toggle" and not isinstance(entry, bool):
            return f'"{name}" expects true or false'
        if kind == "rank" and not (isinstance(entry, list) and sorted(map(str, entry)) == sorted(spec.get("options") or [])):
            return f'"{name}" must be its options, reordered'
    return None


def check_answers(questions: list[Question], answers: object) -> str | None:
    """None when every answer fits its question; else why not (one line)."""
    if not isinstance(answers, dict) or not answers:
        return "answers required"
    by_id = {question.id: question for question in questions}
    for qid, value in answers.items():
        question = by_id.get(qid)
        if question is None:
            return f'no question "{qid}" on this canvas'
        if question.kind == "form":
            why = _check_form(question.block, value)
        elif question.kind == "choice":
            why = _check_choice(question.block, value)
        else:
            why = None if isinstance(value, str) and value.strip() else "an empty answer"
        if why:
            return f"{question.q}: {why}"
    return None


def render_answer(question: Question, value: object) -> str:
    """One answer as it is typed: text as text, picks comma-joined, a form as
    compact JSON — `json.dumps` escapes a newline, and a raw one would be ENTER."""
    if question.kind == "form":
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, list):
        return ", ".join(one_line(item, OTHER_MAX) for item in value)
    return one_line(value, OTHER_MAX)


def format_answers(questions: list[Question], answers: dict) -> str | None:
    """`canvas › <q> → <a>`, one line; several questions join with ` · `, and
    one left blank says `(skipped)` so the agent knows it was seen. None when
    the line would not fit in one typed prompt."""
    parts = [
        f"{question.q} → {render_answer(question, answers[question.id])}"
        if question.id in answers
        else f"{question.q} → (skipped)"
        for question in questions
    ]
    line = f"canvas › {' · '.join(parts)}"
    return line if len(line) <= REPLY_MAX else None
