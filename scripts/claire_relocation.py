"""데이터 위치 이전·되돌리기 엔진 (0.9.0, 이슈 2026-10-24 "SecretaryData 경로 지원 및 안전한 데이터 이전").

CLI 는 scripts/claire_relocate. 이 모듈은 판단하지 않고 정해진 절차만 밟는다.

이전(migrate) 단계 — 이전 기록(journal)에 단계마다 남기고, 중단되면 그 다음 단계부터 resume 하거나 abort 로 원래대로 돌린다.
  planned        잠금(relocation.lock)을 잡는다. 이때부터 Claire 도구는 원본·대상 장부를 열지 않는다.
  paused         OpenClaw 의 Claire 예약(claire-*)을 멈추거나(--pause-schedules), 곧 실행될 예약이 없는지 확인한다.
  quiesced       원본 장부 파일을 연 다른 프로세스(lsof)·진행 중 실행이 없는지 확인한다 (통제되지 않은 writer 면 중단).
  backed_up      최신 원본을 SQLite 백업 API 로 백업 루트 <id>/ 에 스냅숏(WAL 포함) + 파일 사본·SHA256SUMS·manifest.
  staged         대상 옆 임시 폴더(.<이름>.staging-<id>)에 장부·파일·인증정보(보호 복사)를 놓는다. 절대경로 참조만 고친다.
  verified       integrity_check·전체 지문(표별 행 수·내용 해시·번호)·파일 해시·첨부 해시·기존 문제 목록이 원본과 같은지.
  retired        원본이 백업 뒤 바뀌지 않았는지 다시 보고, WAL 을 합친 뒤 옛 장부를 claire.db.relocated-<id> 로 옮기고
                 그 자리에 은퇴 표지(텍스트)를 둔다 — 0.9.0 이전 코드·옛 경로 override 가 옛 장부를 열지 못한다. 지우지 않는다.
  switched       임시 폴더를 대상 이름으로 바꾼다 (빈 대상 폴더만 대체, 내용이 있으면 덮어쓰지 않는다).
  configured     위치 기록을 대상으로 바꾸고 ~/SecretaryData/registry.json 의 schedule 항목만 잠금·원자적으로 갱신한다.
  post_verified  환경변수 없이(예약 실행과 같은 조건) 위치·무결성을 다시 확인하고 전환 기준 지문을 남긴다.
  resumed        멈춘 예약만 다시 켠다.  done  잠금을 푼다.

실패: 전환 전이면 임시 폴더만 치우고 원본으로 계속 운영한다. 전환 중·직후(잠금 중, 새 기록 없음)면 위치 기록·대상·원본을
되돌린다. 되돌리기도 실패하면 잠금을 남겨 장부를 열지 않게 하고 사람이 abort 하게 한다.

되돌리기(rollback, 이전 완료 뒤):
  - 전환 뒤 새 기록이 없으면(revert): 새 위치를 은퇴시키고 옛 장부를 원래 이름으로 되돌린다. 옛 장부 지문을 먼저 확인한다.
  - 새 기록이 있으면(carry-forward): 옛 장부로 단순 복귀하지 않는다. 새 데이터를 먼저 백업하고, 옛 장부 대비 변화(추가·변경·
    사라진 행)를 검증해 사라지면 안 되는 행이 없을 때만, 현재 데이터 전체를 옛 경로로 옮긴다(같은 이전 절차, 반대 방향).
    옛 위치 폴더는 <옛 경로>.retired-<id> 로 비켜 두고 지우지 않는다.

테스트 전용: CLAIRE_RELOCATE_TEST_CRASH_AFTER=<단계> 면 그 단계를 기록한 직후 프로세스를 강제 종료한다(중단 후 재실행 시험).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from claire_core import (
    CLAIRE_VERSION, SCHEMA_VERSION, ClaireError, content_hash, is_synced_location, now_iso, sha256_file,
    table_counts,
)
import claire_data
import claire_paths
from claire_paths import DB_NAME, same_path

HERE = Path(__file__).resolve().parent
DATA_SUBDIRS = ("secrets", "raw", "attachments", "exports", "logs")
MIGRATE_PHASES = ["planned", "paused", "quiesced", "backed_up", "staged", "verified", "retired", "switched",
                  "configured", "post_verified", "resumed", "done"]
REVERT_PHASES = ["planned", "paused", "quiesced", "verified", "retired", "switched", "configured", "post_verified",
                 "resumed", "done"]
SPACE_MARGIN = 64 * 1024 * 1024
SECRETS_POLICY = ("인증정보(secrets/)는 백업에 넣지 않는다. 이전 때만 원본 → 새 위치로 보호 복사(폴더 700·파일 600, 해시 대조, "
                  "값·파일 이름 출력 없음)한다. 옛 위치의 사본은 자동으로 지우지 않는다(확인 후 직접 삭제 권장). "
                  "OAuth 클라이언트(config google.oauth_client_file)는 옮기지 않고 그 자리를 계속 참조한다.")


def _err(code: str, message: str, **extra) -> ClaireError:
    return ClaireError(code, message, **extra)


def _now() -> str:
    return claire_paths.now_utc()


# --------------------------------------------------------------------------
# 옵션·기록
# --------------------------------------------------------------------------

@dataclass
class Options:
    to: Path
    backup_root: Path
    registry: Path | None
    quiet_minutes: int = 20
    pause_schedules: bool = False
    accept_source_problems: bool = False
    replace_retired: bool = False
    kind: str = "migrate"                 # migrate | rollback-move
    parent: str | None = None             # rollback 의 원래 이전 id


def default_options(cfg, to=None, backup_root=None, registry=None, no_registry=False, **kw) -> Options:
    reg = None
    if not no_registry:
        reg = claire_paths.expand(registry) if registry else claire_paths.default_registry()
    return Options(to=Path(os.path.abspath(claire_paths.expand(to))) if to else claire_paths.default_dir(),
                   backup_root=claire_paths.backup_root(cfg.get("backup_root"), backup_root),
                   registry=reg, **kw)


class Journal:
    def __init__(self, data: dict):
        self.data = data

    @property
    def id(self) -> str:
        return self.data["id"]

    @property
    def path(self) -> Path:
        return claire_paths.journal_dir() / f"{self.id}.json"

    @property
    def files_path(self) -> Path:
        return claire_paths.journal_dir() / f"{self.id}.files.json"

    @classmethod
    def load(cls, rid: str) -> "Journal":
        try:
            data = claire_paths.read_json(claire_paths.journal_dir() / f"{rid}.json")
        except ValueError as e:
            raise _err("journal_unreadable", f"이전 기록을 읽지 못했습니다: {rid} ({e})")
        if not data:
            raise _err("journal_missing", f"이전 기록이 없습니다: {rid}")
        return cls(data)

    def save(self) -> None:
        claire_paths.journal_dir().mkdir(parents=True, exist_ok=True)
        claire_paths.write_json_atomic(self.path, self.data)

    def phases(self) -> list:
        return REVERT_PHASES if self.data["kind"] == "rollback-revert" else MIGRATE_PHASES

    def reached(self, phase: str) -> bool:
        ph = self.phases()
        return ph.index(self.data["phase"]) >= ph.index(phase)

    def mark(self, phase: str) -> None:
        self.data["phase"] = phase
        self.data.setdefault("history", []).append({"phase": phase, "at": _now()})
        self.save()

    def files(self) -> dict:
        try:
            return claire_paths.read_json(self.files_path) or {}
        except ValueError:
            return {}

    def save_files(self, section: str, value) -> None:
        d = self.files()
        d[section] = value
        claire_paths.write_json_atomic(self.files_path, d)


def list_journals() -> list[dict]:
    d = claire_paths.journal_dir()
    out = []
    if d.is_dir():
        for p in sorted(d.glob("*.json")):
            if p.name.endswith(".files.json"):
                continue
            try:
                j = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(j, dict) and j.get("id") and j.get("kind"):
                out.append(j)
    out.sort(key=lambda j: j.get("started_at") or "")
    return out


def _summary(j: dict) -> dict:
    return {"id": j.get("id"), "kind": j.get("kind"), "state": j.get("state"), "phase": j.get("phase"),
            "parent": j.get("parent"), "source": (j.get("source") or {}).get("root"),
            "target": (j.get("target") or {}).get("root"), "backup": j.get("backup_dir"),
            "started_at": j.get("started_at"), "finished_at": j.get("finished_at"),
            "error": (j.get("error") or {}).get("message"), "undo_error": j.get("undo_error"),
            "warnings": [w.get("message") for w in j.get("warnings") or []] or None}


def new_id(backup_root: Path) -> str:
    base = datetime.now().strftime("%Y%m%dT%H%M%S")
    rid, n = base, 1
    while (claire_paths.journal_dir() / f"{rid}.json").exists() or (Path(backup_root) / rid).exists():
        n += 1
        rid = f"{base}-{n}"
    return rid


def _maybe_crash(phase: str) -> None:
    if os.environ.get("CLAIRE_RELOCATE_TEST_CRASH_AFTER") == phase:
        os._exit(75)


# --------------------------------------------------------------------------
# 환경 점검: 공간·권한·쓰기 주체·예약
# --------------------------------------------------------------------------

def nearest_existing(p: Path) -> Path:
    p = Path(p)
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def disk_free(p: Path) -> int:
    return shutil.disk_usage(nearest_existing(p)).free


def _device(p: Path) -> int:
    return nearest_existing(p).stat().st_dev


def _writable(p: Path) -> bool:
    a = nearest_existing(p)
    return os.access(a, os.W_OK | os.X_OK)


def _proc_names(pids) -> dict:
    out = {}
    for pid in pids:
        try:
            r = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True, timeout=10)
            out[str(pid)] = r.stdout.strip() or "?"
        except (OSError, subprocess.SubprocessError):
            out[str(pid)] = "?"
    return out


def open_file_holders(paths) -> dict:
    """장부 파일(claire.db·-wal·-shm)을 열고 있는 다른 프로세스. lsof → /proc → 확인 불가."""
    files = [str(p) for p in paths if Path(p).exists()]
    if not files:
        return {"method": "none", "pids": [], "processes": {}}
    me = os.getpid()
    lsof = shutil.which("lsof") or ("/usr/sbin/lsof" if os.path.exists("/usr/sbin/lsof") else None)
    if lsof:
        try:
            r = subprocess.run([lsof, "-t", "--", *files], capture_output=True, text=True, timeout=120)
            if r.returncode in (0, 1):
                pids = sorted({int(x) for x in r.stdout.split() if x.isdigit()} - {me})
                return {"method": "lsof", "pids": pids, "processes": _proc_names(pids)}
        except (OSError, subprocess.SubprocessError):
            pass
    if Path("/proc/self/fd").is_dir():
        targets = {os.path.realpath(f) for f in files}
        pids = set()
        for d in Path("/proc").iterdir():
            if not d.name.isdigit() or int(d.name) == me:
                continue
            try:
                for fd in (d / "fd").iterdir():
                    try:
                        if os.path.realpath(os.readlink(fd)) in targets:
                            pids.add(int(d.name))
                            break
                    except OSError:
                        continue
            except OSError:
                continue
        return {"method": "proc", "pids": sorted(pids), "processes": _proc_names(sorted(pids))}
    return {"method": "unavailable", "pids": [], "processes": {}}


def db_files(root: Path) -> list[Path]:
    root = Path(root)
    return [root / DB_NAME, root / f"{DB_NAME}-wal", root / f"{DB_NAME}-shm"]


def running_runs(conn, cfg) -> dict:
    stale = int(cfg["run"]["stale_run_minutes"])
    now = datetime.now(timezone.utc)
    fresh, old = [], []
    for r in conn.execute("SELECT id, kind, started_at FROM run WHERE status='running'").fetchall():
        try:
            age = (now - datetime.fromisoformat(r[2])).total_seconds() / 60
        except (TypeError, ValueError):
            age = 0
        (old if age > stale else fresh).append({"run_id": r[0], "kind": r[1], "started_at": r[2], "age_min": int(age)})
    return {"fresh": fresh, "stale": old}


def list_claire_schedules(source: Path | None = None) -> dict:
    """OpenClaw 예약 중 Claire 일정·업무(claire-*)만. 다른 Claire 예약(skill-collection-review 등)은 건드리지 않는다."""
    oc = claire_paths.openclaw_bin()
    if not oc:
        return {"available": False, "openclaw": False, "jobs": [], "reason": "openclaw 없음 — 멈출 예약 없음"}
    try:
        r = subprocess.run([oc, "cron", "list", "--json", "--all", "--agent", "claire"],
                           capture_output=True, text=True, timeout=60)
        data = json.loads(r.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return {"available": False, "openclaw": True, "jobs": [], "reason": f"예약 목록을 읽지 못함: {e}"}
    if isinstance(data, dict) and data.get("hasMore"):
        return {"available": False, "openclaw": True, "jobs": [], "reason": "예약 목록이 한 번에 다 오지 않았습니다 (hasMore)"}
    jobs = data if isinstance(data, list) else data.get("jobs") or data.get("automations") or []
    out = []
    needles = _path_needles(source) if source else []
    for j in jobs:
        key = j.get("declarationKey") or ""
        if j.get("agentId") != "claire" or not (key.startswith("claire-") or (j.get("name") or "").startswith("Claire ")):
            continue
        nxt = j.get("nextRunAtMs") or (j.get("state") or {}).get("nextRunAtMs")
        text = json.dumps(j, ensure_ascii=False)
        out.append({"id": j.get("id"), "name": j.get("name"), "key": key, "enabled": j.get("enabled", True),
                    "next_run_ms": nxt,
                    "next_run_at": datetime.fromtimestamp(nxt / 1000, timezone.utc).isoformat() if nxt else None,
                    "mentions_old_path": any(n in text for n in needles)})
    return {"available": True, "openclaw": True, "jobs": out}


def _openclaw(*args) -> None:
    oc = claire_paths.openclaw_bin()
    if not oc:
        raise _err("openclaw_missing", "openclaw 가 없어 예약을 바꾸지 못했습니다")
    r = subprocess.run([oc, *args], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise _err("openclaw_failed", f"openclaw {' '.join(args)} 실패: {(r.stderr or r.stdout).strip()[:300]}")


def _due_soon(sched: dict, minutes: int) -> list:
    now_ms = time.time() * 1000
    return [j for j in sched["jobs"] if j["enabled"] and j["next_run_ms"]
            and 0 <= j["next_run_ms"] - now_ms <= minutes * 60_000]


# --------------------------------------------------------------------------
# 사용자 파일·문서 링크 (바꾸지 않고 목록만)
# --------------------------------------------------------------------------

def _path_needles(source: Path) -> list[str]:
    s = str(source)
    out = [s]
    if same_path(source, claire_paths.legacy_dir()):
        out += ["~/ClaireData", "$HOME/ClaireData", "${HOME}/ClaireData"]
    return out


def _scan_file(p: Path, needles: list[str]) -> list[int]:
    try:
        if p.stat().st_size > 2 * 1024 * 1024:
            return []
        text = p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    if not any(n in text for n in needles):
        return []
    return [i for i, line in enumerate(text.splitlines(), 1) if any(n in line for n in needles)][:20]


def scan_links(cfg, source: Path, schedules: dict | None = None, vault_limit: int = 20000) -> list[dict]:
    """옛 위치를 가리키는 사용자 파일·문서·예약 문구. 고치지 않고 위치(파일·줄 번호)만 보고한다."""
    needles = _path_needles(source)
    found = []
    h = claire_paths.home()
    for name in (".zshrc", ".zprofile", ".zshenv", ".bashrc", ".bash_profile", ".profile"):
        lines = _scan_file(h / name, needles + ["CLAIRE_DATA_DIR"])
        if lines:
            found.append({"where": "shell", "file": str(h / name), "lines": lines,
                          "action": "CLAIRE_DATA_DIR·옛 경로를 지우거나 새 경로로 (지우면 위치 기록을 따른다)"})
    la = h / "Library" / "LaunchAgents"
    if la.is_dir():
        for p in sorted(la.glob("*.plist")):
            lines = _scan_file(p, needles)
            if lines:
                found.append({"where": "launchd", "file": str(p), "lines": lines, "action": "경로 확인"})
    oc_cfg = h / ".openclaw" / "openclaw.json"          # 값은 읽어 보고하지 않는다 — 줄 번호만
    lines = _scan_file(oc_cfg, needles + ["CLAIRE_DATA_DIR"])
    if lines:
        found.append({"where": "openclaw_config", "file": str(oc_cfg), "lines": lines,
                      "action": "Claire 에이전트 환경의 CLAIRE_DATA_DIR·옛 경로 확인 (있으면 지우거나 새 경로로)"})
    ws = Path(os.environ.get("CLAIRE_OPENCLAW_WORKSPACE") or h / ".openclaw" / "workspace-claire")
    if ws.is_dir():
        for p in sorted(ws.rglob("*.md")):
            lines = _scan_file(p, needles)
            if lines:
                found.append({"where": "openclaw_workspace", "file": str(p), "lines": lines, "action": "문구 확인"})
    for j in (schedules or {}).get("jobs", []):
        if j.get("mentions_old_path"):
            found.append({"where": "openclaw_cron", "file": f"cron {j['id']} ({j['name']})", "lines": [],
                          "action": "openclaw cron edit 로 메시지의 옛 경로를 고치세요"})
    vault = (cfg.get("obsidian") or {}).get("vault")
    if vault:
        root = claire_paths.expand(vault)
        n = 0
        if root.is_dir():
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in (".obsidian", ".trash", ".git")]
                for fn in filenames:
                    if not fn.endswith(".md"):
                        continue
                    n += 1
                    if n > vault_limit:
                        break
                    p = Path(dirpath) / fn
                    lines = _scan_file(p, needles)
                    if lines:
                        found.append({"where": "obsidian", "file": str(p.relative_to(root)), "lines": lines,
                                      "action": "노트 안 링크 확인 (자동으로 고치지 않음)"})
    return found


def historical_refs(conn, source: Path) -> dict:
    """장부 안 과거 기록(실행 단계·이력·반영 결과)에 남은 옛 경로. 역사 기록이므로 바꾸지 않는다."""
    s = f"%{source}%"
    out = {}
    for table, col in (("run", "steps"), ("run", "summary"), ("activity_log", "payload"), ("outbox", "payload"),
                       ("outbox", "result"), ("delivery", "payload")):
        try:
            n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} LIKE ?", (s,)).fetchone()[0]
        except sqlite3.Error:
            continue
        if n:
            out[f"{table}.{col}"] = n
    return out


# --------------------------------------------------------------------------
# 절대 경로 참조 (현재 파일 참조만 고친다)
# --------------------------------------------------------------------------

PATH_COLUMNS = (("attachment", "stored_path"), ("source_event", "raw_path"))


def _inside(p: Path, root: Path) -> str | None:
    rp, rr = os.path.realpath(p), os.path.realpath(root)
    if rp == rr or not rp.startswith(rr + os.sep):
        return None
    return os.path.relpath(rp, rr)


def absolute_path_report(conn, source: Path) -> dict:
    out = {}
    for table, col in PATH_COLUMNS:
        rows = conn.execute(f"SELECT id, {col} FROM {table} WHERE {col} LIKE '/%' OR {col} LIKE '~%'").fetchall()
        inside = sum(1 for r in rows if _inside(claire_paths.expand(r[1]), source))
        total = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} IS NOT NULL").fetchone()[0]
        out[f"{table}.{col}"] = {"rows": total, "absolute": len(rows), "inside_source": inside,
                                 "outside_source": len(rows) - inside}
    return out


def fix_db_paths(conn, source: Path) -> list[dict]:
    """데이터 폴더 안을 가리키는 절대 경로만 상대 경로로. 폴더 밖 절대 경로는 그대로 두고 보고한다."""
    out = []
    for table, col in PATH_COLUMNS:
        for rid, val in conn.execute(f"SELECT id, {col} FROM {table} WHERE {col} LIKE '/%' OR {col} LIKE '~%'").fetchall():
            rel = _inside(claire_paths.expand(val), source)
            if rel:
                conn.execute(f"UPDATE {table} SET {col}=? WHERE id=?", (rel, rid))
                out.append({"table": table, "id": rid, "from": val, "to": rel})
            else:
                out.append({"table": table, "id": rid, "from": val, "kept": True, "reason": "데이터 폴더 밖 절대 경로"})
    return out


def config_path_refs(cfg_obj, source: Path, target: Path | None = None, prefix="") -> list[dict]:
    """config.json 값 중 데이터 폴더 안을 가리키는 경로 (target 을 주면 그 값을 새 위치로 바꾼다)."""
    out = []
    items = cfg_obj.items() if isinstance(cfg_obj, dict) else enumerate(cfg_obj) if isinstance(cfg_obj, list) else []
    for k, v in list(items):
        key = f"{prefix}{k}"
        if isinstance(v, (dict, list)):
            out += config_path_refs(v, source, target, key + ".")
        elif isinstance(v, str) and (v.startswith("/") or v.startswith("~")):
            rel = _inside(claire_paths.expand(v), source)
            if rel:
                new = None
                if target:
                    new = str(Path(target) / rel)
                    h = str(claire_paths.home())
                    if v.startswith("~") and (new + os.sep).startswith(h + os.sep):
                        new = "~" + new[len(h):]          # ~ 표기를 유지한다 (사용자별 홈)
                out.append({"key": key, "from": v, "to": new})
                if target:
                    cfg_obj[k] = new
    return out


# --------------------------------------------------------------------------
# 계획 (dry-run) — 아무것도 바꾸지 않는다
# --------------------------------------------------------------------------

def classify_target(t: Path) -> str:
    t = Path(t)
    if not t.exists():
        return "missing"
    if not t.is_dir():
        return "file"
    names = [x.name for x in t.iterdir() if x.name != ".DS_Store"]
    if not names:
        return "empty"
    m = claire_paths.read_marker(t)
    if m and m.get("state") == "retired":
        return "retired"
    return "not_empty"


def _staging_path(target: Path, rid: str) -> Path:
    return Path(target).parent / f".{Path(target).name}.staging-{rid}"


def build_plan(cfg, opts: Options) -> dict:
    blockers, warnings = [], []

    def block(code, message, **extra):
        blockers.append({"code": code, "message": message, **extra})

    def warn(code, message, **extra):
        warnings.append({"code": code, "message": message, **extra})

    canon = claire_paths.resolve(use_env=False)
    if os.environ.get("CLAIRE_DATA_DIR") and not same_path(os.environ["CLAIRE_DATA_DIR"], canon.path):
        block("env_override", f"CLAIRE_DATA_DIR({os.environ['CLAIRE_DATA_DIR']})가 정본({canon.path})과 다릅니다. "
                              "이전은 예약 실행이 쓰는 정본에서만 합니다 (시험·사전 사본은 원본으로 쓰지 않는다)",
              fix="unset CLAIRE_DATA_DIR")
    for b in canon.blockers:
        blockers.append(b)
    source, target = canon.path, Path(opts.to)
    src_db = source / DB_NAME
    if not claire_paths.has_db(source):
        block("source_missing", f"원본 장부가 없습니다: {src_db}")
    if same_path(source, target):
        block("same_location", f"이미 그 위치를 쓰고 있습니다: {target}")
    elif _inside(target, source) or _inside(source, target):
        block("nested", "원본과 대상이 서로 안에 있습니다")
    tstate = classify_target(target)
    if tstate == "not_empty":
        names = sorted(x.name for x in target.iterdir() if x.name != ".DS_Store")[:10]
        block("target_not_empty", f"대상 폴더가 비어 있지 않습니다: {target} ({', '.join(names)}). 덮어쓰지 않습니다")
    elif tstate == "file":
        block("target_not_dir", f"대상이 폴더가 아닙니다: {target}")
    elif tstate == "retired" and not opts.replace_retired:
        m = claire_paths.read_marker(target) or {}
        block("target_retired", f"대상은 은퇴한 Claire 데이터 위치입니다(→ {m.get('moved_to')}). "
                                "되돌리기는 claire_relocate rollback, 그 자리를 비켜 두고 쓰려면 --replace-retired")
    if is_synced_location(target):
        block("target_synced", f"대상이 {is_synced_location(target)} 동기화 폴더 안입니다 (§4.2)")
    for t in (opts.backup_root,):
        if is_synced_location(t):
            warn("backup_root_synced", f"백업 루트가 {is_synced_location(t)} 동기화 폴더 안입니다. 미러는 backup_mirror 로 따로 둡니다")
    if _inside(opts.backup_root, source) or same_path(opts.backup_root, source):
        block("backup_inside_source", "백업 루트가 원본 데이터 폴더 안입니다")

    src = {"path": str(source), "how_resolved": claire_paths.SOURCE_LABELS.get(canon.source),
           "root_id": (canon.marker or {}).get("root_id")}
    files_info = {"files": {}, "skipped_top": [], "symlinks": []}
    abs_paths, hist, runs, integ = {}, {}, {"fresh": [], "stale": []}, None
    db_bytes = 0
    if claire_paths.has_db(source):
        db_bytes = src_db.stat().st_size
        wal = claire_data.wal_pending(src_db)
        conn = claire_data.open_ro(src_db)
        try:
            fp = claire_data.data_fingerprint(conn)
            integ = claire_data.run_integrity(conn, cfg, root=source)
            abs_paths = absolute_path_report(conn, source)
            hist = historical_refs(conn, source)
            runs = running_runs(conn, cfg)
            failed_outbox = conn.execute("SELECT COUNT(*) FROM outbox WHERE status='failed'").fetchone()[0]
        finally:
            conn.close()
        data_p, ops_p = claire_data.split_problems(integ["problems"])
        src.update({"db_bytes": db_bytes, "wal_bytes": wal,
                    "wal_note": "WAL 에 장부 파일에 아직 합쳐지지 않은 기록이 있음 — 백업 API 스냅숏이 함께 가져간다" if wal else None,
                    "schema_version": fp["schema_version"], "rows": {t: v["rows"] for t, v in fp["tables"].items()},
                    "refs": fp["refs"], "fingerprint": fp["sha256"],
                    "integrity": {"sqlite": "ok" if not any(p["check"] == "sqlite" for p in data_p) else "문제",
                                  "storage_problems": data_p, "operational_state": ops_p},
                    "failed_outbox": failed_outbox})
        if data_p:
            msg = f"원본에 장부·첨부 문제 {len(data_p)}건: " + "; ".join(p["detail"] for p in data_p[:5])
            if opts.accept_source_problems:
                warn("source_problems_accepted", msg + " (--accept-source-problems: 그대로 옮기고 기록)")
            else:
                block("source_problems", msg, fix="원인을 확인하세요. 알고 옮기려면 --accept-source-problems")
        if ops_p:
            warn("operational_state", "기존 작업 상태(이전과 무관, 그대로 옮김): " + "; ".join(p["detail"] for p in ops_p)
                 + " — 이전·검증 중에는 outbox 를 실행하지 않는다(재발송 없음)")
        if runs["fresh"]:
            block("run_in_progress", f"진행 중인 실행이 있습니다: {runs['fresh']}", fix="끝난 뒤 다시")
        if runs["stale"]:
            warn("stale_run", f"오래된 running 실행 {len(runs['stale'])}건 (다음 claire_run begin 이 aborted 로 정리)")
        files_info = claire_data.data_files(source)
    f_bytes = sum(v["bytes"] for v in files_info["files"].values())
    groups = {}
    for rel, v in files_info["files"].items():
        top = rel.split("/", 1)[0]
        g = groups.setdefault(top, {"path": top, "files": 0, "bytes": 0})
        g["files"] += 1
        g["bytes"] += v["bytes"]
    sec = claire_data.dir_usage(source / "secrets")
    excluded = [{"path": n, **claire_data.dir_usage(source / n), "why": claire_data.EXCLUDE_TOP_WHY.get(n, "옮기지 않음")}
                for n in files_info["skipped_top"] if n != "secrets"]
    if files_info["symlinks"]:
        warn("symlinks", f"심볼릭 링크는 옮기지 않습니다: {files_info['symlinks'][:10]}")

    # 공간·권한
    need_target = db_bytes + claire_data.wal_pending(src_db) + f_bytes + sec["bytes"]
    need_backup = db_bytes + claire_data.wal_pending(src_db) + f_bytes
    same_dev = _device(target.parent) == _device(opts.backup_root)
    space = {"target_needs": need_target, "backup_needs": need_backup, "same_device": same_dev,
             "target_free": disk_free(target.parent), "backup_free": disk_free(opts.backup_root)}
    if same_dev:
        need = int((need_target + need_backup) * 1.1) + SPACE_MARGIN
        space["ok"] = space["target_free"] >= need
    else:
        space["ok"] = (space["target_free"] >= int(need_target * 1.1) + SPACE_MARGIN and
                       space["backup_free"] >= int(need_backup * 1.1) + SPACE_MARGIN)
    if not space["ok"]:
        block("no_space", f"공간 부족: 필요 약 {_mb(need_target + need_backup)}MB, 남은 공간 {_mb(space['target_free'])}MB")
    perms = {"target_parent": str(nearest_existing(target.parent)), "target_parent_writable": _writable(target.parent),
             "backup_root": str(nearest_existing(opts.backup_root)), "backup_root_writable": _writable(opts.backup_root),
             "app_home": str(claire_paths.app_home()), "app_home_writable": _writable(claire_paths.app_home()),
             "source_readable": os.access(source, os.R_OK | os.X_OK) if source.exists() else False,
             "source_writable": os.access(source, os.W_OK) if source.exists() else False,
             "secrets_readable": (os.access(source / "secrets", os.R_OK | os.X_OK) if (source / "secrets").exists() else None)}
    for k, code in (("target_parent_writable", "target_not_writable"), ("backup_root_writable", "backup_not_writable"),
                    ("app_home_writable", "app_home_not_writable"), ("source_readable", "source_not_readable"),
                    ("source_writable", "source_not_writable")):
        if not perms[k]:
            block(code, f"권한 부족: {k} ({perms.get(k.replace('_writable', '').replace('_readable', ''), '')})")
    if perms["secrets_readable"] is False:
        block("secrets_not_readable", "secrets/ 를 읽을 수 없습니다")

    # 쓰기 주체·예약
    holders = open_file_holders(db_files(source))
    if holders["method"] == "unavailable":
        block("writer_check_unavailable", "장부 파일을 연 프로세스를 확인할 수 없습니다 (lsof·/proc 없음)")
    elif holders["pids"]:
        block("uncontrolled_writer", f"다른 프로세스가 장부를 열고 있습니다: {holders['processes']}",
              fix="그 프로세스를 끝낸 뒤 다시 (Claire 도구·sqlite3 셸·DB 뷰어 등)")
    sched = list_claire_schedules(source)
    if sched["openclaw"] and not sched["available"]:
        block("schedules_unknown", f"OpenClaw 예약을 확인하지 못했습니다: {sched.get('reason')}")
    due = _due_soon(sched, opts.quiet_minutes) if sched["available"] else []
    if due and not opts.pause_schedules:
        block("schedule_due_soon", f"{opts.quiet_minutes}분 안에 Claire 예약이 실행됩니다: {[j['name'] for j in due]}",
              fix="--pause-schedules 로 이전 동안 멈추거나 나중에 다시")
    registry = _registry_preview(opts.registry)
    cfg_refs = []
    try:
        cfg_refs = config_path_refs(json.loads((source / "config.json").read_text(encoding="utf-8")), source)
    except (OSError, ValueError):
        pass
    links = scan_links(cfg, source, sched)
    return {
        "dry_run": True, "changes_made": False, "kind": opts.kind,
        "source": src,
        "target": {"path": str(target), "state": tstate,
                   "staging": str(_staging_path(target, "<id>")),
                   "after": "대상 폴더 이름으로 바꾼다 (빈 폴더만 대체)" if tstate in ("missing", "empty") else
                            f"기존 폴더는 {target}.retired-<id> 로 비켜 둔다" if tstate == "retired" else "불가"},
        "inputs": {"database": {"path": str(src_db), "bytes": db_bytes, "method": "SQLite 온라인 백업 API (WAL 포함)"},
                   "copy": sorted(groups.values(), key=lambda g: g["path"]),
                   "copy_total": {"files": len(files_info["files"]), "bytes": f_bytes},
                   "secrets": {"files": sec["files"], "handling": SECRETS_POLICY},
                   "excluded": excluded,
                   "kept_in_place": [{"what": "OAuth 클라이언트", "config": "google.oauth_client_file",
                                      "path": str(claire_paths.expand(cfg["google"]["oauth_client_file"]))},
                                     {"what": "Obsidian 볼트(정본 노트)", "config": "obsidian.vault"},
                                     {"what": "OpenClaw 내부 저장소·Discord 첨부 수신 폴더", "config": "discord.inbound_media_dir"},
                                     {"what": "백업 미러", "config": "backup_mirror", "path": cfg.get("backup_mirror")}]},
        "writers": {"claire_tools": "이전 잠금 동안 모든 Claire 도구(OpenClaw 에이전트 턴·cron·수동 실행 포함)가 원본·대상 장부를 열지 않는다",
                    "open_handles": holders, "running_runs": runs,
                    "schedules": sched, "schedules_due_soon": [j["name"] for j in due],
                    "schedule_action": ("이전 동안 claire-* 예약을 끄고(openclaw cron disable) 끝나면 그 예약만 다시 켠다"
                                        if opts.pause_schedules else
                                        f"예약은 그대로 둔다 ({opts.quiet_minutes}분 안 실행 없음 확인). 이전 중 실행되면 Claire 가 '이전 중'으로 멈춘다")},
        "stop_scope": ["Claire 일정·업무 장부 쓰기(모든 Claire 도구) — 이전 잠금 동안",
                       "OpenClaw Claire 예약 claire-daily-check·claire-checkin·claire-missed-check" + (" (일시 정지)" if opts.pause_schedules else " (곧 실행될 것 없음 확인만)"),
                       "그 밖의 OpenClaw·다른 비서 시스템은 멈추지 않는다"],
        "backup": {"root": str(opts.backup_root), "dir": str(Path(opts.backup_root) / "<id>"),
                   "contents": "claire.db(백업 API) · 첨부·원문·기타 파일 · config.json · SHA256SUMS · manifest.json · redaction_manifest.json",
                   "excludes": ["secrets/", "backups/", "skill-prev/"],
                   "restore": "claire_check restore --from <backup>/<id> --to <빈 폴더>  (격리 복원·가림 재적용)"},
        "recovery": {"before_switch": "실패하면 임시 폴더만 치우고 원본으로 계속 (자동)",
                     "interrupted": "claire_relocate status → resume --id <id> 또는 abort --id <id>",
                     "after_switch_no_writes": "claire_relocate rollback --id <id> --confirm (옛 장부로 되돌림, 지문 확인)",
                     "after_switch_with_writes": "claire_relocate rollback --id <id> (변화 검증) → --confirm: 새 데이터를 먼저 백업하고 전체를 옛 경로로 옮김",
                     "code_downgrade": "0.9.0 이전 코드는 옛 경로의 은퇴 표지에서 멈춘다. 코드를 내리기 전에 rollback 으로 데이터 위치를 먼저 되돌린다"},
        "space": space, "permissions": perms,
        "absolute_paths": abs_paths, "config_path_refs": cfg_refs,
        "historical_refs": {"count": hist, "note": "과거 감사 기록·출처·보존본의 경로는 바꾸지 않는다"},
        "links_to_review": links,
        "registry": registry,
        "blockers": blockers, "warnings": warnings, "ready": not blockers,
        "next": ("claire_relocate migrate --confirm" + (" --pause-schedules" if opts.pause_schedules else "")
                 + (f" --to {target}" if not same_path(target, claire_paths.default_dir()) else "")) if not blockers else None,
    }


def _mb(n: int) -> int:
    return int(round(n / (1024 * 1024)))


# --------------------------------------------------------------------------
# registry.json (여러 시스템이 같이 쓴다 — schedule 항목만, 잠금·원자적 갱신)
# --------------------------------------------------------------------------

def _registry_preview(path: Path | None) -> dict:
    if not path:
        return {"path": None, "action": "갱신하지 않음 (--no-registry) — 결과를 handoff 파일로 남김"}
    p = Path(path)
    if not p.exists():
        return {"path": str(p), "exists": False, "action": "파일 없음 — 만들지 않고 handoff 파일로 남김"}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"path": str(p), "exists": True, "readable": False, "error": str(e)}
    return {"path": str(p), "exists": True, "writable": os.access(p, os.W_OK),
            "current": {"operational_root": (d.get("operational_roots") or {}).get(claire_paths.SYSTEM),
                        "planned_root": (d.get("planned_roots") or {}).get(claire_paths.SYSTEM),
                        "cutover": (d.get("cutovers") or {}).get(claire_paths.SYSTEM)},
            "action": "operational_roots.schedule 와 cutovers.schedule 만 바꾼다 (다른 시스템 항목은 그대로)"}


def update_registry(path: Path, operational_root: str | None, cutover: dict | None, timeout: float = 15.0) -> dict:
    p = Path(path)
    if not p.exists():
        return {"updated": False, "path": str(p), "reason": "registry 없음"}
    lockf = p.with_name(f".{p.name}.lock")
    import fcntl
    with open(lockf, "a+") as lf:
        deadline = time.time() + timeout
        while True:
            try:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() > deadline:
                    raise _err("registry_busy", f"registry 잠금을 얻지 못했습니다: {p}")
                time.sleep(0.2)
        for _attempt in range(5):
            raw = p.read_bytes()
            data = json.loads(raw)
            sysname = claire_paths.SYSTEM
            before = {"operational_root": (data.get("operational_roots") or {}).get(sysname),
                      "cutover": (data.get("cutovers") or {}).get(sysname)}
            for key, value in (("operational_roots", operational_root), ("cutovers", cutover)):
                if value is None:
                    if isinstance(data.get(key), dict):
                        data[key].pop(sysname, None)
                else:
                    data.setdefault(key, {})[sysname] = value
            mode = stat.S_IMODE(p.stat().st_mode)
            tmp = p.with_name(f".{p.name}.tmp-{os.getpid()}")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            if p.read_bytes() != raw:            # 그 사이 다른 시스템이 고쳤다 → 다시 읽어서
                os.unlink(tmp)
                continue
            os.replace(tmp, p)
            check = json.loads(p.read_text(encoding="utf-8"))
            ok = (check.get("operational_roots") or {}).get(sysname) == operational_root
            return {"updated": ok, "path": str(p), "before": before,
                    "after": {"operational_root": operational_root, "cutover": cutover}}
        raise _err("registry_busy", f"registry 가 계속 바뀌어 갱신하지 못했습니다: {p}")


def _registry_apply(c, operational_root: str, cutover: dict) -> dict:
    j = c.j
    handoff = {"system": claire_paths.SYSTEM, "operational_root": operational_root, "cutover": cutover,
               "note": "registry.json 을 갱신하지 못했거나 하지 않았습니다. 이 값으로 schedule 항목만 고쳐 주세요 (Chris)."}
    if c.opts.registry:
        try:
            res = update_registry(c.opts.registry, operational_root, cutover)
            if res.get("updated"):
                return res
        except (ClaireError, OSError, ValueError) as e:
            res = {"updated": False, "path": str(c.opts.registry), "error": str(getattr(e, "message", e))}
    else:
        res = {"updated": False, "path": None, "reason": "--no-registry"}
    bdir = j.data.get("backup_dir")
    hp = Path(bdir) / "registry-handoff.json" if bdir else claire_paths.journal_dir() / f"{j.id}.registry-handoff.json"
    claire_paths.write_json_atomic(hp, handoff)
    res["handoff"] = str(hp)
    return res


# --------------------------------------------------------------------------
# 은퇴·복귀 (옛 장부는 지우지 않고 이름만 바꾼다)
# --------------------------------------------------------------------------

def retire_root(root: Path, tag: str, moved_to: str, rid: str) -> dict:
    root = Path(root)
    db, kept = root / DB_NAME, root / f"{DB_NAME}.{tag}"
    marker = claire_paths.read_marker(root) or {"format": 1, "system": claire_paths.SYSTEM,
                                                "root_id": claire_paths.new_root_id(), "created_at": _now()}
    if not kept.exists():
        if not db.exists() or claire_paths.is_tombstone(db):
            raise _err("retire_no_db", f"은퇴시킬 장부가 없습니다: {db}")
        conn = sqlite3.connect(db, timeout=10)
        try:
            busy = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            conn.close()
        if busy and busy[0]:
            raise _err("checkpoint_busy", "WAL 을 장부에 합치지 못했습니다 (다른 연결이 있음)")
        wal = root / f"{DB_NAME}-wal"
        if wal.exists() and wal.stat().st_size > 0:
            raise _err("wal_not_empty", "WAL 이 비지 않았습니다 — 은퇴시키지 않습니다")
        marker.update(state="retiring", relocation_id=rid, moved_to=moved_to)
        claire_paths.write_marker(root, marker)
        os.rename(db, kept)
        for suf in ("-wal", "-shm"):
            side = root / f"{DB_NAME}{suf}"
            if side.exists():
                os.rename(side, root / f"{DB_NAME}.{tag}{suf}")
    if db.exists() and not claire_paths.is_tombstone(db):
        raise _err("unexpected_db", f"은퇴 중인 위치에 새 장부가 생겼습니다: {db} — 손대지 않고 멈춥니다")
    tmp = root / f".{DB_NAME}.tomb-{os.getpid()}"
    tmp.write_text(claire_paths.tombstone_text(moved_to, rid, _now()), encoding="utf-8")
    os.chmod(tmp, 0o444)
    os.replace(tmp, db)
    marker.update(state="retired", relocation_id=rid, moved_to=moved_to, retired_at=_now(), retired_db=kept.name)
    claire_paths.write_marker(root, marker)
    return {"root": str(root), "kept_db": str(kept), "tombstone": str(db)}


def unretire_root(root: Path, tag: str) -> dict:
    root = Path(root)
    db, kept = root / DB_NAME, root / f"{DB_NAME}.{tag}"
    marker = claire_paths.read_marker(root) or {}
    if not kept.exists():
        if claire_paths.has_db(root):
            if marker.get("state") != "active":
                marker["state"] = "active"
                claire_paths.write_marker(root, marker)
            return {"root": str(root), "already": True}
        raise _err("retired_db_missing", f"되돌릴 옛 장부가 없습니다: {kept}")
    if db.exists():
        if not claire_paths.is_tombstone(db):
            raise _err("unexpected_db", f"{db} 가 은퇴 표지가 아닙니다 — 덮어쓰지 않습니다")
        os.chmod(db, 0o644)
        os.unlink(db)
    os.rename(kept, db)
    for suf in ("-wal", "-shm"):
        side = root / f"{DB_NAME}.{tag}{suf}"
        if side.exists():
            os.rename(side, root / f"{DB_NAME}{suf}")
    hist = list(marker.get("history") or [])
    hist.append({k: marker.get(k) for k in ("relocation_id", "moved_to", "retired_at")} | {"restored_at": _now()})
    for k in ("moved_to", "retired_at", "retired_db"):
        marker.pop(k, None)
    marker.update(state="active", history=hist[-10:])
    claire_paths.write_marker(root, marker)
    return {"root": str(root), "restored_db": str(db)}


def _secret_error(e: OSError) -> ClaireError:
    """인증정보 파일 오류 — 경로·파일 이름을 메시지에 넣지 않는다(이름이 계정 주소일 수 있다)."""
    return _err("secrets_copy_failed", f"인증정보 복사 실패 ({type(e).__name__}, errno {getattr(e, 'errno', None)})")


def _secret_files(root: Path) -> list[Path]:
    def onerror(e):
        raise _secret_error(e)
    out = []
    for dirpath, _dn, filenames in os.walk(root, onerror=onerror):
        out += [Path(dirpath, fn).relative_to(root) for fn in filenames if fn != ".DS_Store"]
    return sorted(out)


def copy_secrets(src: Path, dst: Path, keep_suffix: str | None = None) -> int:
    """인증정보 보호 복사: 폴더 700·파일 600, 내용 해시·개수 대조. 값·이름을 출력하지 않는다(오류 문구에도).
    keep_suffix 를 주면 대상에 다른 내용의 같은 파일이 있을 때 <이름><suffix> 로 남기고 바꾼다(덮어쓰지 않는다)."""
    src, dst = Path(src), Path(dst)
    try:
        dst.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(dst, 0o700)
        if not src.is_dir():
            return 0
        rels = _secret_files(src)
        n = 0
        for rel in rels:
            s_, d = src / rel, dst / rel
            d.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(d.parent, 0o700)
            want = sha256_file(s_)
            if d.exists():
                if sha256_file(d) == want:
                    os.chmod(d, 0o600)
                    n += 1
                    continue
                if not keep_suffix:
                    raise _err("secrets_conflict", "대상 secrets/ 에 내용이 다른 같은 이름의 파일이 있습니다")
                kept = d.with_name(d.name + keep_suffix)
                os.rename(d, kept)
                os.chmod(kept, 0o600)
            got, _ = claire_data.copy_hashed(s_, d, mode=0o600)
            if got != want or sha256_file(d) != want:
                raise _err("secrets_copy_mismatch", "인증정보 사본의 해시가 맞지 않습니다")
            n += 1
        if n != len(rels):
            raise _err("secrets_count_mismatch", f"인증정보 {len(rels)}개 중 {n}개만 복사했습니다")
        return n
    except ClaireError:
        raise
    except OSError as e:
        raise _secret_error(e) from None


def _secrets_ok(d: Path) -> bool:
    d = Path(d)
    if not d.is_dir() or stat.S_IMODE(d.stat().st_mode) != 0o700:
        return False
    for dirpath, _dn, filenames in os.walk(d):
        for fn in filenames:
            if stat.S_IMODE((Path(dirpath) / fn).stat().st_mode) & 0o077:
                return False
    return True


# --------------------------------------------------------------------------
# 실행
# --------------------------------------------------------------------------

class Ctx:
    def __init__(self, cfg, j: Journal, lock: claire_paths.RelocationLock, opts: Options):
        self.cfg, self.j, self.lock, self.opts = cfg, j, lock, opts
        d = j.data
        self.rid = d["id"]
        self.source = Path(d["source"]["root"])
        self.target = Path(d["target"]["root"])
        self.staging = Path(d["staging"]) if d.get("staging") else None
        self.backup_dir = Path(d["backup_dir"]) if d.get("backup_dir") else None


def _opts_from(j: dict) -> Options:
    o = j.get("options") or {}
    return Options(to=Path(j["target"]["root"]), backup_root=Path(j["backup_root"]),
                   registry=Path(j["registry"]) if j.get("registry") else None,
                   quiet_minutes=o.get("quiet_minutes", 20), pause_schedules=o.get("pause_schedules", False),
                   accept_source_problems=o.get("accept_source_problems", False),
                   replace_retired=o.get("replace_retired", False), kind=j["kind"], parent=j.get("parent"))


def _lock_info(j: Journal) -> dict:
    d = j.data
    return {"id": d["id"], "kind": d["kind"], "source": d["source"]["root"], "target": d["target"]["root"],
            "staging": d.get("staging"), "journal": str(j.path), "started_at": d["started_at"]}


def migrate(cfg, opts: Options) -> dict:
    plan = build_plan(cfg, opts)
    if plan["blockers"]:
        raise _err("relocation_blocked", "이전할 수 없습니다: " + "; ".join(b["message"] for b in plan["blockers"][:4]),
                   blockers=plan["blockers"], warnings=plan["warnings"])
    rid = new_id(opts.backup_root)
    source, target = Path(plan["source"]["path"]), Path(opts.to)
    j = Journal({"format": 1, "id": rid, "kind": opts.kind, "parent": opts.parent, "system": claire_paths.SYSTEM,
                 "claire_version": CLAIRE_VERSION, "state": "running", "phase": "planned", "history": [],
                 "started_at": _now(),
                 "source": {"root": str(source), "db": str(source / DB_NAME), "root_id": plan["source"].get("root_id")},
                 "target": {"root": str(target), "db": str(target / DB_NAME), "prior_state": plan["target"]["state"]},
                 "staging": str(_staging_path(target, rid)), "backup_root": str(opts.backup_root),
                 "backup_dir": str(Path(opts.backup_root) / rid),
                 "registry": str(opts.registry) if opts.registry else None,
                 "options": {"quiet_minutes": opts.quiet_minutes, "pause_schedules": opts.pause_schedules,
                             "accept_source_problems": opts.accept_source_problems, "replace_retired": opts.replace_retired},
                 "plan_warnings": plan["warnings"], "links_to_review": plan["links_to_review"],
                 "historical_refs": plan["historical_refs"], "absolute_paths": plan["absolute_paths"]})
    lock = claire_paths.RelocationLock()
    j.save()
    try:
        lock.acquire(_lock_info(j))
    except claire_paths.LocationError as e:
        j.data.update(state="not_started", error={"code": e.code, "message": e.message})
        j.save()
        raise _err(e.code, e.message, fix=e.fix)
    j.mark("planned")
    return _drive(Ctx(cfg, j, lock, opts))


class TargetChanged(ClaireError):
    """되돌리면 전환 뒤 기록이 정본에서 빠지게 되는 상태. 아무것도 바꾸기 전에 멈춘다."""


CLOSED_STATES = ("done", "aborted", "failed_clean", "not_started")


def _drive(c: Ctx) -> dict:
    steps = REVERT_STEPS if c.j.data["kind"] == "rollback-revert" else MIGRATE_STEPS
    phases = c.j.phases()
    try:
        for phase in phases[phases.index(c.j.data["phase"]) + 1:]:
            steps[phase](c)
            c.j.mark(phase)
            _maybe_crash(phase)
    except Exception as e:  # noqa: BLE001 — 어떤 실패든 보고한다
        if c.j.reached("post_verified"):
            return _finish_despite(c, e)            # 새 위치 확인까지 끝났다 — 되돌리지 않는다
        return _fail(c, e)
    return _result(c)


def _finish_despite(c: Ctx, exc: Exception) -> dict:
    """새 위치 확인 뒤의 실패(예약 재개·기록 사본 등)는 경고로 남기고 이전을 끝낸다."""
    d = c.j.data
    d.setdefault("warnings", []).append({"code": getattr(exc, "code", type(exc).__name__),
                                         "message": getattr(exc, "message", str(exc))})
    c.j.save()
    try:
        (_revert_done if d["kind"] == "rollback-revert" else step_done)(c)
    except Exception as e:  # noqa: BLE001
        d["warnings"].append({"code": "finish", "message": str(getattr(e, "message", e))})
    finally:
        if d.get("state") != "done":
            d.update(state="done", finished_at=_now())
        if d.get("phase") != "done":
            c.j.mark("done")
        else:
            c.j.save()
        c.lock.release(remove=True)
    return _result(c)


def _result(c: Ctx) -> dict:
    d = c.j.data
    return {"relocation_id": d["id"], "kind": d["kind"], "state": d["state"], "phase": d["phase"],
            "source": d["source"]["root"], "target": d["target"]["root"], "backup": d.get("backup_dir"),
            "verification": d.get("verification"), "post_verification": d.get("post_verification"),
            "registry_update": d.get("registry_update"), "schedules": d.get("schedules"),
            "fixups": d.get("fixups"), "secrets": d.get("secrets"), "moved_aside": d["target"].get("moved_aside_to"),
            "links_to_review": d.get("links_to_review"), "historical_refs": d.get("historical_refs"),
            "writes_after_switch": d.get("writes_after_switch"), "warnings": d.get("warnings"),
            "journal": str(c.j.path),
            "rollback": f"claire_relocate rollback --id {d['id']}" if d["state"] == "done" else None}


def _fail(c: Ctx, exc: Exception) -> dict:
    d = c.j.data
    err = {"code": getattr(exc, "code", type(exc).__name__), "message": getattr(exc, "message", str(exc)),
           "phase_completed": d["phase"]}
    d.update(error=err, state="undoing")              # 되돌리는 중 멈추면 resume 이 아니라 abort 로만 잇는다
    c.j.save()
    try:
        undo = UNDO[d["kind"]](c)
    except TargetChanged as e2:
        d.update(state="needs_decision", undo_error=e2.message)
        c.j.save()
        c.lock.release(remove=False)
        raise _err("relocation_failed_locked", f"이전 실패({err['message']}). {e2.message}", relocation_id=c.rid,
                   fix=e2.extra.get("fix"), changes=e2.extra.get("changes"))
    except Exception as e2:  # noqa: BLE001
        d.update(state="needs_attention", undo_error=str(getattr(e2, "message", e2)), finished_at=_now())
        c.j.save()
        c.lock.release(remove=False)
        raise _err("relocation_failed_locked",
                   f"이전 실패({err['message']}) 뒤 원래대로 되돌리지 못했습니다: {getattr(e2, 'message', e2)}. "
                   "잠금을 남겨 장부를 열지 않습니다.", relocation_id=c.rid,
                   fix=f"원인을 고친 뒤 claire_relocate abort --id {c.rid} (되돌리기를 다시 시도)")
    d.update(state="failed_clean", undo=undo, finished_at=_now())
    c.j.save()
    c.lock.release(remove=True)
    raise _err("relocation_failed", f"이전 실패 — 원래 위치로 계속 운영합니다: {err['message']}",
               relocation_id=c.rid, phase_completed=err["phase_completed"], undo=undo)


def step_paused(c: Ctx) -> None:
    sched = list_claire_schedules(c.source)
    if sched["openclaw"] and not sched["available"]:
        raise _err("schedules_unknown", f"OpenClaw 예약을 확인하지 못했습니다: {sched.get('reason')}")
    s = c.j.data.get("schedules") or {}
    s.setdefault("paused", [])                          # 다시 실행(resume)해도 앞서 멈춘 목록을 잃지 않는다
    s.setdefault("resumed", [])
    s.setdefault("checked", sched["jobs"])
    c.j.data["schedules"] = s
    c.j.save()
    if c.opts.pause_schedules:
        for job in sched["jobs"]:
            if not job["enabled"]:
                continue                                # 원래 꺼져 있던 예약은 목록에 넣지 않는다 (끝나도 켜지 않음)
            if job["id"] not in s["paused"]:
                s["paused"].append(job["id"])           # 멈추기 전에 기록 (중간에 죽어도 다시 켤 목록이 남는다)
                c.j.save()
            _openclaw("cron", "disable", job["id"])
    else:
        due = _due_soon(sched, c.opts.quiet_minutes)
        if due:
            raise _err("schedule_due_soon", f"곧 실행될 Claire 예약: {[j['name'] for j in due]} (--pause-schedules)")


def _check_writers(root: Path) -> dict:
    holders = open_file_holders(db_files(root))
    if holders["method"] == "unavailable":
        raise _err("writer_check_unavailable", "장부 파일을 연 프로세스를 확인할 수 없습니다")
    if holders["pids"]:
        raise _err("uncontrolled_writer", f"다른 프로세스가 장부를 열고 있습니다: {holders['processes']}")
    return holders


def step_quiesced(c: Ctx) -> None:
    holders = _check_writers(c.source)
    conn = claire_data.open_ro(c.source / DB_NAME)
    try:
        runs = running_runs(conn, c.cfg)
        integ = claire_data.run_integrity(conn, c.cfg, root=c.source)
    finally:
        conn.close()
    if runs["fresh"]:
        raise _err("run_in_progress", f"진행 중인 실행이 있습니다: {runs['fresh']}")
    data_p, ops_p = claire_data.split_problems(integ["problems"])
    if data_p and not c.opts.accept_source_problems:
        raise _err("source_problems", "원본 장부·첨부 문제: " + "; ".join(p["detail"] for p in data_p[:5]))
    c.j.data.update(writers={"open_handles": holders, "running_runs": runs},
                    source_problems=claire_data.problem_set(integ), source_operational=ops_p)


def _fingerprint(db: Path) -> dict:
    conn = claire_data.open_ro(db)
    try:
        return claire_data.data_fingerprint(conn)
    finally:
        conn.close()


def _snapshot_ok(bdir: Path, rid: str) -> bool:
    m = claire_data.backup_manifest(bdir)
    if not m or m.get("relocation_id") != rid or (bdir / ".incomplete").exists():
        return False
    if sha256_file(bdir / DB_NAME) != m.get("db_sha256"):
        return False
    sums = claire_data.read_sha256sums(bdir)
    return all((bdir / rel).exists() and sha256_file(bdir / rel) == h for rel, h in sums.items())


def step_backed_up(c: Ctx) -> None:
    bdir, src = c.backup_dir, c.source
    if bdir.exists():
        if _snapshot_ok(bdir, c.rid) and c.j.files().get("source"):
            return
        m = claire_data.backup_manifest(bdir)
        if (bdir / ".incomplete").exists() or (m and m.get("relocation_id") == c.rid):
            shutil.rmtree(bdir)
        else:
            raise _err("backup_dir_exists", f"백업 폴더가 이미 있습니다(다른 작업): {bdir}")
    bdir.parent.mkdir(parents=True, exist_ok=True)
    bdir.mkdir(mode=0o700)
    (bdir / ".incomplete").write_text(c.rid, encoding="utf-8")
    claire_data.sqlite_snapshot(src / DB_NAME, bdir / DB_NAME)
    snap = claire_data.open_ro(bdir / DB_NAME)
    try:
        ic = snap.execute("PRAGMA integrity_check").fetchone()[0]
        fp = claire_data.data_fingerprint(snap)
        counts, chash = table_counts(snap), content_hash(snap)
        entries = claire_data.redaction_entries(snap)
        integ = claire_data.run_integrity(snap, c.cfg, root=src)
    finally:
        snap.close()
    if ic != "ok":
        raise _err("snapshot_corrupt", f"스냅숏 integrity_check: {ic}")
    live = _fingerprint(src / DB_NAME)
    if live["sha256"] != fp["sha256"]:
        raise _err("snapshot_mismatch", "스냅숏과 원본 장부 내용이 다릅니다 (스냅숏 중 쓰기?)",
                   diff=claire_data.fingerprint_diff(live, fp))
    inv = claire_data.data_files(src)["files"]
    sums, files = {}, {}
    for rel, meta in sorted(inv.items()):
        sha, size = claire_data.copy_hashed(src / rel, bdir / rel)
        sums[rel] = sha
        files[rel] = [sha, size, meta["mtime_ns"]]
    claire_data.write_sha256sums(bdir, sums)
    # 가림 목록 — 원본의 옛 백업 루트·미러·새 백업 루트 것과 합친다 (지운 개인정보를 다시 들이지 않게)
    ts = _now()
    old = claire_data.read_redaction_files(src / "backups" / claire_data.REDACTION_MANIFEST,
                                           Path(c.opts.backup_root) / claire_data.REDACTION_MANIFEST,
                                           claire_paths.expand(c.cfg.get("backup_mirror") or "/nonexistent") / claire_data.REDACTION_MANIFEST)
    claire_data.merge_redaction_manifest(bdir / claire_data.REDACTION_MANIFEST, old + entries, ts)
    claire_data.merge_redaction_manifest(Path(c.opts.backup_root) / claire_data.REDACTION_MANIFEST, old + entries, ts)
    manifest = {"kind": "relocation", "relocation_id": c.rid, "created_at": now_iso(c.cfg),
                "claire_version": CLAIRE_VERSION, "schema_version": SCHEMA_VERSION,
                "data_dir": str(src), "data_root_id": c.j.data["source"].get("root_id"),
                "counts": counts, "content_hash": chash, "fingerprint": fp["sha256"], "refs": fp["refs"],
                "files": len(sums), "bytes": sum(v[1] for v in files.values()),
                "db_sha256": sha256_file(bdir / DB_NAME), "storage_ok": integ["storage_ok"],
                "operational_problems": integ["operational_problems"],
                "excludes": ["secrets/", "backups/", "skill-prev/"], "secrets_policy": SECRETS_POLICY,
                "restore": f"claire_check restore --from {bdir} --to <빈 폴더>"}
    claire_paths.write_json_atomic(bdir / "manifest.json", manifest, mode=0o600)
    (bdir / ".incomplete").unlink()
    if hasattr(os, "sync"):
        os.sync()
    c.j.save_files("source", files)
    c.j.data["source_fingerprint"] = fp
    c.j.data["backup"] = {"dir": str(bdir), "files": len(sums), "db_sha256": manifest["db_sha256"],
                          "redaction_entries": len(old + entries)}


def step_staged(c: Ctx) -> None:
    st, src = c.staging, c.source
    if st.exists():
        m = claire_paths.read_marker(st) or {}
        if m.get("relocation_id") != c.rid:
            raise _err("staging_exists", f"임시 폴더가 이미 있습니다(다른 작업): {st}")
        shutil.rmtree(st)
    st.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    st.mkdir(mode=0o700)
    new_root = claire_paths.new_root_id()
    claire_paths.write_marker(st, {"format": 1, "system": claire_paths.SYSTEM, "root_id": new_root, "state": "staging",
                                   "relocation_id": c.rid, "created_at": _now(),
                                   "relocated_from": {"path": str(src), "root_id": c.j.data["source"].get("root_id")}})
    want_db = sha256_file(c.backup_dir / DB_NAME)
    got_db, _ = claire_data.copy_hashed(c.backup_dir / DB_NAME, st / DB_NAME, mode=0o600)
    if got_db != want_db:
        raise _err("copy_mismatch", "장부 사본 해시가 맞지 않습니다")
    files = c.j.files().get("source") or {}
    for rel, (sha, _size, _mt) in sorted(files.items()):
        got, _ = claire_data.copy_hashed(src / rel, st / rel)
        if got != sha:
            raise _err("source_changed", f"원본 파일이 백업 뒤 바뀌었습니다: {rel}")
    n_sec = copy_secrets(src / "secrets", st / "secrets")
    for sub in DATA_SUBDIRS:
        (st / sub).mkdir(exist_ok=True)
    conn = sqlite3.connect(st / DB_NAME)
    try:
        conn.execute("BEGIN IMMEDIATE")
        db_fix = fix_db_paths(conn, src)
        conn.execute("COMMIT")
    finally:
        conn.close()
    cfg_fix, cfg_sha = [], None
    cp = st / "config.json"
    if cp.exists():
        obj = json.loads(cp.read_text(encoding="utf-8"))
        cfg_fix = config_path_refs(obj, src, c.target)
        if cfg_fix:
            mode = stat.S_IMODE(cp.stat().st_mode)
            claire_paths.write_json_atomic(cp, obj, mode=mode)
        cfg_sha = sha256_file(cp)
    if hasattr(os, "sync"):
        os.sync()
    c.j.data["fixups"] = {"db": db_fix, "config": cfg_fix, "config_sha256": cfg_sha}
    c.j.data["secrets"] = {"files": n_sec, "policy": SECRETS_POLICY}
    c.j.data["target"]["root_id"] = new_root


def _expected_db(c: Ctx) -> sqlite3.Connection:
    """새 위치 장부가 가져야 할 내용: 이전 백업 사본 + 경로 고침 (메모리)."""
    mem = sqlite3.connect(":memory:")
    src = sqlite3.connect(f"file:{(c.backup_dir / DB_NAME).as_posix()}?immutable=1", uri=True)
    try:
        src.backup(mem)
    finally:
        src.close()
    if any(not f.get("kept") for f in (c.j.data.get("fixups") or {}).get("db") or []):
        fix_db_paths(mem, c.source)
        mem.commit()
    return mem


def _expected_fingerprint(c: Ctx) -> dict:
    if not any(not f.get("kept") for f in (c.j.data.get("fixups") or {}).get("db") or []):
        return c.j.data["source_fingerprint"]
    mem = _expected_db(c)
    try:
        return claire_data.data_fingerprint(mem)
    finally:
        mem.close()


def step_verified(c: Ctx) -> None:
    st = c.staging
    conn = claire_data.open_ro(st / DB_NAME)
    try:
        ic = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fp = claire_data.data_fingerprint(conn)
        integ = claire_data.run_integrity(conn, c.cfg, root=st)
    finally:
        conn.close()
    if ic != "ok":
        raise _err("verify_integrity", f"새 장부 integrity_check: {ic}")
    exp = _expected_fingerprint(c)
    if fp["sha256"] != exp["sha256"]:
        raise _err("verify_db_mismatch", "새 장부 내용이 원본과 다릅니다", diff=claire_data.fingerprint_diff(exp, fp))
    files = c.j.files().get("source") or {}
    cfg_sha = (c.j.data.get("fixups") or {}).get("config_sha256")
    have = claire_data.data_files(st)["files"]
    extra = sorted(set(have) - set(files))
    if extra:
        raise _err("verify_extra_files", f"새 위치에 원본에 없던 파일: {extra[:10]}")
    bad, missing = [], []
    for rel, (sha, _size, _mt) in files.items():
        p = st / rel
        if not p.exists():
            missing.append(rel)
            continue
        want = cfg_sha if (rel == "config.json" and (c.j.data.get("fixups") or {}).get("config")) else sha
        if sha256_file(p) != want:
            bad.append(rel)
    if missing or bad:
        raise _err("verify_files", f"파일 누락 {len(missing)} · 해시 불일치 {len(bad)}: {(missing + bad)[:10]}")
    probs = claire_data.problem_set(integ)
    if probs != c.j.data.get("source_problems", []):
        raise _err("verify_problems_differ", "새 위치의 무결성 결과가 원본과 다릅니다",
                   source=c.j.data.get("source_problems"), target=probs)
    n_sec = (c.j.data.get("secrets") or {}).get("files", 0)
    if n_sec and not _secrets_ok(st / "secrets"):
        raise _err("verify_secrets", "새 위치 secrets/ 권한이 700/600 이 아닙니다")
    c.j.data["verification"] = {
        "integrity_check": ic, "fingerprint": fp["sha256"], "fingerprint_matches_source": True,
        "rows": {t: v["rows"] for t, v in fp["tables"].items()}, "refs": fp["refs"],
        "files": len(files), "files_hash_match": True, "attachments_checked": True,
        "problems_same_as_source": True, "pre_existing_problems": probs, "secrets_files": n_sec}


def step_retired(c: Ctx) -> None:
    src = c.source
    tag = f"relocated-{c.rid}"
    if not (src / f"{DB_NAME}.{tag}").exists():
        _check_writers(src)
        live = _fingerprint(src / DB_NAME)
        if live["sha256"] != c.j.data["source_fingerprint"]["sha256"]:
            raise _err("source_changed", "백업 뒤 원본 장부가 바뀌었습니다 (통제되지 않은 쓰기) — 전환하지 않습니다",
                       diff=claire_data.fingerprint_diff(c.j.data["source_fingerprint"], live))
        now_files = claire_data.data_files(src)["files"]
        old = c.j.files().get("source") or {}
        changed = sorted(r for r in set(now_files) | set(old)
                         if r not in now_files or r not in old or now_files[r]["bytes"] != old[r][1]
                         or now_files[r]["mtime_ns"] != old[r][2])
        if changed:
            raise _err("source_changed", f"백업 뒤 원본 파일이 바뀌었습니다: {changed[:10]}")
    c.j.data["retire"] = retire_root(src, tag, str(c.target), c.rid)


def step_switched(c: Ctx) -> None:
    t, st = c.target, c.staging
    m = claire_paths.read_marker(t) if t.exists() else None
    if m and m.get("relocation_id") == c.rid and claire_paths.has_db(t) and not st.exists():
        pass                                                   # 이미 바꿨다 (resume)
    else:
        state = classify_target(t)
        if state == "empty":
            ds = t / ".DS_Store"
            if ds.exists():
                ds.unlink()
            os.rmdir(t)
        elif state == "retired" and c.opts.replace_retired:
            aside = t.with_name(f"{t.name}.retired-{c.rid}")
            if aside.exists():
                raise _err("aside_exists", f"비켜 둘 이름이 이미 있습니다: {aside}")
            os.rename(t, aside)
            c.j.data["target"]["moved_aside_to"] = str(aside)
            c.j.save()
        elif state != "missing":
            raise _err("target_conflict", f"대상 폴더 상태가 바뀌었습니다({state}): {t} — 덮어쓰지 않습니다")
        t.parent.mkdir(parents=True, exist_ok=True)
        os.rename(st, t)
    m = claire_paths.read_marker(t) or {}
    m.update(state="active", activated_at=_now())
    claire_paths.write_marker(t, m)


def step_configured(c: Ctx) -> None:
    d = c.j.data
    if "pointer_before" not in d:
        try:
            d["pointer_before"] = claire_paths.read_pointer()
        except ValueError:
            d["pointer_before"] = None
        c.j.save()
    m = claire_paths.read_marker(c.target) or {}
    claire_paths.write_pointer(c.target, m.get("root_id"), reason=f"{d['kind']}:{c.rid}", relocation_id=c.rid)
    state = "rolled_back" if d["kind"] == "rollback-move" else "migrated"
    cut = {"state": state, "relocation_id": c.rid, "from": str(c.source), "to": str(c.target), "at": _now(),
           "backup": d.get("backup_dir"), "verified": True, "claire_version": CLAIRE_VERSION}
    if d.get("parent"):
        cut["rolled_back"] = d["parent"]
    d["registry_update"] = _registry_apply(c, str(c.target), cut)


def _cli(script: str, *args, env=None) -> dict:
    e = {k: v for k, v in os.environ.items() if k != "CLAIRE_DATA_DIR"}
    e.update(env or {})
    r = subprocess.run([sys.executable, str(HERE / script), *args], capture_output=True, text=True, env=e, timeout=600)
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {"ok": False, "error": {"message": (r.stderr or r.stdout)[-500:]}}


def step_post_verified(c: Ctx) -> None:
    """예약 실행과 같은 조건(환경변수 없음)으로 새 위치를 확인한다. 쓰기 도구는 부르지 않는다."""
    env = {"CLAIRE_RELOCATION_ID": c.rid}
    where = _cli("claire_relocate", "where", env=env)
    if not same_path(where.get("data_dir"), c.target) or where.get("blockers"):
        raise _err("post_where", f"예약 실행 조건의 데이터 위치가 새 위치가 아닙니다: {where.get('data_dir')} {where.get('blockers')}")
    integ = _cli("claire_check", "integrity", env=env)
    if not integ.get("ok", True) or not same_path(integ.get("db"), c.target / DB_NAME):
        raise _err("post_integrity", f"새 위치 무결성 확인 실패: {integ.get('error') or integ.get('db')}")
    if not claire_paths.is_tombstone(c.source / DB_NAME):
        raise _err("post_old_open", "옛 위치 claire.db 가 은퇴 표지가 아닙니다 (옛 코드가 옛 장부를 열 수 있음)")
    fp = _fingerprint(c.target / DB_NAME)
    writes = None
    if fp["sha256"] != _expected_fingerprint(c)["sha256"]:
        # 잠금 중에는 생기지 않아야 하지만, 생겼다면 지우지 않는다: 옮긴 행이 모두 남아 있으면 그 기록을 포함해 마친다
        exp, new = _expected_db(c), claire_data.open_ro(c.target / DB_NAME)
        try:
            plan = claire_data.change_plan(exp, new)
        finally:
            exp.close()
            new.close()
        if not plan["superset"]:
            raise _err("post_changed", "전환 직후 새 장부에서 옮긴 행이 사라졌습니다", changes=plan)
        writes = {"tables": plan["tables"], "added": plan["added"], "changed": plan["changed"]}
        c.j.data["writes_after_switch"] = writes
    src_probs = c.j.data.get("source_problems", [])
    probs = claire_data.problem_set(integ)
    if writes is None and probs != src_probs:
        raise _err("post_problems", "새 위치 무결성 결과가 원본과 다릅니다", target=probs)
    if writes is not None:
        data_p, _ops = claire_data.split_problems(integ.get("problems") or [])
        new_p = sorted(f"{p['check']}: {p['detail']}" for p in data_p if f"{p['check']}: {p['detail']}" not in src_probs)
        if new_p:
            raise _err("post_problems", "새 위치에 원본에 없던 장부·첨부 문제가 있습니다", target=new_p)
    files = claire_data.data_files(c.target)["files"]
    c.j.save_files("switch", {rel: [v["bytes"], v["mtime_ns"]] for rel, v in files.items()})
    c.j.data["switch_fingerprint"] = fp["sha256"]
    c.j.data["post_verification"] = {"where": where.get("data_dir"), "source": where.get("source"),
                                     "integrity_db": integ.get("db"), "storage_ok": integ.get("storage_ok"),
                                     "old_location_blocked": True, "fingerprint": fp["sha256"]}


def step_resumed(c: Ctx) -> None:
    res = _resume_schedules(c)
    if res["failed"]:                                  # 이전은 끝났다 — 다시 켜지 못한 예약은 경고와 명령으로 남긴다
        c.j.data.setdefault("warnings", []).append({
            "code": "schedule_resume_failed", "message": f"예약 {len(res['failed'])}개를 다시 켜지 못했습니다",
            "commands": [f["command"] for f in res["failed"]]})


def _mark_parent(c: Ctx) -> None:
    if c.j.data.get("parent"):
        try:
            par = Journal.load(c.j.data["parent"])
            par.data["rolled_back_by"] = c.rid
            par.save()
        except ClaireError:
            pass


def step_done(c: Ctx) -> None:
    d = c.j.data
    d.update(state="done", finished_at=_now())
    c.j.save()
    _mark_parent(c)
    try:
        (c.target / "relocation").mkdir(exist_ok=True, mode=0o700)
        shutil.copy2(c.j.path, c.target / "relocation" / f"{c.rid}.json")
        if c.backup_dir and c.backup_dir.is_dir():
            shutil.copy2(c.j.path, c.backup_dir / "relocation.json")
    except OSError:
        pass
    c.lock.release(remove=True)


MIGRATE_STEPS = {"paused": step_paused, "quiesced": step_quiesced, "backed_up": step_backed_up,
                 "staged": step_staged, "verified": step_verified, "retired": step_retired,
                 "switched": step_switched, "configured": step_configured, "post_verified": step_post_verified,
                 "resumed": step_resumed, "done": step_done}


def _resume_schedules(c: Ctx) -> dict:
    """멈춘 예약만 다시 켠다. 실패해도 예외를 올리지 않고 명령을 남긴다 (되돌리기·마무리를 막지 않는다)."""
    s = c.j.data.get("schedules") or {}
    out = {"resumed": [], "failed": []}
    for jid in s.get("paused") or []:
        if jid in (s.get("resumed") or []):
            continue
        try:
            _openclaw("cron", "enable", jid)
        except (ClaireError, OSError, subprocess.SubprocessError) as e:
            out["failed"].append({"id": jid, "error": str(getattr(e, "message", e))[:300],
                                  "command": f"openclaw cron enable {jid}"})
            continue
        s.setdefault("resumed", []).append(jid)
        out["resumed"].append(jid)
        c.j.save()
    if out["failed"]:
        s["resume_failed"] = out["failed"]
        c.j.data["schedules"] = s
        c.j.save()
    return out


def _restore_pointer_and_registry(c: Ctx, fallback_root: Path) -> dict:
    d, out = c.j.data, {}
    try:
        ptr = claire_paths.read_pointer()
    except ValueError:
        ptr = None
    if ptr and ptr.get("relocation_id") == c.rid:
        before = d.get("pointer_before")
        if before and before.get("data_dir"):
            claire_paths.write_pointer(Path(before["data_dir"]), before.get("root_id"),
                                       reason=f"undo:{c.rid}", relocation_id=before.get("relocation_id"))
        else:
            m = claire_paths.read_marker(fallback_root) or {}
            claire_paths.write_pointer(fallback_root, m.get("root_id"), reason=f"undo:{c.rid}")
        out["pointer"] = "restored"
    reg = d.get("registry_update") or {}
    if reg.get("updated") and reg.get("before") is not None and c.opts.registry:
        b = reg["before"]
        out["registry"] = update_registry(c.opts.registry, b.get("operational_root"), b.get("cutover"))
    return out


def _switched_in(c: Ctx) -> bool:
    t, st = c.target, c.staging
    tm = claire_paths.read_marker(t) if t.exists() else None
    return bool(tm and tm.get("relocation_id") == c.rid and not (st and st.exists()))


def _target_changes(c: Ctx) -> dict | None:
    """전환해 들어간 새 위치가 전환 때 내용과 다른가 (장부 전체 지문 + 파일 목록·크기). 같으면 None."""
    t = c.target
    if not claire_paths.has_db(t):
        return {"db": "새 위치에 장부가 없습니다"}
    fp = _fingerprint(t / DB_NAME)
    base = c.j.data.get("switch_fingerprint") or _expected_fingerprint(c)["sha256"]
    now = {rel: v["bytes"] for rel, v in claire_data.data_files(t)["files"].items() if not rel.startswith("relocation/")}
    sw = c.j.files().get("switch")
    if sw:
        want = {rel: v[0] for rel, v in sw.items() if not rel.startswith("relocation/")}
    else:
        want = {rel: v[1] for rel, v in (c.j.files().get("source") or {}).items()}
        if (c.j.data.get("fixups") or {}).get("config") and "config.json" in now:
            want["config.json"] = now["config.json"]
    changed = sorted(r for r in set(now) | set(want) if now.get(r) != want.get(r))
    if fp["sha256"] == base and not changed:
        return None
    return {"db_changed": fp["sha256"] != base, "files_changed": changed[:10], "files_changed_count": len(changed)}


def undo_migrate(c: Ctx, keep_changed_target: bool = False) -> dict:
    """실제 상태를 보고 거꾸로 되돌린다 (기록된 단계와 무관하게 멱등).
    전환해 들어간 새 위치에 그 뒤 기록이 있으면 아무것도 바꾸기 전에 멈춘다(TargetChanged).
    keep_changed_target 이면 그 폴더를 지우지 않고 .<이름>.undone-<id> 로 비켜 둔다. 새 위치를 지우는 일은 없다."""
    t, st = c.target, c.staging
    switched = _switched_in(c)
    changes = _target_changes(c) if switched else None
    if changes and not keep_changed_target:
        raise TargetChanged(
            "target_changed", f"새 위치({t})에 전환 뒤 기록이 있습니다 — 자동으로 되돌리면 그 기록이 원래 위치 장부에 없게 됩니다.",
            changes=changes,
            fix=(f"claire_relocate resume --id {c.rid} (새 기록을 포함해 이전을 마침) 또는 "
                 f"claire_relocate abort --id {c.rid} --keep-changed-target (새 위치를 지우지 않고 비켜 둔 뒤 원래 위치로)"))
    out = _restore_pointer_and_registry(c, c.source)
    if switched:
        if changes:
            keep = t.parent / f".{t.name}.undone-{c.rid}"
            os.rename(t, keep)
            out["changed_target_kept"] = str(keep)
            out["target_changes"] = changes
        else:
            os.rename(t, st)
            out["target"] = "임시 폴더로 되돌림 (원본과 같은 내용)"
    aside = c.j.data["target"].get("moved_aside_to")
    if aside and Path(aside).exists() and not t.exists():
        os.rename(aside, t)
        out["aside_restored"] = f"{aside} → {t}"
    elif c.j.data["target"].get("prior_state") == "empty" and not t.exists():
        t.mkdir(parents=True, mode=0o700)
    tag = f"relocated-{c.rid}"
    unretired = False
    if (c.source / f"{DB_NAME}.{tag}").exists():
        out["source"] = unretire_root(c.source, tag)
        unretired = True
    elif (claire_paths.read_marker(c.source) or {}).get("state") in ("retiring", "retired") and claire_paths.has_db(c.source):
        m = claire_paths.read_marker(c.source)
        m["state"] = "active"
        claire_paths.write_marker(c.source, m)
    if st and st.exists():
        sm = claire_paths.read_marker(st) or {}
        if sm.get("relocation_id") == c.rid:              # 전환된 적 없거나 원본과 같은 내용임을 확인한 임시 폴더만
            shutil.rmtree(st)
            out["staging"] = "removed"
    fp0 = c.j.data.get("source_fingerprint")
    if fp0 and claire_paths.has_db(c.source):
        same = _fingerprint(c.source / DB_NAME)["sha256"] == fp0["sha256"]
        out["source_verified"] = same
        if not same:                                       # 경고만 — 원본이 정본이다 (막으면 장부가 잠긴 채 남는다)
            out["source_note"] = ("되살린 옛 장부의 지문이 백업 때와 다릅니다 — claire_check integrity 로 확인하세요" if unretired else
                                  "원본이 이전 시도 중 바뀌었습니다(통제되지 않은 쓰기 — 이전을 멈춘 이유일 수 있음). 원본을 그대로 씁니다")
    out["schedules"] = _resume_schedules(c)              # 마지막에, 실패해도 되돌리기를 막지 않는다
    return out


def rv_paused(c: Ctx) -> None:
    step_paused(c)


def rv_quiesced(c: Ctx) -> None:
    holders = _check_writers(c.source)                       # 지금 정본(새 위치)
    conn = claire_data.open_ro(c.source / DB_NAME)
    try:
        runs = running_runs(conn, c.cfg)
    finally:
        conn.close()
    if runs["fresh"]:
        raise _err("run_in_progress", f"진행 중인 실행이 있습니다: {runs['fresh']}")
    parent = Journal.load(c.j.data["parent"])
    if writes_since_switch(parent)["writes"]:
        raise _err("writes_since_switch", "전환 뒤 새 기록이 생겼습니다 — 단순 복귀하지 않습니다 (rollback 을 다시 계획)")
    c.j.data["writers"] = {"open_handles": holders, "running_runs": runs}


def rv_verified(c: Ctx) -> None:
    parent = Journal.load(c.j.data["parent"])
    old = c.target                                           # 되돌아갈 옛 위치
    kept = old / f"{DB_NAME}.relocated-{parent.id}"
    if not kept.exists():
        if claire_paths.has_db(old) and (claire_paths.read_marker(old) or {}).get("state") == "active":
            return                                           # 이미 되살렸다 (resume)
        raise _err("retired_db_missing", f"옛 장부가 없습니다: {kept}")
    conn = sqlite3.connect(f"file:{kept.as_posix()}?immutable=1", uri=True)
    try:
        ic = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fp = claire_data.data_fingerprint(conn)
    finally:
        conn.close()
    if ic != "ok" or fp["sha256"] != parent.data["source_fingerprint"]["sha256"]:
        raise _err("old_db_changed", "옛 장부가 이전 때와 다릅니다 — 되돌리지 않습니다")
    now_files = claire_data.data_files(old)["files"]
    want = parent.files().get("source") or {}
    changed = sorted(r for r in set(now_files) | set(want) if r not in now_files or r not in want
                     or now_files[r]["bytes"] != want[r][1])
    if changed:
        raise _err("old_files_changed", f"옛 위치 파일이 이전 때와 다릅니다: {changed[:10]}")
    c.j.data["verification"] = {"old_db_integrity": ic, "old_fingerprint_matches": True, "files": len(want)}


def rv_retired(c: Ctx) -> None:
    c.j.data["retire"] = retire_root(c.source, f"rolled-back-{c.rid}", str(c.target), c.rid)


def rv_switched(c: Ctx) -> None:
    parent = Journal.load(c.j.data["parent"])
    c.j.data["unretire"] = unretire_root(c.target, f"relocated-{parent.id}")
    # 새 위치에서 갱신된 토큰을 옛 위치로 (다른 내용은 .before-<id> 로 남김)
    c.j.data["secrets"] = {"files": copy_secrets(c.source / "secrets", c.target / "secrets", keep_suffix=f".before-{c.rid}")}


def rv_configured(c: Ctx) -> None:
    d = c.j.data
    if "pointer_before" not in d:
        d["pointer_before"] = claire_paths.read_pointer()
        c.j.save()
    m = claire_paths.read_marker(c.target) or {}
    claire_paths.write_pointer(c.target, m.get("root_id"), reason=f"rollback:{c.rid}", relocation_id=c.rid)
    cut = {"state": "rolled_back", "relocation_id": c.rid, "rolled_back": d["parent"], "from": str(c.source),
           "to": str(c.target), "at": _now(), "verified": True, "claire_version": CLAIRE_VERSION}
    d["registry_update"] = _registry_apply(c, str(c.target), cut)


def rv_post_verified(c: Ctx) -> None:
    env = {"CLAIRE_RELOCATION_ID": c.rid}
    where = _cli("claire_relocate", "where", env=env)
    if not same_path(where.get("data_dir"), c.target) or where.get("blockers"):
        raise _err("post_where", f"되돌린 위치가 정본이 아닙니다: {where}")
    integ = _cli("claire_check", "integrity", env=env)
    if not integ.get("ok", True) or not same_path(integ.get("db"), c.target / DB_NAME):
        raise _err("post_integrity", f"되돌린 위치 무결성 확인 실패: {integ.get('error')}")
    if not claire_paths.is_tombstone(c.source / DB_NAME):
        raise _err("post_old_open", "떠난 위치 claire.db 가 은퇴 표지가 아닙니다")
    c.j.data["post_verification"] = {"where": where.get("data_dir"), "integrity_db": integ.get("db"),
                                     "storage_ok": integ.get("storage_ok")}


REVERT_STEPS = {"paused": rv_paused, "quiesced": rv_quiesced, "verified": rv_verified, "retired": rv_retired,
                "switched": rv_switched, "configured": rv_configured, "post_verified": rv_post_verified,
                "resumed": step_resumed, "done": lambda c: _revert_done(c)}


def _revert_done(c: Ctx) -> None:
    c.j.data.update(state="done", finished_at=_now())
    c.j.save()
    _mark_parent(c)
    try:
        (c.target / "relocation").mkdir(exist_ok=True, mode=0o700)
        shutil.copy2(c.j.path, c.target / "relocation" / f"{c.rid}.json")
    except OSError:
        pass
    c.lock.release(remove=True)


def undo_revert(c: Ctx, keep_changed_target: bool = False) -> dict:
    parent = Journal.load(c.j.data["parent"])
    old, cur = c.target, c.source
    ptag, mytag = f"relocated-{parent.id}", f"rolled-back-{c.rid}"
    old_active = not (old / f"{DB_NAME}.{ptag}").exists() and claire_paths.has_db(old) and \
        (claire_paths.read_marker(old) or {}).get("state") == "active"
    if old_active and not keep_changed_target and \
            _fingerprint(old / DB_NAME)["sha256"] != parent.data["source_fingerprint"]["sha256"]:
        raise TargetChanged(
            "target_changed", f"되살린 옛 위치({old})에 그 뒤 기록이 있습니다.",
            fix=(f"claire_relocate resume --id {c.rid} (되돌리기를 마침) 또는 claire_relocate abort --id {c.rid} "
                 f"--keep-changed-target (그 기록은 {old}/{DB_NAME}.{ptag} 파일에 남는다)"))
    out = _restore_pointer_and_registry(c, c.source)
    if old_active:
        out["old"] = retire_root(old, ptag, str(cur), parent.id)
    if (cur / f"{DB_NAME}.{mytag}").exists():
        out["current"] = unretire_root(cur, mytag)
    out["schedules"] = _resume_schedules(c)
    return out


UNDO = {"migrate": undo_migrate, "rollback-move": undo_migrate, "rollback-revert": undo_revert}


# --------------------------------------------------------------------------
# 상태·재개·중단·되돌리기
# --------------------------------------------------------------------------

def _pick(rid: str | None) -> Journal:
    if rid:
        return Journal.load(rid)
    lock = claire_paths.read_lock()
    if lock and lock.get("id"):
        return Journal.load(lock["id"])
    js = list_journals()
    if not js:
        raise _err("no_relocation", "이전 기록이 없습니다")
    return Journal(js[-1])


def status(rid: str | None = None) -> dict:
    loc, canon = claire_paths.resolve(), claire_paths.resolve(use_env=False)
    lock = claire_paths.read_lock()
    live = claire_paths.lock_is_live() if lock else False
    js = list_journals()
    cur = None
    try:
        cur = _pick(rid).data if (rid or lock or js) else None
    except ClaireError:
        cur = None
    nxt = []
    cst = (cur or {}).get("state")
    if lock and not live:
        lid = lock.get("id")
        if cst in ("undoing", "needs_attention"):
            nxt += [f"claire_relocate abort --id {lid}  (되돌리던 중 멈춤 — 원인을 확인한 뒤)"]
        elif cst == "needs_decision":
            nxt += [f"claire_relocate resume --id {lid}  (새 기록을 포함해 이전을 마침)",
                    f"claire_relocate abort --id {lid} --keep-changed-target  (새 위치를 비켜 두고 원래 위치로)"]
        else:
            nxt += [f"claire_relocate resume --id {lid}", f"claire_relocate abort --id {lid}"]
    elif cur and cur.get("state") == "done" and cur.get("kind") in ("migrate", "rollback-move") \
            and not cur.get("rolled_back_by") and same_path(canon.path, (cur.get("target") or {}).get("root")):
        nxt.append(f"(필요 시) claire_relocate rollback --id {cur['id']}")
    detail = None
    if cur:
        detail = {k: v for k, v in cur.items() if k not in ("links_to_review",)}
        detail["links_to_review_count"] = len(cur.get("links_to_review") or [])
    return {"location": loc.as_dict(), "canonical": canon.as_dict() if loc.source == "env" else None,
            "lock": lock, "lock_live": live, "relocations": [_summary(j) for j in js[-10:]],
            "current": detail, "next": nxt}


def resume(cfg, rid: str | None) -> dict:
    j = _pick(rid)
    st = j.data["state"]
    if st in ("aborted", "failed_clean", "not_started"):
        raise _err("relocation_closed", f"이 작업은 {st} 상태입니다. 새로 시작하세요: claire_relocate plan")
    if st in ("undoing", "needs_attention"):
        raise _err("relocation_undoing", "되돌리던 중 멈춘 작업입니다 — 이어서 진행하지 않습니다",
                   fix=f"claire_relocate abort --id {j.id} (되돌리기를 마저 함)")
    lock = claire_paths.RelocationLock()
    try:
        lock.acquire(_lock_info(j))
    except claire_paths.LocationError as e:
        raise _err(e.code, e.message, fix=e.fix)
    if st == "done":
        lock.release(remove=True)
        return {"relocation_id": j.id, "state": "done", "message": "이미 끝난 작업입니다 (남은 잠금만 풀었습니다)"}
    j.data["state"] = "running"
    j.data.setdefault("resumed_at", []).append(_now())
    j.save()
    return _drive(Ctx(cfg, j, lock, _opts_from(j.data)))


def abort(cfg, rid: str | None, keep_changed_target: bool = False) -> dict:
    j = _pick(rid)
    st = j.data["state"]
    if st == "done":
        raise _err("relocation_done", "이미 끝난 이전입니다. 되돌리려면 claire_relocate rollback")
    if st in ("aborted", "failed_clean", "not_started"):
        raise _err("relocation_closed", f"이 작업은 이미 {st} 상태입니다 — 되돌릴 것이 없습니다 (claire_relocate status)")
    lock = claire_paths.RelocationLock()
    try:
        lock.acquire(_lock_info(j))
    except claire_paths.LocationError as e:
        raise _err(e.code, e.message, fix=e.fix)
    j.data["state"] = "undoing"
    j.save()
    c = Ctx(cfg, j, lock, _opts_from(j.data))
    try:
        undo = UNDO[j.data["kind"]](c, keep_changed_target=keep_changed_target)
    except TargetChanged as e:
        j.data.update(state="needs_decision", undo_error=e.message)
        j.save()
        lock.release(remove=False)
        raise _err("target_changed", e.message, relocation_id=j.id, fix=e.extra.get("fix"), changes=e.extra.get("changes"))
    except Exception as e:  # noqa: BLE001
        j.data.update(state="needs_attention", undo_error=str(getattr(e, "message", e)))
        j.save()
        lock.release(remove=False)
        raise _err("abort_failed", f"되돌리지 못했습니다: {getattr(e, 'message', e)}. 잠금을 남겨 둡니다",
                   relocation_id=j.id, fix=f"원인을 고친 뒤 claire_relocate abort --id {j.id}")
    j.data.update(state="aborted", undo=undo, finished_at=_now())
    j.save()
    lock.release(remove=True)
    return {"relocation_id": j.id, "state": "aborted", "undo": undo,
            "data_dir": str(claire_paths.resolve(use_env=False).path)}


def writes_since_switch(j: Journal) -> dict:
    """전환 기준 지문과 지금 새 위치를 비교한다 (장부 전체 표 + 파일 목록·크기·수정 시각)."""
    t = Path(j.data["target"]["root"])
    fp = _fingerprint(t / DB_NAME)
    base = j.files().get("switch") or {}
    now_files = {rel: [v["bytes"], v["mtime_ns"]] for rel, v in claire_data.data_files(t)["files"].items()}
    changed = sorted(r for r in set(base) | set(now_files)
                     if base.get(r) != now_files.get(r) and not r.startswith("relocation/"))   # 이전 기록 사본은 데이터가 아님
    db_changed = fp["sha256"] != j.data.get("switch_fingerprint")
    return {"writes": db_changed or bool(changed), "db_changed": db_changed, "files_changed": changed[:20],
            "files_changed_count": len(changed), "fingerprint_now": fp["sha256"]}


def rollback(cfg, rid: str | None, confirm: bool, opts_kw: dict) -> dict:
    if rid:
        j = Journal.load(rid)
    else:
        cands = [x for x in list_journals() if x.get("state") == "done" and x.get("kind") in ("migrate", "rollback-move")
                 and not x.get("rolled_back_by")]
        if not cands:
            raise _err("no_relocation", "되돌릴 이전이 없습니다")
        j = Journal(cands[-1])
    if j.data.get("state") != "done" or j.data.get("kind") not in ("migrate", "rollback-move"):
        raise _err("not_rollbackable", f"{j.id} 는 되돌릴 수 있는 완료된 이전이 아닙니다 ({j.data.get('kind')}, {j.data.get('state')})")
    if j.data.get("rolled_back_by"):
        raise _err("already_rolled_back", f"{j.id} 는 이미 {j.data['rolled_back_by']} 로 되돌렸습니다")
    canon = claire_paths.resolve(use_env=False)
    src, tgt = Path(j.data["source"]["root"]), Path(j.data["target"]["root"])
    blockers = list(canon.blockers)
    if not same_path(canon.path, tgt):
        blockers.append({"code": "not_current", "message": f"지금 정본({canon.path})이 이 이전의 대상({tgt})이 아닙니다"})
    kept = src / f"{DB_NAME}.relocated-{j.id}"
    if not kept.exists():
        blockers.append({"code": "old_copy_missing", "message": f"옛 장부가 없습니다: {kept}"})
    plan = {"dry_run": not confirm, "relocation_id": j.id, "current": str(tgt), "back_to": str(src), "blockers": blockers}
    if blockers:
        if confirm:
            raise _err("rollback_blocked", "되돌릴 수 없습니다: " + "; ".join(b["message"] for b in blockers), blockers=blockers)
        return plan
    w = writes_since_switch(j)
    plan["writes_since_switch"] = w
    holders = open_file_holders(db_files(tgt))
    sched = list_claire_schedules(tgt)
    plan["writers"] = {"open_handles": holders, "schedules": sched}
    if holders["pids"]:
        blockers.append({"code": "uncontrolled_writer", "message": f"다른 프로세스가 장부를 열고 있습니다: {holders['processes']}"})
    if not w["writes"]:
        plan["mode"] = "revert"
        plan["steps"] = [f"옛 장부 지문 확인({kept.name})", f"{tgt} 은퇴(claire.db.rolled-back-<id> + 은퇴 표지)",
                         f"{src} 의 옛 장부를 원래 이름으로", "위치 기록·registry 를 옛 위치로", "환경변수 없이 위치·무결성 확인"]
    else:
        old = sqlite3.connect(f"file:{kept.as_posix()}?immutable=1", uri=True)
        new = claire_data.open_ro(tgt / DB_NAME)
        try:
            diff = claire_data.change_plan(old, new)
        finally:
            old.close()
            new.close()
        plan["mode"] = "carry-forward"
        plan["changes"] = diff
        plan["steps"] = [f"새 데이터 먼저 백업 ({opts_kw.get('backup_root') or '백업 루트'}/<id>)",
                         "옛 장부 대비 변화 검증 (사라지면 안 되는 행이 없는지)",
                         f"현재 데이터 전체를 {src} 로 옮김 (같은 이전 절차·검증)",
                         f"옛 위치 폴더는 {src}.retired-<id> 로 비켜 둠 (지우지 않음)", f"{tgt} 은퇴"]
        blockers += diff["blockers"]
    plan["blockers"] = blockers
    if not confirm:
        plan["next"] = None if blockers else f"claire_relocate rollback --id {j.id} --confirm"
        return plan
    if blockers:
        raise _err("rollback_blocked", "되돌릴 수 없습니다: " + "; ".join(b["message"] for b in blockers), blockers=blockers)
    if plan["mode"] == "carry-forward":
        opts = default_options(cfg, to=str(src), replace_retired=True, kind="rollback-move", parent=j.id, **opts_kw)
        res = migrate(cfg, opts)
        res["mode"] = "carry-forward"
        res["changes"] = plan["changes"]
        return res
    opts = default_options(cfg, to=str(src), kind="rollback-revert", parent=j.id, **opts_kw)
    return _start_revert(cfg, j, opts)


def _start_revert(cfg, parent: Journal, opts: Options) -> dict:
    src, tgt = Path(parent.data["target"]["root"]), Path(parent.data["source"]["root"])
    rid = new_id(opts.backup_root)
    j = Journal({"format": 1, "id": rid, "kind": "rollback-revert", "parent": parent.id, "system": claire_paths.SYSTEM,
                 "claire_version": CLAIRE_VERSION, "state": "running", "phase": "planned", "history": [],
                 "started_at": _now(), "source": {"root": str(src), "db": str(src / DB_NAME)},
                 "target": {"root": str(tgt), "db": str(tgt / DB_NAME), "prior_state": "retired"},
                 "staging": None, "backup_root": str(opts.backup_root), "backup_dir": None,
                 "registry": str(opts.registry) if opts.registry else None,
                 "options": {"quiet_minutes": opts.quiet_minutes, "pause_schedules": opts.pause_schedules}})
    lock = claire_paths.RelocationLock()
    j.save()
    try:
        lock.acquire(_lock_info(j))
    except claire_paths.LocationError as e:
        j.data.update(state="not_started", error={"code": e.code, "message": e.message})
        j.save()
        raise _err(e.code, e.message, fix=e.fix)
    j.mark("planned")
    res = _drive(Ctx(cfg, j, lock, opts))
    res["mode"] = "revert"
    return res


# --------------------------------------------------------------------------
# 사람이 읽는 요약
# --------------------------------------------------------------------------

def render_plan(p: dict) -> str:
    L = []
    src, tgt = p["source"], p["target"]
    L.append(f"Claire 데이터 이전 계획 (dry-run — 아무것도 바꾸지 않음)")
    L.append(f"  원본  {src['path']}  [{src.get('how_resolved')}]")
    L.append(f"  대상  {tgt['path']}  (지금: {tgt['state']}) · 임시 폴더 {tgt['staging']}")
    if src.get("rows"):
        r = src["rows"]
        L.append(f"  장부  {_mb(src.get('db_bytes', 0))}MB · WAL {src.get('wal_bytes', 0)}B · 스키마 v{src.get('schema_version')} · "
                 f"업무 {r.get('item')} · 원문 {r.get('source_event')} · 첨부 {r.get('attachment')} · 이력 {r.get('activity_log')} · 메모 {r.get('memo')}")
    ct = p["inputs"]["copy_total"]
    L.append(f"  파일  {ct['files']}개 {_mb(ct['bytes'])}MB: " + ", ".join(f"{g['path']} {g['files']}" for g in p["inputs"]["copy"]))
    L.append(f"  인증정보  {p['inputs']['secrets']['files']}개 — 보호 복사, 백업 제외, 값 출력 없음")
    for e in p["inputs"]["excluded"]:
        L.append(f"  옮기지 않음  {e['path']} ({e['files']}개 {_mb(e['bytes'])}MB) — {e['why']}")
    L.append(f"  백업  {p['backup']['dir']} (secrets 제외) · 복원: {p['backup']['restore']}")
    sp = p["space"]
    L.append(f"  공간  필요 {_mb(sp['target_needs'] + sp['backup_needs'])}MB / 남음 {_mb(sp['target_free'])}MB → {'충분' if sp['ok'] else '부족'}")
    w = p["writers"]
    L.append(f"  쓰기 주체  장부를 연 프로세스: {w['open_handles']['pids'] or '없음'} ({w['open_handles']['method']}) · "
             f"진행 중 실행: {len(w['running_runs']['fresh'])}건")
    jobs = w["schedules"].get("jobs") or []
    L.append(f"  예약  " + (", ".join(f"{j['name']}({'켜짐' if j['enabled'] else '꺼짐'})" for j in jobs) or w["schedules"].get("reason", "없음"))
             + f" — {w['schedule_action']}")
    L.append("  중단 범위  " + " / ".join(p["stop_scope"]))
    ap = p.get("absolute_paths") or {}
    L.append("  절대경로 참조  " + ", ".join(f"{k}: {v['absolute']}/{v['rows']}" for k, v in ap.items()) + " (데이터 폴더 안을 가리키는 것만 고침)")
    hc = p["historical_refs"]["count"]
    if hc:
        L.append(f"  과거 기록의 옛 경로  {hc} — 바꾸지 않음")
    if p.get("links_to_review"):
        L.append(f"  확인할 사용자 파일·문서 {len(p['links_to_review'])}곳:")
        for x in p["links_to_review"][:15]:
            L.append(f"    - [{x['where']}] {x['file']} {x['lines'] or ''} → {x['action']}")
    reg = p["registry"]
    L.append(f"  registry  {reg.get('path')} — {reg.get('action')}")
    for wn in p["warnings"]:
        L.append(f"  ⚠️  {wn['message']}")
    for b in p["blockers"]:
        L.append(f"  ❌ {b['message']}" + (f"  ↳ {b['fix']}" if b.get("fix") else ""))
    L.append(f"  → {'준비됨: ' + p['next'] if p['ready'] else '지금은 이전할 수 없음'}")
    return "\n".join(L)


def render_status(s: dict) -> str:
    L = [f"데이터 위치: {s['location']['data_dir']} [{s['location']['source_label']}]"
         + ("" if s["location"]["usable"] else " — 사용 불가: " + "; ".join(b["message"] for b in s["location"]["blockers"]))]
    if s.get("lock"):
        L.append(f"이전 잠금: {s['lock'].get('kind')} {s['lock'].get('id')} ({'실행 중' if s['lock_live'] else '중단됨'})")
    for r in s["relocations"]:
        L.append(f"  {r['id']} {r['kind'] or '?':<16} {r['state'] or '?':<14} {r['phase'] or '?':<14} {r['source']} → {r['target']}"
                 + (f"  ({r['error']})" if r.get("error") else "")
                 + (f"  [되돌리기 문제: {r['undo_error']}]" if r.get("undo_error") else "")
                 + (f"  [경고: {'; '.join(r['warnings'])}]" if r.get("warnings") else ""))
    for n in s["next"]:
        L.append(f"→ {n}")
    return "\n".join(L)
