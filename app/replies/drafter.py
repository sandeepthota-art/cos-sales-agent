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


def draft_reply(
    llm: LLMProvider,
    context: ThreadContext,
    email: Email,
    max_retries: int = 1,
    recipient_preferences: dict | None = None,
) -> ReplyDraftContent:
    # Same retry treatment as app.analysis.extractor.analyze_email_with_validation and
    # app.context.engine.build_next_context, for the same reason: a malformed Claude
    # response (JSONDecodeError) or a response that doesn't match ReplyDraftContent's
    # shape (ValidationError) previously had no retry at all here. Observed live in the
    # 100-email revalidation: a JSONDecodeError ("Invalid control character") from this
    # exact call site, with zero retry opportunity. One extra attempt, same default
    # budget as the other two call sites -- not a new retry mechanism.
    #
    # recipient_preferences: the reply recipient's own Person.preferences (looked up
    # by the caller, app.pipeline.run_pipeline, via the same sender_person it already
    # resolves) -- folded into the context dict under "recipient_preferences" so every
    # LLMProvider.draft_reply implementation can read it without a new interface
    # method. Omitted entirely (never an empty {}) when None, so a provider's own
    # "not present" check stays a plain dict.get with no ambiguity.
    context_dict = context.model_dump()
    if recipient_preferences:
        context_dict["recipient_preferences"] = recipient_preferences

    last_error: Exception | None = None
    for _ in range(max_retries + 1):
        try:
            raw = llm.draft_reply(context_dict, email)
            return ReplyDraftContent.model_validate(raw)
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = exc

    raise last_error
