import json

from pydantic import ValidationError

from app.analysis.schemas import EmailAnalysis
from app.context.models import ThreadContext
from app.email.models import Email
from app.interfaces.llm_provider import LLMProvider
from app.replies.models import ReplyDraftContent


def needs_reply(analysis: EmailAnalysis, email: Email) -> bool:
    has_signal = any(
        [
            analysis.buying_signals,
            analysis.requirements,
            analysis.pain_points,
            analysis.objections,
            analysis.pricing_mentions,
            analysis.action_items,
        ]
    )
    has_question = "?" in email.body
    return has_signal or has_question


def draft_reply(llm: LLMProvider, context: ThreadContext, email: Email, max_retries: int = 1) -> ReplyDraftContent:
    # Same retry treatment as app.analysis.extractor.analyze_email_with_validation and
    # app.context.engine.build_next_context, for the same reason: a malformed Claude
    # response (JSONDecodeError) or a response that doesn't match ReplyDraftContent's
    # shape (ValidationError) previously had no retry at all here. Observed live in the
    # 100-email revalidation: a JSONDecodeError ("Invalid control character") from this
    # exact call site, with zero retry opportunity. One extra attempt, same default
    # budget as the other two call sites -- not a new retry mechanism.
    last_error: Exception | None = None
    for _ in range(max_retries + 1):
        try:
            raw = llm.draft_reply(context.model_dump(), email)
            return ReplyDraftContent.model_validate(raw)
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = exc

    raise last_error
