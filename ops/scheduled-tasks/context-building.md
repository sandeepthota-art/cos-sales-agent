# Scheduled task: context building

## Status: NOT BUILT YET

No code, no skill, no tools exist for this today. `app/tools.py` has no
Person/Organization/Thread-as-entity logic — only `raw_emails_dump`
documents. See `docs/ROADMAP.md` Phase 4 ("Complete the context model")
and `docs/DECISIONS.md`'s open question on how `raw_emails_dump` relates
to any future context collections.

Do not create a Claude Desktop scheduled task for this until Phase 4
ships — there is nothing for it to call.
