#!/usr/bin/env python3
"""Read-only validation of this device's vendor_boot v4 image and recovery fragment."""

import argparse
import hashlib
import gzip
import stat
import struct
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path


def check(condition, message):
    if not condition:
        raise ValueError(message)


def inspect_image(path):
    image = Path(path).read_bytes()
    check(len(image) == 67108864, "vendor_boot must be exactly 64 MiB")
    check(image[:8] == b"VNDRBOOT", "not a vendor_boot image")
    version, page_size = struct.unpack_from("<II", image, 8)
    check(version == 4 and page_size == 4096, "unexpected boot header or page size")
    header_size, dtb_size = struct.unpack_from("<II", image, 2096)
    check(header_size == 2128, "unexpected vendor_boot v4 header size")

    magic, major, minor, original_size, vbmeta_offset, vbmeta_size = struct.unpack_from(
        ">4sIIQQQ28x", image, len(image) - 64
    )
    check(magic == b"AVBf" and major == 1, "missing or unsupported AVB footer")
    check(0 < original_size <= vbmeta_offset < len(image) - 64, "invalid AVB offsets")
    check(vbmeta_offset + vbmeta_size <= len(image) - 64, "truncated vbmeta")
    check(image[vbmeta_offset:vbmeta_offset + 4] == b"AVB0", "missing vbmeta header")
    auth_size, aux_size, algorithm = struct.unpack_from(
        ">QQI", image, vbmeta_offset + 12
    )
    check(algorithm == 0 and auth_size == 0, "expected current unsigned NONE AVB profile")
    descriptor_offset, descriptor_size = struct.unpack_from(
        ">QQ", image, vbmeta_offset + 96
    )
    check(descriptor_offset + descriptor_size <= aux_size, "invalid descriptor bounds")
    check(256 + auth_size + aux_size <= vbmeta_size, "truncated vbmeta blocks")
    descriptor = vbmeta_offset + 256 + auth_size + descriptor_offset
    tag, following = struct.unpack_from(">QQ", image, descriptor)
    check(tag == 2 and following + 16 == descriptor_size, "expected one hash descriptor")
    hash_size = struct.unpack_from(">Q", image, descriptor + 16)[0]
    hash_algorithm = image[descriptor + 24:descriptor + 56].split(b"\0", 1)[0].decode()
    name_size, salt_size, digest_size, flags = struct.unpack_from(
        ">IIII", image, descriptor + 56
    )
    check(132 + name_size + salt_size + digest_size <= descriptor_size,
          "invalid hash descriptor lengths")
    payload = descriptor + 132
    partition = image[payload:payload + name_size]
    salt = image[payload + name_size:payload + name_size + salt_size]
    digest = image[payload + name_size + salt_size:
                   payload + name_size + salt_size + digest_size]
    check(partition == b"vendor_boot" and flags == 0, "unexpected AVB partition profile")
    check(hash_size == original_size, "hash descriptor does not cover the original image")
    check(hash_algorithm == "sha256", "unexpected hash algorithm")
    calculated = hashlib.sha256(salt + image[:hash_size]).digest()
    check(calculated == digest, "AVB hash mismatch")

    align = lambda value: (value + page_size - 1) // page_size * page_size
    ramdisk_size = struct.unpack_from("<I", image, 24)[0]
    ramdisk_start = align(header_size)
    dtb_start = align(ramdisk_start + ramdisk_size)
    table_start = align(dtb_start + dtb_size)
    table_size, count, entry_size, bootconfig_size = struct.unpack_from("<IIII", image, 2112)
    check(entry_size == 108 and count * entry_size == table_size, "invalid ramdisk table")
    check(align(table_start + table_size) + bootconfig_size <= original_size,
          "ramdisk table or bootconfig extends past the image")
    fragments = {}
    for index in range(count):
        entry = table_start + index * entry_size
        size, offset, kind = struct.unpack_from("<III", image, entry)
        name = image[entry + 12:entry + 44].split(b"\0", 1)[0].decode()
        check(offset + size <= ramdisk_size, "ramdisk fragment out of bounds")
        check((kind, name) not in fragments, "duplicate ramdisk fragment")
        fragments[kind, name] = image[ramdisk_start + offset:ramdisk_start + offset + size]
    check((2, "recovery") in fragments, "missing named recovery ramdisk fragment")
    return fragments, image[dtb_start:dtb_start + dtb_size]


def read_cpio(fragment):
    fragment = fragment.lstrip(b"\0")
    if fragment[:2] == b"\x1f\x8b":
        archive = gzip.decompress(fragment)
    elif fragment[:4] == b"\x28\xb5\x2f\xfd":
        from compression import zstd
        decoder = zstd.ZstdDecompressor()
        archive = decoder.decompress(fragment)
        check(decoder.eof and not any(decoder.unused_data), "invalid compressed recovery frame or padding")
    else:
        archive = subprocess.run(["lz4", "-d", "-c"], input=fragment,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
    files = {}
    offset = 0
    while offset + 110 <= len(archive):
        check(archive[offset:offset + 6] == b"070701", "invalid newc archive header")
        fields = [int(archive[offset + 6 + i * 8:offset + 14 + i * 8], 16)
                  for i in range(13)]
        mode, size, name_size = fields[1], fields[6], fields[11]
        name_start = offset + 110
        check(name_size > 0 and name_start + name_size <= len(archive), "invalid cpio filename")
        check(archive[name_start + name_size - 1] == 0, "cpio filename is not NUL terminated")
        name = archive[name_start:name_start + name_size - 1].decode()
        data_start = (name_start + name_size + 3) & ~3
        check(data_start + size <= len(archive), "truncated cpio content")
        if name == "TRAILER!!!":
            return files
        name = name.removeprefix("./")
        check(name not in files, "duplicate cpio path: " + name)
        files[name] = mode, archive[data_start:data_start + size]
        offset = (data_start + size + 3) & ~3
    raise ValueError("cpio trailer missing")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("--reference")
    parser.add_argument("--expect-release")
    parser.add_argument("--expect-vendor-patch")
    parser.add_argument("--expect-theme-width", type=int)
    parser.add_argument("--expect-adb-fallback", action="store_true")
    parser.add_argument("--expect-mtp", action="store_true")
    parser.add_argument("--expect-built-in-ui", help="generated resource directory whose contents must be baked into twres")
    parser.add_argument("--expect-minui", help="compiled display/input library that must be baked into recovery")
    args = parser.parse_args()
    fragments, dtb = inspect_image(args.image)
    files = read_cpio(fragments[2, "recovery"])
    check("etc" in files and stat.S_ISLNK(files["etc"][0])
          and files["etc"][1] == b"/system/etc", "broken /etc -> /system/etc symlink")
    properties = {}
    for line in files["prop.default"][1].decode().splitlines():
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            if name in ("ro.build.version.release", "ro.build.version.security_patch",
                        "ro.vendor.build.security_patch"):
                check(name not in properties, "duplicate crypto property: " + name)
                properties[name] = value
    for name, expected in (("ro.build.version.release", args.expect_release),
                           ("ro.vendor.build.security_patch", args.expect_vendor_patch)):
        if expected is not None:
            check(properties.get(name) == expected, "unexpected " + name)
    resolution = ET.fromstring(files["twres/ui.xml"][1]).find("details/resolution")
    check(resolution is not None, "theme resolution missing")
    if args.expect_theme_width is not None:
        check(int(resolution.get("width")) == args.expect_theme_width, "wrong baked theme width")
    if args.expect_minui:
        check(files["system/lib64/libminuitwrp.so"][1] == Path(args.expect_minui).read_bytes(),
              "stale display/input backend in recovery ramdisk")
        print("PASS: compiled display/input backend is baked byte-for-byte")
    if args.expect_adb_fallback:
        recovery_init = files["init.recovery.mt8792.rc"][1].decode()
        check("\non property:sys.usb.config=mtp,adb && property:sys.usb.configfs=1\n"
              "    setprop sys.usb.config adb\n" in recovery_init,
              "missing ADB-only diagnostic MTP fallback")
    if args.expect_mtp:
        recovery_init = files["init.recovery.mt8792.rc"][1].decode()
        for required in ("functions/ffs.mtp", "mount functionfs mtp /dev/usb-ffs/mtp",
                         "property:sys.usb.ffs.mtp.ready=1", "functions/ffs.adb",
                         "os_desc/b.1"):
            check(required in recovery_init, "missing native MTP setup: " + required)
    if args.expect_built_in_ui:
        from make_landscape_theme import validate_landscape
        resource_root = Path(args.expect_built_in_ui)
        expected = {path.relative_to(resource_root).as_posix(): path.read_bytes()
                    for path in resource_root.rglob("*") if path.is_file()}
        check(bool(expected), "built-in UI reference is empty")
        baked = {}
        for name, contents in expected.items():
            path = "twres/" + name
            check(path in files, "resource not baked into recovery: " + path)
            baked[name] = files[path][1]
            # Soong inserts build/version variables into ui.xml. The native
            # resolution is verified separately; every other asset must match.
            if name != "ui.xml":
                check(baked[name] == contents, "stale built-in resource: " + path)
        validate_landscape(baked)
        print(f"PASS: {len(baked)} native UI resources baked into recovery; no external ui.zip required")
    if args.reference:
        previous_fragments, previous_dtb = inspect_image(args.reference)
        check(dtb == previous_dtb, "DTB differs from known bootable recovery")
        check({key: value for key, value in fragments.items() if key[0] != 2} ==
              {key: value for key, value in previous_fragments.items() if key[0] != 2},
              "platform ramdisk differs from known bootable recovery")
        previous_files = read_cpio(previous_fragments[2, "recovery"])
        changed = sorted(name for name in files.keys() | previous_files.keys()
                         if files.get(name) != previous_files.get(name))
        print("Recovery paths changed from reference:")
        for name in changed:
            print("  " + name)
    print("PASS: 64 MiB vendor_boot v4, AVB hash verified (unsigned NONE), valid recovery cpio")
    print("PASS: /etc symlink and baked theme resolution " + str(resolution.attrib))
    if args.expect_adb_fallback:
        print("PASS: ADB-only diagnostic fallback baked; native MTP is not implemented")
    if args.expect_mtp:
        print("PASS: native MTP/ADB ConfigFS setup baked; live enumeration remains a separate test")
    for name, value in sorted(properties.items()):
        print(name + "=" + value)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, struct.error, subprocess.CalledProcessError,
            ET.ParseError) as error:
        raise SystemExit("FAIL: " + str(error)) from error
