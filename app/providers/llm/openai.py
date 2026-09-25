import json
from typing import Any

from openai import OpenAI

from app.email.models import Email
from app.interfaces.llm_provider import LLMProvider
from app.providers.llm.claude import _ANALYSIS_INSTRUCTIONS, _UPDATE_CONTEXT_INSTRUCTIONS


class OpenAIProvider(LLMProvider):
    def __init__(self, api_key: str, model: str, base_url: str | None = None):
        # base_url=None is the OpenAI SDK's own default (api.openai.com) -- passing it
        # explicitly here is identical to omitting it, so existing behavior is preserved
        # exactly when no custom endpoint is configured. A non-None value lets this same
        # provider target any OpenAI-compatible API (e.g. Groq) with no other change.
        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self._model = model

    def _complete_json(self, system: str, user: str) -> dict[str, Any]:
        response = self._client.chat.completions.create(
            model=self._model,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        return json.loads(response.choices[0].message.content)

    def analyze_email(self, email: Email) -> dict[str, Any]:
        result = self._complete_json(
            _ANALYSIS_INSTRUCTIONS, f"Subject: {email.subject}\n\nBody:\n{email.body}"
        )
        result.setdefault("email_id", email.message_id)
        return result

    def update_context(self, previous_context: dict[str, Any], new_analysis: dict[str, Any]) -> dict[str, Any]:
        user = json.dumps({"previous_context": previous_context, "new_analysis": new_analysis})
        return self._complete_json(_UPDATE_CONTEXT_INSTRUCTIONS, user)

    def verify_same_fact(self, existing_value: str, new_value: str, subject: str, predicate: str) -> bool:
        instructions = "Answer ONLY with JSON: {\"same_fact\": true} or {\"same_fact\": false}."
        user = (
            f"Subject: {subject}\nPredicate: {predicate}\nExisting value: {existing_value}\n"
            f"New value: {new_value}\nAre these describing the same underlying fact?"
        )
        result = self._complete_json(instructions, user)
        return bool(result.get("same_fact", False))

    def draft_reply(self, context: dict[str, Any], latest_email: Email) -> dict[str, Any]:
        instructions = "Draft a professional sales reply. Return ONLY JSON: {\"subject\": ..., \"body\": ...}."
        user = json.dumps(
            {
                "context": context,
                "latest_email_subject": latest_email.subject,
                "latest_email_body": latest_email.body,
                "sender_name": latest_email.from_.name or latest_email.from_.email,
            }
        )
        return self._complete_json(instructions, user)
