#!/usr/bin/env bash
set -euo pipefail

URL='https://zakupki.gov.ru/epz/order/extendedsearch/results.html?searchString=гранит&pageNumber=1&recordsPerPage=50'

echo '=== A1: plain request ==='
code_plain=$(curl -s -o /tmp/eis.html -w "%{http_code}" "$URL")
ctype_plain=$(file -b --mime-type /tmp/eis.html 2>/dev/null || true)
echo "http_code=$code_plain"
echo "content_type_guess=$ctype_plain"
head -n 60 /tmp/eis.html || true

echo
echo '=== A2: browser-like request with cookies/referer ==='
code_browser=$(curl -s -c /tmp/cj.txt -b /tmp/cj.txt -L -o /tmp/eis2.html -w "%{http_code}" \
  -H "User-Agent: Mozilla/5.0" \
  -H "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8" \
  -H "Accept-Language: ru-RU,ru;q=0.9,en;q=0.8" \
  -H "Referer: https://zakupki.gov.ru/epz/main/public/home.html" \
  "$URL")
ctype_browser=$(file -b --mime-type /tmp/eis2.html 2>/dev/null || true)
echo "http_code=$code_browser"
echo "content_type_guess=$ctype_browser"
head -n 60 /tmp/eis2.html || true
