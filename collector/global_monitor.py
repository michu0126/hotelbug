"""Entry point for worldwide monitoring across the six configured hotel groups."""

import sys
from pathlib import Path


def cli():
    backend = Path(__file__).resolve().parents[1] / "backend"
    if not (backend / "app" / "local_global.py").is_file():
        raise SystemExit("Global monitor requires the project's backend/ directory")
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    try:
        from app.local_global import cli as run
    except ImportError as error:
        raise SystemExit(
            "Global monitor dependencies are missing. Install collector/requirements-global.txt "
            "with this same Python environment."
        ) from error
    run()


if __name__ == "__main__":
    cli()
