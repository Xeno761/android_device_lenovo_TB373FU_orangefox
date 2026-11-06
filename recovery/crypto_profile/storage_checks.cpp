#include "storage_checks.h"

#include <array>
#include <cerrno>
#include <cstddef>
#include <cstdint>
#include <fcntl.h>
#include <limits>
#include <linux/fs.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <unistd.h>

namespace recovery_crypto {
namespace {

constexpr size_t kBlockSize = 4096;
constexpr uint32_t kF2fsMagic = 0xf2f52010;
constexpr uint32_t kBlocksPerSegment = 512;

// external/f2fs-tools/include/f2fs_fs.h:750-853. Offsets are on-disk
// little-endian byte offsets, not host-struct casts or alignment assumptions.
constexpr size_t kSuperSize = 3072;
constexpr size_t kSuperCrc = kSuperSize - sizeof(uint32_t);
constexpr size_t kSuperPayload = 124 + 512 * 2 + 4 + 64 * 8;
constexpr size_t kSuperFeatures = kSuperPayload + 4 + 2 * 256;
constexpr size_t kSuperDevices = kSuperFeatures + 4 + 1 + 16;
constexpr size_t kSuperErrors = kSuperDevices + 8 * 68 + 3 * 4 + 1 + 2 + 2 + 32;
constexpr size_t kCheckpointFlags = 3 * 8 + 3 * 4 + 2 * (8 * 4 + 8 * 2);
constexpr size_t kCheckpointChecksum = kCheckpointFlags + 8 * 4;
constexpr size_t kCheckpointHeaderSize = kCheckpointChecksum + 4 + 8 + 16;
static_assert(kSuperCrc == 3068 && kSuperPayload == 1664 && kSuperFeatures == 2180);
static_assert(kSuperDevices == 2201 && kSuperErrors == 2794);
static_assert(kCheckpointFlags == 132 && kCheckpointChecksum == 164);
static_assert(kCheckpointHeaderSize == 192);
static_assert(sizeof(off_t) >= 8, "Storage checks require 64-bit file offsets");

bool Fail(std::string* error, const std::string& message) {
    if (error) *error = message;
    return false;
}

uint16_t Le16(const uint8_t* p) {
    return static_cast<uint16_t>(p[0] | (static_cast<uint16_t>(p[1]) << 8));
}

uint32_t Le32(const uint8_t* p) {
    return static_cast<uint32_t>(p[0]) | (static_cast<uint32_t>(p[1]) << 8) |
           (static_cast<uint32_t>(p[2]) << 16) | (static_cast<uint32_t>(p[3]) << 24);
}

uint64_t Le64(const uint8_t* p) {
    return Le32(p) | (static_cast<uint64_t>(Le32(p + 4)) << 32);
}

bool SizeOf(int fd, uint64_t* size, std::string* error) {
    const int flags = fcntl(fd, F_GETFL);
    if (flags < 0) return Fail(error, "Cannot inspect storage descriptor");
    if ((flags & O_ACCMODE) != O_RDONLY) return Fail(error, "Storage descriptor must be O_RDONLY");
    struct stat st {};
    if (fstat(fd, &st) != 0) return Fail(error, "Cannot stat storage descriptor");
    if (S_ISREG(st.st_mode)) {
        if (st.st_size < 0) return Fail(error, "Negative storage size");
        *size = static_cast<uint64_t>(st.st_size);
    } else if (S_ISBLK(st.st_mode)) {
        if (ioctl(fd, BLKGETSIZE64, size) != 0) return Fail(error, "Cannot read block-device size");
    } else {
        return Fail(error, "Storage descriptor is not a regular file or block device");
    }
    if (*size > static_cast<uint64_t>(std::numeric_limits<off_t>::max())) {
        return Fail(error, "Storage size exceeds supported file offsets");
    }
    return true;
}

bool ReadAt(int fd, uint64_t size, uint64_t offset, uint8_t* data, size_t count,
            std::string* error) {
    if (count > kBlockSize || offset > size || count > size - offset) {
        return Fail(error, "Storage read is outside file/device bounds");
    }
    // Finite attempts also bound repeated EINTR and pathological short reads.
    for (unsigned attempts = 0; count && attempts < 16; ++attempts) {
        const ssize_t n = pread(fd, data, count, static_cast<off_t>(offset));
        if (n < 0) {
            if (errno == EINTR) continue;
            return Fail(error, "Storage pread failed");
        }
        if (n == 0) return Fail(error, "Storage read was truncated");
        data += n;
        offset += static_cast<size_t>(n);
        count -= static_cast<size_t>(n);
    }
    if (count) return Fail(error, "Storage read retry limit exceeded");
    return true;
}

// external/f2fs-tools/lib/libf2fs.c:550-560: reflected CRC32, no final XOR.
uint32_t Crc32(uint32_t crc, const uint8_t* data, size_t size) {
    for (size_t n = 0; n < size; ++n) {
        crc ^= data[n];
        for (unsigned bit = 0; bit < 8; ++bit) {
            crc = (crc >> 1) ^ ((crc & 1) ? 0xedb88320U : 0U);
        }
    }
    return crc;
}

bool CheckpointCrc(const std::array<uint8_t, kBlockSize>& block) {
    const uint32_t offset = Le32(block.data() + kCheckpointChecksum);
    if (offset < kCheckpointHeaderSize || offset > kBlockSize - 4) return false;
    uint32_t crc = Crc32(kF2fsMagic, block.data(), offset);
    // libf2fs.c:599-610 includes the tail when the CRC precedes the bitmap.
    crc = Crc32(crc, block.data() + offset + 4, kBlockSize - offset - 4);
    return crc == Le32(block.data() + offset);
}

bool ReadCheckpoint(int fd, uint64_t size, uint64_t block_address, uint32_t payload,
                    std::array<uint8_t, kBlockSize>* first, std::string* error) {
    if (!ReadAt(fd, size, block_address * kBlockSize, first->data(), first->size(), error)) {
        return false;
    }
    if (!CheckpointCrc(*first)) return Fail(error, "F2FS checkpoint header CRC is invalid");
    const uint32_t total = Le32(first->data() + kCheckpointFlags + 4);
    const uint32_t summary = Le32(first->data() + kCheckpointFlags + 8);
    if (total < 2 || total > kBlocksPerSegment || total < payload + 2 ||
        summary < payload + 1 || summary >= total) {
        return Fail(error, "F2FS checkpoint pack layout is invalid");
    }
    std::array<uint8_t, kBlockSize> last {};
    if (!ReadAt(fd, size, (block_address + total - 1) * kBlockSize,
                last.data(), last.size(), error)) {
        return false;
    }
    // fsck/mount.c:1287-1317 validates both endpoints and their versions.
    // Reject same-version but different endpoint contents as ambiguous too.
    if (!CheckpointCrc(last) || Le64(first->data()) != Le64(last.data()) || *first != last) {
        return Fail(error, "F2FS checkpoint endpoints do not form a complete matching pack");
    }
    return true;
}

bool InspectF2fs(int fd, uint64_t size, bool* clean, bool* replay_safe, std::string* error) {
    std::array<uint8_t, kSuperSize> sb {}, backup {};
    if (!ReadAt(fd, size, 1024, sb.data(), sb.size(), error) ||
        !ReadAt(fd, size, kBlockSize + 1024, backup.data(), backup.size(), error)) {
        return false;
    }
    // No superblock repair/fallback: disagreeing copies cannot establish a
    // trustworthy checkpoint address for this conservative pre-mount check.
    if (sb != backup || Le32(sb.data()) != kF2fsMagic) {
        return Fail(error, "F2FS superblock copies are invalid or disagree");
    }
    const uint32_t log_sector = Le32(sb.data() + 8);
    if (Le32(sb.data() + 16) != 12 || Le32(sb.data() + 20) != 9 ||
        log_sector < 9 || log_sector > 12 || Le32(sb.data() + 12) != 12 - log_sector) {
        return Fail(error, "Unsupported F2FS block/segment/sector layout");
    }
    const uint32_t features = Le32(sb.data() + kSuperFeatures);
    if ((features & ~0x7fffU) || (features & 0x2U)) {
        return Fail(error, "Unsupported F2FS features or zoned layout");
    }
    for (size_t n = kSuperDevices + 68; n < kSuperDevices + 8 * 68; ++n) {
        if (sb[n]) return Fail(error, "Unsupported multi-device F2FS layout");
    }
    if (features & 0x800U) {
        if (Le32(sb.data() + 32) != kSuperCrc ||
            Crc32(kF2fsMagic, sb.data(), kSuperCrc) != Le32(sb.data() + kSuperCrc)) {
            return Fail(error, "F2FS superblock CRC is invalid");
        }
    } else if (Le32(sb.data() + 32) != 0) {
        return Fail(error, "Unsupported F2FS superblock checksum layout");
    }
    const uint64_t blocks = Le64(sb.data() + 36);
    const uint64_t cp = Le32(sb.data() + 76);
    const uint32_t payload = Le32(sb.data() + kSuperPayload);
    if (!blocks || blocks > size / kBlockSize || cp < 2 || cp != Le32(sb.data() + 72) ||
        Le32(sb.data() + 52) != 2 || payload > kBlocksPerSegment - 2 ||
        Le32(sb.data() + 96) != 3 || Le32(sb.data() + 100) != 1 || Le32(sb.data() + 104) != 2) {
        return Fail(error, "F2FS superblock/checkpoint bounds are invalid");
    }
    // fsck/mount.c:886-950: CP, SIT, NAT, SSA, MAIN must be contiguous.
    uint64_t next = cp;
    uint64_t segments = 0;
    for (size_t n = 0; n < 5; ++n) {
        const uint32_t count = Le32(sb.data() + 52 + n * 4);
        if (!count || next != Le32(sb.data() + 76 + n * 4)) {
            return Fail(error, "F2FS metadata area boundaries are invalid");
        }
        segments += count;
        next += static_cast<uint64_t>(count) * kBlocksPerSegment;
        if (next > blocks) return Fail(error, "F2FS metadata area exceeds filesystem bounds");
    }
    if (segments < 9 || segments != Le32(sb.data() + 48) ||
        !Le32(sb.data() + 24) || !Le32(sb.data() + 28) || !Le32(sb.data() + 44)) {
        return Fail(error, "F2FS segment/section layout is invalid");
    }

    std::array<uint8_t, kBlockSize> cp1 {}, cp2 {};
    std::string error1, error2;
    const bool valid1 = ReadCheckpoint(fd, size, cp, payload, &cp1, &error1);
    const bool valid2 = ReadCheckpoint(fd, size, cp + kBlocksPerSegment, payload, &cp2, &error2);
    if (!valid1 && !valid2) return Fail(error, "No valid F2FS checkpoint pack: " + error1 + "; " + error2);
    const auto* latest = valid1 ? &cp1 : &cp2;
    if (valid1 && valid2) {
        // fsck/f2fs.h:41-43, without implementation-defined unsigned-to-signed conversion.
        const uint64_t delta = Le64(cp2.data()) - Le64(cp1.data());
        constexpr uint64_t half_range = uint64_t {1} << 63;
        if (delta == half_range || (delta == 0 && cp1 != cp2)) {
            return Fail(error, "Ambiguous F2FS checkpoint versions");
        }
        if (delta != 0 && delta < half_range) latest = &cp2;
    }
    const uint32_t flags = Le32(latest->data() + kCheckpointFlags);
    // f2fs_fs.h:805-818; 0x2000 is CP_DISABLED_QUICK_FLAG in kernel
    // include/linux/f2fs_fs.h:166. Unknown flags must not become 'clean'.
    if (flags & ~0x7fffU) return Fail(error, "Unsupported F2FS checkpoint flags");
    constexpr uint32_t unclean = 0x2 | 0x8 | 0x10 | 0x800 | 0x1000 | 0x2000 | 0x4000;
    *clean = (flags & 1U) && !(flags & unclean);
    // A missing unmount flag or ordinary orphan cleanup can be handled by a
    // normal kernel mount. Do not attempt repair/format for explicit errors,
    // disabled checkpoints or interrupted resize state.
    constexpr uint32_t unsafe_replay = 0x8 | 0x10 | 0x800 | 0x1000 | 0x2000 | 0x4000;
    *replay_safe = !(flags & unsafe_replay);
    for (size_t n = kSuperErrors; n < kSuperErrors + 16; ++n) {
        if (sb[n]) {
            *clean = false;
            *replay_safe = false;
        }
    }
    return true;
}

}  // namespace

bool InspectFilesystem(int fd, bool require_clean, FilesystemInfo* out, std::string* error) {
    if (!out) return Fail(error, "Missing filesystem result");
    uint64_t size;
    if (!SizeOf(fd, &size, error)) return false;
    std::array<uint8_t, 1024> sb {};
    if (!ReadAt(fd, size, 1024, sb.data(), sb.size(), error)) return false;
    FilesystemInfo info;
    if (Le32(sb.data()) == 0xe0f5e1e2U) {
        info = {"erofs", "", true};
    } else if (Le32(sb.data()) == kF2fsMagic) {
        info.type = "f2fs";
        info.mount_options = "norecovery";
        if (!InspectF2fs(fd, size, &info.clean, &info.replay_safe, error)) return false;
    } else if (Le16(sb.data() + 56) == 0xef53U) {
        // external/e2fsprogs/lib/ext2fs/ext2_fs.h:555-556,669-696,857.
        info = {"ext4", "noload", Le16(sb.data() + 58) == 1 && !(Le32(sb.data() + 96) & 4U)};
        info.replay_safe = !(Le16(sb.data() + 58) & 2U);
    } else {
        return Fail(error, "Unsupported filesystem magic");
    }
    if (require_clean && !info.clean) return Fail(error, "Filesystem is not clean; replay-free metadata may be stale");
    *out = info;
    if (error) error->clear();
    return true;
}

bool CheckIdleVirtualAb(int misc_fd, std::string* error) {
    uint64_t size;
    if (!SizeOf(misc_fd, &size, error)) return false;
    // bootloader_message.h:35,88-104: packed v2 message at system-space start.
    std::array<uint8_t, 64> message {};
    if (!ReadAt(misc_fd, size, 32768, message.data(), message.size(), error)) return false;
    if (message[0] != 2 || Le32(message.data() + 1) != 0x56740ab0U) {
        return Fail(error, "Unknown or invalid misc Virtual A/B message");
    }
    if (message[5] != 0 || message[6] > 1) {
        return Fail(error, "Misc Virtual A/B is not idle or has an invalid source slot");
    }
    if (error) error->clear();
    return true;
}

bool CheckSuperGeometry(int super_fd, uint32_t slot, std::string* error) {
    uint64_t size;
    if (!SizeOf(super_fd, &size, error)) return false;
    // liblp/include/liblp/metadata_format.h:116-144. Deliberately no SHA here:
    // liblp performs integrity validation after this bounded allocation preflight.
    std::array<uint8_t, 52> first {}, second {};
    if (!ReadAt(super_fd, size, 4096, first.data(), first.size(), error) ||
        !ReadAt(super_fd, size, 8192, second.data(), second.size(), error)) {
        return false;
    }
    if (first != second || Le32(first.data()) != 0x616c4467U || Le32(first.data() + 4) != first.size()) {
        return Fail(error, "Super geometry preflight: invalid or disagreeing headers");
    }
    const uint32_t maximum = Le32(first.data() + 40);
    const uint32_t slots = Le32(first.data() + 44);
    if (!maximum || maximum > 1024 * 1024 || maximum % 512 != 0 ||
        slots < 2 || slots > 4 || slot >= slots || Le32(first.data() + 48) != kBlockSize) {
        return Fail(error, "Super geometry preflight: unsupported size, slot, or block layout");
    }
    const uint64_t metadata_end = 3 * kBlockSize + 2 * static_cast<uint64_t>(slots) * maximum;
    if (metadata_end > size) return Fail(error, "Super geometry preflight: metadata exceeds device bounds");
    if (error) error->clear();
    return true;
}

}  // namespace recovery_crypto
