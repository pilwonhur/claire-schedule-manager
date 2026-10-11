"""Claire 메모함 — 공용 모듈 (0.8.0, ISSUE 2026-10-11).

메모는 업무(item)가 아니다: 생활·업무에서 잊지 않으려는 지식·생각·질문·중요하지 않은 할 일의 기록이다.
- 아침 보고·업무 현황·진행 확인·누락 감지·Calendar·Tasks·Daily 완료 기록·cron 어디에도 들어가지 않는다(표가 따로다).
- 기한·중요도·재확인일이 없다. 본문에 날짜가 있어도 일정이 되지 않는다.
- 상태: open(열림) · reference(참고 보관) · resolved(해결됨) · not_needed(불필요). 상태 변경은 삭제가 아니다(이력 보관).
- Clio 글감 수집과 다르다. 글감으로 분류하거나 Clio 로 넘기지 않는다.

CLI 는 scripts/claire_memo, 버튼은 claire_buttons memos·actions --memo, Obsidian 반영은 outbox op `obsidian.memo`(claire_apply).
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "connectors"))
from claire_core import (  # noqa: E402
    ClaireError, fts_query, hits, normalize_memo_ref, parse_iso, reindex_memo, tokenize, tz_of,
)

STATUSES = ["open", "reference", "resolved", "not_needed"]
STATUS_KO = {"open": "열림", "reference": "참고 보관", "resolved": "해결됨", "not_needed": "불필요"}
CLOSED = ("resolved", "not_needed")
OP = "obsidian.memo"


def mcfg(cfg: dict) -> dict:
    m = dict(cfg.get("memo") or {})
    m.setdefault("prefix", "MEM")
    m.setdefault("note", "01 Inbox/Claire 메모함.md")
    m.setdefault("list_page", 20)
    m.setdefault("list_messages", 4)
    m.setdefault("title_chars", 40)
    return m


def norm_ref(cfg: dict, ref: str) -> str:
    return normalize_memo_ref(ref, mcfg(cfg)["prefix"], int(cfg.get("ref_digits", 4)))


def get(conn, cfg, ref: str):
    r = norm_ref(cfg, ref)
    row = conn.execute("SELECT * FROM memo WHERE ref=?", (r,)).fetchone()
    if row is None:
        raise ClaireError("memo_not_found", f"메모를 찾을 수 없습니다: {r}")
    return row


def auto_title(cfg: dict, text: str) -> str:
    """제목을 따로 주지 않으면 본문 첫 줄 앞부분. 요약을 지어내지 않는다."""
    first = next((l.strip() for l in (text or "").splitlines() if l.strip()), "")
    first = re.sub(r"^(메모\s*[:：]\s*)", "", first)
    n = int(mcfg(cfg)["title_chars"])
    return first if len(first) <= n else first[:n - 1].rstrip() + "…"


def source_key(platform: str, message_id: str | None, part: int) -> str | None:
    """같은 메시지(·같은 순번)의 재처리를 막는 키. Discord 메시지 id 는 채널과 무관하게 유일하므로 채널을 넣지 않는다
    (채널 인자를 빠뜨리고 다시 처리해도 같은 키)."""
    if not message_id:
        return None
    return f"{platform}:{message_id}:{int(part)}"


def log(conn, memo_id: int, action: str, actor: str, ts: str, *, field=None, old=None, new=None, reason=None,
        message_id=None) -> int:
    return conn.execute(
        "INSERT INTO memo_log(memo_id,action,actor,field,old_value,new_value,reason,source_message_id,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (memo_id, action, actor, field, None if old is None else str(old), None if new is None else str(new),
         reason, message_id, ts)).lastrowid


def already_done(conn, memo_id: int, action: str, message_id: str | None) -> bool:
    """같은 지시 메시지(재전달·재시도)로 같은 동작을 이미 했으면 다시 하지 않는다."""
    if not message_id:
        return False
    return conn.execute("SELECT 1 FROM memo_log WHERE memo_id=? AND action=? AND source_message_id=?",
                        (memo_id, action, message_id)).fetchone() is not None


def enqueue_sync(conn, memo_id: int, ref: str, ts: str, tag) -> None:
    """Obsidian 반영 예약. 이미 대기 중인 반영이 있으면 그것이 현재 내용을 쓰므로 하나 더 만들지 않는다."""
    conn.execute("UPDATE memo SET sync_status=CASE WHEN sync_status='conflict' THEN 'conflict' ELSE 'pending' END "
                 "WHERE id=?", (memo_id,))
    if conn.execute("SELECT 1 FROM outbox WHERE op=? AND status='pending' AND json_extract(payload,'$.memo')=?",
                    (OP, ref)).fetchone():
        return
    conn.execute("INSERT OR IGNORE INTO outbox(op,item_id,payload,dedupe_key,requires_confirm,status,created_at) "
                 "VALUES(?,NULL,?,?,0,'pending',?)",
                 (OP, json.dumps({"memo": ref}, ensure_ascii=False), f"{OP}:{ref}:{tag}", ts))


def touch(conn, memo_id: int, ts: str) -> None:
    conn.execute("UPDATE memo SET updated_at=? WHERE id=?", (ts, memo_id))
    reindex_memo(conn, memo_id)


# --------------------------------------------------------------------------
# 연결 (관련 업무·관련 메모)
# --------------------------------------------------------------------------

def links(conn, memo_id: int) -> dict:
    tasks = [dict(r) for r in conn.execute(
        "SELECT i.ref, i.title, i.status FROM memo_link l JOIN item i ON i.id=l.target_id "
        "WHERE l.memo_id=? AND l.kind='task' ORDER BY l.id", (memo_id,))]
    memos = [dict(r) for r in conn.execute(
        "SELECT m.ref, m.title, m.status FROM memo_link l JOIN memo m ON m.id=l.target_id "
        "WHERE l.memo_id=? AND l.kind='memo' "
        "UNION SELECT m.ref, m.title, m.status FROM memo_link l JOIN memo m ON m.id=l.memo_id "
        "WHERE l.kind='memo' AND l.target_id=?", (memo_id, memo_id))]
    return {"tasks": tasks, "memos": memos}


def add_link(conn, memo_id: int, kind: str, target_id: int, by: str, ts: str) -> bool:
    if kind == "memo":
        if target_id == memo_id:
            raise ClaireError("bad_link", "같은 메모끼리는 연결하지 않습니다.")
        a, b = sorted((memo_id, target_id))
        cur = conn.execute("INSERT OR IGNORE INTO memo_link(memo_id,kind,target_id,created_by,created_at) VALUES(?,?,?,?,?)",
                           (a, kind, b, by, ts))
    else:
        cur = conn.execute("INSERT OR IGNORE INTO memo_link(memo_id,kind,target_id,created_by,created_at) VALUES(?,?,?,?,?)",
                           (memo_id, kind, target_id, by, ts))
    return cur.rowcount > 0


# --------------------------------------------------------------------------
# 표시
# --------------------------------------------------------------------------

def fmt_ts(cfg, ts: str | None, with_time: bool = True) -> str:
    if not ts:
        return ""
    try:
        d = parse_iso(ts).astimezone(tz_of(cfg))
    except ValueError:
        return ts
    return d.strftime("%Y-%m-%d %H:%M") if with_time else d.strftime("%Y-%m-%d")


def short_day(cfg, ts: str | None) -> str:
    if not ts:
        return ""
    try:
        d = parse_iso(ts).astimezone(tz_of(cfg))
    except ValueError:
        return ts[:10]
    return f"{d.month}/{d.day}"


def summary(conn, cfg, m) -> dict:
    lk = links(conn, m["id"])
    return {"ref": m["ref"], "title": m["title"], "status": m["status"], "status_ko": STATUS_KO[m["status"]],
            "recorded_at": m["recorded_at"], "topic": m["topic"], "edited": m["body"] != m["original_text"],
            "tasks": [t["ref"] for t in lk["tasks"]], "memos": [x["ref"] for x in lk["memos"]],
            "sync_status": m["sync_status"]}


def list_line(cfg, s: dict) -> str:
    """목록 한 줄: `MEM-0001 · NAS 학생별 공간 · 10/11 · 열림` (+ 주제·할 일 연결)"""
    bits = [s["ref"], s["title"], short_day(cfg, s["recorded_at"]), s["status_ko"]]
    if s.get("topic"):
        bits.append(f"#{s['topic']}")
    if s.get("tasks"):
        bits.append("할 일 연결됨 " + ",".join(s["tasks"]))
    return " · ".join(bits)


def source_text(m) -> str:
    if m["source_message_id"]:
        where = "Discord" if m["source_platform"] == "discord" else "직접 입력"
        part = f" ({m['source_part']}번째 항목)" if m["source_part"] and m["source_part"] > 1 else ""
        link = f" [원문]({m['source_url']})" if m["source_url"] else ""
        return f"{where} 메시지 {m['source_message_id']}{part}{link}"
    return "Discord" if m["source_platform"] == "discord" else "직접 입력"


def render_block(conn, cfg, m) -> list[str]:
    """Obsidian 메모 블록 (표식 포함). 체크박스 없음."""
    import obsidian_memos
    lk = links(conn, m["id"])
    head = f"### {m['ref']} · {m['title']}"
    meta = [f"상태: {STATUS_KO[m['status']]}", f"기록 {fmt_ts(cfg, m['recorded_at'])}"]
    if m["topic"]:
        meta.append(f"주제: {m['topic']}")
    lines = [obsidian_memos.start_mark(m["ref"]), head, "- " + " · ".join(meta)]
    lines += obsidian_memos.quote_text(m["body"])
    if m["body"] != m["original_text"]:
        lines.append("- 처음 기록:")
        lines += obsidian_memos.quote_text(m["original_text"])
    lines.append(f"- 출처: {source_text(m)}")
    if lk["tasks"]:
        lines.append("- 관련 업무: " + ", ".join(f"{t['ref']} {t['title']}" for t in lk["tasks"]))
    if lk["memos"]:
        lines.append("- 관련 메모: " + ", ".join(f"{x['ref']} {x['title']}" for x in lk["memos"]))
    if m["status"] != "open" and m["status_changed_at"]:
        why = f" — {m['status_reason']}" if m["status_reason"] else ""
        lines.append(f"- 정리: {STATUS_KO[m['status']]} {fmt_ts(cfg, m['status_changed_at'], False)}{why}")
    lines.append(obsidian_memos.end_mark(m["ref"]))
    return lines


def detail(conn, cfg, m, history: int = 10) -> dict:
    lk = links(conn, m["id"])
    hist = [dict(r) for r in conn.execute(
        "SELECT action, actor, field, old_value, new_value, reason, source_message_id, created_at FROM memo_log "
        "WHERE memo_id=? ORDER BY id DESC LIMIT ?", (m["id"], history))]
    return {**summary(conn, cfg, m), "body": m["body"], "original_text": m["original_text"],
            "status_reason": m["status_reason"], "status_changed_at": m["status_changed_at"],
            "source": {"platform": m["source_platform"], "channel": m["source_channel"],
                       "message_id": m["source_message_id"], "part": m["source_part"], "url": m["source_url"]},
            "links": lk, "obsidian": {"note": m["obsidian_note"], "sync_status": m["sync_status"],
                                      "error": m["sync_error"], "synced_at": m["synced_at"]},
            "history": hist}


def detail_text(cfg, d: dict) -> str:
    lines = [f"{d['ref']} · {d['title']}",
             " · ".join(x for x in (d["status_ko"], f"기록 {fmt_ts(cfg, d['recorded_at'])}",
                                    f"주제 {d['topic']}" if d.get("topic") else "") if x)]
    lines.append("내용: " + d["body"])
    if d["edited"]:
        lines.append("처음 기록: " + d["original_text"])
    if d["status"] != "open" and d.get("status_changed_at"):
        lines.append(f"정리: {d['status_ko']} {fmt_ts(cfg, d['status_changed_at'], False)}"
                     + (f" — {d['status_reason']}" if d.get("status_reason") else ""))
    src = d["source"]
    if src["message_id"]:
        lines.append(f"출처: Discord 메시지 {src['message_id']}" + (f" ({src['part']}번째 항목)" if src["part"] > 1 else ""))
    if d["links"]["tasks"]:
        lines.append("관련 업무: " + ", ".join(f"{t['ref']} {t['title']}" for t in d["links"]["tasks"]))
    if d["links"]["memos"]:
        lines.append("관련 메모: " + ", ".join(f"{x['ref']} {x['title']}" for x in d["links"]["memos"]))
    ob = d["obsidian"]
    lines.append("Obsidian: " + {"ok": f"반영됨 ({ob['note']})", "pending": "반영 대기",
                                 "failed": f"반영 실패 — 재시도 중 ({(ob['error'] or '')[:80]})",
                                 "conflict": f"반영 보류 — {(ob['error'] or '')[:120]}"}[ob["sync_status"]])
    if d["history"]:
        lines.append("이력: " + " / ".join(f"{h['created_at'][5:16].replace('T', ' ')} {HIST_KO.get(h['action'], h['action'])}"
                                         + (f"({STATUS_KO.get(h['new_value'], h['new_value'])})" if h["action"] == "status" else "")
                                         for h in reversed(d["history"][:5])))
    return "\n".join(lines)


HIST_KO = {"create": "기록", "status": "상태", "edit": "수정", "link": "연결", "unlink": "연결 해제", "to_task": "할 일 등록",
           "sync": "Obsidian 다시 씀"}


# --------------------------------------------------------------------------
# 검색
# --------------------------------------------------------------------------

def search(conn, cfg, *, q: str | None, statuses: list[str], topic: str | None = None) -> list:
    """메모 검색: 제목·본문(처음 기록 포함)·주제. 검색어의 낱말이 모두 맞는 메모를 먼저, 그다음 일부만 맞는 메모.
    결과는 상태(열림 → 참고 → 해결·불필요) 뒤 최신 기록순."""
    sql = "SELECT * FROM memo WHERE status IN (%s)" % ",".join("?" * len(statuses))
    params: list = list(statuses)
    if topic:
        sql += " AND topic LIKE ?"
        params.append(f"%{topic}%")
    rows = conn.execute(sql + " ORDER BY recorded_at DESC, id DESC", params).fetchall()
    order = {s: i for i, s in enumerate(["open", "reference", "resolved", "not_needed"])}
    if not q:
        return sorted(rows, key=lambda r: order[r["status"]]) if len(statuses) > 1 else rows
    toks = tokenize(q)
    if not toks:
        raise ClaireError("empty_query", "검색어가 비어 있습니다")
    fts = set()
    try:
        fq = fts_query(toks)
        if fq:
            fts = {r[0] for r in conn.execute("SELECT rowid FROM memo_fts WHERE memo_fts MATCH ?", (fq,))}
    except Exception:  # noqa: BLE001 — FTS 문법 문제면 부분 일치로만 찾는다
        fts = set()
    scored = []
    for r in rows:
        hay = " ".join(x for x in (r["title"], r["body"], r["original_text"], r["topic"], r["ref"]) if x)
        n = sum(1 for t in toks if hits(t, hay))
        if n == 0 and r["id"] not in fts:
            continue
        scored.append((-(n == len(toks)), -n, order[r["status"]], r))
    scored.sort(key=lambda x: x[:3])          # 정렬은 안정적이므로 같은 점수 안에서는 최신순이 유지된다
    return [x[3] for x in scored]


# --------------------------------------------------------------------------
# Obsidian 반영 (claire_apply 의 op 와 claire_memo 가 함께 쓴다)
# --------------------------------------------------------------------------

def sync_one(conn, cfg, ref: str, ts: str, overwrite: bool = False) -> dict:
    """메모 블록을 노트에 맞추고 결과를 메모에 기록한다. 실패하면 예외(트랜잭션째 되돌리고 호출자가 실패를 기록)."""
    import obsidian_memos
    m = get(conn, cfg, ref)
    block = render_block(conn, cfg, m)
    res = obsidian_memos.write_block(cfg, m["ref"], block, configured=mcfg(cfg)["note"], hint=m["obsidian_note"],
                                     last_hash=m["obsidian_hash"], overwrite=overwrite)
    conn.execute("UPDATE memo SET obsidian_note=?, obsidian_hash=?, sync_status='ok', sync_error=NULL, synced_at=? WHERE id=?",
                 (res["note"], res["hash"], ts, m["id"]))
    if overwrite:
        log(conn, m["id"], "sync", "user", ts, field="obsidian", new=res["action"], reason="Claire 내용으로 다시 씀")
    # 같은 메모의 대기 중 반영은 이것으로 끝났고(현재 내용을 썼다), 이전 실패 기록은 이 반영이 대신한다
    out = json.dumps({"note": res["note"], "action": res["action"]}, ensure_ascii=False)
    conn.execute("UPDATE outbox SET status='done', done_at=?, result=?, last_error=NULL WHERE op=? AND status='pending' "
                 "AND json_extract(payload,'$.memo')=?", (ts, out, OP, m["ref"]))
    conn.execute("UPDATE outbox SET status='cancelled', done_at=?, result=? WHERE op=? AND status='failed' "
                 "AND json_extract(payload,'$.memo')=?", (ts, json.dumps({"superseded": True}), OP, m["ref"]))
    return {"memo": m["ref"], **res}


def mark_failed(conn, ref: str, code: str, msg: str) -> None:
    status = "conflict" if code in ("memo_conflict", "duplicate_memo_blocks") else "failed"
    conn.execute("UPDATE memo SET sync_status=?, sync_error=? WHERE ref=?", (status, msg[:500], ref))


NO_RETRY = ("memo_conflict", "duplicate_memo_blocks")      # 교수님 확인이 필요한 실패: 재시도해도 같다


def op_memo(conn, cfg, row, payload, ts):
    """claire_apply 핸들러."""
    return sync_one(conn, cfg, payload["memo"], ts)


def sync_issues(conn) -> dict:
    """운영 점검용: 메모의 Obsidian 반영 실패·보류 건수 (업무 실패 목록과 섞지 않는다)."""
    rows = conn.execute("SELECT ref, sync_status, sync_error FROM memo WHERE sync_status IN ('failed','conflict') "
                        "ORDER BY id").fetchall() if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memo'").fetchone() else []
    return {"failed": [r["ref"] for r in rows if r["sync_status"] == "failed"],
            "conflict": [r["ref"] for r in rows if r["sync_status"] == "conflict"]}


def now_parts(cfg, captured: str | None, now_s: str) -> str:
    if not captured:
        return now_s
    try:
        dt = datetime.fromisoformat(captured)
    except ValueError:
        raise ClaireError("bad_captured_at", "--captured-at 은 ISO8601(오프셋 포함)이어야 합니다.")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz_of(cfg))
    return dt.replace(microsecond=0).isoformat()
