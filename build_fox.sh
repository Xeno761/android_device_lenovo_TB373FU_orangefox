#!/bin/bash
set -eo pipefail

WORKSPACE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT=${FOX_SOURCE_ROOT:-/root/fox_14.1}
TREE=$WORKSPACE
export FOX_SOURCE_ROOT="$ROOT"
test -d "$WORKSPACE"
export RECOVERY_OUTPUT="${RECOVERY_OUTPUT:-$WORKSPACE/OrangeFox-TB373FU-Adaptive.img}"
test ! -e "$RECOVERY_OUTPUT" || { echo "Refusing to overwrite existing image: $RECOVERY_OUTPUT" >&2; exit 1; }

echo "=========================================="
echo "OrangeFox R12.1 Local Build TB373FU"
echo "Started at: $(date)"
echo "=========================================="

export USE_CCACHE=1
export CCACHE_EXEC=/usr/bin/ccache
export CCACHE_DIR="${CCACHE_DIR:-$ROOT/out/.ccache}"
mkdir -p "$CCACHE_DIR"
export MALLOC_ARENA_MAX=2
export BUILD_BROKEN_DISABLE_BAZEL=true
export BUILD_BROKEN_SRC_DIR_IS_WRITABLE=true
export ALLOW_MISSING_DEPENDENCIES=true
export LC_ALL=C

# OrangeFox Build Flags
export FOX_BUILD_TYPE="Unofficial"
export OF_MAINTAINER="XenoOP"
export FOX_MAINTAINER_PATCH_VERSION=1
export OF_DEVICE_WITHOUT_RECOVERY=1
export FOX_VIRTUAL_AB_DEVICE=1
export OF_SCREEN_W=2944
export OF_SCREEN_H=1840
export OF_STATUS_H=100
export OF_STATUS_INDENT_LEFT=48
export OF_STATUS_INDENT_RIGHT=48
export OF_CLOCK_POS=1
export OF_ALLOW_DISABLE_NAVBAR=0
export OF_USE_MAGISKBOOT=1
export OF_USE_NEW_MAGISKBOOT=1
export OF_NO_TREBLE_COMPATIBILITY_CHECK=1
export OF_NO_RELOAD_AFTER_DEC=1
export OF_SKIP_MULTIUSER_FOLDERS_BACKUP=1
export OF_RUN_POST_FORMAT_PROCESS=1
# Firmware build metadata is not a substitute for the installed ROM's crypto
# profile. Runtime discovery supplies OS/vendor bindings before KeyMint starts.
export BOOT_SECURITY_PATCH=2026-05-05
export VENDOR_SECURITY_PATCH=2026-05-05

test -d "$ROOT/bootable/recovery"
test -d "$ROOT/system/vold"
test -d "$ROOT/vendor/recovery/FoxExtras"
# Apply direct source components; no patch files or Git history are required.
tar -C "$WORKSPACE/components/recovery" -cf - . | tar -C "$ROOT/bootable/recovery" -xf -
tar -C "$WORKSPACE/components/vold" -cf - . | tar -C "$ROOT/system/vold" -xf -
tar -C "$WORKSPACE/components/foxextras" -cf - . | tar -C "$ROOT/vendor/recovery/FoxExtras" -xf -
python3 -B "$WORKSPACE/tools/make_landscape_theme.py" "$ROOT"
cd "$ROOT"

# Sync verified device tree into build tree
mkdir -p device/lenovo/TB373FU
tar -C "$TREE" --exclude=.git --exclude=./components --exclude=./tools \
    --exclude=./build_fox.sh --exclude=./quick_repack.sh -cf - . | tar -C device/lenovo/TB373FU -xf -

echo "[1/4] Setting up build environment..."
source build/envsetup.sh

echo "[2/4] Selecting target fox_TB373FU-ap2a-eng..."
lunch fox_TB373FU-ap2a-eng

# Force the selected built-in resources to replace any stale portrait staging.
rm -rf out/target/product/TB373FU/recovery/root/twres

echo "[3/4] Compiling adbd and vendorbootimage..."
m adbd tb373fu_crypto_profile vendorbootimage -j4

echo "[4/4] Packing and validating the built-in horizontal recovery..."
bash "$WORKSPACE/quick_repack.sh"
python3 -B "$WORKSPACE/tools/validate_recovery_image.py" "$RECOVERY_OUTPUT" \
    --expect-adaptive-crypto \
    --expect-theme-width 2944 --expect-mtp \
    --expect-built-in-ui "$ROOT/bootable/recovery/gui/theme/landscape_hdpi" \
    --expect-minui "$ROOT/out/target/product/TB373FU/system/lib64/libminuitwrp.so" \
    --expect-crypto-detector "$ROOT/out/target/product/TB373FU/system/bin/tb373fu_crypto_profile"

echo "=========================================="
echo "ORANGEFOX BUILD COMPLETE!"
ls -lh "$RECOVERY_OUTPUT"
sha256sum "$RECOVERY_OUTPUT"
echo "=========================================="
