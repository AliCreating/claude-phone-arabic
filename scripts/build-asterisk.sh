#!/usr/bin/env bash
# Build Asterisk 22 from source into ~/.claude-phone/asterisk.
# Recipe and the reason for every fix: docs/build-notes.md section 1.
set -euo pipefail

VER=22.11.0
ROOT="${CLAUDE_PHONE_HOME:-$HOME/.claude-phone}"
PREFIX="$ROOT/asterisk"
SRC="$ROOT/src"
SSL=/opt/homebrew/opt/openssl@3
TARBALL="${ASTERISK_TARBALL:-}"
DARWIN_TARGET="aarch64-apple-darwin$(uname -r)"

mkdir -p "$SRC" "$PREFIX"
cd "$SRC"
if [ ! -d "asterisk-$VER" ]; then
  if [ -z "$TARBALL" ]; then
    TARBALL="$SRC/asterisk-$VER.tar.gz"
    curl -fL -o "$TARBALL" "https://downloads.asterisk.org/pub/telephony/asterisk/asterisk-$VER.tar.gz"
  fi
  tar xzf "$TARBALL"
fi
cd "asterisk-$VER"

# Fix 4: macOS has no TCP_KEEPIDLE.
if ! grep -q 'define TCP_KEEPIDLE TCP_KEEPALIVE' res/res_http_websocket.c; then
  awk 'BEGIN{done=0} /^#include/ {last=NR} {lines[NR]=$0} END{for(i=1;i<=NR;i++){print lines[i]; if(i==last){print "#if defined(__APPLE__) && !defined(TCP_KEEPIDLE)"; print "#define TCP_KEEPIDLE TCP_KEEPALIVE"; print "#endif"}}}' \
    res/res_http_websocket.c > res/res_http_websocket.c.new && mv res/res_http_websocket.c.new res/res_http_websocket.c
fi

# Fix 1
./configure --prefix="$PREFIX" --with-jansson-bundled --with-pjproject-bundled \
  PJPROJECT_CONFIGURE_OPTS="--with-ssl=$SSL" CPPFLAGS="-I$SSL/include" LDFLAGS="-L$SSL/lib"

# Fix 2: configure appends a bare --with-ssl that pjproject reads as prefix "yes".
sed -i '' "s|^PJPROJECT_CONFIGURE_OPTS=.*|PJPROJECT_CONFIGURE_OPTS=--with-ssl=$SSL|" makeopts

# Fix 3: upstream hardcodes an old darwin target triple.
sed -i '' "s|^PJ_TARGET := .*|PJ_TARGET := $DARWIN_TARGET|" main/Makefile

# Fix 5
make menuselect.makeopts
menuselect/menuselect --disable res_geolocation --disable res_pjsip_geolocation \
  --disable app_voicemail --disable app_voicemail_imap --disable app_voicemail_odbc \
  --enable res_mwi_external --enable res_mwi_external_ami --enable res_ari_mailboxes \
  menuselect.makeopts

# Fix 6: pjproject's install step fails on mkdir /opt/pjproject; harmless (statically linked).
make -j"$(sysctl -n hw.ncpu)"
make -k install || true

# Fix 7: regenerate XML docs or Stasis init fails.
rm -f doc/core-en_US.xml
make doc/core-en_US.xml
mkdir -p "$PREFIX/var/lib/asterisk/documentation"
cp doc/core-en_US.xml doc/appdocsxml.* "$PREFIX/var/lib/asterisk/documentation/"

test -x "$PREFIX/sbin/asterisk"
echo "BUILD OK: $PREFIX/sbin/asterisk"
