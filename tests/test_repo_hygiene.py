import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_FORBIDDEN_PATTERNS = [
    re.compile(r"C:\\Users\\", re.IGNORECASE),
    re.compile(r"/Users/[a-zA-Z0-9_.-]+"),
    re.compile(r"/home/[a-zA-Z0-9_.-]+"),
    re.compile(r"sk-[A-Za-z0-9-]{20,}"),
]

_SCAN_EXTENSIONS = {".py", ".md", ".yml", ".yaml", ".env.example", ".toml", ".txt"}
_EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".superpowers",
    "node_modules",
    "dist",
}


def _iter_source_files():
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _EXCLUDED_DIRS for part in path.parts):
            continue
        if path.suffix in _SCAN_EXTENSIONS or path.name in {".env.example", "Dockerfile"}:
            yield path


def test_no_machine_specific_paths_or_leaked_credentials():
    violations = []
    for path in _iter_source_files():
        if path.name == "test_repo_hygiene.py":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in _FORBIDDEN_PATTERNS:
            if pattern.search(text):
                violations.append((str(path.relative_to(REPO_ROOT)), pattern.pattern))
    assert violations == []


def test_env_file_is_gitignored():
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in gitignore.splitlines()


def test_env_example_has_no_real_secrets():
    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "LLM_API_KEY=" in env_example
    assert "LLM_API_KEY=sk-" not in env_example
