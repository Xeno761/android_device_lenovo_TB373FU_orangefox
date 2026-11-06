#!/usr/bin/env bash
# Fast iteration helper: repack ONLY the recovery ramdisk fragment from the
# already-built AOSP out/ tree, without re-running the full Android build.
# Use after editing device-tree files under recovery/root or twres.
set -euo pipefail

WORKSPACE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
FOX=${FOX_SOURCE_ROOT:-/root/fox_14.1}
TREE=$WORKSPACE
PROD=$FOX/out/target/product/TB373FU
IMG=${RECOVERY_OUTPUT:-$WORKSPACE/OrangeFox-TB373FU-FinalBase-Dark120.img}
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

echo "[2/6] Enforcing /etc -> /system/etc symlink"
if [ ! -L "$PROD/recovery/root/etc" ]; then
    rm -rf "$PROD/recovery/root/etc"
    ln -s /system/etc "$PROD/recovery/root/etc"
fi

# MediaTek's KeyMint TA (isee510) compares the userdata metadata key blob's
# patch level against ro.build.version.security_patch, NOT ro.vendor.*. The
# AOSP-14 recovery base declares 2024-09-05, so the unwrap is rejected with
#   [TZ_LOG] km7 | [CheckPatchLevel:82] Key blob invalid! key patchlevel 202608
#            is > current patchlevel 202409
# and /data cannot be decrypted. Force it to the installed ROM's platform
# security patch level (settings > about, or getprop on the running ROM).
echo "[3/6] Applying recovery crypto profile before TEE startup"
RECOVERY_PLATFORM_SECURITY_PATCH="${RECOVERY_PLATFORM_SECURITY_PATCH:-2026-08-05}"
PROP="$PROD/recovery/root/prop.default"
[[ "$RECOVERY_PLATFORM_SECURITY_PATCH" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]
profile=("ro.build.version.security_patch=$RECOVERY_PLATFORM_SECURITY_PATCH")
if [ -n "${RECOVERY_PLATFORM_VERSION:-}" ]; then
    [[ "$RECOVERY_PLATFORM_VERSION" =~ ^[0-9]{1,2}(\.[0-9]{1,2}){0,2}$ ]]
    profile+=("ro.build.version.release=$RECOVERY_PLATFORM_VERSION")
fi
if [ -n "${RECOVERY_VENDOR_SECURITY_PATCH:-}" ]; then
    [[ "$RECOVERY_VENDOR_SECURITY_PATCH" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]
    profile+=("ro.vendor.build.security_patch=$RECOVERY_VENDOR_SECURITY_PATCH")
fi
for binding in "${profile[@]}"; do
    name=${binding%%=*}
    pattern=${name//./\\.}
    if grep -q "^$pattern=" "$PROP"; then
        sed -i "s|^$pattern=.*|$binding|" "$PROP"
    else
        printf '%s\n' "$binding" >> "$PROP"
    fi
done
grep -E '^ro\.(build\.version\.(release|security_patch)|vendor\.build\.security_patch)=' "$PROP"

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
