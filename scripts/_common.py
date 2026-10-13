"""Shared helpers for eval scripts (load_golden.py, score_eval.py). Not
imported by app/ -- the production server has no reason to ever run
against an eval database, so this guard has no business being on the
main runtime path.
"""

from urllib.parse import urlparse

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _hosts_from_uri(uri: str) -> list[str]:
    """Every host named in a (possibly multi-host) mongodb:// or
    mongodb+srv:// URI, credentials and ports stripped. A multi-host URI
    is local only if EVERY listed host is -- one remote host in the list
    is enough to refuse."""
    netloc = urlparse(uri).netloc
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[-1]
    hosts = []
    for part in netloc.split(","):
        host = part.strip()
        if not host:
            continue
        if host.startswith("["):
            host = host[1:].split("]", 1)[0]
        else:
            host = host.split(":", 1)[0]
        hosts.append(host.lower())
    return hosts


def require_eval_database(database_name: str, uri: str | None = None, allow_remote: bool = False) -> None:
    """Refuses to proceed unless database_name ends with '_eval' --
    checked unconditionally, regardless of host. Every eval script calls
    this before touching Mongo, so a misconfigured MONGODB_DATABASE can
    never point an eval run (or its drop/reset) at the real database.

    When uri is given, also refuses unless every host in it is
    localhost/127.0.0.1/::1 -- unless allow_remote is True (the
    --allow-remote-eval flag). Never includes the URI or any credential
    in the error text, even when refusing; the message is always the
    fixed string "non-local host refused"."""
    if not database_name.endswith("_eval"):
        raise RuntimeError(
            f"refusing to run against database {database_name!r} -- eval scripts only run "
            "against a database whose name ends with '_eval' (set MONGODB_DATABASE accordingly)"
        )
    if uri is not None and not allow_remote:
        hosts = _hosts_from_uri(uri)
        if any(host not in _LOCAL_HOSTS for host in hosts):
            raise RuntimeError("non-local host refused")
