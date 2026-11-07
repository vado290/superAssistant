"""Local artifact staging. No network upload or arbitrary source-file import."""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path


class ArtifactStore:
    def __init__(self, root: Path, max_bytes: int = 16 * 1024 * 1024):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        if max_bytes < 1:
            raise ValueError("invalid artifact size limit")

    @staticmethod
    def owner(context: dict) -> dict:
        project = context.get("project_id")
        identity = context.get("caller_identity") or {}
        scope = identity.get("task_id") or context.get("run_id")
        if not project or not scope:
            raise PermissionError("trusted project and task/run scope required")
        return {"project_id": str(project), "scope": str(scope)}

    def stage(self, content: bytes, context: dict, media_type: str = "application/octet-stream") -> dict:
        owner = self.owner(context)
        if not isinstance(content, bytes) or not 0 < len(content) <= self.max_bytes:
            raise ValueError("artifact must be nonempty and within the size limit")
        identity = uuid.uuid4().hex
        metadata = {"artifact_id": identity, **owner, "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(), "media_type": media_type}
        folder = self.root / identity
        folder.mkdir()
        (folder / "content").write_bytes(content)
        (folder / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        return {key: metadata[key] for key in ("artifact_id", "size", "sha256", "media_type")}

    def read(self, identity: str, context: dict) -> bytes:
        if not isinstance(identity, str) or len(identity) != 32 or any(c not in "0123456789abcdef" for c in identity):
            raise ValueError("invalid artifact reference")
        folder = self.root / identity
        if folder.is_symlink() or any((folder / name).is_symlink() for name in ("metadata.json", "content")):
            raise PermissionError("artifact links are not allowed")
        metadata = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
        if any(metadata.get(key) != value for key, value in self.owner(context).items()):
            raise PermissionError("artifact belongs to another scope")
        if not 0 < (folder / "content").stat().st_size <= self.max_bytes:
            raise ValueError("artifact exceeds size limit")
        content = (folder / "content").read_bytes()
        if len(content) != metadata["size"] or hashlib.sha256(content).hexdigest() != metadata["sha256"]:
            raise ValueError("artifact integrity check failed")
        return content
