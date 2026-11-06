#pragma once

#include <cstdint>
#include <map>
#include <string>

namespace recovery_crypto {

using Properties = std::map<std::string, std::string>;

struct Profile {
    std::string release;
    std::string system_patch;
    std::string vendor_patch;
    uint32_t os_version = 0;
    uint32_t os_patchlevel = 0;
    uint32_t vendor_patchlevel = 0;
};

bool ParseProperties(const std::string& text, Properties* result, std::string* error);
bool ResolveProfile(const Properties& system, const Properties& vendor,
                    Profile* result, std::string* error);

}  // namespace recovery_crypto
