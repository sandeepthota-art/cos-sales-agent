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


def format_person_context(person_context: dict[str, Any] | None) -> str:
    """Renders a bounded person-context view (see
    app.entities.person_context.get_bounded_person_context_for_llm) as a plain-text
    block to prepend before the thread-history/Subject/Body block in an
    analyze_email prompt. Returns "" when there's nothing to show -- a sender with
    no prior processed email must never show an empty, misleading header."""
    if not person_context:
        return ""

    lines = [f"Known context on the sender, {person_context['name']!r}:"]
    if person_context.get("org"):
        lines.append(f"- Organization: {person_context['org']}")
    for entry in person_context.get("current_context", []):
        lines.append(f"- [{entry['category']}, {entry['provenance']}] {entry['summary']}")
    for entry in person_context.get("historical_context", []):
        lines.append(f"- [{entry['category']}, historical] {entry['summary']}")
    for item in person_context.get("knowledge", []):
        # item["predicate"] is itself a semantic label (role/preference/concern/...)
        # for a person-attributed fact (see app.pipeline._process_person_facts), or
        # a generic fact predicate otherwise -- rendered as the leading tag either
        # way, exactly like current_context/historical_context entries above.
        lines.append(f"- [{item['predicate']}, {item['basis']}] {item['current_value']}")

    return "\n".join(lines) + "\n\n===\n\n"
