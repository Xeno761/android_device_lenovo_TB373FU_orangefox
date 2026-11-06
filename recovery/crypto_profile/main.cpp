#include "profile.h"
#include "storage_checks.h"

#include <aidl/android/hardware/security/keymint/IKeyMintDevice.h>
#include <aidl/android/hardware/security/keymint/IKeyMintOperation.h>
#include <android-base/file.h>
#include <android-base/properties.h>
#include <android-base/scopeguard.h>
#include <android-base/unique_fd.h>
#include <android/binder_manager.h>
#include <android/binder_status.h>
#include <fs_mgr_dm_linear.h>
#include <fstab/fstab.h>
#include <libdm/dm.h>
#include <liblp/liblp.h>
#include <openssl/mem.h>
#include <openssl/sha.h>

#include <array>
#include <cerrno>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <dirent.h>
#include <fcntl.h>
#include <limits.h>
#include <iterator>
#include <memory>
#include <poll.h>
#include <sched.h>
#include <signal.h>
#include <string>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <sys/wait.h>
#include <unistd.h>
#include <vector>

using android::base::GetProperty;
using android::base::SetProperty;
using android::base::unique_fd;
using namespace std::chrono_literals;

namespace recovery_crypto {
namespace {

constexpr const char* kStatus = "vendor.recovery.crypto.profile";
constexpr const char* kModeProperty = "ro.recovery.crypto_profile";
constexpr const char* kServices[] = {"teei_daemon", "vendor.keymint-beanpod", "vendor.gatekeeper-beanpod"};
constexpr const char* kParts[] = {"system", "vendor", "system_ext", "product"};
constexpr size_t kMaxPropertyBytes = 256 * 1024;

bool Fail(std::string* error, const std::string& message) {
    *error = message;
    return false;
}

bool ReadInside(const std::string& root, const std::string& relative, size_t limit,
                std::string* output, bool optional, std::string* error) {
    output->clear();
    char resolved[PATH_MAX];
    const std::string requested = root + "/" + relative;
    if (!realpath(requested.c_str(), resolved)) {
        if (optional && errno == ENOENT) return true;
        return Fail(error, "unreadable:" + relative);
    }
    if (std::string(resolved).compare(0, root.size() + 1, root + "/") != 0)
        return Fail(error, "path_escapes_partition:" + relative);
    unique_fd fd(open(resolved, O_RDONLY | O_CLOEXEC | O_NOFOLLOW));
    struct stat file{}, directory{};
    if (fd < 0 || fstat(fd.get(), &file) || stat(root.c_str(), &directory) ||
        !S_ISREG(file.st_mode) || file.st_dev != directory.st_dev ||
        file.st_size < 0 || static_cast<uint64_t>(file.st_size) > limit)
        return Fail(error, "invalid_file:" + relative);
    std::array<char, 4096> buffer;
    for (;;) {
        const ssize_t size = TEMP_FAILURE_RETRY(read(fd.get(), buffer.data(), buffer.size()));
        if (size < 0) return Fail(error, "read_failed:" + relative);
        if (!size) return true;
        if (output->size() + size > limit) return Fail(error, "oversized_file:" + relative);
        output->append(buffer.data(), size);
    }
}

bool DirectoryEmpty(const std::string& path, std::string* error) {
    struct stat info{};
    if (lstat(path.c_str(), &info)) {
        if (errno == ENOENT) return true;
        return Fail(error, "unreadable_ota_directory");
    }
    if (!S_ISDIR(info.st_mode)) return Fail(error, "invalid_ota_directory");
    std::unique_ptr<DIR, int (*)(DIR*)> directory(opendir(path.c_str()), closedir);
    if (!directory) {
        if (errno == ENOENT) return true;
        return Fail(error, "unreadable_ota_directory");
    }
    errno = 0;
    while (dirent* entry = readdir(directory.get())) {
        if (strcmp(entry->d_name, ".") && strcmp(entry->d_name, ".."))
            return Fail(error, "snapshot_entries_present");
    }
    return errno == 0 || Fail(error, "ota_directory_read_failed");
}

bool IdleMetadata(const std::string& root, std::string* error) {
    for (const char* directory : {"ota", "gsi", "gsi/dsu"}) {
        struct stat info{};
        if (!lstat((root + "/" + directory).c_str(), &info)) {
            if (!S_ISDIR(info.st_mode)) return Fail(error, "invalid_metadata_state_directory");
        } else if (errno != ENOENT) return Fail(error, "unreadable_metadata_state_directory");
    }
    for (const char* marker : {"ota/snapshot-boot", "ota/rollback-indicator", "gsi/dsu/active"}) {
        struct stat info{};
        if (!lstat((root + "/" + marker).c_str(), &info))
            return Fail(error, "unsupported_snapshot_or_dsu_state");
        if (errno != ENOENT) return Fail(error, "unreadable_snapshot_state");
    }
    struct stat state_info{};
    if (!lstat((root + "/ota/state").c_str(), &state_info) && !S_ISREG(state_info.st_mode))
        return Fail(error, "invalid_snapshot_state_file");
    std::string state;
    if (!ReadInside(root, "ota/state", 4096, &state, true, error)) return false;
    // Empty proto3 state is None. Legacy literal None is accepted too; every
    // other encoding is rejected instead of interpreting a parse error as idle.
    if (!state.empty() && state != "none" && state != "none\n")
        return Fail(error, "non_idle_or_unsupported_snapshot_state");
    return DirectoryEmpty(root + "/ota/snapshots", error);
}

bool LoadProperties(const std::string& root, bool system, Properties* result, std::string* error,
                    bool required = true) {
    const std::vector<std::string> paths = system
        ? std::vector<std::string>{"system/build.prop", "build.prop", "system/etc/build.prop", "etc/build.prop"}
        : std::vector<std::string>{"build.prop", "etc/build.prop"};
    bool found = false;
    for (const auto& path : paths) {
        std::string text;
        if (!ReadInside(root, path, kMaxPropertyBytes, &text, true, error)) return false;
        if (text.empty()) continue;
        found = true;
        Properties file;
        if (!ParseProperties(text, &file, error)) return false;
        for (const auto& [key, value] : file) {
            auto old = result->find(key);
            if (old != result->end() && old->second != value &&
                (key == "ro.build.version.release" || key == "ro.build.version.security_patch" ||
                 key == "ro.system.build.version.release" || key == "ro.system.build.version.security_patch" ||
                 key == "ro.vendor.build.security_patch"))
                return Fail(error, "conflicting_property_files:" + key);
            (*result)[key] = value;
        }
    }
    return found || !required || Fail(error, "no_build_properties");
}

bool EqualFiles(const std::string& vendor, const std::string& relative, std::string* error) {
    std::string installed, bundled;
    if (!ReadInside(vendor, relative, 32 * 1024 * 1024, &installed, false, error) ||
        !ReadInside("/vendor", relative, 32 * 1024 * 1024, &bundled, false, error)) return false;
    if (installed.empty() || installed != bundled)
        return Fail(error, "unsupported_vendor_bundle:" + relative);
    return true;
}

bool CompatibleVendor(const std::string& vendor, std::string* error) {
    for (const char* path : {
             "bin/teei_daemon", "bin/hw/android.hardware.security.keymint@3.0-service.beanpod",
             "bin/hw/android.hardware.gatekeeper-service.beanpod", "lib64/libkeymint.so",
             "lib64/libTEECommon.so", "lib64/libteei_daemon_vfs.so",
             "lib64/libkeymaster_messages.so", "lib64/libkeymaster_portable.so"}) {
        if (!EqualFiles(vendor, path, error)) return false;
    }
    std::unique_ptr<DIR, int (*)(DIR*)> directory(opendir("/vendor/thh/ta"), closedir);
    if (!directory) return Fail(error, "bundled_ta_directory_missing");
    unsigned count = 0;
    for (;;) {
        errno = 0;
        dirent* entry = readdir(directory.get());
        if (!entry) {
            if (errno) return Fail(error, "ta_inventory_read_failed");
            break;
        }
        const std::string name(entry->d_name);
        if (name.size() != 35 || name.substr(32) != ".ta") continue;
        if (name.find_first_not_of("0123456789abcdef", 0) < 32)
            return Fail(error, "invalid_ta_name");
        if (++count > 64 || !EqualFiles(vendor, "thh/ta/" + name, error)) return false;
    }
    return count > 0 || Fail(error, "unverified_ta_inventory");
}

struct Context {
    std::string scratch;
    std::vector<std::string> mountpoints;
    std::vector<std::string> mapping_names;
    std::unique_ptr<android::fs_mgr::LpMetadata> metadata;
    std::string key_directory;

    bool Mount(const std::string& source, const std::string& target, bool require_clean,
               std::string* error) {
        unique_fd fd(open(source.c_str(), O_RDONLY | O_CLOEXEC));
        FilesystemInfo info;
        if (fd < 0 || !InspectFilesystem(fd.get(), require_clean, &info, error)) return false;
        if (mount(source.c_str(), target.c_str(), info.type.c_str(),
                  MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC, info.mount_options.c_str()))
            return Fail(error, "readonly_mount_failed:" + target + ":" + std::to_string(errno));
        mountpoints.push_back(target);
        return true;
    }

    bool MountMetadata(std::string* error) {
        const std::string target = scratch + "/metadata";
        struct stat block{}, mounted{};
        if (stat("/dev/block/by-name/metadata", &block) || !S_ISBLK(block.st_mode))
            return Fail(error, "metadata_device_missing");
        if (!stat("/metadata", &mounted) && mounted.st_dev == block.st_rdev) {
            // Existing recovery mount: create a read-only view, never remount
            // or detach the live filesystem or replay an on-disk checkpoint.
            if (mount("/metadata", target.c_str(), nullptr, MS_BIND | MS_REC, nullptr))
                return Fail(error, "metadata_bind_failed");
            mountpoints.push_back(target);
            if (mount(nullptr, target.c_str(), nullptr,
                      MS_BIND | MS_REMOUNT | MS_RDONLY | MS_NODEV | MS_NOSUID | MS_NOEXEC, nullptr))
                return Fail(error, "metadata_readonly_bind_failed");
        } else if (!Mount("/dev/block/by-name/metadata", target, true, error)) return false;
        return IdleMetadata(target, error);
    }

    bool MountLogical(size_t index, const std::string& suffix, bool optional, std::string* error) {
        const std::string partition_name = std::string(kParts[index]) + suffix;
        const LpMetadataPartition* partition = nullptr;
        for (const auto& item : metadata->partitions) {
            if (android::fs_mgr::GetPartitionName(item) == partition_name) partition = &item;
        }
        if (!partition || partition->num_extents == 0)
            return optional || Fail(error, "active_partition_missing:" + partition_name);
        if (partition->num_extents > 4096 || partition->first_extent_index > metadata->extents.size() ||
            partition->num_extents > metadata->extents.size() - partition->first_extent_index)
            return Fail(error, "invalid_partition_extents");
        android::fs_mgr::CreateLogicalPartitionParams params;
        params.block_device = "/dev/block/by-name/super";
        params.metadata = metadata.get();
        params.partition = partition;
        params.partition_name = partition_name;
        params.device_name = mapping_names[index];
        params.timeout_ms = 1500ms;
        params.force_writable = false;
        android::dm::DmTable table;
        if (!android::fs_mgr::CreateDmTable(params, &table)) return Fail(error, "dm_table_failed");
        table.set_readonly(true);
        std::string path;
        auto& dm = android::dm::DeviceMapper::Instance();
        if (!dm.CreateDevice(params.device_name, table, &path, params.timeout_ms))
            return Fail(error, "readonly_mapping_failed");
        auto info = dm.GetDetailedInfo(params.device_name);
        if (!info || !info->IsReadOnly()) return Fail(error, "mapping_is_not_readonly");
        return Mount(path, scratch + "/" + kParts[index], true, error);
    }

    bool Unmount() {
        bool result = true;
        for (auto it = mountpoints.rbegin(); it != mountpoints.rend(); ++it)
            if (umount(it->c_str())) result = false;
        mountpoints.clear();
        return result;
    }
};

bool MountMetadataForStartup(std::string* error) {
    unique_fd misc(open("/dev/block/by-name/misc", O_RDONLY | O_CLOEXEC));
    if (misc < 0 || !CheckIdleVirtualAb(misc.get(), error)) return false;
    unique_fd fd(open("/dev/block/by-name/metadata", O_RDONLY | O_CLOEXEC));
    FilesystemInfo filesystem;
    if (fd < 0 || !InspectFilesystem(fd.get(), false, &filesystem, error)) return false;
    if ((filesystem.type != "f2fs" && filesystem.type != "ext4") || !filesystem.replay_safe)
        return Fail(error, "metadata_requires_manual_filesystem_check");
    struct stat device{}, directory{};
    if (fstat(fd.get(), &device) || !S_ISBLK(device.st_mode))
        return Fail(error, "metadata_device_missing");
    if (!stat("/metadata", &directory) && directory.st_dev == device.st_rdev) {
        struct statvfs filesystem_flags{};
        if (statvfs("/metadata", &filesystem_flags)) return Fail(error, "metadata_mount_state_unreadable");
        if ((filesystem_flags.f_flag & ST_RDONLY) && !filesystem.clean)
            return Fail(error, "readonly_metadata_needs_normal_recovery");
        return true;
    }
    if (mkdir("/metadata", 0770) && errno != EEXIST) return Fail(error, "metadata_mountpoint_missing");
    // This is the same ordinary Metadata mount recovery normally performs.
    // Let the kernel replay a valid journal/checkpoint before inspecting OTA
    // state; never read a stale norecovery view as if it were an idle filesystem.
    if (mount("/dev/block/by-name/metadata", "/metadata", filesystem.type.c_str(),
              MS_NOSUID | MS_NODEV | MS_NOEXEC | MS_NOATIME, nullptr))
        return Fail(error, "normal_metadata_mount_failed:" + std::to_string(errno));
    return true;
}

bool ReadEncryptionPolicy(Context* context, std::string* error) {
    const auto hardware = GetProperty("ro.hardware", "");
    if (hardware.empty() || hardware.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-") != std::string::npos)
        return Fail(error, "invalid_hardware_identifier");
    const std::vector<std::string> candidates = {
        "etc/fstab." + hardware, "etc/fstab.mt8792", "etc/fstab.mt6897",
    };
    const std::string vendor = context->scratch + "/vendor";
    for (const auto& candidate : candidates) {
        std::string contents;
        if (!ReadInside(vendor, candidate, kMaxPropertyBytes, &contents, true, error)) return false;
        if (contents.empty()) continue;
        android::fs_mgr::Fstab fstab;
        // This only parses flags. Never invoke mount/check/format/resize actions
        // from the installed ROM's fstab during profile discovery.
        if (!android::fs_mgr::ReadFstabFromFile(vendor + "/" + candidate, &fstab))
            return Fail(error, "invalid_vendor_fstab");
        for (const auto& entry : fstab) {
            if (entry.mount_point != "/data") continue;
            if (entry.fs_type != "f2fs" && entry.fs_type != "ext4")
                return Fail(error, "unsupported_data_filesystem");
            if (entry.metadata_encryption_options.find("wrappedkey") != std::string::npos ||
                entry.encryption_options.find("wrappedkey") != std::string::npos)
                return Fail(error, "wrapped_keys_require_separate_validation");
            const std::string& directory = entry.metadata_key_dir;
            if (directory.compare(0, 10, "/metadata/") != 0 || directory.size() > 200 ||
                directory.find("..") != std::string::npos ||
                directory.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-/") != std::string::npos)
                return Fail(error, "unsupported_metadata_key_directory");
            std::string relative = directory.substr(10);
            while (!relative.empty() && relative.back() == '/') relative.pop_back();
            if (relative.empty()) return Fail(error, "invalid_metadata_key_directory");
            relative += "/key/";
            if (!context->key_directory.empty() && context->key_directory != relative)
                return Fail(error, "conflicting_data_fstab_entries");
            context->key_directory = relative;
        }
        return !context->key_directory.empty() || Fail(error, "metadata_encryption_not_declared");
    }
    return Fail(error, "vendor_fstab_not_found");
}

bool Discover(Context* context, Profile* profile, std::string* error) {
    if (unshare(CLONE_NEWNS) || mount(nullptr, "/", nullptr, MS_REC | MS_PRIVATE, nullptr))
        return Fail(error, "private_namespace_failed");
    const std::string suffix = GetProperty("ro.boot.slot_suffix", "");
    if (suffix != "_a" && suffix != "_b") return Fail(error, "unknown_booted_slot");
    const auto gsi = GetProperty("ro.gsid.image_running", "0");
    if ((gsi != "0" && gsi != "false") || !GetProperty("ro.gsid.dsu_slot", "").empty())
        return Fail(error, "dsu_not_supported");
    unique_fd misc(open("/dev/block/by-name/misc", O_RDONLY | O_CLOEXEC));
    unique_fd super(open("/dev/block/by-name/super", O_RDONLY | O_CLOEXEC));
    const uint32_t slot = suffix == "_a" ? 0 : 1;
    if (misc < 0 || super < 0 || !CheckIdleVirtualAb(misc.get(), error) ||
        !CheckSuperGeometry(super.get(), slot, error) || !context->MountMetadata(error)) return false;
    context->metadata = android::fs_mgr::ReadMetadata("/dev/block/by-name/super", slot);
    if (!context->metadata || context->metadata->partitions.size() > 128 ||
        context->metadata->extents.size() > 4096 || context->metadata->block_devices.size() != 1 ||
        (context->metadata->header.flags & LP_HEADER_FLAG_OVERLAYS_ACTIVE))
        return Fail(error, "unsupported_logical_metadata");
    for (size_t i = 0; i < std::size(kParts); ++i)
        if (!context->MountLogical(i, suffix, i >= 2, error)) return false;
    Properties system, vendor;
    if (!LoadProperties(context->scratch + "/system", true, &system, error) ||
        !LoadProperties(context->scratch + "/vendor", false, &vendor, error) ||
        !ResolveProfile(system, vendor, profile, error)) return false;
    for (const char* name : {"vendor", "system_ext", "product"}) {
        const std::string root = context->scratch + "/" + name;
        bool mounted = false;
        for (const auto& item : context->mountpoints) if (item == root) mounted = true;
        if (!mounted) continue;
        Properties extra;
        if (!LoadProperties(root, false, &extra, error, false)) return false;
        for (const auto& key : {"ro.build.version.release", "ro.build.version.security_patch"}) {
            const auto found = extra.find(key);
            if (found != extra.end() && found->second !=
                (std::string(key) == "ro.build.version.release" ? profile->release : profile->system_patch))
                return Fail(error, "conflicting_cross_partition_crypto_property");
        }
    }
    return ReadEncryptionPolicy(context, error) && CompatibleVendor(context->scratch + "/vendor", error);
}

bool ProbeMetadata(const std::string& metadata, const std::string& key_dir, std::string* error) {
    using namespace aidl::android::hardware::security::keymint;
    std::string version, blob_string, secdiscardable, encrypted;
    if (!ReadInside(metadata, key_dir + "version", 16, &version, false, error) ||
        !ReadInside(metadata, key_dir + "keymaster_key_blob", 32768, &blob_string, false, error) ||
        !ReadInside(metadata, key_dir + "secdiscardable", 16384, &secdiscardable, false, error) ||
        !ReadInside(metadata, key_dir + "encrypted_key", 512, &encrypted, false, error)) return false;
    if ((version != "1" && version != "1\n") || blob_string.empty() || secdiscardable.size() != 16384 ||
        encrypted.size() < 28) return Fail(error, "unsupported_metadata_key_record");
    std::vector<uint8_t> app_id(SHA512_DIGEST_LENGTH), plaintext, tail;
    auto erase = android::base::make_scope_guard([&]() {
        OPENSSL_cleanse(secdiscardable.data(), secdiscardable.size());
        OPENSSL_cleanse(app_id.data(), app_id.size());
        OPENSSL_cleanse(plaintext.data(), plaintext.size());
        OPENSSL_cleanse(tail.data(), tail.size());
    });
    std::array<uint8_t, SHA512_CBLOCK> prefix{};
    constexpr char domain[] = "Android secdiscardable SHA512";
    memcpy(prefix.data(), domain, sizeof(domain) - 1);
    SHA512_CTX hash;
    SHA512_Init(&hash);
    SHA512_Update(&hash, prefix.data(), prefix.size());
    SHA512_Update(&hash, secdiscardable.data(), secdiscardable.size());
    SHA512_Final(app_id.data(), &hash);
    const std::string instance = std::string(IKeyMintDevice::descriptor) + "/default";
    ndk::SpAIBinder binder;
    for (unsigned i = 0; i < 20; ++i) {
        binder = ndk::SpAIBinder(AServiceManager_checkService(instance.c_str()));
        if (binder.get()) break;
        usleep(100000);
    }
    auto device = IKeyMintDevice::fromBinder(binder);
    if (!device) return Fail(error, "keymint_not_registered");
    std::vector<uint8_t> blob(blob_string.begin(), blob_string.end());
    std::vector<KeyCharacteristics> characteristics;
    auto status = device->getKeyCharacteristics(blob, app_id, {}, &characteristics);
    if (!status.isOk()) return Fail(error, "metadata_characteristics_refused:" + std::to_string(status.getServiceSpecificError()));
    for (const auto& entry : characteristics) {
        for (const auto& param : entry.authorizations) {
            if (param.tag == Tag::USAGE_COUNT_LIMIT || param.tag == Tag::MAX_USES_PER_BOOT ||
                param.tag == Tag::MIN_SECONDS_BETWEEN_OPS)
                return Fail(error, "usage_limited_metadata_key");
        }
    }
    auto parameter = [](Tag tag, KeyParameterValue value) {
        KeyParameter result;
        result.tag = tag;
        result.value = std::move(value);
        return result;
    };
    std::vector<KeyParameter> parameters = {
        parameter(Tag::APPLICATION_ID, KeyParameterValue::make<KeyParameterValue::blob>(app_id)),
        parameter(Tag::ALGORITHM, KeyParameterValue::make<KeyParameterValue::algorithm>(Algorithm::AES)),
        parameter(Tag::BLOCK_MODE, KeyParameterValue::make<KeyParameterValue::blockMode>(BlockMode::GCM)),
        parameter(Tag::PADDING, KeyParameterValue::make<KeyParameterValue::paddingMode>(PaddingMode::NONE)),
        parameter(Tag::MAC_LENGTH, KeyParameterValue::make<KeyParameterValue::integer>(128)),
        parameter(Tag::NONCE, KeyParameterValue::make<KeyParameterValue::blob>(
            std::vector<uint8_t>(encrypted.begin(), encrypted.begin() + 12))),
    };
    BeginResult begin;
    status = device->begin(KeyPurpose::DECRYPT, blob, parameters, std::nullopt, &begin);
    // Never call upgradeKey: an incompatible startup profile must not convert
    // the original record into a form the installed ROM can no longer use.
    if (!status.isOk() || !begin.operation)
        return Fail(error, "metadata_begin_refused:" + std::to_string(status.getServiceSpecificError()));
    auto abort = android::base::make_scope_guard([&]() { begin.operation->abort(); });
    std::vector<uint8_t> ciphertext(encrypted.begin() + 12, encrypted.end());
    status = begin.operation->update(ciphertext, std::nullopt, std::nullopt, &plaintext);
    if (!status.isOk()) return Fail(error, "metadata_update_refused");
    status = begin.operation->finish(std::nullopt, std::nullopt, std::nullopt, std::nullopt, std::nullopt, &tail);
    if (!status.isOk()) return Fail(error, "metadata_authentication_failed");
    abort.Disable();
    if (plaintext.size() + tail.size() != 32 && plaintext.size() + tail.size() != 64)
        return Fail(error, "unsupported_metadata_key_length");
    return true;
}

struct Report {
    uint32_t magic = 0x43505246;
    bool success = false;
    char release[16]{};
    char system_patch[16]{};
    char vendor_patch[16]{};
    char error[192]{};
};

bool ServicesStopped() {
    for (const auto* name : kServices) {
        const auto state = GetProperty(std::string("init.svc.") + name, "stopped");
        if (state != "stopped") return false;
    }
    return true;
}

bool WaitChildUntil(pid_t child, int* status, std::chrono::steady_clock::time_point deadline) {
    do {
        const pid_t result = waitpid(child, status, WNOHANG);
        if (result == child) return true;
        if (result < 0 && errno != EINTR) return false;
        if (std::chrono::steady_clock::now() >= deadline) return false;
        usleep(10000);
    } while (true);
}

bool ResetProperty(const char* key, const std::string& value,
                   std::chrono::steady_clock::time_point deadline) {
    if (std::chrono::steady_clock::now() >= deadline) return false;
    pid_t child = fork();
    if (child == 0) {
        execl("/system/bin/resetprop", "resetprop", "-n", key, value.c_str(), nullptr);
        _exit(127);
    }
    if (child < 0) return false;
    int status = 0;
    if (!WaitChildUntil(child, &status, deadline)) {
        kill(child, SIGKILL);
        WaitChildUntil(child, &status, std::chrono::steady_clock::now() + 250ms);
        return false;
    }
    return WIFEXITED(status) && WEXITSTATUS(status) == 0 && GetProperty(key, "") == value;
}

int Run(const std::string& mode) {
    const auto deadline = std::chrono::steady_clock::now() + 30s;
    const bool prepare = mode == "--prepare", verify = mode == "--verify";
    const bool check_key = verify || mode == "--check";
    if ((prepare || verify) && GetProperty(kModeProperty, "") != "adaptive") {
        fprintf(stderr, "crypto_profile: adaptive mode is not enabled\n");
        return 1;
    }
    if (prepare && !ServicesStopped()) {
        fprintf(stderr, "crypto_profile: refusing to reconfigure an initialized TEE\n");
        return 1;
    }
    if (verify && GetProperty(kStatus, "") != "configured") return 1;
    const std::string tag = "rcrypto_" + std::to_string(getpid());
    const std::string scratch = "/tmp/" + tag;
    auto& dm = android::dm::DeviceMapper::Instance();
    Context context;
    context.scratch = scratch;
    for (const char* part : kParts) {
        const std::string name = tag + "_" + part;
        if (dm.GetState(name) != android::dm::DmDeviceState::INVALID) return 1;
        context.mapping_names.push_back(name);
    }
    if (mkdir(scratch.c_str(), 0700)) return 1;
    std::vector<std::string> directories{scratch + "/metadata"};
    for (const auto* part : kParts) directories.push_back(scratch + "/" + part);
    for (const auto& dir : directories) if (mkdir(dir.c_str(), 0700)) return 1;
    int descriptors[2];
    if (pipe2(descriptors, O_CLOEXEC)) return 1;
    unique_fd read_end(descriptors[0]), write_end(descriptors[1]);
    pid_t child = fork();
    if (child == 0) {
        read_end.reset();
        Profile profile;
        std::string error;
        // Preparation alone performs the normal global Metadata mount. All
        // discovery mappings/mounts after this are private and read-only.
        bool ok = !prepare || MountMetadataForStartup(&error);
        if (ok) ok = Discover(&context, &profile, &error);
        if (ok && verify && (GetProperty("ro.build.version.release", "") != profile.release ||
            GetProperty("ro.build.version.security_patch", "") != profile.system_patch ||
            GetProperty("ro.vendor.build.security_patch", "") != profile.vendor_patch))
            ok = Fail(&error, "configured_profile_changed");
        if (ok && check_key) ok = ProbeMetadata(scratch + "/metadata", context.key_directory, &error);
        if (!context.Unmount()) ok = Fail(&error, "readonly_mount_cleanup_failed");
        Report report;
        report.success = ok;
        snprintf(report.release, sizeof(report.release), "%s", profile.release.c_str());
        snprintf(report.system_patch, sizeof(report.system_patch), "%s", profile.system_patch.c_str());
        snprintf(report.vendor_patch, sizeof(report.vendor_patch), "%s", profile.vendor_patch.c_str());
        snprintf(report.error, sizeof(report.error), "%s", error.c_str());
        const bool delivered = android::base::WriteFully(write_end.get(), &report, sizeof(report));
        _exit(ok && delivered ? 0 : 1);
    }
    write_end.reset();
    Report report;
    bool received = false;
    bool reaped = child < 0;
    int status = 0;
    if (child > 0) {
        pollfd descriptor{read_end.get(), POLLIN, 0};
        fcntl(read_end.get(), F_SETFL, O_NONBLOCK);
        const auto io_deadline = std::chrono::steady_clock::now() + 25s;
        while (std::chrono::steady_clock::now() < io_deadline) {
            const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(
                io_deadline - std::chrono::steady_clock::now()).count();
            const int result = poll(&descriptor, 1, remaining > 0 ? remaining : 0);
            if (result > 0) {
                // The report is smaller than PIPE_BUF, so its write is atomic.
                received = read(read_end.get(), &report, sizeof(report)) == static_cast<ssize_t>(sizeof(report));
                break;
            }
            if (!result || errno != EINTR) break;
        }
        if (!received) kill(child, SIGKILL);
        reaped = WaitChildUntil(child, &status, deadline);
        if (!reaped) {
            kill(child, SIGKILL);
            reaped = WaitChildUntil(child, &status, std::chrono::steady_clock::now() + 250ms);
        }
    }
    bool cleaned = reaped;
    if (reaped) {
        for (const auto& name : context.mapping_names) {
            if (dm.GetState(name) != android::dm::DmDeviceState::INVALID && !dm.DeleteDevice(name, 500ms)) cleaned = false;
        }
        for (auto it = directories.rbegin(); it != directories.rend(); ++it) if (rmdir(it->c_str())) cleaned = false;
        if (rmdir(scratch.c_str())) cleaned = false;
    }
    bool ok = child > 0 && received && report.magic == 0x43505246 && report.success &&
              WIFEXITED(status) && WEXITSTATUS(status) == 0 && cleaned;
    if (ok && prepare) {
        if (!ServicesStopped() ||
            !ResetProperty("ro.build.version.release", report.release, deadline) ||
            !ResetProperty("ro.build.version.security_patch", report.system_patch, deadline) ||
            !ResetProperty("ro.vendor.build.security_patch", report.vendor_patch, deadline) ||
            !SetProperty(kStatus, "configured")) ok = false;
        if (ok) {
            // ctl.start is handled while init waits for exec. A queued property
            // action could otherwise run after the early-boot verification gate.
            for (const char* service : kServices) {
                if (std::chrono::steady_clock::now() >= deadline || !SetProperty("ctl.start", service)) {
                    ok = false;
                    break;
                }
            }
        }
    } else if (ok && verify) {
        ok = SetProperty(kStatus, "ready") && SetProperty("crypto.ready", "1");
    }
    if (!ok && (prepare || verify)) SetProperty(kStatus, "blocked");
    const char* failure = !reaped ? "worker_cleanup_pending" :
        (!received ? "worker_failed_or_timed_out" : (report.error[0] ? report.error : "profile_commit_failed"));
    printf("crypto_profile: status=%s release=%s system_patch=%s vendor_patch=%s key_check=%s error=%s\n",
           ok ? (prepare ? "configured" : "verified") : "blocked", report.release, report.system_patch,
           report.vendor_patch, check_key ? "requested" : "not_requested", ok ? "" : failure);
    return ok ? 0 : 1;
}

}  // namespace
}  // namespace recovery_crypto

int main(int argc, char** argv) {
    const std::string mode = argc == 2 ? argv[1] : "--inspect";
    if (argc == 2 && mode == "--prepare") {
        unique_fd log(open("/tmp/tb373fu_crypto_prepare.log",
                           O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC | O_NOFOLLOW, 0600));
        if (log < 0 || dup2(log.get(), STDOUT_FILENO) < 0 || dup2(log.get(), STDERR_FILENO) < 0)
            return 1;
    }
    if (argc == 2 && mode == "--check-metadata-storage") {
        unique_fd fd(open("/dev/block/by-name/metadata", O_RDONLY | O_CLOEXEC));
        recovery_crypto::FilesystemInfo filesystem;
        std::string error;
        const bool ok = fd >= 0 && recovery_crypto::InspectFilesystem(fd.get(), true, &filesystem, &error);
        printf("crypto_profile: metadata_cold_read=%s type=%s clean=%d error=%s\n",
               ok ? "allowed" : "blocked", filesystem.type.c_str(), filesystem.clean, error.c_str());
        return ok ? 0 : 1;
    }
    if (argc > 2 || (mode != "--inspect" && mode != "--check" && mode != "--prepare" && mode != "--verify")) {
        fprintf(stderr, "usage: tb373fu_crypto_profile [--inspect|--check|--check-metadata-storage|--prepare|--verify]\n");
        return 2;
    }
    return recovery_crypto::Run(mode);
}
