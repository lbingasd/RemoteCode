#!/usr/bin/env python3
"""Build the deployment ZIP from an allowlist; never include runtime secrets/caches."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

PACKAGE = Path(__file__).resolve().parent.parent


def build(output):
    files = [PACKAGE / name for name in ("README.md", "LICENSE", ".gitignore", "deploy-cloud.sh", "deploy-local.sh", "deploy-local.ps1", "deploy-qq.sh", "deploy-qq.ps1")]
    for folder, suffixes in (("scripts", {".py", ".sh", ".ps1", ".mjs"}), ("tests", {".py", ".ps1", ".mjs"}),
                             ("examples", {".json"}), ("docs", {".md", ".json", ".png"})):
        files += [path for path in (PACKAGE / folder).rglob("*")
                  if path.is_file() and not path.is_symlink() and path.suffix in suffixes
                  and not any(part.startswith(".") or part == "__pycache__" for part in path.relative_to(PACKAGE / folder).parts)]
    files.append(PACKAGE / "assets/README.md")
    files.append(PACKAGE / "skills/remotecode-qq/SKILL.md")
    for name in ("nssm.lock.json", "acp-extension.lock.json"):
        item = json.loads((PACKAGE / "examples" / name).read_text())
        asset = PACKAGE / "assets" / item["name"]
        assert not asset.is_symlink() and hashlib.sha256(asset.read_bytes()).hexdigest() == item["sha256"], f"Unverified bundled asset: {name}"
        files.append(asset)
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = output.with_suffix(".zip.tmp")
    with zipfile.ZipFile(stage, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(set(files)):
            info = zipfile.ZipInfo(path.relative_to(PACKAGE).as_posix(), (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (0o100755 if path.suffix == ".sh" else 0o100644) << 16
            archive.writestr(info, path.read_bytes())
    with zipfile.ZipFile(stage) as archive:
        assert archive.testzip() is None, "ZIP CRC verification failed"
        count = len(archive.namelist())
    stage.replace(output)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(".sha256").write_text(f"{digest}  {output.name}\n", encoding="utf-8")
    print(f"{output}\n{count} files, {output.stat().st_size} bytes\nSHA-256: {digest}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=PACKAGE / "RemoteCode-IP-deployment.zip")
    build(parser.parse_args().output.resolve())
