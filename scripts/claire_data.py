"""데이터 검증·백업 공용 도구 (0.9.0). claire_check(integrity·backup·restore)와 claire_relocation 이 같이 쓴다.

- run_integrity: 무결성 검사 (§10). 0.9.0 부터 임의의 데이터 루트(이전 중 사본·복원 사본)를 검사할 수 있다.
- data_fingerprint: 모든 표(검색 색인 제외)의 행 수·내용 해시·핵심 번호. 이전 전후·전환 뒤 새 기록 판정의 기준.
- change_plan: 옛 장부 → 새 장부 변화(추가·변경·사라진 행). 사라지면 안 되는 표에서 행이 사라졌으면 막는다.
- data_files / copy_hashed: 데이터 폴더의 복사 대상 목록과 해시 확인 복사.
- 가림(redaction) 목록: 백업 루트의 redaction_manifest.json 은 합집합으로만 늘어난다. 복원할 때 다시 적용한다.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path

from claire_core import (
    CLAIRE_VERSION, EVIDENCE_CODES, EVIDENCE_CRITERIA_RE, data_dir, hours_between, now_iso, sha256_file,
    table_counts, atomic_write_text,
)
import claire_paths

DB_NAME = claire_paths.DB_NAME
REDACTION_MANIFEST = "redaction_manifest.json"

# 운영 상태 문제(장부 손상이 아님). 이전·백업 판정에서 "기존 작업 상태"로 따로 보고한다.
OPERATIONAL_CHECKS = {"outbox_failed", "memo_sync_failed", "delivery_failed", "sync_stale", "sync_full_needed", "run_lock"}

# 코드가 행을 지우는 표 (0.8.0 기준: 메모 연결 해제·보고 재생성·관계 해제). 그 밖의 표에서 행이 사라지면 데이터 유실이다.
DELETABLE_TABLES = {"memo_link", "delivery", "relation"}

# 데이터 폴더 맨 위에서 복사하지 않는 것. secrets/ 는 따로(보호 복사), 장부는 백업 API 로.
EXCLUDE_TOP = {DB_NAME, f"{DB_NAME}-wal", f"{DB_NAME}-shm", claire_paths.MARKER_NAME, "backups", "skill-prev",
               "secrets", "tmp"}
EXCLUDE_TOP_WHY = {
    "backups": "옛 백업 (0.9.0 부터 백업 루트를 따로 쓴다 — 옛 위치에 그대로 남김)",
    "skill-prev": "직전 스킬 사본 (프로그램이지 데이터가 아님 — 0.9.0 부터 ~/.claire-schedule-manager/skill-prev)",
    "secrets": "인증정보 — 백업에는 넣지 않고 이전할 때만 보호 복사(700/600, 값 출력 없음)",
    "tmp": "임시 파일",
}


# --------------------------------------------------------------------------
# 무결성 (§10)
# --------------------------------------------------------------------------

def run_integrity(conn, cfg=None, root: Path | None = None) -> dict:
    """root 를 주면 첨부 파일을 그 데이터 루트 기준으로 본다 (기본: 현재 데이터 위치)."""
    base = Path(root) if root is not None else data_dir()
    problems = []

    def add(kind, detail):
        problems.append({"check": kind, "detail": detail})

    orphan_sql = {
        "item_source.item": "SELECT COUNT(*) c FROM item_source s LEFT JOIN item i ON i.id=s.item_id WHERE i.id IS NULL",
        "item_source.source_event": "SELECT COUNT(*) c FROM item_source s LEFT JOIN source_event e ON e.id=s.source_event_id WHERE e.id IS NULL",
        "link.item": "SELECT COUNT(*) c FROM link l LEFT JOIN item i ON i.id=l.item_id WHERE i.id IS NULL",
        "attachment.source_event": "SELECT COUNT(*) c FROM attachment a LEFT JOIN source_event e ON e.id=a.source_event_id WHERE e.id IS NULL",
        "question.item": "SELECT COUNT(*) c FROM question q WHERE q.item_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM item i WHERE i.id=q.item_id)",
        "relation.from": "SELECT COUNT(*) c FROM relation r LEFT JOIN item i ON i.id=r.from_item_id WHERE i.id IS NULL",
        "relation.to": "SELECT COUNT(*) c FROM relation r LEFT JOIN item i ON i.id=r.to_item_id WHERE i.id IS NULL",
        "activity_log.item": "SELECT COUNT(*) c FROM activity_log a WHERE a.item_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM item i WHERE i.id=a.item_id)",
    }
    if _has_table(conn, "memo"):     # 0.8.0 메모함
        orphan_sql.update({
            "memo_log.memo": "SELECT COUNT(*) c FROM memo_log l LEFT JOIN memo m ON m.id=l.memo_id WHERE m.id IS NULL",
            "memo_link.task": "SELECT COUNT(*) c FROM memo_link l WHERE l.kind='task' AND NOT EXISTS (SELECT 1 FROM item i WHERE i.id=l.target_id)",
            "memo_link.memo": "SELECT COUNT(*) c FROM memo_link l WHERE NOT EXISTS (SELECT 1 FROM memo m WHERE m.id=l.memo_id) "
                              "OR (l.kind='memo' AND NOT EXISTS (SELECT 1 FROM memo m WHERE m.id=l.target_id))",
        })
    for name, sql in orphan_sql.items():
        n = conn.execute(sql).fetchone()[0]
        if n:
            add("orphan", f"{name} 고아 레코드 {n}건")

    # done 항목의 근거 누락 (G5)
    for r in conn.execute("SELECT id, ref FROM item WHERE status='done'").fetchall():
        ev = conn.execute(
            "SELECT reason FROM activity_log WHERE item_id=? AND action='complete' AND undone_at IS NULL "
            "ORDER BY id DESC LIMIT 1", (r[0],)).fetchone()
        code = ev[0] if ev else None
        if not code or not (code in EVIDENCE_CODES or EVIDENCE_CRITERIA_RE.match(code)):
            add("done_without_evidence", f"{r[1]} 가 근거 코드 없이 done 입니다")

    for r in conn.execute(
        "SELECT i.ref FROM item i WHERE i.status='needs_info' AND NOT EXISTS "
        "(SELECT 1 FROM question q WHERE q.item_id=i.id AND q.status='open')").fetchall():
        add("needs_info_without_question", f"{r[0]} 는 needs_info 인데 열린 질문이 없습니다")

    # outbox 실패 잔량 (0.8.0: 메모함 반영은 따로 센다 — 업무 반영 실패와 섞지 않는다)
    n = conn.execute("SELECT COUNT(*) FROM outbox WHERE status='failed' AND op <> 'obsidian.memo'").fetchone()[0]
    if n:
        add("outbox_failed", f"outbox 실패 {n}건")
    n = conn.execute("SELECT COUNT(*) FROM outbox WHERE status='failed' AND op = 'obsidian.memo'").fetchone()[0]
    if n:
        add("memo_sync_failed", f"메모함 Obsidian 반영 실패·보류 {n}건 (메모는 저장됨 — claire_memo sync --pending, "
                                "Obsidian 에서 고친 블록이면 --memo … --overwrite)")
    n = conn.execute("SELECT COUNT(*) FROM delivery WHERE status='failed'").fetchone()[0]
    if n:
        add("delivery_failed", f"보고 전송 실패 {n}쪽 (claire_buttons resend)")

    if cfg:
        now_s = now_iso(cfg)
        for r in conn.execute("SELECT platform, account, last_success_at, full_sync_needed FROM sync_state").fetchall():
            if r[2] and hours_between(r[2], now_s) > 48:
                add("sync_stale", f"{r[0]}/{r[1]} 마지막 성공 {r[2]}")
            if r[3]:
                add("sync_full_needed", f"{r[0]}/{r[1]} 전체 재동기화 필요")

    # 첨부 sha256 (데이터 루트 기준 상대 경로. 절대 경로면 그대로 본다)
    for r in conn.execute("SELECT stored_path, sha256 FROM attachment WHERE redacted_at IS NULL").fetchall():
        p = attachment_file(base, r[0])
        if not p.exists():
            add("attachment_missing", f"첨부 파일 없음: {r[0]}")
        elif sha256_file(p) != r[1]:
            add("attachment_hash", f"첨부 해시 불일치: {r[0]}")

    ref_tables = [("item", "항목"), ("question", "질문")]
    if _has_table(conn, "memo"):
        ref_tables.append(("memo", "메모"))
    for table, label in ref_tables:
        refs = [r[0] for r in conn.execute(f"SELECT ref FROM {table}")]
        if len(refs) != len(set(refs)):
            add("ref_duplicate", f"{label} 번호가 중복되었습니다")
        nums = sorted(int(x.rsplit("-", 1)[1]) for x in refs if "-" in x and x.rsplit("-", 1)[1].isdigit())
        if nums:
            missing = sorted(set(range(1, nums[-1] + 1)) - set(nums))
            if missing:
                add("ref_gap", f"{label} 번호 공백 {len(missing)}건: {missing[:10]}")

    n_items = conn.execute("SELECT COUNT(*) FROM item WHERE deleted_at IS NULL").fetchone()[0]
    try:
        n_fts = conn.execute("SELECT COUNT(*) FROM search_fts").fetchone()[0]
        if n_fts != n_items:
            add("fts_stale", f"검색 색인 {n_fts}건 ≠ 항목 {n_items}건. rebuild 가 필요합니다")
    except sqlite3.OperationalError as e:
        add("fts_error", str(e))

    n = conn.execute("SELECT COUNT(*) FROM run WHERE status='running'").fetchone()[0]
    if n > 1:
        add("run_lock", f"running 상태의 실행이 {n}건입니다")

    pragma = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if pragma != "ok":
        add("sqlite", pragma)

    data_p, ops_p = split_problems(problems)
    return {"healthy": not problems, "problem_count": len(problems), "problems": problems,
            "storage_ok": not data_p, "operational_problems": ops_p,
            "counts": table_counts(conn), "claire_version": CLAIRE_VERSION}


def split_problems(problems: list) -> tuple[list, list]:
    """(장부·파일 문제, 운영 상태 문제). 이전 성공 판정과 기존 작업 상태(예: outbox 실패 1건)를 구분한다."""
    data_p = [p for p in problems if p["check"] not in OPERATIONAL_CHECKS]
    ops_p = [p for p in problems if p["check"] in OPERATIONAL_CHECKS]
    return data_p, ops_p


def problem_set(result: dict) -> list:
    """비교용: sync_stale 처럼 시각에 따라 바뀌는 항목은 뺀다."""
    return sorted(f"{p['check']}: {p['detail']}" for p in result["problems"] if p["check"] not in ("sync_stale",))


def attachment_file(root: Path, stored_path: str) -> Path:
    p = Path(stored_path)
    return p if p.is_absolute() else Path(root) / p


def _has_table(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


# --------------------------------------------------------------------------
# 장부 지문·변화
# --------------------------------------------------------------------------

def open_ro(db: Path) -> sqlite3.Connection:
    """읽기 전용으로 연다. WAL 에 미반영 기록이 있으면 mode=ro(WAL 을 읽는다 — 색인 파일 -shm 만 갱신될 수 있음),
    WAL 이 없거나 비었으면 immutable(어떤 파일도 건드리지 않음)."""
    db = Path(db)
    wal = db.with_name(db.name + "-wal")
    uri = f"file:{db.as_posix()}?" + ("mode=ro" if wal.exists() and wal.stat().st_size > 0 else "immutable=1")
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def wal_pending(db: Path) -> int:
    wal = Path(db).with_name(Path(db).name + "-wal")
    return wal.stat().st_size if wal.exists() else 0


def user_tables(conn) -> list[str]:
    """검색 색인(가상 표와 그 내부 표)을 뺀 실제 표 (이름순)."""
    rows = conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    virtual = [r[0] for r in rows if (r[1] or "").upper().startswith("CREATE VIRTUAL TABLE")]
    out = []
    for name, _sql in rows:
        if name.startswith("sqlite_") or name in virtual or any(name.startswith(v + "_") for v in virtual):
            continue
        out.append(name)
    return out


def _table_rows(conn, table: str):
    return conn.execute(f'SELECT rowid AS __rowid, * FROM "{table}" ORDER BY rowid')


def _row_hash(row) -> str:
    return hashlib.sha256(json.dumps(list(row)[1:], ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def data_fingerprint(conn) -> dict:
    """모든 표의 행 수와 내용 해시, 핵심 번호(업무·질문·메모 번호) 요약. 바이트가 아니라 내용 기준이라
    같은 장부의 백업 API 사본·파일 사본은 같은 값을 낸다."""
    tables = {}
    total = hashlib.sha256()
    for t in user_tables(conn):
        h = hashlib.sha256()
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')]
        h.update(("#" + ",".join(cols) + "\n").encode("utf-8"))
        n = 0
        for row in _table_rows(conn, t):
            h.update(json.dumps(list(row), ensure_ascii=False, default=str).encode("utf-8") + b"\n")
            n += 1
        tables[t] = {"rows": n, "sha256": h.hexdigest()}
        total.update(f"{t}:{n}:{tables[t]['sha256']}\n".encode("utf-8"))
    refs = {}
    for t in ("item", "question", "memo"):
        if t in tables:
            vals = sorted(r[0] for r in conn.execute(f"SELECT ref FROM {t}"))
            refs[t] = {"count": len(vals), "first": vals[0] if vals else None, "last": vals[-1] if vals else None,
                       "sha256": hashlib.sha256("\n".join(vals).encode("utf-8")).hexdigest()}
    sv = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() if "meta" in tables else None
    return {"sha256": total.hexdigest(), "tables": tables, "refs": refs,
            "schema_version": int(sv[0]) if sv else None}


def fingerprint_diff(a: dict, b: dict) -> list:
    out = []
    for t in sorted(set(a["tables"]) | set(b["tables"])):
        x, y = a["tables"].get(t), b["tables"].get(t)
        if x != y:
            out.append({"table": t, "before": x and x["rows"], "after": y and y["rows"],
                        "content_changed": bool(x and y and x["sha256"] != y["sha256"])})
    if a.get("refs") != b.get("refs"):
        out.append({"refs": "changed"})
    return out


def change_plan(old, new) -> dict:
    """옛 장부(old) 대비 새 장부(new)의 표별 추가·변경·사라진 행. 사라지면 안 되는 표의 손실은 blockers."""
    tables, blockers = {}, []
    old_t, new_t = set(user_tables(old)), set(user_tables(new))
    total_added = total_changed = 0
    for t in sorted(old_t | new_t):
        o = {r[0]: _row_hash(r) for r in _table_rows(old, t)} if t in old_t else {}
        n = {r[0]: _row_hash(r) for r in _table_rows(new, t)} if t in new_t else {}
        added = len(n.keys() - o.keys())
        missing = sorted(o.keys() - n.keys())
        changed = sum(1 for k in o.keys() & n.keys() if o[k] != n[k])
        if added or missing or changed:
            tables[t] = {"before": len(o), "after": len(n), "added": added, "changed": changed, "missing": len(missing)}
        total_added += added
        total_changed += changed
        if missing and t not in DELETABLE_TABLES:
            blockers.append({"code": "rows_missing", "table": t, "count": len(missing), "rowids": missing[:20],
                             "message": f"{t} 표에서 옛 장부의 행 {len(missing)}개가 새 장부에 없습니다 (지워지지 않는 표)"})
    return {"tables": tables, "added": total_added, "changed": total_changed, "superset": not blockers,
            "blockers": blockers}


def sqlite_snapshot(src_db: Path, dst_db: Path) -> None:
    """SQLite 온라인 백업 API: WAL 에만 있는 기록까지 포함한 일관된 사본."""
    dst_db = Path(dst_db)
    dst_db.parent.mkdir(parents=True, exist_ok=True)
    if dst_db.exists():
        dst_db.unlink()
    src = sqlite3.connect(f"file:{Path(src_db).as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(dst_db)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    os.chmod(dst_db, 0o600)


# --------------------------------------------------------------------------
# 파일
# --------------------------------------------------------------------------

def _skip_name(name: str) -> bool:
    return name == ".DS_Store" or name == ".incoming" or (name.startswith(".") and ".tmp-" in name)


def data_files(root: Path) -> dict:
    """복사 대상 파일 {상대경로: {"bytes", "mtime_ns"}} 와 건너뛴 맨 위 항목·심볼릭 링크.
    장부(백업 API 로 따로), 표지, secrets/(보호 복사), backups/·skill-prev/(옮기지 않음)는 뺀다."""
    root = Path(root)
    files, skipped, links = {}, [], []
    if not root.is_dir():
        return {"files": files, "skipped_top": skipped, "symlinks": links}
    for top in sorted(root.iterdir(), key=lambda p: p.name):
        if top.name in EXCLUDE_TOP or top.name.startswith(DB_NAME + ".") or _skip_name(top.name):
            if top.name not in (DB_NAME, f"{DB_NAME}-wal", f"{DB_NAME}-shm", claire_paths.MARKER_NAME, ".DS_Store"):
                skipped.append(top.name)
            continue
        if top.is_symlink():
            links.append(top.name)
            continue
        if top.is_file():
            st = top.stat()
            files[top.name] = {"bytes": st.st_size, "mtime_ns": st.st_mtime_ns}
            continue
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames[:] = sorted(d for d in dirnames if not _skip_name(d))
            for fn in sorted(filenames):
                if _skip_name(fn):
                    continue
                p = Path(dirpath) / fn
                rel = p.relative_to(root).as_posix()
                if p.is_symlink():
                    links.append(rel)
                    continue
                st = p.stat()
                files[rel] = {"bytes": st.st_size, "mtime_ns": st.st_mtime_ns}
    return {"files": files, "skipped_top": skipped, "symlinks": links}


def dir_usage(path: Path) -> dict:
    n = b = 0
    if Path(path).is_dir():
        for dirpath, _d, filenames in os.walk(path):
            for fn in filenames:
                try:
                    b += (Path(dirpath) / fn).lstat().st_size
                    n += 1
                except OSError:
                    pass
    return {"files": n, "bytes": b}


def copy_hashed(src: Path, dst: Path, mode: int | None = None) -> tuple[str, int]:
    """복사하면서 원본 내용의 sha256 을 구한다 (임시 파일 → 원자적 교체). mode 를 주면 그 권한으로."""
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.tmp-{os.getpid()}")
    h = hashlib.sha256()
    size = 0
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(tmp, flags, mode if mode is not None else 0o644)
    with open(src, "rb") as fi, os.fdopen(fd, "wb") as fo:
        for chunk in iter(lambda: fi.read(1 << 20), b""):
            h.update(chunk)
            size += len(chunk)
            fo.write(chunk)
    shutil.copystat(src, tmp)
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, dst)
    return h.hexdigest(), size


def write_sha256sums(root: Path, sums: dict) -> Path:
    p = Path(root) / "SHA256SUMS"
    atomic_write_text(p, "".join(f"{sums[k]}  {k}\n" for k in sorted(sums)))
    return p


def read_sha256sums(root: Path) -> dict:
    p = Path(root) / "SHA256SUMS"
    if not p.exists():
        return {}
    out = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        if "  " in line:
            h, rel = line.split("  ", 1)
            out[rel] = h
    return out


# --------------------------------------------------------------------------
# 가림(redaction) 목록 — 오래된 백업에서 지운 개인정보를 다시 들여오지 않는다
# --------------------------------------------------------------------------

def redaction_entries(conn) -> list:
    items = [{"kind": "source_event", "id": r[0], "redacted_at": r[1], "fingerprint": r[2]}
             for r in conn.execute("SELECT id, redacted_at, fingerprint FROM source_event WHERE redacted_at IS NOT NULL")]
    items += [{"kind": "attachment", "id": r[0], "redacted_at": r[1], "sha256": r[2]}
              for r in conn.execute("SELECT id, redacted_at, sha256 FROM attachment WHERE redacted_at IS NOT NULL")]
    return items


def _entry_key(e: dict) -> tuple:
    return (e.get("kind"), e.get("id"), e.get("fingerprint") or e.get("sha256"))


def read_redaction_files(*paths) -> list:
    out, seen = [], set()
    for p in paths:
        if not p:
            continue
        try:
            data = json.loads(Path(p).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for e in data.get("entries") or []:
            if _entry_key(e) not in seen:
                seen.add(_entry_key(e))
                out.append(e)
    return out


def merge_redaction_manifest(path: Path, entries: list, ts: str) -> int:
    """기존 목록과 합쳐 쓴다 (지우지 않는다). 옛 백업으로 장부를 되돌려도 가림 기록이 사라지지 않게."""
    merged = read_redaction_files(path)
    seen = {_entry_key(e) for e in merged}
    for e in entries:
        if _entry_key(e) not in seen:
            seen.add(_entry_key(e))
            merged.append(e)
    merged.sort(key=lambda e: (e.get("kind") or "", e.get("id") or 0))
    atomic_write_text(Path(path), json.dumps({"updated_at": ts, "entries": merged}, ensure_ascii=False, indent=2))
    return len(merged)


def pending_redactions(conn, entries: list) -> list:
    """이 장부에 아직 가려지지 않은 가림 대상 (fingerprint·sha256 이 같은 행만)."""
    out = []
    for e in entries:
        if e.get("kind") == "source_event":
            r = conn.execute("SELECT redacted_at, fingerprint FROM source_event WHERE id=?", (e.get("id"),)).fetchone()
            if r and r[0] is None and r[1] == e.get("fingerprint"):
                out.append(e)
        elif e.get("kind") == "attachment":
            r = conn.execute("SELECT redacted_at, sha256 FROM attachment WHERE id=?", (e.get("id"),)).fetchone()
            if r and r[0] is None and r[1] == e.get("sha256"):
                out.append(e)
    return out


def apply_redactions(conn, root: Path, entries: list) -> list:
    """복원 사본에 가림을 다시 적용한다 (claire_store redact 와 같은 결과: 발췌·헤더·원문·첨부 본문 제거, redacted_at 유지)."""
    applied = []
    for e in pending_redactions(conn, entries):
        if e["kind"] == "source_event":
            ev = conn.execute("SELECT raw_path FROM source_event WHERE id=?", (e["id"],)).fetchone()
            conn.execute("UPDATE source_event SET excerpt='[redacted]', headers=?, redacted_at=? WHERE id=?",
                         (json.dumps({"redacted": True}, ensure_ascii=False), e.get("redacted_at"), e["id"]))
            if ev and ev[0]:
                p = attachment_file(root, ev[0])
                if p.exists():
                    p.unlink()
            for a in conn.execute("SELECT id, stored_path FROM attachment WHERE source_event_id=? AND redacted_at IS NULL",
                                  (e["id"],)).fetchall():
                p = attachment_file(root, a[1])
                if p.exists():
                    p.unlink()
                conn.execute("UPDATE attachment SET redacted_at=? WHERE id=?", (e.get("redacted_at"), a[0]))
        else:
            a = conn.execute("SELECT stored_path FROM attachment WHERE id=?", (e["id"],)).fetchone()
            p = attachment_file(root, a[0])
            if p.exists():
                p.unlink()
            conn.execute("UPDATE attachment SET redacted_at=? WHERE id=?", (e.get("redacted_at"), e["id"]))
        applied.append({"kind": e["kind"], "id": e["id"]})
    return applied


# --------------------------------------------------------------------------
# 백업 폴더 분류 — Claire 가 만든 정기 백업만 보존 정책으로 정리한다
# --------------------------------------------------------------------------

def backup_manifest(d: Path) -> dict | None:
    try:
        return json.loads((Path(d) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def is_prunable_backup(d: Path) -> bool:
    """정기 백업(claire_check backup)만. 이전용 백업·업그레이드 전 사본·다른 시스템이 만든 사본(사전 백업 등)은 건드리지 않는다."""
    m = backup_manifest(d)
    if not m:
        return False
    kind = m.get("kind")
    if kind is None:                                 # 0.8.x 이전 정기 백업 manifest
        return "claire_version" in m and "content_hash" in m
    return kind == "claire-backup"
