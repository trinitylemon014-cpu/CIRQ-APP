#!/usr/bin/env bash
# Downloads the LiveKit browser SDK into app/static/vendor/ so it is served from our own origin (the site's CSP only allows
# script-src 'self': no CDN). Run once on a machine with internet, commit the file, and redeploy.
#   scripts/vendor_livekit.sh            # pinned default version
#   scripts/vendor_livekit.sh 2.9.1      # or choose a version
set -euo pipefail
VERSION="${1:-2.9.1}"
OUT="$(cd "$(dirname "$0")/.." && pwd)/app/static/vendor/livekit-client.umd.min.js"
mkdir -p "$(dirname "$OUT")"
curl -fsSL "https://cdn.jsdelivr.net/npm/livekit-client@${VERSION}/dist/livekit-client.umd.min.js" -o "$OUT"
head -c 300 "$OUT" | grep -qi "livekit\|function" || { echo "download looks wrong"; rm -f "$OUT"; exit 1; }
echo "Saved $OUT ($(wc -c < "$OUT") bytes, livekit-client@${VERSION})."
echo "Test it on a real LiveKit project before relying on it. The room shows a clear message if this file is missing."
