"""Obsidian 볼트 스캔·Tasks 줄 파싱 (PRD §6.3, D4, D12).

읽기만 한다. 쓰기(🆔 부여·체크)는 4단계 claire_apply가 이 모듈의 원자적 교체 헬퍼를 쓴다.

Tasks 플러그인 문법:
  - [ ] #tasks 설명 📅 2026-09-12 ⏳ 2026-09-10 🛫 2026-09-08 ✅ 2026-09-12 🔁 every week 🆔 clr0031
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from claire_core import ClaireError  # noqa: E402

TASK_RE = re.compile(r"^(?P<indent>\s*)[-*+]\s+\[(?P<sym>.)\]\s+(?P<body>.*)$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]")
TAG_RE = re.compile(r"(?<![\w/])#([\w가-힣/_-]+)")

# 필드 표식. 값은 다음 표식 또는 줄 끝까지.
FIELD_EMOJI = {
    "📅": "due", "⏳": "scheduled", "🛫": "start", "✅": "done", "❌": "cancelled",
    "➕": "created", "🔁": "recurrence", "🆔": "id", "⛔": "depends_on",
    "🔺": "priority", "⏫": "priority", "🔼": "priority", "🔽": "priority", "⏬": "priority",
}
PRIORITY_EMOJI = {"🔺": "highest", "⏫": "high", "🔼": "medium", "🔽": "low", "⏬": "lowest"}
_FIELD_SPLIT = re.compile("(" + "|".join(re.escape(e) for e in FIELD_EMOJI) + ")")
DATE_RE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})")


def vault_path(cfg: dict) -> Path:
    return Path(cfg["obsidian"]["vault"]).expanduser()


def parse_task_line(line: str, cfg: dict | None = None) -> dict | None:
    """체크박스 줄을 파싱한다. 체크박스가 아니면 None."""
    m = TASK_RE.match(line.rstrip("\n"))
    if not m:
        return None
    sym = m.group("sym")
    body = m.group("body")
    parts = _FIELD_SPLIT.split(body)
    desc = parts[0].strip()
    fields: dict = {}
    i = 1
    while i < len(parts) - 1:
        emoji, value = parts[i], parts[i + 1]
        key = FIELD_EMOJI[emoji]
        if key == "priority":
            fields["priority"] = PRIORITY_EMOJI[emoji]
        elif key in ("due", "scheduled", "start", "done", "cancelled", "created"):
            dm = DATE_RE.match(value)
            fields[key] = dm.group(1) if dm else None
            rest = value[dm.end():].strip() if dm else value.strip()
            if rest:
                desc = (desc + " " + rest).strip()
        else:
            fields[key] = value.strip()
        i += 2
    custom = (cfg or {}).get("obsidian", {}).get("custom_status_symbols", {}) if cfg else {}
    if sym in ("x", "X"):
        state = "done"
    elif sym == " ":
        state = "todo"
    elif sym == "-":
        state = "cancelled"
    else:
        state = custom.get(sym, "other")
    wikilinks = [(t.strip(), (a or "").strip()) for t, a in WIKILINK_RE.findall(body)]
    tags = ["#" + t for t in TAG_RE.findall(body)]
    normalized = unicodedata.normalize("NFC", re.sub(r"\s+", " ", desc)).strip()
    return {
        "symbol": sym, "state": state, "description": desc, "normalized": normalized,
        "desc_hash": hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16],
        "tags": tags, "wikilinks": wikilinks, "indent": len(m.group("indent")),
        **{k: fields.get(k) for k in ("due", "scheduled", "start", "done", "cancelled",
                                      "created", "recurrence", "id", "priority", "depends_on")},
    }


def iter_markdown_files(cfg: dict):
    root = vault_path(cfg)
    if not root.is_dir():
        raise ClaireError("vault_missing", f"Obsidian 볼트가 없습니다: {root}")
    excl = [e.rstrip("/") for e in cfg["obsidian"].get("exclude_folders", [])]
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        # 제외 폴더는 내려가지 않는다
        dirnames[:] = sorted(d for d in dirnames
                             if not d.startswith(".") and str(rel_dir / d) not in excl and d not in excl)
        for fn in sorted(filenames):
            if fn.endswith(".md"):
                yield Path(dirpath) / fn


def scan_file(path: Path, cfg: dict, root: Path | None = None) -> list[dict]:
    """파일 하나에서 대상 Tasks 줄을 모은다. 제외 섹션(## Routine) 안의 줄은 건너뛴다."""
    root = root or vault_path(cfg)
    tag = cfg["obsidian"].get("tag", "#tasks")
    excl_sections = [s.strip() for s in cfg["obsidian"].get("exclude_sections", [])]
    excl = []
    for s in excl_sections:
        hm = HEADING_RE.match(s)
        excl.append((len(hm.group(1)), hm.group(2).strip()) if hm else (0, s))
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []
    rel = str(path.relative_to(root))
    mtime = path.stat().st_mtime
    out = []
    skip_level = None  # 제외 섹션 안이면 그 헤딩 레벨
    for lineno, line in enumerate(text.splitlines(), 1):
        hm = HEADING_RE.match(line)
        if hm:
            level, title = len(hm.group(1)), hm.group(2).strip()
            if skip_level is not None and level <= skip_level:
                skip_level = None
            if any((lv == 0 or lv == level) and title == t for lv, t in excl):
                skip_level = level
            continue
        if skip_level is not None:
            continue
        t = parse_task_line(line, cfg)
        if not t or (tag and tag not in t["tags"]):
            continue
        key = f"{rel}#{t['id']}" if t["id"] else f"{rel}#{t['desc_hash']}"
        t.update({"file": rel, "line": lineno, "raw": line, "external_id": key,
                  "file_mtime": mtime,
                  "line_hash": hashlib.sha256(line.strip().encode("utf-8")).hexdigest()[:16]})
        out.append(t)
    return out


def scan_vault(cfg: dict, since_mtime: float | None = None, limit_files: int | None = None) -> dict:
    root = vault_path(cfg)
    files = tasks = 0
    changed_files = 0
    by_state: dict[str, int] = {}
    with_id = 0
    items = []
    for p in iter_markdown_files(cfg):
        files += 1
        if since_mtime is not None and p.stat().st_mtime <= since_mtime:
            continue
        changed_files += 1
        if limit_files and changed_files > limit_files:
            break
        for t in scan_file(p, cfg, root):
            tasks += 1
            by_state[t["state"]] = by_state.get(t["state"], 0) + 1
            if t["id"]:
                with_id += 1
            items.append(t)
    return {"vault": str(root), "files_seen": files, "files_scanned": changed_files,
            "tasks": tasks, "by_state": by_state, "with_id": with_id, "items": items}


def replace_line_atomic(path: Path, lineno: int, expected_line: str, new_line: str,
                        expected_mtime: float | None = None) -> dict:
    """읽기 → 대상 줄만 치환 → 임시 파일 → 원자적 교체 (§6.3).

    읽은 뒤 mtime이 바뀌었거나 대상 줄이 기대와 다르면 쓰지 않고 거부한다 (TC19).
    """
    if expected_mtime is not None and abs(path.stat().st_mtime - expected_mtime) > 1e-6:
        raise ClaireError("file_changed", "읽은 뒤 파일이 바뀌어 쓰지 않았습니다.", path=str(path))
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    if lineno < 1 or lineno > len(lines) or lines[lineno - 1].rstrip() != expected_line.rstrip():
        raise ClaireError("line_mismatch", "대상 줄이 기대와 달라 쓰지 않았습니다.",
                          path=str(path), line=lineno)
    lines[lineno - 1] = new_line
    tmp = path.with_name(f".{path.name}.claire-tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    if abs(path.stat().st_mtime - (expected_mtime or path.stat().st_mtime)) > 1e-6:
        tmp.unlink(missing_ok=True)
        raise ClaireError("file_changed", "쓰기 직전 파일이 바뀌어 중단했습니다.", path=str(path))
    os.replace(tmp, path)
    return {"path": str(path), "line": lineno}


def format_id(ref: str) -> str:
    """CLR-0031 → clr0031 (🆔 값, D12)."""
    return ref.lower().replace("-", "")


def parse_id(task_id: str | None) -> str | None:
    """clr0031 → CLR-0031. Claire가 붙인 🆔가 아니면 None."""
    m = re.fullmatch(r"clr(\d{4,})", (task_id or "").strip().lower())
    return f"CLR-{m.group(1)}" if m else None


# --------------------------------------------------------------------------
# 쓰기 (4단계 claire_apply 전용, §6.3 규약: 읽기 → 대상 줄만 치환 → 임시 파일 → 원자적 교체)
# --------------------------------------------------------------------------

def find_task_by_key(path: Path, cfg: dict, external_key: str) -> dict | None:
    """external_key(`파일#🆔` 또는 `파일#desc_hash`)에 해당하는 줄을 찾는다."""
    root = vault_path(cfg)
    key = external_key.split("#", 1)[1] if "#" in external_key else external_key
    for t in scan_file(path, cfg, root):
        if (t.get("id") and t["id"] == key) or t["desc_hash"] == key:
            return t
    return None


def line_with_id(line: str, ref: str) -> str:
    """줄 끝에 `🆔 clrNNNN` 을 붙인다 (이미 있으면 그대로)."""
    if "🆔" in line:
        return line
    return line.rstrip() + f" 🆔 {format_id(ref)}"


def line_checked(line: str, done_day: str) -> str:
    """`[ ]` → `[x]`, `✅ 완료일` 추가. 줄을 지우거나 옮기지 않는다."""
    m = TASK_RE.match(line)
    if not m:
        raise ClaireError("not_a_task_line", "체크박스 줄이 아닙니다", line=line)
    new = re.sub(r"^(\s*[-*+]\s+)\[.\]", lambda mm: mm.group(1) + "[x]", line, count=1)
    if "✅" not in new:
        new = new.rstrip() + f" ✅ {done_day}"
    return new


def format_task_line(title: str, due: str | None, ref: str, note_link: str | None = None,
                     tag: str = "#tasks", due_time: str | None = None) -> str:
    """todo-list-management 규약 + 🆔 (D12). `- [ ] #tasks [[정본 노트]]: 설명 📅 YYYY-MM-DD 🆔 clrNNNN`
    마감 시각이 확정된 항목은 설명 끝에 `(15:00 마감)` 을 붙인다 (Tasks 문법에는 시각 필드가 없다)."""
    body = f"[[{note_link}]]: {title}" if note_link else title
    if due_time:
        body += f" ({due_time} 마감)"
    line = f"- [ ] {tag} {body}"
    if due:
        line += f" 📅 {due[:10]}"
    return line + f" 🆔 {format_id(ref)}"


def daily_path(cfg: dict, day: str) -> Path:
    return vault_path(cfg) / cfg["obsidian"]["daily_folder"] / f"{day}.md"


def ensure_daily(cfg: dict, day: str) -> tuple[Path, bool]:
    """Daily 노트가 없으면 `92 Templates/template_daily.md` 형식으로 만든다 (규약 A-2). (path, created)"""
    p = daily_path(cfg, day)
    if p.exists():
        return p, False
    tpl = vault_path(cfg) / "92 Templates" / "template_daily.md"
    if tpl.exists():
        text = tpl.read_text(encoding="utf-8")
        text = re.sub(r"^date created:.*$", f"date created: {day}", text, count=1, flags=re.M)
    else:
        text = f"---\ntags:\n  - \"Daily\"\ndate created: {day}\n---\n\n## Tasks\n\n\n## Reflections\n\n\n## Routine\n"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.claire-tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)
    return p, True


def add_task_to_daily(cfg: dict, day: str, line: str) -> dict:
    """`## Tasks` 섹션 끝(다음 헤딩 앞)에 줄을 추가한다. 같은 🆔 줄이 이미 있으면 추가하지 않는다."""
    p, created = ensure_daily(cfg, day)
    mtime = p.stat().st_mtime
    text = p.read_text(encoding="utf-8")
    m_id = re.search(r"🆔\s+(\S+)", line)
    if m_id and re.search(r"🆔\s+" + re.escape(m_id.group(1)) + r"(\s|$)", text):
        return {"path": str(p), "added": False, "reason": "already_present", "created": created}
    lines = text.split("\n")
    start = next((i for i, l in enumerate(lines) if l.strip() == "## Tasks"), None)
    if start is None:
        # Tasks 섹션이 없으면 frontmatter 뒤에 만든다
        insert_at = 0
        if lines and lines[0].strip() == "---":
            try:
                insert_at = lines.index("---", 1) + 1
            except ValueError:
                insert_at = 0
        lines[insert_at:insert_at] = ["", "## Tasks", "", line, ""]
    else:
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("#")), len(lines))
        # 섹션 안의 마지막 비어 있지 않은 줄 뒤에 넣는다
        last = start
        for i in range(start + 1, end):
            if lines[i].strip():
                last = i
        lines.insert(last + 1, line)
    if abs(p.stat().st_mtime - mtime) > 1e-6:
        raise ClaireError("file_changed", "읽은 뒤 파일이 바뀌어 쓰지 않았습니다.", path=str(p))
    tmp = p.with_name(f".{p.name}.claire-tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    os.replace(tmp, p)
    return {"path": str(p), "added": True, "created": created,
            "line_no": (lines.index(line) + 1)}


# --------------------------------------------------------------------------
# 완료 기록 (0.5.0) — 완료한 날 Daily 의 `## 완료한 일` 섹션에 항목당 한 줄
# --------------------------------------------------------------------------

def done_marker(ref: str) -> str:
    """Obsidian 주석(읽기·미리보기에서 숨김)으로 줄을 찾는 표식. 같은 표식 줄은 한 Daily 에 하나만 둔다."""
    return f"%%claire-done:{ref}%%"


def format_done_line(ref: str, title: str, time_label: str | None, note_link: str | None) -> str:
    """`- ✅ 14:32 심사 의견 정리 (CLR-0031) · [[20260911 논문 심사]] %%claire-done:CLR-0031%%`
    체크박스가 아니므로 Tasks 플러그인·Claire 수집 대상이 아니다."""
    parts = ["- ✅"]
    if time_label:
        parts.append(time_label)
    parts.append(f"{title} ({ref})")
    line = " ".join(parts)
    if note_link:
        line += f" · [[{note_link}]]"
    return f"{line} {done_marker(ref)}"


def _write_lines(p: Path, lines: list[str], mtime: float) -> None:
    if abs(p.stat().st_mtime - mtime) > 1e-6:
        raise ClaireError("file_changed", "읽은 뒤 파일이 바뀌어 쓰지 않았습니다.", path=str(p))
    tmp = p.with_name(f".{p.name}.claire-tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    os.replace(tmp, p)


def _section_bounds(lines: list[str], heading: str) -> tuple[int, int] | None:
    """heading 줄 번호와 섹션 끝(같거나 높은 레벨의 다음 헤딩, 없으면 파일 끝)."""
    hm = HEADING_RE.match(heading)
    level = len(hm.group(1)) if hm else 2
    title = hm.group(2).strip() if hm else heading.strip()
    for i, l in enumerate(lines):
        m = HEADING_RE.match(l)
        if m and len(m.group(1)) == level and m.group(2).strip() == title:
            end = next((j for j in range(i + 1, len(lines))
                        if (HEADING_RE.match(lines[j]) and len(HEADING_RE.match(lines[j]).group(1)) <= level)), len(lines))
            return i, end
    return None


def upsert_done_line(cfg: dict, day: str, ref: str, line: str) -> dict:
    """완료일 Daily 에 기록 줄을 둔다. 같은 표식 줄이 있으면 내용만 맞추고(중복 없음), 없으면 섹션 끝에 추가한다.
    섹션이 없으면 `## Tasks` 섹션 뒤(없으면 frontmatter 뒤)에 만든다. Daily 가 없으면 템플릿으로 만든다."""
    dd = cfg.get("daily_done", {})
    section = dd.get("section", "## 완료한 일")
    after = dd.get("after_section", "## Tasks")
    p, created = ensure_daily(cfg, day)
    mtime = p.stat().st_mtime
    lines = p.read_text(encoding="utf-8").split("\n")
    marker = done_marker(ref)
    hits = [i for i, l in enumerate(lines) if marker in l]
    if hits:
        first = hits[0]
        changed = lines[first] != line or len(hits) > 1
        if not changed:
            return {"path": str(p), "action": "unchanged", "created": created}
        lines[first] = line
        for i in reversed(hits[1:]):
            del lines[i]
        _write_lines(p, lines, mtime)
        return {"path": str(p), "action": "updated", "created": created}
    b = _section_bounds(lines, section)
    if b is None:
        anchor = _section_bounds(lines, after)
        if anchor is not None:
            insert_at = anchor[1]
            while insert_at > anchor[0] + 1 and not lines[insert_at - 1].strip():
                insert_at -= 1
            block = ["", section, line, ""]
        else:
            insert_at = 0
            if lines and lines[0].strip() == "---":
                try:
                    insert_at = lines.index("---", 1) + 1
                except ValueError:
                    insert_at = 0
            block = ["", section, line, ""]
        lines[insert_at:insert_at] = block
    else:
        start, end = b
        last = start
        for i in range(start + 1, end):
            if lines[i].strip():
                last = i
        lines.insert(last + 1, line)
    _write_lines(p, lines, mtime)
    return {"path": str(p), "action": "added", "created": created}


def remove_done_line(cfg: dict, day: str, ref: str) -> dict:
    """그 날 Daily 에서 표식 줄을 지운다. 파일·줄이 없으면 할 일이 없다(성공)."""
    p = daily_path(cfg, day)
    if not p.exists():
        return {"path": str(p), "action": "absent"}
    mtime = p.stat().st_mtime
    lines = p.read_text(encoding="utf-8").split("\n")
    marker = done_marker(ref)
    keep = [l for l in lines if marker not in l]
    if len(keep) == len(lines):
        return {"path": str(p), "action": "absent"}
    _write_lines(p, keep, mtime)
    return {"path": str(p), "action": "removed", "count": len(lines) - len(keep)}
