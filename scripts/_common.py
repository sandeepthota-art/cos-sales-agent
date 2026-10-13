"""Shared helpers for eval scripts (load_golden.py, score_eval.py). Not
imported by app/ -- the production server has no reason to ever run
against an eval database, so this guard has no business being on the
main runtime path.
"""


def require_eval_database(database_name: str) -> None:
    """Refuses to proceed unless database_name ends with '_eval'. Every
    eval script calls this before touching Mongo, so a misconfigured
    MONGODB_DATABASE can never point an eval run (or its drop/reset) at
    the real database."""
    if not database_name.endswith("_eval"):
        raise RuntimeError(
            f"refusing to run against database {database_name!r} -- eval scripts only run "
            "against a database whose name ends with '_eval' (set MONGODB_DATABASE accordingly)"
        )
