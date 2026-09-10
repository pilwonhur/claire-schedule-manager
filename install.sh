#!/usr/bin/env bash
# Claire 일정·업무 관리 스킬 설치·업데이트 (PRD.md §4.2, Clio install.sh 계승)
#
#   ./install.sh
#
# 바뀐 파일만 덮어쓰고 원본에서 사라진 파일만 지운다. 데이터(~/ClaireData)와
# 사용자가 고친 config.json 값은 보존한다. 직전 스킬을 데이터 디렉터리 안에 한 세대 보관한다.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="${CLAIRE_SKILL_DIR:-$HOME/.claude/skills/claire-schedule-manager}"
DATA_DIR="${CLAIRE_DATA_DIR:-$HOME/ClaireData}"
PREV_DIR="${CLAIRE_PREV_DIR:-$DATA_DIR/skill-prev}"

PAYLOAD=(SKILL.md CHANGELOG.md scripts connectors references evals tests)

version_of() {
  local f="$1/scripts/claire_core.py"
  [ -f "$f" ] || return 0
  python3 - "$f" <<'PY'
import re, sys
m = re.search(r'^CLAIRE_VERSION\s*=\s*"([^"]+)"', open(sys.argv[1], encoding='utf-8').read(), re.M)
print(m.group(1) if m else "")
PY
}

NEW_VER="$(version_of "$SRC")"
OLD_VER="$(version_of "$SKILL_DIR")"

if [ -f "$SKILL_DIR/scripts/claire_core.py" ]; then
  MODE="업데이트"
  if [ "$OLD_VER" = "$NEW_VER" ]; then
    echo "== Claire 스킬 재적용 (버전 $OLD_VER, 변경분만 반영) =="
  else
    echo "== Claire 스킬 업데이트: $OLD_VER → $NEW_VER =="
  fi
else
  MODE="설치"
  echo "== Claire 스킬 설치: $NEW_VER =="
fi
echo "스킬  : $SKILL_DIR"
echo "데이터: $DATA_DIR  (claire.db·secrets·첨부·백업은 건드리지 않음)"
echo

if [ "$MODE" = "업데이트" ]; then
  mkdir -p "$(dirname "$PREV_DIR")"
  rm -rf "$PREV_DIR"
  cp -R "$SKILL_DIR" "$PREV_DIR"
  echo "이전 버전 보관: $PREV_DIR"
fi

mkdir -p "$SKILL_DIR"
python3 - "$SRC" "$SKILL_DIR" "${PAYLOAD[@]}" <<'PY'
import filecmp, shutil, sys
from pathlib import Path

src, dst, *payload = sys.argv[1:]
src, dst = Path(src), Path(dst)

def files_under(root, names):
    out = set()
    for name in names:
        p = root / name
        if p.is_file():
            out.add(Path(name))
        elif p.is_dir():
            for f in p.rglob("*"):
                if f.is_file() and "__pycache__" not in f.parts:
                    out.add(f.relative_to(root))
    return out

want = files_under(src, payload)
have = files_under(dst, payload)
added = updated = kept = 0
for rel in sorted(want):
    s, d = src / rel, dst / rel
    if d.exists():
        if filecmp.cmp(s, d, shallow=False):
            kept += 1
            continue
        updated += 1
    else:
        added += 1
    d.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(s, d)
removed = 0
for rel in sorted(have - want):
    (dst / rel).unlink()
    removed += 1
for cache in dst.rglob("__pycache__"):
    shutil.rmtree(cache, ignore_errors=True)
for name in payload:
    p = dst / name
    if p.is_dir():
        for d in sorted((x for x in p.rglob("*") if x.is_dir()), reverse=True):
            if not any(d.iterdir()):
                d.rmdir()
print(f"파일: 추가 {added} · 갱신 {updated} · 유지 {kept} · 삭제 {removed}")
PY

chmod +x "$SKILL_DIR"/scripts/claire_check "$SKILL_DIR"/scripts/claire_run "$SKILL_DIR"/scripts/claire_sync "$SKILL_DIR"/scripts/claire_store "$SKILL_DIR"/scripts/claire_search "$SKILL_DIR"/scripts/claire_apply \
         "$SKILL_DIR"/scripts/claire_export "$SKILL_DIR"/connectors/*.py

# 데이터 디렉터리·스키마·config.json (멱등). secrets/ 권한 700.
CLAIRE_DATA_DIR="$DATA_DIR" python3 "$SKILL_DIR/scripts/claire_check" init \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['message'], '(스키마 v%s)' % d['schema_version']); w=d.get('warning'); print('경고:', w) if w else None"
CLAIRE_DATA_DIR="$DATA_DIR" python3 "$SKILL_DIR/scripts/claire_check" integrity \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('무결성:', '이상 없음' if d['healthy'] else d['problems'])"

# OpenClaw Claire 에이전트 워크스페이스에도 판단 계층 문서를 둔다 (심볼릭 링크는 OpenClaw가 거부한다).
# 실행 스크립트는 ~/.claude/skills 한 곳만 쓴다. SKILL.md 의 경로가 그곳을 가리킨다.
OC_WS="${CLAIRE_OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace-claire}"
if [ -d "$OC_WS" ]; then
  OC_SKILL="$OC_WS/skills/claire-schedule-manager"
  mkdir -p "$OC_SKILL"
  cp "$SKILL_DIR/SKILL.md" "$SKILL_DIR/CHANGELOG.md" "$OC_SKILL/"
  rm -rf "$OC_SKILL/references" && cp -R "$SKILL_DIR/references" "$OC_SKILL/references"
  echo "OpenClaw 워크스페이스 스킬 갱신: $OC_SKILL (SKILL.md·references)"
fi

echo
echo "$MODE 완료 — claire-schedule-manager $NEW_VER"
echo "데이터베이스: $DATA_DIR/claire.db"
echo "다음: $SKILL_DIR/scripts/claire_check doctor --format text"
exit 0
