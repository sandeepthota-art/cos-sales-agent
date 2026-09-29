from typing import Any

# Shared by app.providers.llm.claude and app.providers.llm.openai -- both real
# providers format thread_history into the analyze_email prompt identically, so
# this one function is the single source of truth for that text, rather than
# duplicating the loop/format string in both provider files.


def format_thread_history(thread_history: list[dict[str, Any]] | None) -> str:
    """Renders prior thread messages as a plain-text block to prepend before the
    new message's own Subject/Body in an analyze_email prompt. Returns "" (no
    block at all) when there's no history -- a thread's first message must never
    show an empty, misleading "Prior messages" header."""
    if not thread_history:
        return ""

    rendered = []
    for message in thread_history:
        sender = message.get("from", {}) or {}
        sender_label = sender.get("name") or sender.get("email") or "unknown sender"
        rendered.append(
            f"[{message.get('timestamp')}] {sender_label}: {message.get('subject')}\n{message.get('body')}"
        )

    return (
        "Prior messages in this thread, oldest first (context only -- the new message "
        "below is what you are analyzing):\n\n" + "\n\n---\n\n".join(rendered) + "\n\n===\n\n"
    )
