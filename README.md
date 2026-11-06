# OrangeFox Recovery for Lenovo TB373FU

Device configuration and direct recovery/UI components for the Lenovo Idea Tab Pro,
MediaTek MT6897/MT8792, NT36532 touchscreen. Maintainer: **XenoOP**.

## Included

- Device build configuration, firmware dependencies and recovery init files.
- `components/recovery`: directly compiled recovery changes and built-in UI resources.
- `components/vold`: the decryption implementation used by this build.
- `components/foxextras`: startup integration that preserves the verified crypto profile.
- Build and image-validation tools only; no patch archive or workflow.

## Features and Fixes

- Built-in native 2944x1840 landscape layout with dark default and full-width keyboard.
- Corrected sidebar/arrow alignment, backup keyboard clearance and maintainer image.
- Built-in scrolling Credits, Translators and Changelog under About, without external text-file dependencies.
- Native 120 Hz mode selection, MediaTek mode-index handling and smoother time-based scrolling.
- Recovery ADB, Windows MTP transfers and fastbootd entry/return verified on-device.
- Metadata and user-0 decryption verified with the tested Android 17 One UI port.
- Driver-preserving vendor_boot repacking, validated ramdisk framing and corrected HAL manifests.
- Audited Mount/backup targets, consistent partition labels and guarded crypto-mapper release before Format Data.
- Active-ROM crypto profile detection, normal Metadata recovery before inspection, and a no-upgrade key verification gate.
- Automatic Android timezone selection, power-key wake fixes, and reduced transparent/rounded-texture rendering work.
- Right-aligned Files sorting without a redundant overflow menu.

## Build

Start with an OrangeFox `fox_14.1` / Android 14 source tree. The component baselines are
`bootable/recovery` commit `bcff7d6e9ee6f80387d1b036c0f9e58216053b69` and `system/vold` commit
`b865130d263a84d86440f6b8b9e1f08c6938d6a5`. The script copies the direct components into that
tree before compiling; keep this device repository outside the Android tree.

Required tools include the normal Android build dependencies, Python 3.14+ (`compression.zstd`),
LZ4, liblz4/libzstd and AVB tools. Provide the driver-base and matching installed generic-ramdisk
images separately.

```bash
FOX_SOURCE_ROOT=/path/to/fox_14.1 \
VENDOR_BOOT_BASE=/path/to/vendor_boot_final.img \
VALIDATION_INIT_BOOT=/path/to/init_boot.img \
RECOVERY_OUTPUT=/path/to/new-recovery.img \
bash build_fox.sh
```

The build preserves the supplied platform drivers, DTB, boot arguments and board IDs. It checks
the complete selected ramdisk stream, manifests, AVB integrity, crypto profile and baked resources.
Use the final image from `RECOVERY_OUTPUT`, not intermediate images or ZIPs from Android build hooks.

The current tested ROM uses release 17, OS patch 2026-08-05 and vendor/boot patch 2026-05-05.
The detector accepts numeric Android 14-17 profiles but requires compatible vendor crypto
components and safe storage state. Other ROMs and credential-protected CE require separate
hardware verification; this is not an unconditional all-ROM compatibility claim.
Data backup excludes internal storage and
boot-image backups select the active slot. Destructive wipe/restore/format/re-encryption and USB OTG
media have not been exercised on user data. License notices remain with the source components.
