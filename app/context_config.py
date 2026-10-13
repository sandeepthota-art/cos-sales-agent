"""Phase 4 context-building config. Policy constants live here, never
hardcoded inline in app/context.py's validation/business logic -- that
module imports from here. Not a pydantic Settings class: nothing here is
deployment-specific (no secrets, no per-environment values), so a plain
module is simplest (see docs/PHASE4_DESIGN.md's deviations table).
"""

# Free-mail/ISP domains -- never get an organization record (locked rule:
# "organizations._id = domain (never free-mail)").
FREE_MAIL_DOMAINS: set[str] = {
    "gmail.com",
    "googlemail.com",
    "outlook.com",
    "hotmail.com",
    "live.com",
    "msn.com",
    "yahoo.com",
    "ymail.com",
    "icloud.com",
    "me.com",
    "mac.com",
    "proton.me",
    "protonmail.com",
    "aol.com",
    "gmx.com",
    "zoho.com",
}

# Local-parts that mean "this is not a human" -- such an address never gets
# person attributes (observations rejected by construction).
ROLE_LOCAL_PARTS: set[str] = {
    "noreply",
    "no-reply",
    "donotreply",
    "do-not-reply",
    "info",
    "support",
    "admin",
    "billing",
    "notifications",
    "team",
    "hello",
    "sales",
    "contact",
}

# Allowed attribute names per entity type. Anything not listed here is
# rejected by persist_context_delta's validation -- this is what keeps
# sensitive categories (health, religion, politics, ethnicity, sexual
# orientation, finances/credentials) out: they are simply never whitelisted,
# so there is no attribute name that would let them through.
PERSON_ATTRIBUTE_WHITELIST: set[str] = {
    "title",
    "department",
    "phone",
    "timezone",
    "relationship_to_owner",
    "working_hours",
    "preferred_channel",
}

ORGANIZATION_ATTRIBUTE_WHITELIST: set[str] = {
    "industry",
    "description",
    "website",
    "size_hint",
    "relationship_to_owner",
}

VALUE_MAX_LENGTH = 300
EVIDENCE_MAX_LENGTH = 300
MAX_BODY_CHARS_PER_MESSAGE = 4000
MAX_MESSAGES_PER_THREAD_CLAIM = 15
MAX_ENTITIES_PER_DELTA = 20
CONTEXT_CLAIM_TTL_MINUTES = 10
MAX_CONTEXT_ATTEMPTS = 3
THREAD_HISTORY_MAX_SNAPSHOTS = 20
THREAD_SUMMARY_MAX_LENGTH = 600
EMAIL_INSIGHT_SUMMARY_MAX_LENGTH = 200
CONTEXT_LAST_ERROR_MAX_LENGTH = 500

THREAD_STATUSES = {"active", "waiting_on_us", "waiting_on_them", "resolved", "unknown"}
OPEN_ITEM_OWNERS = {"us", "them", "unknown"}
INTENTS = {
    "request",
    "question",
    "fyi",
    "scheduling",
    "negotiation",
    "follow_up",
    "introduction",
    "complaint",
    "approval",
    "automated",
    "other",
}
SENTIMENTS = {"positive", "neutral", "negative"}
SENDER_TYPES = {"human", "automated", "newsletter", "cold_outreach", "unknown"}
ENTITY_TYPES = {"person", "organization"}
