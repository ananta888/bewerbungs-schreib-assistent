from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def initialize_profiles(target: Path, force: bool = False) -> list[Path]:
    skill_root = Path(__file__).resolve().parents[1]
    target.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    for name in ("candidate-profile", "style-profile"):
        source = skill_root / f"{name}.example.yaml"
        destination = target / f"{name}.yaml"
        if destination.exists() and not force:
            raise FileExistsError(f"Refusing to overwrite {destination}; use --force only after explicit approval")
        shutil.copyfile(source, destination)
        created.append(destination)
    return created


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize private candidate and style profiles")
    parser.add_argument("--target", type=Path, required=True, help="Private application workspace")
    parser.add_argument("--force", action="store_true", help="Overwrite existing profiles")
    args = parser.parse_args()
    try:
        created = initialize_profiles(args.target, args.force)
    except (FileExistsError, OSError) as exc:
        print(f"ERROR: {exc}")
        return 1
    for path in created:
        print(f"Created {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
