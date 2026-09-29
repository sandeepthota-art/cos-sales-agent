from abc import ABC, abstractmethod
from typing import Any

from app.email.models import Email


class LLMProvider(ABC):
    @abstractmethod
    def analyze_email(
        self, email: Email, thread_history: list[dict[str, Any]] | None = None,
        person_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """thread_history (optional): every prior message in this email's thread,
        oldest first, capped to the most recent 20 (see app.pipeline.build_thread_timeline)
        -- purely additive context so the model understands what the newest message
        is replying to. None/empty for a thread's first message. A provider that has
        no use for it (e.g. a pure-regex mock) may simply ignore the parameter.

        person_context (optional): a bounded view of what earlier processing has
        already established about this email's sender (see
        app.entities.person_context.get_bounded_person_context_for_llm) -- None for
        a sender with no prior processed email. Purely additive context, same as
        thread_history; a provider with no use for it may simply ignore it."""
        ...

    @abstractmethod
    def update_context(self, previous_context: dict[str, Any], new_analysis: dict[str, Any]) -> dict[str, Any]:
        """Return a bounded app.context.models.ContextDelta (as a dict), describing only
        what THIS email adds, changes, or invalidates -- never the full previous_context
        echoed back. app.context.engine.apply_context_delta merges the delta into
        previous_context deterministically; omitting a field (or returning empty
        added/removed lists for it) means "no change," and nothing already present is
        ever lost because the delta didn't repeat it.
        """
        ...

    @abstractmethod
    def verify_same_fact(self, existing_value: str, new_value: str, subject: str, predicate: str) -> bool:
        ...

    @abstractmethod
    def draft_reply(self, context: dict[str, Any], latest_email: Email) -> dict[str, Any]:
        ...
