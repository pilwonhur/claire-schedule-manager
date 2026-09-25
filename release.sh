#!/usr/bin/env bash
# 릴리스 도구 (관리자용, mac mini 에서만 — 저장소가 Dropbox 안이라 git 은 한 기기에서만 쓴다)
#
#   ./release.sh bump 0.5.1     버전 문자열 4곳(VERSION·claire_core.py·SKILL.md 머리·제목)을 바꾸고 CHANGELOG 에 빈 절을 만든다
#   ./release.sh check          버전 일치·CHANGELOG 절·테스트를 확인한다 (바꾸는 것 없음)
#   ./release.sh tag            check 뒤 커밋이 깨끗하면 v<VERSION> 태그를 만들고 main 과 태그를 push 한다 (확인 질문)
#
# 태그를 push 하면 GitHub Actions(.github/workflows/release.yml)가 테스트 후 GitHub Release 를 만든다.
# 설치된 기기에서는 claire-update 로 받는다.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

ver_file()   { tr -d ' \n' < VERSION; }
ver_core()   { sed -n 's/^CLAIRE_VERSION = "\(.*\)"/\1/p' scripts/claire_core.py; }
ver_skill()  { sed -n 's/^version: \(.*\)$/\1/p' SKILL.md | head -1; }

check() {
  local v; v="$(ver_file)"
  local ok=1
  [ "$(ver_core)" = "$v" ]  || { echo "✗ scripts/claire_core.py CLAIRE_VERSION=$(ver_core) ≠ VERSION=$v"; ok=0; }
  [ "$(ver_skill)" = "$v" ] || { echo "✗ SKILL.md version: $(ver_skill) ≠ VERSION=$v"; ok=0; }
  grep -q "^## \[$v\]" CHANGELOG.md || { echo "✗ CHANGELOG.md 에 '## [$v]' 절이 없음"; ok=0; }
  if grep -A3 "^## \[$v\]" CHANGELOG.md | grep -q "(작성 중)"; then echo "✗ CHANGELOG [$v] 가 아직 '(작성 중)'"; ok=0; fi
  [ "$ok" = 1 ] || exit 1
  echo "✓ 버전 $v 일치 (VERSION · claire_core · SKILL.md · CHANGELOG)"
  python3 tests/test_claire.py > /tmp/claire-release-tests.log 2>&1 || { tail -30 /tmp/claire-release-tests.log; echo "✗ 테스트 실패"; exit 1; }
  echo "✓ $(grep -Eo 'Ran [0-9]+ tests' /tmp/claire-release-tests.log) 통과"
}

case "${1:-}" in
  bump)
    new="${2:?새 버전 (예: 0.5.1)}"
    echo "$new" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' || { echo "SemVer(X.Y.Z)로 적어 주세요"; exit 1; }
    old="$(ver_file)"
    printf '%s\n' "$new" > VERSION
    sed -i '' "s/^CLAIRE_VERSION = \"$old\"/CLAIRE_VERSION = \"$new\"/" scripts/claire_core.py
    sed -i '' "s/^version: $old\$/version: $new/; s/(v$old /(v$new /" SKILL.md
    if ! grep -q "^## \[$new\]" CHANGELOG.md; then
      python3 - "$new" <<'PY'
import datetime, re, sys
v = sys.argv[1]
p = "CHANGELOG.md"; s = open(p, encoding="utf-8").read()
stub = f"## [{v}] — {datetime.date.today():%Y-%m-%d}\n\n(작성 중)\n\n"
i = s.index("\n## [") + 1
open(p, "w", encoding="utf-8").write(s[:i] + stub + s[i:])
PY
    fi
    echo "버전 $old → $new. CHANGELOG.md 의 [$new] 절을 채운 뒤 커밋하고 ./release.sh tag"
    ;;
  check)
    check
    ;;
  tag)
    check
    v="$(ver_file)"
    [ "$(git rev-parse --abbrev-ref HEAD)" = "main" ] || { echo "main 브랜치에서만 태그합니다"; exit 1; }
    [ -z "$(git status --porcelain)" ] || { echo "커밋 안 된 변경이 있습니다:"; git status --short; exit 1; }
    git rev-parse -q --verify "refs/tags/v$v" >/dev/null && { echo "태그 v$v 가 이미 있습니다. ./release.sh bump 로 버전을 올리세요"; exit 1; }
    echo "태그 v$v 를 만들고 origin 에 main 과 함께 push 합니다."
    read -r -p "계속할까요? [y/N] " yn
    [ "$yn" = "y" ] || [ "$yn" = "Y" ] || { echo "취소"; exit 1; }
    notes="$(awk -v v="$v" '$0 ~ "^## \\[" v "\\]" {f=1; next} /^## \[/ {f=0} f' CHANGELOG.md)"
    git tag -a "v$v" -m "claire-schedule-manager $v" -m "$notes"
    git push origin main "v$v"
    echo "완료. 설치된 기기에서: claire-update"
    ;;
  *)
    sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
