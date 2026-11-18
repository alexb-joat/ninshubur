#!/usr/bin/env sh
# Refresh the vendored Public Suffix List snapshot. One request, to the only URL the list's maintainers support.
# Review the diff, run the tests, then commit the file with the VERSION line in the message.
set -eu
out="$(dirname "$0")/../ninshubur/data/public_suffix_list.dat"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
curl -fsS --proto '=https' -o "$tmp" https://publicsuffix.org/list/public_suffix_list.dat
python3 - "$tmp" "$out" <<'PY'
import sys
src = open(sys.argv[1], encoding="utf-8").read().splitlines()
if not any("===BEGIN ICANN DOMAINS===" in l for l in src):
    sys.exit("downloaded file does not look like the Public Suffix List; nothing written")
out = src[:9] + ["// Comments below this header were removed for size (MPL-2.0 permits modification); rules and the",
                 "// ICANN/PRIVATE section markers are unchanged. Refresh: scripts/update_psl.sh", ""]
out += [s for s in (l.strip() for l in src[9:]) if s and (not s.startswith("//") or "===" in s)]
open(sys.argv[2], "w", encoding="utf-8").write("\n".join(out) + "\n")
print(next((l for l in src if l.startswith("// VERSION:")), "// VERSION: unknown"))
PY
