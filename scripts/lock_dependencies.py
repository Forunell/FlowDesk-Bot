"""Record the actually tested packages, separating runtime and optional tools."""
from importlib.metadata import distribution
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parent.parent


def lock(filename, roots):
    pinned = {}
    pending = list(roots)
    while pending:
        name = canonicalize_name(pending.pop())
        if name in pinned:
            continue
        package = distribution(name)
        pinned[name] = package.version
        for line in package.requires or []:
            requirement = Requirement(line)
            if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
                pending.append(requirement.name)
    text = "# Tested on Windows x64 / Python 3.13.12. Generated from the installed environment.\n"
    text += "\n".join(f"{name}=={version}" for name, version in sorted(pinned.items())) + "\n"
    (ROOT / filename).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    runtime = ["fastapi", "uvicorn", "jinja2", "python-multipart", "python-dotenv"]
    lock("requirements-lock.txt", runtime)
    lock("requirements-dev-lock.txt", runtime + ["pytest", "httpx", "ruff", "playwright"])
    lock("requirements-telegram-lock.txt", runtime + ["aiogram"])
