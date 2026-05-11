#!/usr/bin/env python3
"""Upload a PLS pronunciation dictionary to ElevenLabs and cache its locator.

Usage:
    python tools/upload_pls.py [PLS_PATH]

Default PLS_PATH: projects/youtube/pronunciation/narration.pls
Manifest written alongside the PLS:
    <pls_dir>/manifest.json  → {"dictionary_id", "version_id", "sha256", "name", "uploaded_at"}

Idempotent: if the file's sha256 matches the manifest, no upload is performed.
On content change, a fresh dictionary is created (ElevenLabs returns a new
{id, version_id} pair) and the manifest is overwritten. The old dictionary
remains on the account until manually removed — that's fine, it just isn't
referenced anymore.

Requires ELEVENLABS_API_KEY in env.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import sys
from pathlib import Path

import requests

ELEVEN_API_BASE = "https://api.elevenlabs.io"
DEFAULT_PLS = Path("projects/youtube/pronunciation/narration.pls")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def upload(api_key: str, pls_path: Path, name: str) -> dict:
    url = f"{ELEVEN_API_BASE}/v1/pronunciation-dictionaries/add-from-file"
    headers = {"xi-api-key": api_key}
    with pls_path.open("rb") as f:
        files = {"file": (pls_path.name, f, "text/xml")}
        data = {"name": name, "description": f"Auto-uploaded from {pls_path}"}
        resp = requests.post(url, headers=headers, files=files, data=data, timeout=60)
    if not resp.ok:
        sys.stderr.write(f"Upload failed [{resp.status_code}]: {resp.text}\n")
        resp.raise_for_status()
    return resp.json()


def main() -> int:
    pls_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PLS
    if not pls_path.is_file():
        sys.stderr.write(f"PLS file not found: {pls_path}\n")
        return 2

    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        sys.stderr.write("ELEVENLABS_API_KEY is not set.\n")
        return 2

    manifest_path = pls_path.parent / "manifest.json"
    current_hash = sha256_of(pls_path)

    if manifest_path.is_file():
        prior = json.loads(manifest_path.read_text())
        if prior.get("sha256") == current_hash and prior.get("dictionary_id"):
            print(f"No change (sha256 match). Using cached dictionary:")
            print(f"  dictionary_id = {prior['dictionary_id']}")
            print(f"  version_id    = {prior['version_id']}")
            return 0

    name = f"{pls_path.stem}-{current_hash[:8]}"
    print(f"Uploading {pls_path} as '{name}' ...")
    body = upload(api_key, pls_path, name)

    dictionary_id = body.get("id") or body.get("pronunciation_dictionary_id")
    version_id = body.get("version_id") or body.get("latest_version_id")
    if not dictionary_id or not version_id:
        sys.stderr.write(f"Unexpected response shape: {json.dumps(body)[:400]}\n")
        return 1

    manifest = {
        "dictionary_id": dictionary_id,
        "version_id": version_id,
        "name": name,
        "sha256": current_hash,
        "source": str(pls_path),
        "uploaded_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Wrote manifest: {manifest_path}")
    print(f"  dictionary_id = {dictionary_id}")
    print(f"  version_id    = {version_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
