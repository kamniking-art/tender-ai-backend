#!/usr/bin/env bash
set -euo pipefail

LOG_FILE="${1:-/root/opendata_discovery.txt}"
exec > >(tee -a "$LOG_FILE") 2>&1

echo "===== OPENDATA DISCOVERY START $(date -Is) ====="

URL="https://zakupki.gov.ru/epz/opendata/search/results.html"

echo "[A1] Download HTML: $URL"
curl -s -L -o /tmp/opendata.html "$URL"
echo "HTML_CODE=$?"
wc -c /tmp/opendata.html
head -n 80 /tmp/opendata.html

echo
echo "[A1.2] Extract script src"
grep -Eo 'src="[^"]+\.js[^"]*"' /tmp/opendata.html | head -n 20 | tee /tmp/opendata_scripts_raw.txt || true

i=0
while read -r src; do
  [[ -z "$src" ]] && continue
  rel="${src#src=\"}"
  rel="${rel%\"}"
  if [[ "$rel" =~ ^https?:// ]]; then
    full="$rel"
  else
    full="https://zakupki.gov.ru${rel}"
  fi
  i=$((i+1))
  out="/tmp/opendata_${i}.js"
  echo "Downloading JS[$i]: $full -> $out"
  curl -s -L -o "$out" "$full" || true
  [[ $i -ge 5 ]] && break
done < /tmp/opendata_scripts_raw.txt

echo
echo "[A1.3] grep api-like strings in HTML/JS"
grep -RInE "api|/opendata/|package|dataset|search|download|file|json" /tmp/opendata.html /tmp/opendata_*.js 2>/dev/null | head -n 200 || true

echo
echo "[A1.4] grep base/endpoint hints"
grep -RInE "BASE|endpoint|host|origin|opendata" /tmp/opendata.html /tmp/opendata_*.js 2>/dev/null | head -n 200 || true

echo
echo "[A2] Probe typical URLs"
for u in \
  "https://zakupki.gov.ru/epz/opendata/search" \
  "https://zakupki.gov.ru/epz/opendata/search/results.html" \
  "https://zakupki.gov.ru/epz/opendata" \
  "https://zakupki.gov.ru/epz/opendata/api" \
  "https://zakupki.gov.ru/epz/opendata/api/search" \
  "https://zakupki.gov.ru/epz/opendata/api/public" \
  "https://zakupki.gov.ru/epz/opendata/api/v1" \
  "https://zakupki.gov.ru/epz/opendata/api/v2"; do
  echo "== $u =="
  curl -s -I "$u" | head -n 5
  echo
done

echo "===== OPENDATA DISCOVERY END $(date -Is) ====="
