#pragma once

#include <cstdint>
#include <string>

namespace recovery_crypto {

struct FilesystemInfo {
    std::string type;
    std::string mount_options;
    // Recognized clean-state flags, not a full filesystem consistency check.
    bool clean = false;
    bool replay_safe = false;
};

// Borrow O_RDONLY regular-file or block-device descriptors; never change their
// position or contents. Reads/allocations are bounded; the caller owns timeouts.
// On failure, out is unchanged. error may be null.
bool InspectFilesystem(int fd, bool require_clean, FilesystemInfo* out, std::string* error);
bool CheckIdleVirtualAb(int misc_fd, std::string* error);

// Allocation/bounds preflight only. liblp MUST subsequently validate geometry
// and metadata checksums. Neither check establishes trusted image provenance.
bool CheckSuperGeometry(int super_fd, uint32_t slot, std::string* error);

}  // namespace recovery_crypto
