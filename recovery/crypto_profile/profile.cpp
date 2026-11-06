#include "profile.h"

#include <array>
#include <cctype>
#include <sstream>

namespace recovery_crypto {
namespace {

bool Fail(std::string* error, const std::string& message) {
    if (error) *error = message;
    return false;
}

std::string Trim(const std::string& value) {
    const auto first = value.find_first_not_of(" \t\r\n");
    if (first == std::string::npos) return {};
    return value.substr(first, value.find_last_not_of(" \t\r\n") - first + 1);
}

bool Critical(const std::string& name) {
    return name == "ro.build.version.release" || name == "ro.system.build.version.release" ||
           name == "ro.build.version.security_patch" ||
           name == "ro.system.build.version.security_patch" ||
           name == "ro.vendor.build.security_patch";
}

bool Decimal(const std::string& text, unsigned* value) {
    if (text.empty() || text.size() > 4) return false;
    unsigned number = 0;
    for (unsigned char ch : text) {
        if (ch < '0' || ch > '9') return false;
        number = number * 10 + ch - '0';
    }
    *value = number;
    return true;
}

bool Version(const std::string& text, uint32_t* value) {
    if (text.empty() || text.size() > 8 || text.back() == '.') return false;
    std::array<unsigned, 3> parts{};
    std::istringstream input(text);
    std::string part;
    size_t count = 0;
    while (std::getline(input, part, '.')) {
        if (count == parts.size() || part.size() > 2 || !Decimal(part, &parts[count])) return false;
        ++count;
    }
    if (!count || parts[0] < 14 || parts[0] > 17) return false;
    *value = parts[0] * 10000 + parts[1] * 100 + parts[2];
    return true;
}

bool PatchLevel(const std::string& text, uint32_t* value) {
    if (text.size() != 10 || text[4] != '-' || text[7] != '-') return false;
    unsigned year, month, day;
    if (!Decimal(text.substr(0, 4), &year) || !Decimal(text.substr(5, 2), &month) ||
        !Decimal(text.substr(8, 2), &day) || year < 2000 || year > 2099 || month < 1 || month > 12)
        return false;
    const unsigned month_days[] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
    unsigned last_day = month_days[month - 1];
    if (month == 2 && year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)) ++last_day;
    if (day < 1 || day > last_day) return false;
    *value = year * 10000 + month * 100 + day;
    return true;
}

bool Select(const Properties& props, const char* primary, const char* alternate,
            std::string* value, std::string* error) {
    const auto first = props.find(primary);
    const auto second = alternate ? props.find(alternate) : props.end();
    if (first != props.end() && second != props.end() && first->second != second->second)
        return Fail(error, std::string("conflicting_properties:") + primary + ":" + alternate);
    if (first != props.end()) *value = first->second;
    else if (second != props.end()) *value = second->second;
    else return Fail(error, std::string("missing_property:") + primary);
    if (value->empty()) return Fail(error, std::string("empty_property:") + primary);
    return true;
}

}  // namespace

bool ParseProperties(const std::string& text, Properties* result, std::string* error) {
    if (!result) return Fail(error, "missing_properties_output");
    if (text.size() > 1024 * 1024 || text.find('\0') != std::string::npos)
        return Fail(error, "invalid_property_file");
    Properties parsed;
    std::istringstream input(text);
    std::string line;
    while (std::getline(input, line)) {
        line = Trim(line);
        if (line.empty() || line[0] == '#') continue;
        // Never execute imports or shell text from the installed OS.
        if (line.compare(0, 7, "import ") == 0 || line.compare(0, 7, "import\t") == 0)
            return Fail(error, "unsupported_property_import");
        const auto equals = line.find('=');
        if (equals == std::string::npos) return Fail(error, "malformed_property_line");
        const auto name = Trim(line.substr(0, equals));
        const auto value = Trim(line.substr(equals + 1));
        if (name.empty() || name.size() > 256 || value.size() > 4096)
            return Fail(error, "invalid_property_length");
        for (unsigned char ch : name) {
            if (!std::isalnum(ch) && ch != '.' && ch != '_' && ch != '-' && ch != '@')
                return Fail(error, "invalid_property_name:" + name);
        }
        const auto old = parsed.find(name);
        if (Critical(name) && old != parsed.end() && old->second != value)
            return Fail(error, "duplicate_crypto_property:" + name);
        parsed[name] = value;
    }
    *result = std::move(parsed);
    if (error) error->clear();
    return true;
}

bool ResolveProfile(const Properties& system, const Properties& vendor,
                    Profile* result, std::string* error) {
    if (!result) return Fail(error, "missing_profile_output");
    Profile profile;
    if (!Select(system, "ro.build.version.release", "ro.system.build.version.release", &profile.release, error) ||
        !Select(system, "ro.build.version.security_patch", "ro.system.build.version.security_patch", &profile.system_patch, error) ||
        !Select(vendor, "ro.vendor.build.security_patch", nullptr, &profile.vendor_patch, error))
        return false;
    if (!Version(profile.release, &profile.os_version))
        return Fail(error, "unsupported_android_release");
    uint32_t full_system_patch;
    if (!PatchLevel(profile.system_patch, &full_system_patch))
        return Fail(error, "invalid_system_security_patch");
    if (!PatchLevel(profile.vendor_patch, &profile.vendor_patchlevel))
        return Fail(error, "invalid_vendor_security_patch");
    profile.os_patchlevel = full_system_patch / 100;
    *result = std::move(profile);
    if (error) error->clear();
    return true;
}

}  // namespace recovery_crypto
