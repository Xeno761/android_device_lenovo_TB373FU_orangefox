#!/usr/bin/env bash
# Fast iteration helper: repack ONLY the recovery ramdisk fragment from the
# already-built AOSP out/ tree, without re-running the full Android build.
# Use after editing device-tree files under recovery/root or twres.
set -euo pipefail

WORKSPACE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
FOX=${FOX_SOURCE_ROOT:-/root/fox_14.1}
TREE=$WORKSPACE
PROD=$FOX/out/target/product/TB373FU
IMG=${RECOVERY_OUTPUT:-$WORKSPACE/OrangeFox-TB373FU-Adaptive.img}
BASE=${VENDOR_BOOT_BASE:?Set VENDOR_BOOT_BASE to the approved driver vendor_boot image}
INIT_BOOT=${VALIDATION_INIT_BOOT:?Set VALIDATION_INIT_BOOT to the matching installed init_boot image}
test -d "$(dirname "$IMG")"
test -f "$BASE"
test -f "$INIT_BOOT"

echo "[1/6] Syncing device-tree recovery/root overlay"
cp -a "$TREE"/recovery/root/. "$PROD/recovery/root/"
# Make installs this backend in system/, but recovery loads its private copy.
# A successful rebuild must not leave the old display/input library in the image.
test -f "$PROD/system/lib64/libminuitwrp.so"
cp -a "$PROD/system/lib64/libminuitwrp.so" "$PROD/recovery/root/system/lib64/libminuitwrp.so"
test -f "$PROD/system/bin/tb373fu_crypto_profile"
cp -a "$PROD/system/bin/tb373fu_crypto_profile" "$PROD/recovery/root/system/bin/tb373fu_crypto_profile"
cp -a "$FOX/vendor/recovery/FoxExtras/sbin/foxstart.sh" "$PROD/recovery/root/sbin/foxstart.sh"

echo "[2/6] Enforcing /etc -> /system/etc symlink"
if [ ! -L "$PROD/recovery/root/etc" ]; then
    rm -rf "$PROD/recovery/root/etc"
    ln -s /system/etc "$PROD/recovery/root/etc"
fi

# Unknown profiles remain gated rather than using a hardcoded release/SPL.
echo "[3/6] Enabling runtime crypto-profile gating"
PROP="$PROD/recovery/root/prop.default"
if grep -q '^ro\.recovery\.crypto_profile=' "$PROP"; then
    sed -i 's|^ro\.recovery\.crypto_profile=.*|ro.recovery.crypto_profile=adaptive|' "$PROP"
else
    printf '%s\n' 'ro.recovery.crypto_profile=adaptive' >> "$PROP"
fi
grep '^ro.recovery.crypto_profile=' "$PROP"

echo "[4/6] Building recovery.cpio.lz4"
"$FOX"/out/host/linux-x86/bin/mkbootfs -d "$PROD/system" "$PROD/recovery/root" \
  | lz4 -l -12 --favor-decSpeed -f > "$PROD/obj/PACKAGING/vendor_ramdisk_fragments_intermediates/recovery.cpio.lz4"

echo "[5/6] Replacing recovery only in the approved improved-driver base"
python3 -B "$WORKSPACE/tools/repack_current_vendor_boot.py" \
  --base "$BASE" \
  --recovery-ramdisk "$PROD/obj/PACKAGING/vendor_ramdisk_fragments_intermediates/recovery.cpio.lz4" \
  --init-boot "$INIT_BOOT" --output "$IMG"

echo "[6/6] Full selected-stream and AVB validation complete"

ls -lh "$IMG"
sha256sum "$IMG"
