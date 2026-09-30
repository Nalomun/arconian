#!/usr/bin/env bash
# Rebuild docs/ARCONIAN_WRITEUP.pdf from docs/ARCONIAN_WRITEUP.md.
# Needs pandoc and google-chrome. Run from the repo root.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"
pandoc docs/ARCONIAN_WRITEUP.md -f gfm-tex_math_dollars -t html5 -s --embed-resources \
  --resource-path=docs -c "$HERE/writeup_pdf.css" --metadata title="Arconian writeup" -o "$TMP/writeup.html"
# drop pandoc's generated title block; the markdown carries its own H1
python3 -c "import re,sys;p=sys.argv[1];s=open(p).read();s=re.sub(r'<header id=\"title-block-header\">.*?</header>','',s,flags=re.S);open(p,'w').write(s)" "$TMP/writeup.html"
google-chrome --headless=new --disable-gpu --no-sandbox --no-pdf-header-footer \
  --print-to-pdf=docs/ARCONIAN_WRITEUP.pdf "file://$TMP/writeup.html" 2>/dev/null
rm -rf "$TMP"
echo "wrote docs/ARCONIAN_WRITEUP.pdf"
