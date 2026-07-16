#!/bin/bash
# make_icns.sh — rasterize assets/earditor-icon.svg into assets/Earditor.icns.
#
# Run this after editing the SVG, then commit the regenerated .icns (the build
# consumes the .icns, not the SVG):
#
#     packaging/make_icns.sh && git add packaging/assets/Earditor.icns
#
# ALPHA MUST SURVIVE. The icon is a squircle inset on a transparent 1024 canvas
# (Apple's 824/1024 grid); a rasterizer that flattens transparency to white
# produces the white-slab-with-margins Dock icon this script exists to avoid.
# `qlmanage` does exactly that — it is deliberately NOT used here.
#
# Needs one of:
#   rsvg-convert   brew install librsvg
#   cairosvg       pip install cairosvg   (needs: brew install cairo)
set -euo pipefail
cd "$(dirname "$0")"

SVG="assets/earditor-icon.svg"
ICNS="assets/Earditor.icns"
ICONSET="$(mktemp -d)/Earditor.iconset"
mkdir -p "$ICONSET"
trap 'rm -rf "$(dirname "$ICONSET")"' EXIT

[ -f "$SVG" ] || { echo "missing $SVG"; exit 1; }

if command -v rsvg-convert >/dev/null 2>&1; then
  RENDERER=rsvg-convert
elif python3 -c "import cairosvg" >/dev/null 2>&1; then
  RENDERER=cairosvg
else
  echo "No SVG rasterizer found. Install one:" >&2
  echo "  brew install librsvg        # provides rsvg-convert" >&2
  echo "  pip install cairosvg        # needs: brew install cairo" >&2
  exit 1
fi
echo "renderer: $RENDERER"

render() {  # render <px> <outfile>
  case "$RENDERER" in
    rsvg-convert)
      rsvg-convert -w "$1" -h "$1" -f png -o "$2" "$SVG" ;;
    cairosvg)
      python3 -c "import cairosvg,sys; cairosvg.svg2png(url=sys.argv[1], write_to=sys.argv[2], output_width=int(sys.argv[3]), output_height=int(sys.argv[3]))" \
        "$SVG" "$2" "$1" ;;
  esac
}

# iconutil wants this exact set of names; @2x is the same pixel size as the next
# size up, drawn for a retina slot.
for size in 16 32 128 256 512; do
  render "$size"            "$ICONSET/icon_${size}x${size}.png"
  render "$((size * 2))"    "$ICONSET/icon_${size}x${size}@2x.png"
done

# Fail loudly rather than shipping a white slab: a correct render has a
# transparent corner pixel (the squircle's inset margin).
if command -v sips >/dev/null 2>&1; then
  if ! sips -g hasAlpha "$ICONSET/icon_512x512.png" | grep -q "hasAlpha: yes"; then
    echo "ERROR: rendered PNGs have no alpha channel — the Dock icon would be a white slab." >&2
    exit 1
  fi
fi

iconutil -c icns "$ICONSET" -o "$ICNS"
echo "wrote $ICNS ($(du -h "$ICNS" | cut -f1))"
