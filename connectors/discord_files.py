"""Discord 첨부 보관 (PRD §6.4, Clio D6·D7 계승).

OpenClaw 브리지는 첨부를 `~/.openclaw/media/inbound/`에 로컬 파일로 내려받아 경로를 넘긴다.
URL이 오는 경우도 대비한다. 원본은 sha256 이름으로 `attachments/<2자>/<sha256>.<ext>`에 보관하고
같은 내용은 한 번만 저장한다.
"""

from __future__ import annotations

import mimetypes
import os
import shutil
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from claire_core import ClaireError, data_dir, sha256_file  # noqa: E402

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".bmp", ".tiff"}


def inbound_dir(cfg: dict) -> Path:
    return Path(cfg["discord"]["inbound_media_dir"]).expanduser()


def inbound_summary(cfg: dict, limit: int = 5) -> dict:
    d = inbound_dir(cfg)
    if not d.is_dir():
        return {"present": False, "path": str(d)}
    files = sorted((p for p in d.iterdir() if p.is_file()), key=lambda p: p.stat().st_mtime,
                   reverse=True)
    images = [p for p in files if p.suffix.lower() in IMAGE_EXTS]
    return {
        "present": True, "path": str(d), "readable": os.access(d, os.R_OK),
        "files": len(files), "images": len(images),
        "recent": [{"name": p.name, "bytes": p.stat().st_size} for p in files[:limit]],
    }


def stage_source(src: str, is_url: bool, timeout: int = 30) -> tuple[Path, str, str | None]:
    """원본을 임시 위치로 가져온다. (staged_path, filename, source_url)"""
    att_dir = data_dir() / "attachments"
    tmp = att_dir / ".incoming"
    tmp.mkdir(parents=True, exist_ok=True)
    if is_url:
        name = os.path.basename(src.split("?")[0]) or "attachment"
        staged = tmp / name
        with urllib.request.urlopen(src, timeout=timeout) as resp, staged.open("wb") as fh:
            shutil.copyfileobj(resp, fh)
        return staged, name, src
    p = Path(src).expanduser()
    if not p.is_file():
        raise ClaireError("attachment_missing", f"첨부파일이 없습니다: {src}")
    staged = tmp / p.name
    shutil.copyfile(p, staged)
    return staged, p.name, None


def store_attachment(conn, source_event_id: int, src: str, ts: str, is_url: bool = False) -> dict:
    """원본을 sha256 파일명으로 보관하고 attachment 행을 남긴다. 같은 내용은 한 번만."""
    staged, name, source_url = stage_source(src, is_url)
    return store_staged(conn, source_event_id, staged, name, source_url, ts)


def store_staged(conn, source_event_id: int, staged: Path, name: str, source_url: str | None, ts: str) -> dict:
    """이미 .incoming 에 있는 파일을 sha256 이름으로 옮기고 attachment 행을 남긴다."""
    att_dir = data_dir() / "attachments"
    digest = sha256_file(staged)
    ext = Path(name).suffix.lower()
    dest_dir = att_dir / digest[:2]
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{digest}{ext}"
    if not dest.exists():
        shutil.move(str(staged), str(dest))
    else:
        staged.unlink(missing_ok=True)
    rel = str(dest.relative_to(data_dir()))
    existing = conn.execute("SELECT id FROM attachment WHERE source_event_id=? AND sha256=?",
                            (source_event_id, digest)).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO attachment(source_event_id,filename,mime_type,byte_size,sha256,"
            "stored_path,source_url,fetched_at) VALUES(?,?,?,?,?,?,?,?)",
            (source_event_id, name, mimetypes.guess_type(name)[0], dest.stat().st_size,
             digest, rel, source_url, ts))
    return {"filename": name, "sha256": digest, "stored_path": rel,
            "duplicate_content": existing is not None}


def find_by_sha256(conn, digest: str):
    """같은 이미지를 다시 올렸는지 (TC24)."""
    return conn.execute(
        "SELECT a.*, s.id AS source_event_id FROM attachment a "
        "JOIN source_event s ON s.id = a.source_event_id WHERE a.sha256=? ORDER BY a.id",
        (digest,)).fetchall()
