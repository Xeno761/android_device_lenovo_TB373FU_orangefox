#!/usr/bin/env python3
"""Read-only Linux 6.1 framing validation of TB373FU's recovery-mode initramfs.

Requires liblz4 and libzstd (run under WSL). AVB, installed-slot identity,
kernel configuration, and runtime bootability are separate checks.
"""

import argparse
import ctypes
import ctypes.util
import hashlib
import stat
import struct
import zlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

LZ4_MAGIC = b"\x02\x21\x4c\x18"
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
CPIO_MAGICS = (b"070701", b"070702")
LZ4_CHUNK_SIZE = 8 << 20
ZSTD_WINDOW_MAX = 128 << 20


def check(condition, message):
    if not condition:
        raise ValueError(message)


def library(name):
    location = ctypes.util.find_library(name)
    check(location is not None, f"lib{name} unavailable; run under WSL with lib{name} installed")
    return ctypes.CDLL(location)


@dataclass
class DecodedStream:
    method: str
    start: int
    end: int
    archive: bytes
    trailers: int
    files: dict
    detail: str = ""


def read_cpio(data, allow_compressed_tail=False):
    """Validate newc/CRC CPIO, including every trailer and padding byte."""
    pos, trailers = 0, 0
    unfinished = False
    files = {}
    view = memoryview(data)
    while pos < len(data):
        check(pos > 0 or data[pos] != 0, "CPIO begins with padding instead of its first header")
        while pos < len(data) and data[pos] == 0:
            pos += 1
        if pos == len(data):
            break
        if data[pos:pos + 6] not in CPIO_MAGICS:
            if allow_compressed_tail and trailers and not unfinished:
                break
            raise ValueError(f"no newc header/junk in CPIO at {pos}")
        check(pos % 4 == 0, f"broken CPIO padding at {pos}")
        check(pos + 110 <= len(data), f"truncated CPIO header at {pos}")
        header = data[pos + 6:pos + 110]
        check(all(value in b"0123456789abcdefABCDEF" for value in header),
              f"invalid CPIO hex fields at {pos}")
        fields = [int(header[index:index + 8], 16) for index in range(0, 104, 8)]
        mode, size, namesize, checksum = fields[1], fields[6], fields[11], fields[12]
        name_start = pos + 110
        check(0 < namesize <= 4096 and name_start + namesize <= len(data), "invalid CPIO name size")
        name_bytes = data[name_start:name_start + namesize]
        check(name_bytes[-1] == 0 and b"\0" not in name_bytes[:-1], "invalid CPIO name NUL termination")
        name = name_bytes[:-1].decode()
        check(bool(name), "empty CPIO filename")
        body_start = (name_start + namesize + 3) & ~3
        body_end = body_start + size
        next_pos = (body_end + 3) & ~3
        check(next_pos <= len(data), f"truncated CPIO content/padding: {name}")
        check(not any(data[name_start + namesize:body_start]) and not any(data[body_end:next_pos]),
              f"nonzero CPIO padding: {name}")
        if data[pos:pos + 6] == b"070702":
            # newc's CRC variant uses an unsigned byte sum, not CRC32.
            check(sum(view[body_start:body_end]) & 0xffffffff == checksum, f"CPIO checksum mismatch: {name}")
        if name == "TRAILER!!!":
            check(size == 0, "nonempty CPIO trailer")
            trailers += 1
            unfinished = False
        else:
            check(stat.S_IFMT(mode) in (stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK, stat.S_IFCHR,
                                       stat.S_IFBLK, stat.S_IFIFO, stat.S_IFSOCK), f"invalid CPIO mode: {name}")
            if stat.S_ISLNK(mode):
                check(size <= 4096, f"CPIO symlink too long: {name}")
            files[name.removeprefix("./")] = mode, view[body_start:body_end]
            unfinished = True
        pos = next_pos
    check(trailers > 0 and not unfinished, "CPIO trailer missing/incomplete archive at decoder exit")
    return pos, trailers, files


def decode_lz4(data, start):
    lib = library("lz4")
    lib.LZ4_decompress_safe.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    lib.LZ4_decompress_safe.restype = ctypes.c_int
    output = ctypes.create_string_buffer(LZ4_CHUNK_SIZE)
    chunks = []
    pos = start + 4
    while pos + 4 <= len(data):
        word = struct.unpack_from("<I", data, pos)[0]
        if word == 0x184c2102:
            pos += 4
            continue
        if word == 0:
            # Linux unlz4(fill=NULL) returns without consuming this EOF word.
            return pos, b"".join(chunks), "zero EOF left for initramfs"
        check(word <= 0x7fffffff and pos + 4 + word <= len(data),
              f"invalid LZ4 block length {word:#x} at selected offset {pos}; "
              "a mixed-codec transition needs an explicit zero EOF")
        pos += 4
        result = lib.LZ4_decompress_safe(data[pos:pos + word], output, word, LZ4_CHUNK_SIZE)
        check(result >= 0, f"LZ4 decoding failed at selected offset {pos - 4}")
        chunks.append(output.raw[:result])
        pos += word
    return pos, b"".join(chunks), "end of input"


class ZstdBuffer(ctypes.Structure):
    _fields_ = [("data", ctypes.c_void_p), ("size", ctypes.c_size_t), ("pos", ctypes.c_size_t)]


class ZstdFrameHeader(ctypes.Structure):
    _fields_ = [("content_size", ctypes.c_ulonglong), ("window_size", ctypes.c_ulonglong),
                ("block_size", ctypes.c_uint), ("frame_type", ctypes.c_int),
                ("header_size", ctypes.c_uint), ("dict_id", ctypes.c_uint),
                ("checksum", ctypes.c_uint), ("reserved1", ctypes.c_uint), ("reserved2", ctypes.c_uint)]


def decode_zstd(data, start):
    lib = library("zstd")
    lib.ZSTD_getFrameHeader.argtypes = [ctypes.POINTER(ZstdFrameHeader), ctypes.c_void_p, ctypes.c_size_t]
    lib.ZSTD_getFrameHeader.restype = ctypes.c_size_t
    lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
    lib.ZSTD_isError.restype = ctypes.c_uint
    lib.ZSTD_createDStream.restype = ctypes.c_void_p
    lib.ZSTD_initDStream.argtypes = [ctypes.c_void_p]
    lib.ZSTD_initDStream.restype = ctypes.c_size_t
    lib.ZSTD_DCtx_setMaxWindowSize.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    lib.ZSTD_DCtx_setMaxWindowSize.restype = ctypes.c_size_t
    lib.ZSTD_decompressStream.argtypes = [ctypes.c_void_p, ctypes.POINTER(ZstdBuffer), ctypes.POINTER(ZstdBuffer)]
    lib.ZSTD_decompressStream.restype = ctypes.c_size_t
    lib.ZSTD_freeDStream.argtypes = [ctypes.c_void_p]
    header = ZstdFrameHeader()
    source = ctypes.create_string_buffer(data[start:])
    result = lib.ZSTD_getFrameHeader(ctypes.byref(header), source, len(data) - start)
    check(result == 0, f"invalid/incomplete Zstandard header at {start}")
    check(header.dict_id == 0, "kernel initramfs does not supply a Zstandard dictionary")
    check(header.window_size <= ZSTD_WINDOW_MAX, "Zstandard window exceeds Linux 6.1's 128 MiB limit")
    context = lib.ZSTD_createDStream()
    check(context, "Zstandard context allocation failed")
    try:
        check(not lib.ZSTD_isError(lib.ZSTD_initDStream(context)), "Zstandard initialization failed")
        check(not lib.ZSTD_isError(lib.ZSTD_DCtx_setMaxWindowSize(context, ZSTD_WINDOW_MAX)),
              "cannot set Zstandard window limit")
        incoming = ZstdBuffer(ctypes.addressof(source), len(data) - start, 0)
        output = ctypes.create_string_buffer(1 << 17)
        outgoing = ZstdBuffer(ctypes.addressof(output), len(output), 0)
        chunks = []
        while True:
            previous = incoming.pos
            outgoing.pos = 0
            result = lib.ZSTD_decompressStream(context, ctypes.byref(outgoing), ctypes.byref(incoming))
            check(not lib.ZSTD_isError(result), f"Zstandard decoding failed at {start + incoming.pos}")
            if outgoing.pos:
                chunks.append(output.raw[:outgoing.pos])
            if result == 0:
                break
            check(incoming.pos != previous or outgoing.pos, "truncated/stalled Zstandard frame")
        return start + incoming.pos, b"".join(chunks), f"frame end; window={header.window_size}"
    finally:
        lib.ZSTD_freeDStream(context)


def decode_stream(data):
    """Emulate initramfs codec dispatch and consumption; never extract files."""
    pos = 0
    streams = []
    while pos < len(data):
        while pos < len(data) and data[pos] == 0:
            pos += 1
        if pos == len(data):
            break
        start = pos
        if data[pos:pos + 4] == LZ4_MAGIC:
            method = "LZ4"
            pos, archive, detail = decode_lz4(data, pos)
        elif data[pos:pos + 4] == ZSTD_MAGIC:
            method = "Zstandard"
            pos, archive, detail = decode_zstd(data, pos)
        elif data[pos:pos + 2] == b"\x1f\x8b":
            method = "gzip"
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            archive = decoder.decompress(data[pos:]) + decoder.flush()
            check(decoder.eof, "truncated gzip stream")
            pos = len(data) - len(decoder.unused_data)
            detail = "gzip member end"
        elif data[pos:pos + 6] in CPIO_MAGICS:
            method = "raw CPIO"
            check(pos % 4 == 0, f"raw CPIO is not four-byte aligned at selected offset {pos}")
            consumed, trailers, files = read_cpio(data[pos:], allow_compressed_tail=True)
            pos += consumed
            archive = data[start:pos]
            streams.append(DecodedStream(method, start, pos, archive, trailers, files))
            continue
        else:
            raise ValueError(f"unsupported compression/junk at selected offset {pos}: {data[pos:pos + 8].hex()}")
        check(pos > start, "decoder made no progress")
        _, trailers, files = read_cpio(archive)
        streams.append(DecodedStream(method, start, pos, archive, trailers, files, detail))
    check(bool(streams), "empty ramdisk stream")
    return streams


def decode_fragment(fragment):
    """Decode one source fragment, including leading EOF and trailing zero padding."""
    return b"".join(stream.archive for stream in decode_stream(fragment))


def validate_manifest_types(files):
    for name, (_, contents) in files.items():
        if not name.endswith(".xml"):
            continue
        for partition, expected in (("system", "framework"), ("vendor", "device")):
            prefix = partition + "/etc/vintf/manifest"
            if name != prefix + ".xml" and not name.startswith(prefix + "/"):
                continue
            try:
                root = ET.fromstring(bytes(contents))
            except ET.ParseError as error:
                raise ValueError(f"invalid composed VINTF manifest {name}: {error}") from error
            check(root.tag == "manifest" and root.get("type") == expected,
                  f"incompatible {partition} manifest fragment breaks service discovery: {name}")


def validate_ramdisk_stream(platform, recovery, generic_ramdisk=None, check_paths=False):
    """Compare actual selected-stream decoding bytewise with individual sources."""
    components = [platform, recovery]
    if generic_ramdisk is not None:
        check(bool(generic_ramdisk), "explicit generic ramdisk is empty")
        components.append(generic_ramdisk)
    check(all(components), "empty platform/recovery fragment")
    streams = decode_stream(b"".join(components))
    actual_index = actual_pos = 0
    for component in components:
        for expected in decode_stream(component):
            expected_pos = 0
            while expected_pos < len(expected.archive):
                check(actual_index < len(streams), "selected stream lost source archive bytes")
                actual = streams[actual_index].archive
                size = min(len(expected.archive) - expected_pos, len(actual) - actual_pos)
                check(memoryview(expected.archive)[expected_pos:expected_pos + size] ==
                      memoryview(actual)[actual_pos:actual_pos + size],
                      "selected stream differs bytewise from individual source archives")
                expected_pos += size
                actual_pos += size
                if actual_pos == len(actual):
                    actual_index += 1
                    actual_pos = 0
    check(actual_index == len(streams) and actual_pos == 0, "selected stream added source archive bytes")
    if check_paths:
        files = {}
        for stream in streams:
            files.update(stream.files)
        for name in ("init", "system/bin/init", "system/bin/recovery"):
            check(name in files, f"missing recovery boot path: /{name}")
            mode, content = files[name]
            if name == "init" and stat.S_ISLNK(mode):
                check(bytes(content) in (b"/system/bin/init", b"system/bin/init"), "unexpected /init symlink")
            else:
                check(stat.S_ISREG(mode) and mode & 0o111, f"nonexecutable recovery boot path: /{name}")
        check("system/etc/recovery.fstab" in files or "etc/recovery.fstab" in files, "missing recovery fstab")
        validate_manifest_types(files)
    return streams


def selected_fragments(data):
    """Model the mapped LK's unambiguous PLATFORM/RECOVERY selection, not gaps."""
    check(len(data) >= 2128 and data[:8] == b"VNDRBOOT", "not a complete vendor_boot header")
    check(struct.unpack_from("<I", data, 8)[0] == 4, "vendor_boot v4 required")
    page = struct.unpack_from("<I", data, 12)[0]
    header_size, dtb_size = struct.unpack_from("<II", data, 2096)
    check(page == 4096 and header_size == 2128, "unexpected TB373FU header/page size")
    align = lambda size: (size + page - 1) // page * page
    ramdisk_start = align(header_size)
    ramdisk_size = struct.unpack_from("<I", data, 24)[0]
    table_start = align(align(ramdisk_start + ramdisk_size) + dtb_size)
    table_size, count, entry_size, bootconfig_size = struct.unpack_from("<IIII", data, 2112)
    check(count == 2 and entry_size == 108 and table_size == count * entry_size,
          "expected unambiguous TB373FU PLATFORM+RECOVERY table")
    check(align(table_start + table_size) + bootconfig_size <= len(data), "truncated vendor_boot sections")
    fragments, ranges = {}, []
    for index in range(count):
        entry = table_start + index * entry_size
        size, offset, kind = struct.unpack_from("<III", data, entry)
        name_bytes = data[entry + 12:entry + 44]
        check(b"\0" in name_bytes, "unterminated ramdisk table name")
        name = name_bytes.split(b"\0", 1)[0].decode()
        check(size > 0 and offset + size <= ramdisk_size, "fragment out of bounds/empty")
        check(kind in (1, 2) and kind not in fragments, "ambiguous PLATFORM/RECOVERY selection")
        check(kind != 2 or name == "recovery", "expected named recovery fragment")
        fragments[kind] = data[ramdisk_start + offset:ramdisk_start + offset + size]
        ranges.append((offset, offset + size))
    ranges.sort()
    check(ranges[0][1] <= ranges[1][0], "overlapping ramdisk fragments")
    check(set(fragments) == {1, 2}, "missing PLATFORM or RECOVERY")
    return fragments[1], fragments[2]


def validate_image(image, generic_ramdisk=None, check_paths=False):
    return validate_ramdisk_stream(*selected_fragments(image), generic_ramdisk, check_paths)


def read_init_boot(image):
    check(len(image) >= 1580 and image[:8] == b"ANDROID!", "not a complete init_boot image")
    version = struct.unpack_from("<I", image, 40)[0]
    check(version in (3, 4), "init_boot v3/v4 required")
    header_size = struct.unpack_from("<I", image, 20)[0]
    check(header_size == (1584 if version == 4 else 1580) and len(image) >= header_size,
          "invalid init_boot header size")
    kernel_size, ramdisk_size = struct.unpack_from("<II", image, 8)
    check(kernel_size == 0, "--init-boot expects an init_boot image with no kernel")
    check(ramdisk_size > 0 and 4096 + ramdisk_size <= len(image), "empty/out-of-bounds generic ramdisk")
    return image[4096:4096 + ramdisk_size]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, type=Path)
    generic = parser.add_mutually_exclusive_group()
    generic.add_argument("--generic-ramdisk", type=Path)
    generic.add_argument("--init-boot", type=Path)
    parser.add_argument("--check-paths", action="store_true", help="check active init/recovery/fstab paths")
    args = parser.parse_args()
    try:
        image = args.image.read_bytes()
        ramdisk = None
        if args.generic_ramdisk:
            ramdisk = args.generic_ramdisk.read_bytes()
        elif args.init_boot:
            init_boot = args.init_boot.read_bytes()
            ramdisk = read_init_boot(init_boot)
            print(f"init_boot sha256={hashlib.sha256(init_boot).hexdigest()}")
        streams = validate_image(image, ramdisk, args.check_paths)
        for stream in streams:
            print(f"PASS: {stream.method} start={stream.start} consumed={stream.end - stream.start} "
                  f"end={stream.end} trailers={stream.trailers} {stream.detail} "
                  f"cpio_sha256={hashlib.sha256(stream.archive).hexdigest()}")
        print("PASS: selected-stream archives match individual source archives byte-for-byte")
        scope = "PLATFORM+RECOVERY+supplied generic" if ramdisk is not None else "PLATFORM+RECOVERY only"
        print(f"PASS: {scope}, image_sha256={hashlib.sha256(image).hexdigest()}")
        if ramdisk is None:
            print("NOTE: generic ramdisk was not supplied and was not validated")
        print("NOTE: AVB, installed generic identity, kernel config and runtime bootability are separate checks")
    except (ValueError, OSError, zlib.error) as error:
        parser.exit(1, f"FAIL: {error}\n")


if __name__ == "__main__":
    main()
