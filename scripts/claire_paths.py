"""Claire 데이터 위치 해석 (0.9.0, 이슈 2026-10-24 "SecretaryData 경로 지원·안전한 데이터 이전").

모든 CLI·예약 실행(OpenClaw cron)·install/update 가 이 모듈 하나로 데이터 위치를 정한다.

우선순위
  1. 환경변수 CLAIRE_DATA_DIR — 그 프로세스에만 적용되는 명시 override.
  2. 위치 기록 `~/.claire-schedule-manager/data-location.json` — init·claire_relocate 가 쓴다. 예약 실행이 쓰는 정본.
  3. 기록이 없을 때만 자동 판단:
     - 기존 설치 `~/ClaireData`(장부·설정·secrets 중 하나라도 있음)가 있으면 그곳을 계속 쓴다.
     - 없으면 새 기본 `~/SecretaryData/services/schedule` (신규 설치).
     - 둘 다 장부가 있으면 고르지 않는다(data_conflict). 기본값 변경만으로 빈 DB 를 만들거나 옛 DB 를 버리지 않는다.

기존→신규 전환은 `claire_relocate` 의 명시적 이전 절차로만 한다. 그 절차가 쓰는 표지:
  - `<data>/claire-root.json`  데이터 루트 표지(root_id·상태). 은퇴한 옛 위치는 state=retired, moved_to.
  - `<data>/claire.db` 자리의 은퇴 표지 파일(텍스트) — 0.9.0 이전 코드가 옛 DB 를 열지 못하고 멈추게 한다.
  - `~/.claire-schedule-manager/relocation.lock`  이전 작업 중 표지. 그 동안 Claire 도구는 원본·대상 장부를 열지 않는다.

표준 라이브러리만 쓰고 claire_core 를 import 하지 않는다 (install.sh 가 설치 전에 부른다).
홈 경로는 사용자별로 해석한다(특정 사용자 경로를 코드에 적지 않는다).
"""

from __future__ import annotations

import fcntl
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SYSTEM = "schedule"
DB_NAME = "claire.db"
MARKER_NAME = "claire-root.json"
POINTER_NAME = "data-location.json"
LOCK_NAME = "relocation.lock"
TOMBSTONE_MAGIC = "CLAIRE-SCHEDULE-MANAGER DATA MOVED"

LEGACY_REL = ("ClaireData",)
DEFAULT_REL = ("SecretaryData", "services", "schedule")
BACKUP_REL = ("SecretaryBackups", "schedule")
REGISTRY_REL = ("SecretaryData", "registry.json")
APP_HOME_REL = (".claire-schedule-manager",)

# 은퇴·이전 작업 중 장부를 Claire 도구가 열지 않게 하는 사유 코드 (connect·install.sh 가 멈춘다)
BLOCKING_CODES = ("maintenance", "data_conflict", "pointer_target_missing", "pointer_unreadable",
                  "data_dir_retired", "root_mismatch")


class LocationError(Exception):
    """위치를 정할 수 없거나 지금 쓰면 안 되는 상태. claire_core 가 ClaireError 로 바꿔 보고한다."""

    def __init__(self, code: str, message: str, fix: str | None = None, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.fix = fix
        self.extra = extra


# --------------------------------------------------------------------------
# 경로
# --------------------------------------------------------------------------

def home() -> Path:
    return Path(os.path.expanduser("~"))


def expand(p: str | os.PathLike) -> Path:
    """`~`는 현재 사용자의 홈으로. 상대 경로는 그대로 둔다."""
    return Path(os.path.expanduser(str(p)))


def app_home() -> Path:
    """설치 소스·위치 기록·이전 기록·직전 스킬 사본의 자리 (데이터가 아니라 프로그램 쪽)."""
    env = os.environ.get("CLAIRE_HOME")
    return expand(env) if env else home().joinpath(*APP_HOME_REL)


def legacy_dir() -> Path:
    return home().joinpath(*LEGACY_REL)


def default_dir() -> Path:
    return home().joinpath(*DEFAULT_REL)


def default_backup_root() -> Path:
    return home().joinpath(*BACKUP_REL)


def default_registry() -> Path:
    return home().joinpath(*REGISTRY_REL)


def pointer_path() -> Path:
    return app_home() / POINTER_NAME


def lock_path() -> Path:
    return app_home() / LOCK_NAME


def journal_dir() -> Path:
    return app_home() / "relocations"


def same_path(a, b) -> bool:
    """같은 폴더·파일인가. 둘 다 있으면 samefile(대소문자 무시 파일 시스템·별칭 포함), 아니면 실제 경로 비교."""
    if a is None or b is None:
        return False
    a, b = expand(a), expand(b)
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.realpath(a) == os.path.realpath(b)


def backup_root(cfg_value: str | None = None, override: str | None = None) -> Path:
    """백업 루트: --out > CLAIRE_BACKUP_ROOT > config backup_root > ~/SecretaryBackups/schedule.
    0.9.0 부터 데이터 폴더 안 backups/ 를 쓰지 않는다 (옛 백업은 그 자리에 그대로 둔다)."""
    for v in (override, os.environ.get("CLAIRE_BACKUP_ROOT"), cfg_value):
        if v:
            return expand(v)
    return default_backup_root()


def openclaw_bin() -> str | None:
    """OpenClaw CLI. CLAIRE_OPENCLAW_BIN 이 빈 문자열이면 '없음'(테스트가 실제 예약을 건드리지 않게)."""
    env = os.environ.get("CLAIRE_OPENCLAW_BIN")
    if env is not None:
        return env or None
    import shutil
    return shutil.which("openclaw")


# --------------------------------------------------------------------------
# 파일 표지
# --------------------------------------------------------------------------

def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: Path) -> dict | None:
    """없으면 None. 깨졌으면 ValueError."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"JSON 객체가 아닙니다: {path}")
    return data


def write_json_atomic(path: Path, data: dict, mode: int = 0o600) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def is_tombstone(db: Path) -> bool:
    try:
        with open(db, "rb") as fh:
            head = fh.read(len(TOMBSTONE_MAGIC))
    except OSError:
        return False
    return head == TOMBSTONE_MAGIC.encode()


def tombstone_text(moved_to: str, relocation_id: str, at: str) -> str:
    return (f"{TOMBSTONE_MAGIC} — this file is not a database.\n"
            f"이 폴더는 은퇴한 Claire 데이터 위치입니다. 현재 데이터: {moved_to}\n"
            f"relocation: {relocation_id}\nat: {at}\n"
            "0.9.0 이전 Claire 코드는 여기서 의도적으로 멈춥니다. 확인: claire_relocate status\n")


def read_marker(d: Path) -> dict | None:
    try:
        return read_json(Path(d) / MARKER_NAME)
    except (ValueError, OSError):
        return {"state": "unreadable"}


def write_marker(d: Path, data: dict) -> None:
    write_json_atomic(Path(d) / MARKER_NAME, data)


def new_root_id() -> str:
    return str(uuid.uuid4())


def ensure_marker(d: Path, root_id: str | None = None) -> dict:
    """데이터 루트 표지가 없으면 만든다 (init). 있으면 그대로."""
    m = read_marker(d)
    if m and m.get("state") != "unreadable":
        return m
    m = {"format": 1, "system": SYSTEM, "root_id": root_id or new_root_id(), "state": "active",
         "created_at": now_utc()}
    write_marker(d, m)
    return m


def install_present(d: Path) -> bool:
    """그 위치에 '살아 있는' 기존 설치가 있는가 (장부·설정·secrets 중 하나)."""
    d = Path(d)
    m = read_marker(d)
    if m and m.get("state") == "retired":
        return False
    db = d / DB_NAME
    if db.is_file():
        return not is_tombstone(db)
    return (d / "config.json").is_file() or (d / "secrets").is_dir()


def has_db(d: Path) -> bool:
    db = Path(d) / DB_NAME
    return db.is_file() and not is_tombstone(db)


def read_pointer() -> dict | None:
    return read_json(pointer_path())


def write_pointer(data_dir: Path, root_id: str | None, reason: str, relocation_id: str | None = None) -> dict:
    """위치 기록을 원자적으로 바꾼다. 직전 값 몇 개를 history 에 남긴다."""
    try:
        old = read_pointer() or {}
    except ValueError:
        old = {}
    hist = list(old.get("history") or [])
    if old.get("data_dir"):
        hist.append({k: old.get(k) for k in ("data_dir", "root_id", "reason", "relocation_id", "updated_at")})
    data = {"format": 1, "system": SYSTEM, "data_dir": str(Path(data_dir)), "root_id": root_id,
            "reason": reason, "relocation_id": relocation_id, "updated_at": now_utc(), "history": hist[-10:]}
    app_home().mkdir(parents=True, exist_ok=True)
    write_json_atomic(pointer_path(), data)
    clear_cache()
    return data


# --------------------------------------------------------------------------
# 이전 작업 잠금
# --------------------------------------------------------------------------

def read_lock() -> dict | None:
    try:
        return read_json(lock_path())
    except ValueError:
        return {"id": None, "unreadable": True}


def lock_is_live(path: Path | None = None) -> bool:
    """잠금 파일을 지금 어떤 프로세스가 쥐고 있는가 (flock). 쥐고 있지 않으면 중단된 작업이다."""
    p = path or lock_path()
    try:
        fd = os.open(p, os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


class RelocationLock:
    """이전 작업 잠금. 파일(상태 표지) + flock(지금 실행 중인지). 같은 id 로만 다시 잡을 수 있다(resume·abort)."""

    def __init__(self):
        self.fd = None
        self.info = None

    def _open_locked(self) -> int:
        """잠금 파일을 열고 flock. 여는 사이 다른 작업이 파일을 지우고 새로 만들었으면(inode 다름) 다시 연다."""
        p = lock_path()
        for _ in range(20):
            fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                cur = read_lock() or {}
                raise LocationError("relocation_running",
                                    f"다른 이전 작업이 지금 실행 중입니다 ({cur.get('kind')} {cur.get('id')}, pid {cur.get('pid')}).",
                                    "끝날 때까지 기다린 뒤 claire_relocate status")
            try:
                same = os.fstat(fd).st_ino == os.stat(p).st_ino
            except FileNotFoundError:
                same = False
            if same:
                return fd
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        raise LocationError("relocation_running", "이전 잠금 파일이 계속 바뀝니다 — 다른 작업이 실행 중인 것으로 봅니다",
                            "잠시 뒤 claire_relocate status")

    def acquire(self, info: dict) -> "RelocationLock":
        app_home().mkdir(parents=True, exist_ok=True)
        fd = self._open_locked()
        raw = os.read(fd, 1 << 20)
        cur = None
        if raw.strip():
            try:
                cur = json.loads(raw)
            except ValueError:
                cur = {"id": None}
        if cur and cur.get("id") != info.get("id"):
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            raise LocationError("relocation_interrupted",
                                f"중단된 이전 작업이 있습니다 ({cur.get('kind')} {cur.get('id')}). 새 작업을 시작하지 않습니다.",
                                f"claire_relocate status → claire_relocate resume --id {cur.get('id')} 또는 abort --id {cur.get('id')}")
        info = dict(info, pid=os.getpid(), locked_at=now_utc())
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, (json.dumps(info, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        os.fsync(fd)
        self.fd, self.info = fd, info
        clear_cache()
        return self

    def update(self, **fields) -> None:
        if self.fd is None:
            return
        self.info.update(fields)
        os.lseek(self.fd, 0, os.SEEK_SET)
        os.ftruncate(self.fd, 0)
        os.write(self.fd, (json.dumps(self.info, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        os.fsync(self.fd)

    def release(self, remove: bool = True) -> None:
        """remove=False 면 표지를 남긴다(실패 후 사람이 봐야 하는 상태). 잠금(flock)만 푼다."""
        if self.fd is None:
            return
        if remove:
            try:
                os.unlink(lock_path())
            except FileNotFoundError:
                pass
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)
        self.fd = None
        clear_cache()


# --------------------------------------------------------------------------
# 위치 해석
# --------------------------------------------------------------------------

SOURCE_LABELS = {
    "env": "환경변수 CLAIRE_DATA_DIR (이 프로세스만)",
    "pointer": "위치 기록 data-location.json",
    "legacy": "기존 설치 위치 (~/ClaireData)",
    "default": "새 기본 위치 (~/SecretaryData/services/schedule)",
    "moved": "은퇴 표지를 따라 찾은 위치 (위치 기록 없음 — claire_check init 이 다시 남긴다)",
}


def follow_moved(d: Path, depth: int = 5) -> Path | None:
    """은퇴한 위치의 표지(moved_to)를 따라 지금 정본을 찾는다.
    따라간 곳의 표지가 active 이고 같은 이전(relocation_id)으로 들어온 장부일 때만 인정한다 (최대 depth 번)."""
    cur = Path(d)
    for _ in range(depth):
        m = read_marker(cur) if cur.exists() else None
        if not m or m.get("state") != "retired" or not m.get("moved_to"):
            return None
        nxt = expand(m["moved_to"])
        nm = read_marker(nxt) if nxt.exists() else None
        if not nm or nm.get("relocation_id") != m.get("relocation_id"):
            return None
        if nm.get("state") == "active" and has_db(nxt):
            return nxt
        cur = nxt
    return None


def relocation_history() -> list[str]:
    """이 사용자에게 위치 이전 흔적이 있는가 (있으면 위치 기록 없이 빈 장부를 새로 만들지 않는다)."""
    out = []
    jd = journal_dir()
    if jd.is_dir() and any(p.suffix == ".json" for p in jd.iterdir()):
        out.append(f"이전 기록 {jd}")
    for d in (legacy_dir(), default_dir()):
        m = read_marker(d) if d.exists() else None
        if m and m.get("state") == "retired":
            out.append(f"은퇴 표지 {d}")
        elif is_tombstone(d / DB_NAME):
            out.append(f"은퇴 표지 {d / DB_NAME}")
    return out


@dataclass
class Location:
    path: Path
    source: str
    blockers: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    pointer: dict | None = None
    marker: dict | None = None
    lock: dict | None = None

    def as_dict(self) -> dict:
        return {"data_dir": str(self.path), "source": self.source, "source_label": SOURCE_LABELS.get(self.source),
                "blockers": self.blockers, "warnings": self.warnings, "pointer": _pointer_summary(self.pointer),
                "root_id": (self.marker or {}).get("root_id"), "lock": self.lock,
                "usable": not self.blockers}


def _pointer_summary(p: dict | None) -> dict | None:
    if not p:
        return None
    return {k: p.get(k) for k in ("data_dir", "root_id", "reason", "relocation_id", "updated_at")}


def resolve(use_env: bool = True) -> Location:
    """지금 쓸 데이터 위치와, 쓰면 안 되는 이유(blockers)·주의(warnings). 아무것도 바꾸지 않는다.
    use_env=False 는 '예약 실행이 보는 정본'(환경변수 없이)을 구한다."""
    blockers, warnings = [], []

    def block(code, message, fix=None, **extra):
        blockers.append({"code": code, "message": message, "fix": fix, **extra})

    def warn(code, message, fix=None, **extra):
        warnings.append({"code": code, "message": message, "fix": fix, **extra})

    env = os.environ.get("CLAIRE_DATA_DIR") if use_env else None
    try:
        ptr = read_pointer()
    except (ValueError, OSError) as e:
        ptr = None
        block("pointer_unreadable", f"위치 기록을 읽지 못했습니다: {pointer_path()} ({e})",
              "claire_relocate status 로 확인한 뒤 파일을 고치거나 지우세요 (지우면 자동 판단으로 돌아갑니다)")

    if env:
        path, source = expand(env), "env"
        if ptr and ptr.get("data_dir") and not same_path(ptr["data_dir"], path):
            warn("env_override", f"CLAIRE_DATA_DIR({path})가 위치 기록({ptr['data_dir']})과 다릅니다. "
                                 "예약 실행(OpenClaw cron)은 환경변수 없이 위치 기록을 씁니다.",
                 "의도한 격리 사본이 아니면 CLAIRE_DATA_DIR 을 지우세요")
    elif ptr and ptr.get("data_dir"):
        path, source = expand(ptr["data_dir"]), "pointer"
        if not (path / DB_NAME).exists():
            block("pointer_target_missing", f"위치 기록이 가리키는 장부가 없습니다: {path / DB_NAME}. 빈 장부를 새로 만들지 않습니다.",
                  "디스크 연결·경로를 확인하세요. 되돌리려면 claire_relocate status")
    else:
        legacy, new = legacy_dir(), default_dir()
        legacy_on, new_on = install_present(legacy), has_db(new)
        moved = None if legacy_on else follow_moved(legacy)
        if moved is None and not new_on:
            moved = follow_moved(new)
        if moved is not None and same_path(moved, new):
            moved = None                                       # 기본 위치로 옮긴 경우 — 평소 판단과 같다
        if legacy_on and new_on:
            path, source = legacy, "legacy"
            block("data_conflict", f"기존 위치({legacy})와 새 기본 위치({new}) 모두에 장부가 있고 위치 기록이 없습니다. "
                                   "어느 쪽이 정본인지 추측하지 않습니다.",
                  "claire_relocate plan 으로 두 위치를 비교한 뒤, 정본이 아닌 쪽을 치우거나 위치 기록을 정하세요")
        elif moved is not None and new_on:
            path, source = new, "default"
            block("data_conflict", f"위치 기록이 없고, 옛 위치 표지는 {moved} 로 옮겼다고 하는데 새 기본 위치({new})에도 장부가 있습니다. "
                                   "어느 쪽이 정본인지 추측하지 않습니다.",
                  "claire_relocate status 로 이전 기록을 확인한 뒤 위치 기록을 정하세요")
        elif moved is not None:
            path, source = moved, "moved"
            warn("pointer_missing", f"위치 기록({pointer_path()})이 없어 은퇴 표지를 따라 {moved} 를 씁니다.",
                 "claire_check init 으로 위치 기록을 다시 남기세요")
        elif legacy_on:
            path, source = legacy, "legacy"
        else:
            path, source = new, "default"
            hist = [] if new_on else relocation_history()
            if hist:
                block("pointer_missing_relocated",
                      f"위치 기록이 없고 이전 흔적({'; '.join(hist[:3])})이 있는데 정본 장부를 찾지 못했습니다. "
                      "빈 장부를 새로 만들지 않습니다.",
                      "claire_relocate status 로 이전 기록과 새 위치를 확인하세요 (디스크 연결·경로)")
        if not ptr and source == "legacy" and new.is_dir() and any(x.name != ".DS_Store" for x in new.iterdir()) and not new_on:
            warn("default_dir_not_empty", f"새 기본 위치 {new} 에 파일이 있지만 장부는 없습니다 (이전 준비용?)")

    marker = read_marker(path) if path.exists() else None
    if marker and marker.get("state") == "retired":
        block("data_dir_retired", f"이 위치는 은퇴했습니다: {path} → 현재 데이터 {marker.get('moved_to')} "
                                  f"(이전 {marker.get('relocation_id')})",
              "CLAIRE_DATA_DIR 을 지우거나 새 위치로 바꾸세요. 되돌리기: claire_relocate rollback")
    elif is_tombstone(path / DB_NAME):
        block("data_dir_retired", f"이 위치의 claire.db 는 은퇴 표지입니다: {path}", "claire_relocate status")
    elif marker and marker.get("state") == "unreadable":
        warn("marker_unreadable", f"데이터 루트 표지를 읽지 못했습니다: {path / MARKER_NAME}")
    if source == "pointer" and marker and marker.get("root_id") and ptr.get("root_id") \
            and marker.get("root_id") != ptr.get("root_id") and marker.get("state") != "retired":
        block("root_mismatch", f"위치 기록의 데이터 루트({ptr.get('root_id')})와 {path} 의 표지({marker.get('root_id')})가 다릅니다. "
                               "폴더 내용이 바뀐 것으로 보고 열지 않습니다.", "claire_relocate status")

    lock = read_lock()
    if lock:
        targets = [lock.get(k) for k in ("source", "target", "staging")]
        mine = os.environ.get("CLAIRE_RELOCATION_ID") and os.environ.get("CLAIRE_RELOCATION_ID") == lock.get("id")
        if lock.get("unreadable") or (not mine and any(same_path(path, t) for t in targets if t)):
            live = lock_is_live()
            block("maintenance",
                  ("데이터 이전 작업 중입니다" if live else "데이터 이전 작업이 중단된 상태입니다") +
                  f" ({lock.get('kind')} {lock.get('id')}). 끝나기 전에는 장부를 열지 않습니다.",
                  "claire_relocate status" + ("" if live else f" → resume --id {lock.get('id')} 또는 abort --id {lock.get('id')}"),
                  live=live)
    return Location(path=path, source=source, blockers=blockers, warnings=warnings, pointer=ptr, marker=marker, lock=lock)


_CACHE: dict = {}


def _cache_key() -> tuple:
    return tuple(os.environ.get(k) for k in ("CLAIRE_DATA_DIR", "CLAIRE_HOME", "HOME"))


def resolved_dir() -> Path:
    """data_dir() 의 빠른 경로 (프로세스 안 캐시). 쓰기 가능 여부 판단은 resolve()·require_usable() 로."""
    key = _cache_key()
    if key not in _CACHE:
        _CACHE.clear()
        _CACHE[key] = resolve().path
    return _CACHE[key]


def clear_cache() -> None:
    _CACHE.clear()


def require_usable(create: bool = False) -> Location:
    """장부를 열기 전에 부른다. 이전 작업 중·은퇴·충돌·위치 기록 대상 없음이면 LocationError."""
    loc = resolve()
    if loc.blockers:
        b = loc.blockers[0]
        raise LocationError(b["code"], b["message"], b.get("fix"), data_dir=str(loc.path), blockers=loc.blockers)
    return loc
