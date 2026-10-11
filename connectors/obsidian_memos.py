"""Obsidian 메모함 노트 쓰기 (0.8.0, ISSUE 2026-10-11 §6).

메모는 업무가 아니다. 메모 노트(기본 `01 Inbox/Claire 메모함.md`)에 메모마다 표식으로 감싼 블록 하나를 둔다:

    %%claire-memo:MEM-0001:start%%
    ### MEM-0001 · NAS 학생별 공간
    - 상태: 열림 · 기록 2026-10-11 14:02 · 주제: NAS
    > NAS에 학생별 공간을 나누면 좋을 듯
    - 출처: Discord 메시지 1558745571030999131
    %%claire-memo:MEM-0001:end%%

- 체크박스를 쓰지 않는다. 원문 속 체크박스 줄도 `\\[ ]`로 무력화해 Tasks 플러그인·Claire 수집기가 업무로 가져가지 않게 한다
  (obsidian_tasks.scan_file 도 메모 블록 안은 건너뛴다).
- 블록 밖(교수님이 쓴 글)은 건드리지 않는다. 블록 안을 교수님이 고쳤으면(마지막으로 쓴 내용의 해시와 다르면) 덮어쓰지 않고
  conflict 로 드러낸다 — Obsidian → DB 역동기화는 하지 않는다(0.8.0). `overwrite=True` 일 때만 DB 내용으로 다시 쓴다.
- 노트를 옮기거나 이름을 바꿔도 표식으로 찾아간다. 같은 메모의 블록이 두 곳 이상이면 어느 쪽인지 정하지 않고 실패로 드러낸다.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from claire_core import ClaireError  # noqa: E402
import obsidian_tasks  # noqa: E402

MARK_ANY = "%%claire-memo:"
MARK_RE = re.compile(r"^%%claire-memo:([A-Za-z]+-\d+):(start|end)%%\s*$")
CHECKBOX_RE = re.compile(r"^(\s*[-*+]\s+)\[(.)\]")

NOTE_HEADER = ("---\ntags:\n  - \"Claire\"\n---\n\n# Claire 메모함\n\n"
               "> Discord 일정 채널에서 남긴 **메모**입니다. 업무(Tasks)가 아니며 아침 보고·진행 확인에 나오지 않습니다.\n"
               "> 메모마다 `%%claire-memo:…%%` 표식 사이를 Claire 가 갱신합니다. 상태 변경·수정은 Discord 에서 Claire 에게 말해 주세요"
               " — 이 노트에서 블록 안을 고쳐도 Claire 에 반영되지 않고, Claire 도 고친 블록을 덮어쓰지 않습니다.\n"
               "> 표식 밖에 적은 글은 Claire 가 건드리지 않습니다.\n")


def start_mark(ref: str) -> str:
    return f"%%claire-memo:{ref}:start%%"


def end_mark(ref: str) -> str:
    return f"%%claire-memo:{ref}:end%%"


def quote_text(text: str) -> list[str]:
    """원문을 인용 줄로. 체크박스 줄은 `\\[ ]`로 무력화하고 표식 문자열을 깨뜨린다(업무로 다시 수집되지 않게)."""
    out = []
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        line = CHECKBOX_RE.sub(lambda m: m.group(1) + "\\[" + m.group(2) + "]", line)
        line = line.replace("%%", "%\\%")
        out.append(f"> {line}".rstrip() if line.strip() else ">")
    return out


def block_hash(lines: list[str]) -> str:
    return hashlib.sha256("\n".join(l.rstrip() for l in lines).encode("utf-8")).hexdigest()[:16]


def find_blocks(lines: list[str], ref: str) -> list[tuple[int, int]]:
    """(시작 표식 줄, 끝 표식 줄) 목록. 끝 표식이 없는 블록은 시작 줄만으로 (i, -1)."""
    out = []
    s, e = start_mark(ref), end_mark(ref)
    i = 0
    while i < len(lines):
        if lines[i].strip() == s:
            j = next((k for k in range(i + 1, len(lines)) if lines[k].strip() == e), -1)
            out.append((i, j))
            i = j + 1 if j >= 0 else i + 1
            continue
        i += 1
    return out


def memo_files(cfg: dict) -> list[str]:
    """메모 표식이 있는 노트(볼트 기준 경로). 노트를 옮긴 경우를 찾는다."""
    root = obsidian_tasks.vault_path(cfg)
    out = []
    for p in obsidian_tasks.iter_markdown_files(cfg):
        try:
            if MARK_ANY in p.read_text(encoding="utf-8"):
                out.append(str(p.relative_to(root)))
        except (UnicodeDecodeError, OSError):
            continue
    return out


def locate(cfg: dict, ref: str, hint: str | None) -> list[str]:
    """이 메모의 블록이 있는 노트들. hint(마지막으로 쓴 노트)를 먼저 보고, 없으면 볼트 전체에서 찾는다."""
    root = obsidian_tasks.vault_path(cfg)
    if hint:
        p = root / hint
        if p.is_file() and find_blocks(p.read_text(encoding="utf-8").split("\n"), ref):
            return [hint]
    out = []
    for rel in memo_files(cfg):
        if find_blocks((root / rel).read_text(encoding="utf-8").split("\n"), ref):
            out.append(rel)
    return out


def ensure_note(cfg: dict, rel: str) -> Path:
    p = obsidian_tasks.vault_path(cfg) / rel
    if not p.exists():
        if not obsidian_tasks.vault_path(cfg).is_dir():
            raise ClaireError("vault_missing", f"Obsidian 볼트가 없습니다: {obsidian_tasks.vault_path(cfg)}")
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.claire-tmp")
        tmp.write_text(NOTE_HEADER, encoding="utf-8")
        os.replace(tmp, p)
    return p


def home_note(cfg: dict, configured: str) -> tuple[str, str | None]:
    """새 메모 블록을 넣을 노트. 설정한 노트가 없고 메모 블록이 있는 노트가 한 곳뿐이면(교수님이 옮김) 그 노트를 쓴다."""
    root = obsidian_tasks.vault_path(cfg)
    if (root / configured).is_file():
        return configured, None
    found = memo_files(cfg)
    if len(found) == 1:
        return found[0], f"메모 노트가 '{found[0]}'(으)로 옮겨진 것으로 보고 그곳에 적었습니다."
    return configured, None


def write_block(cfg: dict, ref: str, block: list[str], *, configured: str, hint: str | None,
                last_hash: str | None, overwrite: bool = False) -> dict:
    """메모 블록을 원하는 내용으로 맞춘다 (멱등). 반환: {note, action(added|updated|unchanged), hash, note_moved?}.
    교수님이 블록 안을 고쳤거나(해시 불일치) 이미 쓴 블록이 사라졌으면 쓰지 않고 memo_conflict 를 낸다(overwrite 면 쓴다)."""
    new_hash = block_hash(block)
    where = locate(cfg, ref, hint)
    if len(where) > 1:
        raise ClaireError("duplicate_memo_blocks", f"{ref} 메모 블록이 {len(where)}곳에 있습니다: {', '.join(where)}. "
                          "어느 쪽인지 정하지 않았습니다 — 하나를 지워 주세요.", notes=where)
    note_moved = None
    if where:
        rel = where[0]
        if hint and rel != hint:
            note_moved = f"메모 노트가 '{rel}'(으)로 옮겨진 것을 찾아 그곳을 갱신했습니다."
    else:
        if last_hash and not overwrite:
            raise ClaireError("memo_conflict", f"Obsidian 에서 {ref} 메모 블록이 사라졌습니다(지워졌거나 표식이 바뀜). "
                              "다시 쓰지 않았습니다 — 다시 넣으려면 claire_memo sync --memo " + ref + " --overwrite",
                              reason="block_missing")
        rel, note_moved = home_note(cfg, configured)
    p = ensure_note(cfg, rel)
    mtime = p.stat().st_mtime
    lines = p.read_text(encoding="utf-8").split("\n")
    blocks = find_blocks(lines, ref)
    if blocks:
        i, j = blocks[0]
        if j < 0:
            raise ClaireError("memo_conflict", f"{rel} 의 {ref} 메모 블록에 끝 표식이 없습니다. 쓰지 않았습니다.",
                              reason="block_broken", note=rel)
        current = lines[i:j + 1]
        cur_hash = block_hash(current)
        if cur_hash == new_hash:
            return {"note": rel, "action": "unchanged", "hash": new_hash, "note_moved": note_moved}
        if last_hash and cur_hash != last_hash and not overwrite:
            raise ClaireError("memo_conflict", f"Obsidian({rel})에서 {ref} 메모 블록을 직접 고친 것으로 보여 덮어쓰지 않았습니다 "
                              "(Obsidian → Claire 반영은 지원하지 않습니다). 고친 내용을 Discord 로 알려 주시면 반영하고, "
                              "Claire 내용으로 되돌리려면 claire_memo sync --memo " + ref + " --overwrite",
                              reason="edited_in_obsidian", note=rel)
        lines[i:j + 1] = block
        action = "updated"
    else:
        while lines and not lines[-1].strip():
            lines.pop()
        lines += [""] + block + [""]
        action = "added"
    obsidian_tasks._write_lines(p, lines, mtime)
    return {"note": rel, "action": action, "hash": new_hash, "note_moved": note_moved}
