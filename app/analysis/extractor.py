import json

from pydantic import BaseModel, ValidationError

from app.analysis.schemas import EmailAnalysis
from app.email.models import Email
from app.interfaces.llm_provider import LLMProvider


class AnalysisOutcome(BaseModel):
    success: bool
    analysis: EmailAnalysis | None = None
    error: str | None = None


def analyze_email_with_validation(
    llm: LLMProvider, email: Email, max_retries: int = 1
) -> AnalysisOutcome:
    last_error: str | None = None
    attempts = max_retries + 1

    for _ in range(attempts):
        # A JSONDecodeError raised by llm.analyze_email() itself (a malformed Claude
        # response, e.g. valid JSON followed by stray trailing text that even
        # _extract_json's raw_decode couldn't salvage, or genuinely broken JSON) used to
        # propagate straight past this loop uncaught -- only a Pydantic ValidationError
        # on an already-parsed result got a retry. Live reproduction of two real
        # JSONDecodeError failures showed both succeeded cleanly on a fresh attempt with
        # identical input, so this failure mode gets the same retry chance as a schema
        # mismatch, using the same max_retries budget -- not a new retry mechanism.
        try:
            raw = llm.analyze_email(email)
        except json.JSONDecodeError as exc:
            last_error = f"JSONDecodeError: {exc}"
            continue

        raw.setdefault("email_id", email.message_id)
        try:
            analysis = EmailAnalysis.model_validate(raw)
            return AnalysisOutcome(success=True, analysis=analysis, error=None)
        except ValidationError as exc:
            last_error = str(exc)

    return AnalysisOutcome(success=False, analysis=None, error=last_error)
