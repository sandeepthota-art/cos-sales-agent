"""Rebuilds every people/organizations profile from observations and
reports what would change. Dry-run by default; prints entity keys and
changed attribute names only, never values/evidence/email content.

Local script, NOT an MCP tool -- never reachable from a scheduled task.

Usage:
    python scripts/rebuild_profiles.py            # dry run
    python scripts/rebuild_profiles.py --apply    # actually rebuild
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.context import compute_organization_profile, compute_person_profile, rebuild_organization, rebuild_person
from app.context_db import ObservationRepository, OrganizationRepository, PersonRepository
from app.db import get_client, initialize_database


def diff_person(db, email: str) -> list[str]:
    computed = compute_person_profile(db, email)
    existing = (PersonRepository(db).find(email) or {}).get("profile") or {}
    return sorted(k for k in computed["profile"] if existing.get(k) != computed["profile"][k])


def diff_organization(db, domain: str) -> list[str]:
    computed = compute_organization_profile(db, domain)
    existing = (OrganizationRepository(db).find(domain) or {}).get("profile") or {}
    return sorted(k for k in computed["profile"] if existing.get(k) != computed["profile"][k])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Actually rebuild. Default is dry-run (report only).")
    args = parser.parse_args()

    settings = get_settings()
    db = initialize_database(get_client(settings.mongodb_uri), settings.mongodb_database)

    entities = ObservationRepository(db).distinct_entities()
    if not entities:
        print("No observations recorded -- nothing to rebuild.")
        return

    changed = 0
    for entity_type, entity_key in entities:
        changed_attrs = diff_person(db, entity_key) if entity_type == "person" else diff_organization(db, entity_key)
        if changed_attrs:
            changed += 1
            verb = "Rebuilding" if args.apply else "Would rebuild"
            print(f"{verb} {entity_type} {entity_key}: changed attributes {changed_attrs}")
        if args.apply:
            if entity_type == "person":
                rebuild_person(db, entity_key)
            else:
                rebuild_organization(db, entity_key)

    print(f"{changed} of {len(entities)} profile(s) differ from stored state.")
    if not args.apply:
        print("Dry run -- no changes made. Re-run with --apply to rebuild.")


if __name__ == "__main__":
    main()
