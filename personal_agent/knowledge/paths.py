from pathlib import Path


def canonical_source_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)
