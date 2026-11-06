#!/usr/bin/env python3
"""Replace only recovery in an installed vendor_boot v4, retaining its drivers."""

import argparse
import gzip
import hashlib
import struct
import subprocess
from compression import zstd
from pathlib import Path

from validate_recovery_image import inspect_image
from validate_ramdisk_stream import read_init_boot, validate_image

PARTITION_SIZE = 67108864


def layout(image):
    if image[:8] != b"VNDRBOOT" or struct.unpack_from("<I", image, 8)[0] != 4:
        raise ValueError("base must be vendor_boot v4")
    page = struct.unpack_from("<I", image, 12)[0]
    header_size, dtb_size = struct.unpack_from("<II", image, 2096)
    if page != 4096 or header_size != 2128:
        raise ValueError("unexpected device header/page size")
    align = lambda value: (value + page - 1) // page * page
    ramdisk_start = align(header_size)
    ramdisk_size = struct.unpack_from("<I", image, 24)[0]
    dtb_start = align(ramdisk_start + ramdisk_size)
    table_start = align(dtb_start + dtb_size)
    table_size, count, entry_size, bootconfig_size = struct.unpack_from("<IIII", image, 2112)
    if entry_size != 108 or count * entry_size != table_size:
        raise ValueError("invalid base ramdisk table")
    bootconfig_start = align(table_start + table_size)
    if bootconfig_start + bootconfig_size > len(image):
        raise ValueError("truncated base")
    entries = []
    for index in range(count):
        raw = bytearray(image[table_start + index * entry_size:table_start + (index + 1) * entry_size])
        size, offset, kind = struct.unpack_from("<III", raw)
        name = raw[12:44].split(b"\0", 1)[0].decode()
        if offset + size > ramdisk_size:
            raise ValueError("base fragment is out of bounds")
        payload = image[ramdisk_start + offset:ramdisk_start + offset + size]
        entries.append((raw, kind, name, payload))
    return page, image[:header_size], image[dtb_start:dtb_start + dtb_size], entries, image[bootconfig_start:bootconfig_start + bootconfig_size]


def decode(fragment):
    fragment = fragment.lstrip(b"\0")
    if fragment[:2] == b"\x1f\x8b":
        return gzip.decompress(fragment)
    if fragment[:4] == b"\x28\xb5\x2f\xfd":
        decoder = zstd.ZstdDecompressor()
        archive = decoder.decompress(fragment)
        if not decoder.eof or any(decoder.unused_data):
            raise ValueError("invalid recovery frame or nonzero trailing bytes")
        return archive
    return subprocess.run(["lz4", "-d", "-c"], input=fragment, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=True).stdout


def repack(base, replacement):
    page, header, dtb, entries, bootconfig = layout(base)
    header = bytearray(header)
    archive = decode(replacement)
    # CONFIG_RD_ZSTD=y was read from this device's running kernel. Limit the
    # decoder window to 8 MiB; retain all CPIO bytes, unlike slimming drivers.
    compressed = zstd.compress(archive, options={
        zstd.CompressionParameter.compression_level: 19,
        zstd.CompressionParameter.window_log: 23,
    })
    payloads, table = bytearray(), bytearray()
    replaced = 0
    for raw, kind, name, payload in entries:
        if kind == 2 and name == "recovery":
            # The preceding legacy-LZ4 fragment has no EOF marker. Without
            # these selected bytes its decoder interprets the Zstd magic as
            # an LZ4 block length. Also align any following raw init_boot CPIO.
            payload = b"\0" * 4 + compressed
            payload += b"\0" * (-(len(payloads) + len(payload)) % 4)
            replaced += 1
        struct.pack_into("<II", raw, 0, len(payload), len(payloads))
        table.extend(raw)
        payloads.extend(payload)
    if replaced != 1:
        raise ValueError("expected exactly one named recovery fragment")
    struct.pack_into("<I", header, 24, len(payloads))
    output = bytearray(header)
    for section in (payloads, dtb, table, bootconfig):
        output.extend(b"\0" * (-len(output) % page))
        output.extend(section)
    output.extend(b"\0" * (-len(output) % page))
    if len(output) > PARTITION_SIZE - 8192:
        raise ValueError(f"image does not fit without removing drivers: {len(output)} bytes")
    _, new_header, new_dtb, new_entries, new_bootconfig = layout(output)
    if header[:24] != base[:24] or header[28:] != base[28:len(header)]:
        raise ValueError("non-recovery header fields changed")
    if dtb != new_dtb or bootconfig != new_bootconfig:
        raise ValueError("DTB or bootconfig changed")
    for original, updated in zip(entries, new_entries):
        if original[0][8:] != updated[0][8:]:
            raise ValueError("fragment type/name/board IDs changed")
        if original[1] != 2 and original[3] != updated[3]:
            raise ValueError("installed platform/driver fragment changed")
    if decode(next(entry[3] for entry in new_entries if entry[1:3] == (2, "recovery"))) != archive:
        raise ValueError("recovery CPIO differs after lossless compression")
    return bytes(output), len(compressed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    recovery_source = parser.add_mutually_exclusive_group(required=True)
    recovery_source.add_argument("--recovery-image")
    recovery_source.add_argument("--recovery-ramdisk")
    parser.add_argument("--init-boot", required=True,
                        help="installed generic ramdisk snapshot for complete stream validation")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    target = Path(args.output)
    if target.exists():
        raise ValueError("refusing to overwrite an existing image")
    if args.recovery_image:
        fragments, _ = inspect_image(args.recovery_image)
        replacement = fragments[2, "recovery"]
    else:
        replacement = Path(args.recovery_ramdisk).read_bytes()
    original = Path(args.base).read_bytes()
    output, compressed_size = repack(original, replacement)
    generic = read_init_boot(Path(args.init_boot).read_bytes())
    validate_image(output, generic, check_paths=True)
    target.write_bytes(output)
    subprocess.run(["/usr/local/bin/avbtool", "add_hash_footer", "--image", str(target),
                    "--partition_size", str(PARTITION_SIZE), "--partition_name", "vendor_boot"], check=True)
    verified, _ = inspect_image(target)
    validate_image(target.read_bytes(), generic, check_paths=True)
    current_entries = layout(original)[3]
    for _, kind, name, contents in current_entries:
        if kind != 2 and verified[kind, name] != contents:
            raise ValueError("platform fragment changed after AVB footer")
    print(f"PASS: installed platform/drivers, DTB, header, board IDs and bootconfig retained")
    print(f"PASS: recovery CPIO unchanged, explicit LZ4 EOF prefix, aligned tail, Zstandard window8MiB, compressed_size={compressed_size}")
    print("PASS: complete selected PLATFORM + RECOVERY + installed generic ramdisk stream")
    print(f"PASS: {target.name}, size={target.stat().st_size}, sha256={hashlib.sha256(target.read_bytes()).hexdigest()}")


if __name__ == "__main__":
    main()
