"""Claire 통합 일정·업무 관리 시스템 — 공용 코어.

PRD.md v1.1 §4.1 "저장·동기화 계층"의 공용 모듈.
Python 3.11+ 표준 라이브러리만 사용한다 (PRD §4.1).

이 모듈은 판단하지 않는다. 설정, 스키마 제약, 트랜잭션, 번호 발급, 시각,
이력 기록, 한국어 검색 토큰만 책임진다. Clio의 clio_core.py에서
DB 초기화·트랜잭션·백업·FTS·어간 처리를 이식했다.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------
# 버전
# --------------------------------------------------------------------------
#
# 스킬 전체의 배포 버전 (SemVer). 동작이나 문서가 바뀌면 반드시 올리고
# CHANGELOG.md에 항목을 남긴다. tests/test_claire.py가 둘의 일치를 검사한다.
CLAIRE_VERSION = "0.4.3"

# 데이터 파일 형식의 버전. 스키마가 바뀌면 올린다.
SCHEMA_VERSION = 1

# --------------------------------------------------------------------------
# 설정 (PRD §12, §15)
# --------------------------------------------------------------------------
#
# 값은 ~/ClaireData/config.json 에서 덮어쓴다. 계정·캘린더·볼트 경로 같은 개인값은 저장소에 두지 않고
# config.json 에만 둔다. 채워야 할 키는 config.example.json 을 본다.

DEFAULT_CONFIG = {
    "timezone": "Asia/Seoul",
    "ref_prefix": "CLR",
    "ref_digits": 4,
    "question_prefix": "Q",
    "backup_mirror": "~/Dropbox/ClaireBackup",
    "backup_retention": {"daily": 7, "weekly": 4, "monthly": 12},

    # D1 — 수집 계정. 비어 있으면 doctor 가 설정을 요구한다. 실제 값은 ~/ClaireData/config.json 에 둔다
    # (저장소에는 개인값을 넣지 않는다. config.example.json 참고).
    # user_addresses 는 "본인 주소" 판정용(To/Cc 확인, §6.1 사전 필터). 포워드되는 주소도 여기 넣는다.
    "gmail": {
        "accounts": [],
        "user_addresses": [],
        "initial_days": 30,
        "initial_query": "newer_than:30d -category:promotions -category:social",
        "resync_days_on_cursor_loss": 7,
        # 최초 수집분 중 이보다 오래된 메일은 해석하지 않고 initial_backlog 로 기록만 한다 (검색·근거용 보존)
        "interpret_days": 7,
        # 결정론적 사전 필터 (§6.1). 실측(30일 1,510통)으로 정한 일반 규칙. 개인별 발신자는 config.json 에.
        "ignore_senders": ["calendar-notification@google.com", "drive-shares-dm-noreply@google.com",
                           "comments-noreply@docs.google.com"],
        "ignore_sender_patterns": [r"^[\w.+-]*no-?reply@([\w-]+\.)*google\.com$",
                                   r"^scholar[\w-]*@google\.com$", r"^mailer-daemon@"],
        "ignore_domains": ["notice.aliexpress.com", "mail.instagram.com", "academia-mail.com",
                           "deliver.ieee.org", "accounts.google.com"],
        # 이 도메인은 List-Unsubscribe 가 있어도 제외하지 않는다 (소속 기관 공지 목록 등)
        "never_ignore_domains": [],
        "list_unsubscribe_rule": "any",   # any | not_personal (PRD 원문 규칙)
    },

    # D3 — 읽기 캘린더 목록. mode: track(항목 생성) | overlay(겹침 확인만). write: True 는 하나만.
    # 예: {"name": "Prof. Hur", "id": "...@group.calendar.google.com", "account": "me@gmail.com", "mode": "track", "write": True}
    "calendars": [],
    "calendar_window": {"past_days": 7, "future_days": 90},

    # D4 — #tasks 줄 전체, 92 Templates·## Routine 제외
    "obsidian": {
        "vault": "~/Documents/Obsidian/Vault",     # 실제 경로는 config.json 에
        "tag": "#tasks",
        "daily_folder": "50 Daily",
        "exclude_folders": ["92 Templates", ".obsidian", ".trash"],
        "exclude_sections": ["## Routine"],
        "custom_status_symbols": {},
        "completed_days": 90,
        "navigation_guide": "Vault_Navigation_Guide.md",
    },

    # D11 — OpenClaw 브리지가 첨부를 로컬 파일로 내려받는 위치
    "discord": {
        "inbound_media_dir": "~/.openclaw/media/inbound",
        "reference_prefixes": ["참고:"],
        "register_prefixes": ["등록:", "일정:", "할일:"],
    },

    # §4.3 — gog CLI의 OAuth 클라이언트를 재사용하되 토큰은 읽기 전용 범위로 따로 발급
    "google": {
        "oauth_client_file": "~/Library/Application Support/gogcli/credentials.json",
        "read_scopes": [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/calendar.readonly",
        ],
        "write_scopes": [
            "https://www.googleapis.com/auth/calendar.events",
            "https://www.googleapis.com/auth/userinfo.email",   # 발급 직후 계정 확인용 (쓰기 권한 아님)
        ],
        "timeout_seconds": 30,
    },

    # §8 — 실행 관리
    "run": {
        "daily_hour": 6,
        "missed_hours": 26,
        "stale_run_minutes": 180,
        "required_steps": ["sync", "propose", "brief"],
    },

    # D9 — 표시 임계
    "briefing": {"imminent_days": 3, "travel_minutes": 30, "section_limit": 7},

    # D6 — 후속·재질문
    "followup": {"waiting_business_days": 3, "reask_daily_days": 2,
                 "reask_weekday": "mon", "reask_urgent_hours": 48,
                 "question_expire_days_after_due": 7},

    # 4단계 외부 반영 (D5): 지시·답변 확정 항목은 자동, 메일·이미지 발견 항목의 Calendar 등록은 승인
    "apply": {
        "enabled": True,
        "calendar_create_requires_confirm_for_discovered": 1,
        "obsidian_add_task": "confirm_unless_directed",   # always | confirm_unless_directed | never
        "daily_for_due_date": True,                        # Daily 줄을 마감일 Daily 에 (False 면 오늘 Daily)
        "max_attempts": 5,
        "backoff_minutes": [1, 5, 15, 60, 240],
        "calendar_reminder_minutes": 30,
    },

    # §7.1, §9
    "min_confidence": 0.4,
    "auto_merge_threshold": 1.01,
    "default_list_limit": 20,
    "recency_halflife_days": 30,
}

ITEM_KINDS = ["task", "meeting", "deadline", "reply", "prep", "waiting"]
ITEM_STATUSES = ["captured", "needs_info", "todo", "in_progress", "waiting",
                 "on_hold", "done", "cancelled"]
OPEN_STATUSES = ["captured", "needs_info", "todo", "in_progress", "waiting", "on_hold"]
PRIORITIES = ["high", "normal", "low"]
OWNERS = ["user", "claire", "other"]
PLATFORMS = ["gmail", "calendar", "obsidian", "discord", "user"]
TRIAGES = ["new", "proposed", "ignored", "superseded", "error"]
SOURCE_ROLES = ["origin", "update", "cancel", "evidence", "mention"]
REL_TYPES = ["prep_for", "subtask_of", "blocks", "follow_up_of", "related", "same_thread"]
LINK_SYSTEMS = ["calendar", "obsidian_task", "obsidian_note", "ledger"]
RUN_KINDS = ["daily", "on_demand", "source_only", "apply", "backup"]
RUN_STATUSES = ["running", "ok", "partial", "failed", "aborted"]
ACTORS = ["user", "claire", "sync", "apply"]
# §5.3 — done 전이의 근거 코드. 그 외 값은 거부한다.
EVIDENCE_CODES = ["user_report", "tasks_checked"]
EVIDENCE_CRITERIA_RE = re.compile(r"^criteria_evidence:(\d+)$")


def data_dir() -> Path:
    return Path(os.environ.get("CLAIRE_DATA_DIR", "~/ClaireData")).expanduser()


def _merge(defaults: dict, user: dict) -> dict:
    out = json.loads(json.dumps(defaults))
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    path = data_dir() / "config.json"
    if path.exists():
        user = json.loads(path.read_text(encoding="utf-8"))
        return _merge(DEFAULT_CONFIG, user)
    return json.loads(json.dumps(DEFAULT_CONFIG))


class ClaireError(Exception):
    """도구 계층의 모든 실패. 종료 코드 1과 함께 JSON으로 보고된다."""

    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


# --------------------------------------------------------------------------
# 시각 (PRD §9 — ISO8601, 초 단위, 오프셋 포함)
# --------------------------------------------------------------------------

def tz_of(cfg: dict) -> ZoneInfo:
    return ZoneInfo(cfg.get("timezone", "Asia/Seoul"))


def now_dt(cfg: dict) -> datetime:
    override = os.environ.get("CLAIRE_NOW")  # 테스트용 고정 시각
    if override:
        dt = datetime.fromisoformat(override)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz_of(cfg))
        return dt.replace(microsecond=0)
    return datetime.now(tz_of(cfg)).replace(microsecond=0)


def now_iso(cfg: dict) -> str:
    return now_dt(cfg).isoformat()


def day_of(ts: str) -> str:
    return ts[:10]


def parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def hours_between(a: str, b: str) -> float:
    return (parse_iso(b) - parse_iso(a)).total_seconds() / 3600.0


# --------------------------------------------------------------------------
# 스키마 (PRD §5.2) — 이 문자열이 DDL의 정본이다.
# --------------------------------------------------------------------------

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- 관리 항목: 현재 상태만 보유. 이력은 activity_log.
CREATE TABLE IF NOT EXISTS item (
  id             INTEGER PRIMARY KEY,
  ref            TEXT UNIQUE NOT NULL,
  title          TEXT NOT NULL,
  kind           TEXT NOT NULL CHECK (kind IN
                 ('task','meeting','deadline','reply','prep','waiting')),
  status         TEXT NOT NULL DEFAULT 'captured' CHECK (status IN
                 ('captured','needs_info','todo','in_progress','waiting','on_hold','done','cancelled')),
  priority       TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('high','normal','low')),
  project        TEXT,
  owner          TEXT NOT NULL DEFAULT 'user' CHECK (owner IN ('user','claire','other')),
  next_action    TEXT,
  done_criteria  TEXT,
  start_at       TEXT,
  end_at         TEXT,
  all_day        INTEGER NOT NULL DEFAULT 0,
  due_at         TEXT,
  scheduled_on   TEXT,
  tz             TEXT NOT NULL DEFAULT 'Asia/Seoul',
  tentative      INTEGER NOT NULL DEFAULT 1,
  confidence     REAL,
  waiting_on     TEXT,
  next_check_at  TEXT,
  blocked_reason TEXT,
  series_key     TEXT,
  occurrence_at  TEXT,
  canonical_note TEXT,
  ledger_note    TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  completed_at   TEXT,
  cancelled_at   TEXT,
  deleted_at     TEXT
);
CREATE INDEX IF NOT EXISTS ix_item_status ON item(status);
CREATE INDEX IF NOT EXISTS ix_item_due ON item(due_at);
CREATE INDEX IF NOT EXISTS ix_item_start ON item(start_at);
CREATE INDEX IF NOT EXISTS ix_item_series ON item(series_key);

-- 출처 이벤트: 수집된 원문 단위. 미처리 큐이자 근거.
CREATE TABLE IF NOT EXISTS source_event (
  id             INTEGER PRIMARY KEY,
  platform       TEXT NOT NULL CHECK (platform IN ('gmail','calendar','obsidian','discord','user')),
  account        TEXT,
  external_id    TEXT,
  thread_id      TEXT,
  version        TEXT,
  event_ts       TEXT NOT NULL,
  ingested_at    TEXT NOT NULL,
  fingerprint    TEXT NOT NULL,
  headers        TEXT,
  excerpt        TEXT,
  raw_path       TEXT,
  triage         TEXT NOT NULL DEFAULT 'new' CHECK (triage IN
                 ('new','proposed','ignored','superseded','error')),
  ignore_reason  TEXT,
  redacted_at    TEXT,
  UNIQUE (platform, account, external_id, version)
);
CREATE INDEX IF NOT EXISTS ix_source_triage ON source_event(triage);
CREATE INDEX IF NOT EXISTS ix_source_thread ON source_event(platform, thread_id);
CREATE INDEX IF NOT EXISTS ix_source_fp ON source_event(fingerprint);

CREATE TABLE IF NOT EXISTS item_source (
  item_id          INTEGER NOT NULL REFERENCES item(id),
  source_event_id  INTEGER NOT NULL REFERENCES source_event(id),
  role             TEXT NOT NULL CHECK (role IN ('origin','update','cancel','evidence','mention')),
  PRIMARY KEY (item_id, source_event_id, role)
);

-- Discord 이미지 등 첨부 (Clio D6과 동일)
CREATE TABLE IF NOT EXISTS attachment (
  id              INTEGER PRIMARY KEY,
  source_event_id INTEGER NOT NULL REFERENCES source_event(id),
  filename        TEXT NOT NULL,
  mime_type       TEXT,
  byte_size       INTEGER,
  sha256          TEXT NOT NULL,
  stored_path     TEXT NOT NULL,
  source_url      TEXT,
  fetched_at      TEXT NOT NULL,
  redacted_at     TEXT,
  UNIQUE (source_event_id, sha256)
);
CREATE INDEX IF NOT EXISTS ix_attachment_sha ON attachment(sha256);

-- 확인 질문
CREATE TABLE IF NOT EXISTS question (
  id             INTEGER PRIMARY KEY,
  ref            TEXT UNIQUE NOT NULL,
  item_id        INTEGER REFERENCES item(id),
  field          TEXT,
  question       TEXT NOT NULL,
  options        TEXT,
  reason         TEXT,
  status         TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','answered','withdrawn','expired')),
  asked_count    INTEGER NOT NULL DEFAULT 0,
  first_asked_at TEXT,
  last_asked_at  TEXT,
  next_ask_at    TEXT,
  answered_at    TEXT,
  answer         TEXT,
  resolution     TEXT,
  created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_question_status ON question(status);

-- 실행 기록
CREATE TABLE IF NOT EXISTS run (
  id          INTEGER PRIMARY KEY,
  kind        TEXT NOT NULL CHECK (kind IN ('daily','on_demand','source_only','apply','backup')),
  trigger     TEXT NOT NULL,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  status      TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','ok','partial','failed','aborted')),
  delayed     INTEGER NOT NULL DEFAULT 0,
  steps       TEXT,
  summary     TEXT
);
CREATE INDEX IF NOT EXISTS ix_run_status ON run(status);

-- 변경 이력: 되돌리기와 감사의 근거
CREATE TABLE IF NOT EXISTS activity_log (
  id          INTEGER PRIMARY KEY,
  item_id     INTEGER REFERENCES item(id),
  action      TEXT NOT NULL,
  actor       TEXT NOT NULL CHECK (actor IN ('user','claire','sync','apply')),
  field       TEXT,
  old_value   TEXT,
  new_value   TEXT,
  reason      TEXT,
  evidence_source_event_id INTEGER REFERENCES source_event(id),
  run_id      INTEGER REFERENCES run(id),
  payload     TEXT,
  undone_at   TEXT,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_activity_item ON activity_log(item_id);

-- 항목 관계
CREATE TABLE IF NOT EXISTS relation (
  id           INTEGER PRIMARY KEY,
  from_item_id INTEGER NOT NULL REFERENCES item(id),
  to_item_id   INTEGER NOT NULL REFERENCES item(id),
  rel_type     TEXT NOT NULL CHECK (rel_type IN ('prep_for','subtask_of','blocks','follow_up_of','related','same_thread')),
  created_by   TEXT NOT NULL CHECK (created_by IN ('user','claire','sync')),
  created_at   TEXT NOT NULL,
  CHECK (from_item_id <> to_item_id),
  UNIQUE (from_item_id, to_item_id, rel_type)
);

-- 외부 시스템 연결: 항목 <-> Calendar 이벤트 / Obsidian 줄 / Ledger
CREATE TABLE IF NOT EXISTS link (
  id             INTEGER PRIMARY KEY,
  item_id        INTEGER NOT NULL REFERENCES item(id),
  system         TEXT NOT NULL CHECK (system IN ('calendar','obsidian_task','obsidian_note','ledger')),
  external_key   TEXT NOT NULL,
  version        TEXT,
  last_seen_at   TEXT,
  last_synced_at TEXT,
  sync_status    TEXT NOT NULL DEFAULT 'ok' CHECK (sync_status IN ('ok','pending','conflict','missing')),
  UNIQUE (system, external_key)
);
CREATE INDEX IF NOT EXISTS ix_link_item ON link(item_id);

-- 소스별 증분 커서
CREATE TABLE IF NOT EXISTS sync_state (
  platform         TEXT NOT NULL,
  account          TEXT NOT NULL,
  cursor           TEXT,
  last_success_at  TEXT,
  last_attempt_at  TEXT,
  last_error       TEXT,
  full_sync_needed INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (platform, account)
);

-- 외부 반영·알림 대기열
CREATE TABLE IF NOT EXISTS outbox (
  id               INTEGER PRIMARY KEY,
  op               TEXT NOT NULL,
  item_id          INTEGER REFERENCES item(id),
  payload          TEXT NOT NULL,
  dedupe_key       TEXT UNIQUE NOT NULL,
  requires_confirm INTEGER NOT NULL DEFAULT 0,
  confirmed_at     TEXT,
  not_before       TEXT,
  attempts         INTEGER NOT NULL DEFAULT 0,
  last_error       TEXT,
  status           TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','awaiting_confirm','done','failed','cancelled')),
  result           TEXT,
  created_at       TEXT NOT NULL,
  done_at          TEXT
);
CREATE INDEX IF NOT EXISTS ix_outbox_status ON outbox(status);

-- 브리핑 발송 기록 (중복 발송 방지)
CREATE TABLE IF NOT EXISTS briefing (
  id            INTEGER PRIMARY KEY,
  run_id        INTEGER NOT NULL REFERENCES run(id),
  kind          TEXT NOT NULL,
  content_hash  TEXT NOT NULL,
  item_refs     TEXT,
  question_refs TEXT,
  sent_at       TEXT,
  delivery      TEXT
);

-- 한국어 검색 (Clio의 어간 근사 처리 재사용)
CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
  ref, title, project, next_action, waiting_on, excerpts,
  tokenize='unicode61 remove_diacritics 2'
);
"""

# 백업·복구 대조와 무결성 집계에 쓰는 표 목록 (TC20)
COUNT_TABLES = ["item", "source_event", "item_source", "attachment", "question",
                "activity_log", "relation", "link", "sync_state", "run", "outbox",
                "briefing"]

# 내용 해시에 참여하는 표. 복구 후 "건수·해시 일치"의 해시가 이것이다 (TC20).
HASH_TABLES = ["item", "source_event", "item_source", "question", "activity_log",
               "relation", "link"]

DB_NAME = "claire.db"


def db_path() -> Path:
    return data_dir() / DB_NAME


def connect(create: bool = True) -> sqlite3.Connection:
    d = data_dir()
    db = d / DB_NAME
    if not db.exists() and not create:
        raise ClaireError("db_missing", f"데이터베이스가 없습니다: {db}. "
                          "먼저 claire_check init 을 실행하세요.")
    d.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db, isolation_level=None, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT INTO meta(key,value) VALUES('schema_version',?) "
        "ON CONFLICT(key) DO NOTHING",
        (str(SCHEMA_VERSION),),
    )
    conn.execute(
        "INSERT INTO meta(key,value) VALUES('created_by_version',?) "
        "ON CONFLICT(key) DO NOTHING",
        (CLAIRE_VERSION,),
    )
    return conn


class tx:
    """단일 트랜잭션. 예외가 나면 전부 되돌린다 (부분 저장 금지, §11)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False


# --------------------------------------------------------------------------
# 공용 헬퍼
# --------------------------------------------------------------------------

def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def normalize_text(text: str) -> str:
    """fingerprint용 정규화: NFC, 공백 압축, 소문자. 원문 저장에는 쓰지 않는다."""
    text = unicodedata.normalize("NFC", text or "")
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def fingerprint(text: str) -> str:
    return sha256_text(normalize_text(text))


def content_hash(conn: sqlite3.Connection, tables: list[str] | None = None) -> str:
    """표 내용의 결정론적 해시. 복구 대조(TC20)에 쓴다. rowid 순으로 모든 열을 직렬화한다."""
    h = hashlib.sha256()
    for t in (tables or HASH_TABLES):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
        h.update(f"#{t}:{','.join(cols)}\n".encode("utf-8"))
        order = "rowid" if "id" not in cols else "id"
        for row in conn.execute(f"SELECT {', '.join(cols)} FROM {t} ORDER BY {order}"):
            h.update(json.dumps([row[c] for c in cols], ensure_ascii=False,
                                default=str).encode("utf-8"))
            h.update(b"\n")
    return h.hexdigest()


def table_counts(conn: sqlite3.Connection, tables: list[str] | None = None) -> dict:
    return {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
            for t in (tables or COUNT_TABLES)}


def ref_sort_key(ref: str) -> tuple:
    m = re.fullmatch(r"(.*?)-(\d+)", ref or "")
    if m:
        return (m.group(1), int(m.group(2)), "")
    return (ref or "", -1, ref or "")


def ref_order_sql(col: str = "ref") -> str:
    return f"substr({col}, 1, instr({col}, '-')), length({col}), {col}"


def next_ref(conn: sqlite3.Connection, table: str, prefix: str, digits: int) -> str:
    """번호는 재사용하지 않는다 (§5.2). cancelled·deleted 행이 남으므로 MAX 채번으로 충분하다."""
    top = 0
    for (ref,) in conn.execute(f"SELECT ref FROM {table} WHERE ref LIKE ?", (prefix + "-%",)):
        m = re.fullmatch(re.escape(prefix) + r"-(\d+)", ref)
        if m:
            top = max(top, int(m.group(1)))
    return f"{prefix}-{top + 1:0{digits}d}"


def next_item_ref(conn, cfg) -> str:
    return next_ref(conn, "item", cfg["ref_prefix"], cfg["ref_digits"])


def next_question_ref(conn, cfg) -> str:
    return next_ref(conn, "question", cfg["question_prefix"], cfg["ref_digits"])


def get_item(conn: sqlite3.Connection, ref: str, allow_deleted: bool = False) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM item WHERE ref = ?", (ref.upper(),)).fetchone()
    if row is None:
        raise ClaireError("item_not_found", f"항목을 찾을 수 없습니다: {ref}")
    if row["deleted_at"] and not allow_deleted:
        raise ClaireError("item_deleted", f"삭제된 항목입니다: {ref}")
    return row


def get_question(conn: sqlite3.Connection, ref: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM question WHERE ref = ?", (ref.upper(),)).fetchone()
    if row is None:
        raise ClaireError("question_not_found", f"질문을 찾을 수 없습니다: {ref}")
    return row


def get_run(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise ClaireError("run_not_found", f"실행 기록을 찾을 수 없습니다: {run_id}")
    return row


def log_activity(conn, *, item_id, action, actor, ts, field=None, old_value=None,
                 new_value=None, reason=None, evidence_source_event_id=None,
                 run_id=None, payload=None) -> int:
    if actor not in ACTORS:
        raise ClaireError("bad_actor", f"actor 값이 잘못되었습니다: {actor}")
    cur = conn.execute(
        "INSERT INTO activity_log(item_id,action,actor,field,old_value,new_value,reason,"
        "evidence_source_event_id,run_id,payload,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (item_id, action, actor, field,
         None if old_value is None else str(old_value),
         None if new_value is None else str(new_value),
         reason, evidence_source_event_id, run_id,
         None if payload is None else json.dumps(payload, ensure_ascii=False), ts),
    )
    return cur.lastrowid


def validate_evidence(code: str) -> str:
    """§5.3 — done 전이 근거 코드 검증. 셋 중 하나가 아니면 거부한다."""
    if code in EVIDENCE_CODES or EVIDENCE_CRITERIA_RE.match(code or ""):
        return code
    raise ClaireError("bad_evidence",
                      "완료 근거 코드가 잘못되었습니다. user_report | tasks_checked | "
                      "criteria_evidence:<source_event_id> 중 하나여야 합니다.",
                      given=code)


def sync_state_get(conn, platform: str, account: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM sync_state WHERE platform=? AND account=?",
                        (platform, account)).fetchone()


def sync_state_upsert(conn, platform: str, account: str, **fields) -> None:
    row = sync_state_get(conn, platform, account)
    if row is None:
        conn.execute("INSERT INTO sync_state(platform,account) VALUES(?,?)", (platform, account))
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE sync_state SET {sets} WHERE platform=? AND account=?",
                     (*fields.values(), platform, account))


# --------------------------------------------------------------------------
# 검색 인덱스 (§5.2 search_fts) — 항목 단위로 재색인한다
# --------------------------------------------------------------------------

def reindex_item(conn, item_id: int) -> None:
    item = conn.execute("SELECT * FROM item WHERE id = ?", (item_id,)).fetchone()
    conn.execute("DELETE FROM search_fts WHERE rowid = ?", (item_id,))
    if item is None or item["deleted_at"]:
        return
    excerpts = " ".join(
        r["excerpt"] or ""
        for r in conn.execute(
            "SELECT s.excerpt FROM item_source i JOIN source_event s ON s.id = i.source_event_id "
            "WHERE i.item_id = ? AND s.redacted_at IS NULL", (item_id,))
    )
    conn.execute(
        "INSERT INTO search_fts(rowid, ref, title, project, next_action, waiting_on, excerpts) "
        "VALUES(?,?,?,?,?,?,?)",
        (item_id, item["ref"], item["title"], item["project"] or "", item["next_action"] or "",
         item["waiting_on"] or "", excerpts),
    )


def rebuild_fts(conn) -> int:
    conn.execute("DELETE FROM search_fts")
    n = 0
    for (iid,) in conn.execute("SELECT id FROM item WHERE deleted_at IS NULL"):
        reindex_item(conn, iid)
        n += 1
    return n


# --------------------------------------------------------------------------
# 한국어 토큰·어간 (Clio §7.1 이식)
# --------------------------------------------------------------------------

_PUNCT = re.compile(r"[^\w가-힣]+", re.UNICODE)
_HANGUL = re.compile(r"[가-힣]")

_PARTICLES = sorted([
    "에서는", "에게서", "이라는", "라는", "으로는", "에서", "에게", "으로", "부터",
    "까지", "처럼", "보다", "마다", "조차", "이다", "한다", "하는", "하고", "했다",
    "이란", "라도", "이나", "든지", "지만", "면서", "이며", "으며", "에는", "에도",
    "은", "는", "이", "가", "을", "를", "에", "의", "와", "과", "도", "만", "로",
    "며", "고", "다", "인", "된", "될", "한", "할",
], key=len, reverse=True)


def tokenize(text: str) -> list[str]:
    text = unicodedata.normalize("NFC", text or "")
    parts = [p for p in _PUNCT.split(text) if p]
    return [p for p in parts if len(p) >= 2]


def stems(token: str) -> set[str]:
    out = {token}
    if _HANGUL.search(token):
        for suf in _PARTICLES:
            if len(token) > len(suf) + 1 and token.endswith(suf):
                out.add(token[: -len(suf)])
                break
        if len(token) >= 3:
            out.add(token[:-1])
            out.add(token[:2])
    return {s for s in out if len(s) >= 2}


def hits(token: str, haystack: str) -> bool:
    return any(s in haystack for s in stems(token))


def fts_query(tokens: list[str]) -> str:
    terms = []
    for t in tokens:
        stem = min(stems(t), key=len).replace('"', "")
        if stem:
            terms.append(f'"{stem}"*')
    return " OR ".join(dict.fromkeys(terms))


# --------------------------------------------------------------------------
# 파일 쓰기 헬퍼 — 원자적 교체 (§6.3, §10)
# --------------------------------------------------------------------------

def atomic_write_text(path: Path, text: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def write_secret_json(path: Path, payload: dict) -> None:
    """secrets/ 아래 파일은 항상 600, 디렉터리는 700."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n", mode=0o600)


def is_synced_location(path: Path) -> str | None:
    """Dropbox·iCloud 등 동기화 폴더 안이면 그 이름을 돌려준다 (§4.2)."""
    parts = [p.lower() for p in Path(path).expanduser().resolve().parts]
    # 경로 구성 요소가 정확히 일치할 때만 (이름에 'Dropbox'가 들어간 다른 폴더는 오탐하지 않는다)
    for marker, name in (("dropbox", "Dropbox"), ("mobile documents", "iCloud"),
                         ("onedrive", "OneDrive"), ("google drive", "Google Drive")):
        if any(p == marker or p.startswith(marker + " (") for p in parts):
            return name
    return None


# --------------------------------------------------------------------------
# CLI 공통 (§11 — JSON 출력, 실패 시 종료 코드 1)
# --------------------------------------------------------------------------

def emit(payload: dict) -> None:
    payload.setdefault("ok", True)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def run_cli(build_parser, dispatch) -> None:
    parser = build_parser()
    parser.add_argument("--version", action="version",
                        version=f"claire-schedule-manager {CLAIRE_VERSION}")
    args = parser.parse_args()
    if not getattr(args, "cmd", None):
        parser.print_help()
        sys.exit(2)
    try:
        cfg = load_config()
        result = dispatch(args, cfg)
        if getattr(args, "format", None) in ("text", "llm") and isinstance(result, dict) and "text" in result:
            print(result["text"])
            return
        emit(result if isinstance(result, dict) else {"result": result})
    except ClaireError as e:
        print(json.dumps(
            {"ok": False, "error": {"code": e.code, "message": e.message, **e.extra}},
            ensure_ascii=False, indent=2, default=str))
        sys.exit(1)
    except sqlite3.Error as e:
        print(json.dumps(
            {"ok": False, "error": {"code": "db_error", "message": str(e)}},
            ensure_ascii=False, indent=2))
        sys.exit(1)
    except OSError as e:
        print(json.dumps(
            {"ok": False, "error": {"code": "storage_unavailable", "message": str(e)}},
            ensure_ascii=False, indent=2))
        sys.exit(1)
