"""Build dist/ankime-<version>.ankiaddon: the add-on's files as git knows them (tracked, or
new and not ignored), so stray media, user_files/ and meta.json never get in, minus what
only development needs."""
import json
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXCLUDE_DIRS = ("tests/", "tools/", ".github/", "user_files/", "dist/")
EXCLUDE_FILES = {".gitignore", "ruff.toml", "meta.json"}


def addon_files():
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                         cwd=ROOT, check=True, capture_output=True, text=True).stdout
    for name in sorted(set(out.splitlines())):
        if (name.startswith(EXCLUDE_DIRS) or name in EXCLUDE_FILES
                or "__pycache__" in name or not (ROOT / name).is_file()):
            continue
        yield name


def main():
    version = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))["human_version"]
    target = ROOT / "dist" / f"ankime-{version}.ankiaddon"
    target.parent.mkdir(exist_ok=True)
    names = list(addon_files())
    for required in ("__init__.py", "manifest.json", "config.json"):
        if required not in names:
            sys.exit(f"{required} is missing")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for name in names:
            z.write(ROOT / name, name)
    print(f"{target.relative_to(ROOT)}: {len(names)} files")


if __name__ == "__main__":
    main()
