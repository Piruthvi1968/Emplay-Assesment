"""Validator / Critic agent: deterministic checks first, an LLM judge for free-text fields, then confidence.

Failures reject a value (the orchestrator sends the field back for another round); warnings only lower
its confidence.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel

from app.agents.base import AgentContext, agent_node
from app.agents.evidence import NOT_FOUND, quote_supported
from app.agents.prompts import JUDGE_PROMPT, JUDGE_SYSTEM
from app.agents.state import BidState
from app.config import FieldSpec
from app.ingestion.detect import find_dates
from app.llm_client import LLMError
from app.models import AddendumChange, FieldResult, FieldValidation, FieldValue
from app.search.engine import chunk_body, id_like_tokens

_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2}( [A-Z]{2,5})?)?$")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"\(?\b\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b")
_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_TIME = re.compile(r"\b\d{1,2}:\d{2}\b")
_NUMBER = re.compile(r"\b\d[\d,]*\d\b")  # two or more digits; single digits are too ambiguous to check
_UNITS = r"tb|gb|mb|kb|ghz|mhz|hz|wh|w|mah|nit|nits|mm|cm|in|inch|kg|lb|lbs|mt/s|gbps|mbps|v"
_CLOCK = re.compile(r"\b(\d{1,2})(?::(\d{2}))?(?::\d{2})?\s*([AaPp])\.?[Mm]\b|\b(\d{1,2}):(\d{2})(?::\d{2})?\b")


class Verdict(BaseModel):
    field: str
    verdict: Literal["yes", "partial", "no"]
    reason: str


class JudgeReport(BaseModel):
    verdicts: list[Verdict]


def as_text(value: FieldValue) -> str:
    return " | ".join(value) if isinstance(value, list) else (value or "")


def format_problem(value: FieldValue, spec: FieldSpec) -> str | None:
    """Type and format check for a non-null value."""
    if spec.type == "list":
        return None if isinstance(value, list) else "expected a list of items"
    if not isinstance(value, str):
        return "expected a single text value"
    if spec.type == "datetime":
        if not _DATETIME.match(value):
            return f"'{value}' is not in the format YYYY-MM-DD HH:MM TZ"
        try:
            date.fromisoformat(value[:10])
        except ValueError:
            return f"'{value[:10]}' is not a valid date"
    if spec.type == "contact" and not (_EMAIL.search(value) or _PHONE.search(value)):
        return "a contact needs at least an e-mail address or a phone number"
    return None


def key_facts(value: FieldValue) -> list[str]:
    """The checkable facts in a value: e-mails, phone numbers, IDs, dates and numbers of 2+ digits."""
    text = as_text(value)
    facts: list[str] = []
    for pattern in (_EMAIL, _PHONE, _ISO_DATE):
        facts += [m.group(0) for m in pattern.finditer(text)]
        text = pattern.sub(" ", text)
    ids = [part for t in id_like_tokens(text) for part in t.split("/")]  # 'Gen/16GB/512GB' -> parts
    facts += [t for t in ids if sum(c.isdigit() for c in t) >= 2]  # '210-BLYZ', not '3-year'
    text = _TIME.sub(" ", text)
    for tok in id_like_tokens(text):
        text = text.replace(tok, " ")
    facts += [m.group(0) for m in _NUMBER.finditer(text)]
    return list(dict.fromkeys(facts))


def _join_units(text: str) -> str:
    """'128 gb' -> '128gb', '1920 x 1080' -> '1920x1080', so value and source compare with the same spacing."""
    text = re.sub(r"(\d)\s*x\s*(\d)", r"\1x\2", text)
    return re.sub(rf"(\d)\s+({_UNITS})\b", r"\1\2", text)


def ungrounded_facts(value: FieldValue, cited_texts: list[str]) -> list[str]:
    """Facts of the value that appear in none of the cited chunks (dates compared as dates, phones as digits)."""
    corpus = " ".join(cited_texts)
    lower = _join_units(corpus.lower())
    dates = find_dates(corpus)
    phones = {re.sub(r"\D", "", m.group(0)) for m in _PHONE.finditer(corpus)}
    missing = []
    for fact in key_facts(value):
        if _ISO_DATE.fullmatch(fact):
            ok = date.fromisoformat(fact) in dates
        elif _PHONE.fullmatch(fact):
            ok = re.sub(r"\D", "", fact) in phones
        else:
            ok = re.search(rf"(?<![\w-]){re.escape(_join_units(fact.lower()))}(?![\w-])", lower) is not None
        if not ok:
            missing.append(fact)
    return missing


def clock_times(text: str) -> set[str]:
    """Times written in the text as 24-hour 'HH:MM' ('2:00 PM' -> '14:00', '14:00:00' -> '14:00')."""
    times = set()
    for m in _CLOCK.finditer(text):
        if m.group(3):  # 12-hour clock with AM/PM
            hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0)
            minutes = m.group(2) or "00"
        else:
            hour, minutes = int(m.group(4)), m.group(5)
        if hour < 24:
            times.add(f"{hour:02d}:{minutes}")
    return times


def datetime_problems(value: str, cited_texts: list[str]) -> tuple[list[str], list[str]]:
    """A time must be stated in the cited text (no invented '00:00'); a missing zone is only a warning."""
    corpus = " ".join(cited_texts)
    parts = value.split()
    failures, warnings = [], []
    if len(parts) >= 2 and parts[1] not in clock_times(corpus):
        failures.append(f"time {parts[1]} is not stated in the cited text")
    if len(parts) == 3 and not re.search(rf"\b{re.escape(parts[2])}\b", corpus):
        warnings.append(f"time zone {parts[2]} is not stated in the cited text")
    return failures, warnings


def latest_change(changes: list[AddendumChange], field: str) -> AddendumChange | None:
    relevant = [c for c in changes if c.field == field]
    return relevant[-1] if relevant else None


def confidence_for(result: FieldResult, status: str, warnings: list[str], attempts: int) -> float:
    """Explainable confidence: start from the extractor's own estimate, then adjust for evidence quality."""
    if status == "not_found":
        return 0.8 if attempts > 1 else 0.6  # sure it is absent once a wider search also found nothing
    conf = result.confidence or 0.7
    files = {s.file for s in result.sources}
    conf = min(conf + 0.05, 0.99) if len(files) >= 2 else conf * 0.9  # two documents agree / a single source
    if warnings:
        conf *= 0.6
    if status == "failed":
        conf = min(conf, 0.3)
    return round(conf, 2)


class ValidatorAgent:
    def __init__(self, ctx: AgentContext) -> None:
        self.ctx = ctx

    def check(self, name: str, result: FieldResult, state: BidState) -> tuple[list[str], list[str]]:
        """Deterministic checks for one field: (failures, warnings)."""
        failures = list(state.get("extraction_issues", {}).get(name, []))
        warnings: list[str] = []
        if result.value is None:
            return failures, warnings
        if not result.sources:
            failures.append("the value has no citation")
        texts, bad_quotes = [], 0
        for src in result.sources:
            chunk = self.ctx.engine.get_chunk(src.chunk_id)
            if chunk is None:
                failures.append(f"cited chunk {src.chunk_id} does not exist")
                continue
            texts.append(chunk_body(chunk))
            bad_quotes += not quote_supported(src.quote, texts[-1])
        if result.sources and bad_quotes == len(result.sources):
            failures.append("no quote matches the text it cites")
        elif bad_quotes:
            warnings.append(f"{bad_quotes} quote(s) do not match the text they cite")
        if problem := format_problem(result.value, self.ctx.specs[name]):
            failures.append(problem)
        elif self.ctx.specs[name].type == "datetime" and texts:
            time_failures, time_warnings = datetime_problems(str(result.value), texts)
            failures += time_failures
            warnings += time_warnings
        facts, missing = key_facts(result.value), ungrounded_facts(result.value, texts)
        if missing and texts:
            message = f"not found in the cited text: {', '.join(missing)}"
            (failures if len(missing) == len(facts) else warnings).append(message)
        change = latest_change(state.get("addendum_changes", []), name)
        if change and change.new_value is not None and as_text(change.new_value) != as_text(result.value):
            failures.append(f"value differs from the latest addendum change ({change.addendum})")
        return failures, warnings

    def judge(self, names: list[str], drafts: dict[str, FieldResult]) -> dict[str, Verdict]:
        """One batched call to the fast model for the free-text fields that passed the deterministic checks."""
        if not names:
            return {}
        blocks = []
        for name in names:
            # the full cited passages, not only the short quotes: a value often combines several facts
            chunk_ids = list(dict.fromkeys(s.chunk_id for s in drafts[name].sources))
            cited = [chunk_body(c)[:3000] for cid in chunk_ids if (c := self.ctx.engine.get_chunk(cid))]
            passages = "\n".join(f"  [{i}] {text}" for i, text in enumerate(cited, start=1))
            blocks.append(
                f"### {name}\nQuestion: {self.ctx.specs[name].question}\n"
                f"Value: {as_text(drafts[name].value)}\nCited text:\n{passages}"
            )
        try:
            report = self.ctx.llm.structured(
                JUDGE_PROMPT.format(fields="\n\n".join(blocks)),
                JudgeReport,
                system=JUDGE_SYSTEM,
                role="fast",
                agent="validator_judge",
            )
        except LLMError as exc:
            self.ctx.trace.log("validator", "decision", judge="unavailable", error=str(exc))
            return {}
        return {v.field: v for v in report.verdicts if v.field in names}

    def _failed(self, state: dict, exc: Exception) -> dict:
        """If validation itself breaks, the round's fields are failed (unverified values are not passed)."""
        reason = f"validation failed: {exc}"
        validation = dict(state.get("validation", {}))
        for f in state.get("fields_todo", []):
            validation[f] = FieldValidation(status="failed", confidence=0.3, failures=[reason])
        return {"validation": validation, "retry_reasons": {}}

    @agent_node("validator", on_error=_failed)
    def __call__(self, state: BidState) -> dict:
        todo = state["fields_todo"]
        drafts = {f: state["drafts"].get(f, FieldResult(notes="no draft produced")) for f in todo}
        checks = {f: self.check(f, drafts[f], state) for f in todo}
        to_judge = [f for f in todo if self.ctx.specs[f].judge and drafts[f].value is not None and not checks[f][0]]
        verdicts = self.judge(to_judge, drafts)

        validation, retry, updated, history = dict(state["validation"]), {}, {}, dict(state.get("history", {}))
        for f in todo:
            failures, warnings = checks[f]
            if (v := verdicts.get(f)) and v.verdict != "yes":
                (failures if v.verdict == "no" else warnings).append(f"judge ({v.verdict}): {v.reason}")
            result, attempts = drafts[f], state["attempts"].get(f, 1)
            if failures:
                status = "failed"
                retry[f] = "; ".join(failures)
            elif result.value is None:
                status = "not_found"
                if attempts == 1:  # one wider search before accepting "not found"
                    retry[f] = "no value was found in the first search; look again in the wider evidence"
                if not result.notes.startswith(NOT_FOUND):
                    result = result.model_copy(update={"notes": f"{NOT_FOUND}. {result.notes}".strip(" .")})
            else:
                status = "passed"
            conf = confidence_for(result, status, warnings, attempts)
            updated[f] = result.model_copy(update={"confidence": conf})
            validation[f] = FieldValidation(
                status=status, confidence=conf, attempts=attempts, failures=failures, warnings=warnings
            )
            history[f] = [*history.get(f, []), (updated[f], validation[f])]
        self.ctx.trace.log(
            "validator", "decision", results={f: validation[f].status for f in todo}, retry=retry, judged=to_judge
        )
        return {"validation": validation, "retry_reasons": retry, "drafts": updated, "history": history}
