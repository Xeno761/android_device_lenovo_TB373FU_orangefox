/*
 * Copyright (C) 2017 Team Win Recovery Project
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/* To the best of my knowledge there is no native implementation for
 * Weaver so I made this by looking at the IWeaver.h file that gets
 * compiled by the build system. I took the information from this header
 * file and looked at keymaster source to get an idea of the proper way
 * to write the functions.
 */

#include "Weaver1.h"

//#include <android-base/logging.h>
//#include <keystore/keymaster_tags.h>
//#include <keystore/authorization_set.h>
//#include <keystore/keystore_hidl_support.h>

#include <android/binder_libbinder.h>
#include <android-base/properties.h>

#include <android/hardware/weaver/1.0/IWeaver.h>
#include <aidl/android/hardware/weaver/IWeaver.h>
#include <android/hidl/manager/1.0/BpHwServiceManager.h>
#include <android/os/IServiceManager.h>
#include <binder/ProcessState.h>
#include <hwbinder/ProcessState.h>

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <iostream>
#include <map>
#include <mutex>
#include <thread>
#include <unistd.h>
#define ERROR 1
#define LOG(x) std::cout

using namespace android::hardware::weaver;
using android::hardware::hidl_string;
using ::android::hardware::weaver::V1_0::IWeaver;
using ::android::hardware::weaver::V1_0::WeaverConfig;
using ::android::hardware::weaver::V1_0::WeaverReadStatus;
using ::android::hardware::weaver::V1_0::WeaverReadResponse;
using ::android::hardware::weaver::V1_0::WeaverStatus;
using ::android::hardware::Return;
using ::android::sp;

namespace android {
namespace vold {

Weaver::Weaver() : GottenConfig(false) {
	using Clock = std::chrono::steady_clock;
	using Manager = ::android::hidl::manager::V1_0::IServiceManager;
	using AidlManager = ::android::os::IServiceManager;
	using AidlWeaver = ::aidl::android::hardware::weaver::IWeaver;
	const auto deadline = Clock::now() + std::chrono::milliseconds(1500);
	const std::string instance = std::string(AidlWeaver::descriptor) + "/default";
	bool tryAidl = ::android::base::GetBoolProperty("servicemanager.ready", false) ||
			::android::base::GetProperty("init.svc.servicemanager", "") == "running";
	bool tryHidl = ::android::base::GetBoolProperty("hwservicemanager.ready", false) ||
			::android::base::GetProperty("init.svc.hwservicemanager", "") == "running";
	sp<Manager> manager;
	sp<AidlManager> aidlManager;
	bool requestedAidl = false;

	// Bound registration polling, not synchronous Binder transactions (which have no timeout API).
	// Both the NDK manager wrappers and HIDL tryGetService() contain manager-readiness waits.
	for (;;) {
		if (tryAidl && aidlManager == nullptr &&
				::android::base::GetBoolProperty("servicemanager.ready", false)) {
			aidlManager = ::android::interface_cast<AidlManager>(
					::android::ProcessState::self()->getContextObject(nullptr));
			if (aidlManager == nullptr) {
				tryAidl = false;
			} else {
				bool declared = false;
				auto ret = aidlManager->isDeclared(instance, &declared);
				if (!ret.isOk())
					printf("Weaver: AIDL declaration transport error\n");
				tryAidl = ret.isOk() && declared;
			}
		}
		if (tryAidl && aidlManager != nullptr) {
			sp<::android::IBinder> binder;
			// Raw AIDL getService requests lazy startup but does not poll like the libbinder shim.
			// Request it once; subsequent passes only check registration within the same deadline.
			auto ret = requestedAidl ? aidlManager->checkService(instance, &binder) :
					aidlManager->getService(instance, &binder);
			requestedAidl = true;
			if (!ret.isOk()) {
				printf("Weaver: AIDL discovery transport error\n");
				tryAidl = false;
			} else if (binder != nullptr) {
				auto device = AidlWeaver::fromBinder(ndk::SpAIBinder(AIBinder_fromPlatformBinder(binder)));
				if (device != nullptr) {
					int32_t version = 0;
					auto versionRet = device->getInterfaceVersion(&version);
					if (versionRet.isOk() && version >= 2) {
						mAidlDevice = std::move(device);
						return;
					}
				}
				printf("Weaver: unusable AIDL interface version (requires v2+)\n");
				tryAidl = false;
			}
		}
		if (tryHidl && manager == nullptr &&
				::android::base::GetBoolProperty("hwservicemanager.ready", false)) {
			if (access("/dev/hwbinder", R_OK | W_OK) != 0) {
				tryHidl = false;
			} else {
				auto binder = ::android::hardware::ProcessState::self()->getContextObject(nullptr);
				if (binder == nullptr) {
					tryHidl = false;
				} else {
					// A one-shot proxy avoids defaultServiceManager's unbounded readiness loop.
					manager = new ::android::hidl::manager::V1_0::BpHwServiceManager(binder);
					auto transport = manager->getTransport(IWeaver::descriptor, "default");
					if (!transport.isOk()) {
						printf("Weaver: HIDL declaration transport error\n");
						tryHidl = false;
					} else {
						const Manager::Transport transportType = transport;
						if (transportType == Manager::Transport::PASSTHROUGH)
							printf("Weaver: passthrough HIDL is unsupported by bounded discovery\n");
						tryHidl = transportType == Manager::Transport::HWBINDER;
					}
				}
			}
		}
		if (tryHidl && manager != nullptr) {
			auto ret = manager->get(IWeaver::descriptor, "default");
			if (!ret.isOk()) {
				printf("Weaver: HIDL discovery transport error\n");
				tryHidl = false;
			} else {
				sp<::android::hidl::base::V1_0::IBase> base = ret;
				if (base != nullptr) {
					auto cast = IWeaver::castFrom(base);
					if (cast.isOk()) {
						mDevice = cast;
						if (mDevice != nullptr)
							return;
					}
					printf("Weaver: unusable HIDL interface\n");
					tryHidl = false;
				}
			}
		}
		const auto now = Clock::now();
		if ((!tryAidl && !tryHidl) || now >= deadline)
			break;
		std::this_thread::sleep_until(std::min(deadline, now + std::chrono::milliseconds(100)));
	}
	printf("Weaver: no available declared AIDL v2+/HIDL service\n");
}

Weaver::VerifyStatus Weaver::GetConfig() {
	if (GottenConfig)
		return VerifyStatus::OK;

	if (mAidlDevice != nullptr) {
		auto r = mAidlDevice->getConfig(&aidlConfig);
		if (!r.isOk()) {
			printf("Weaver: AIDL getConfig error: %s\n", r.getDescription().c_str());
			return r.getExceptionCode() == EX_SERVICE_SPECIFIC ? VerifyStatus::FAILED :
					VerifyStatus::TRANSPORT_ERROR;
		}
		if (aidlConfig.slots <= 0 || aidlConfig.keySize <= 0 || aidlConfig.valueSize <= 0)
			return VerifyStatus::INVALID_RESPONSE;
		GottenConfig = true;
		return VerifyStatus::OK;
	}
	if (mDevice == nullptr)
		return VerifyStatus::UNAVAILABLE;

	WeaverStatus status = WeaverStatus::FAILED;
	WeaverConfig cfg{};

	bool callbackCalled = false;
	auto ret = mDevice->getConfig([&](WeaverStatus s, WeaverConfig c) {
		callbackCalled = true;
		status = s;
		cfg = c;
	});
	if (!ret.isOk()) {
		printf("Weaver: HIDL getConfig transport error\n");
		return VerifyStatus::TRANSPORT_ERROR;
	}
	if (!callbackCalled)
		return VerifyStatus::INVALID_RESPONSE;
	if (status != WeaverStatus::OK)
		return VerifyStatus::FAILED;
	if (cfg.slots == 0 || cfg.keySize == 0 || cfg.valueSize == 0)
		return VerifyStatus::INVALID_RESPONSE;
	config = cfg;
	GottenConfig = true;
	return VerifyStatus::OK;
}

bool Weaver::GetSlots(uint32_t* slots) {
	if (slots == nullptr || GetConfig() != VerifyStatus::OK)
		return false;
	if (mAidlDevice != nullptr) {
		*slots = aidlConfig.slots;
		return true;
	}
	*slots = config.slots;
	return true;
}

bool Weaver::GetKeySize(uint32_t* keySize) {
	if (keySize == nullptr || GetConfig() != VerifyStatus::OK)
		return false;
	if (mAidlDevice != nullptr) {
		*keySize = aidlConfig.keySize;
		return true;
	}
	*keySize = config.keySize;
	return true;
}

bool Weaver::GetValueSize(uint32_t* valueSize) {
	if (valueSize == nullptr || GetConfig() != VerifyStatus::OK)
		return false;
	if (mAidlDevice != nullptr) {
		*valueSize = aidlConfig.valueSize;
		return true;
	}
	*valueSize = config.valueSize;
	return true;
}

template <typename Status>
static Weaver::VerifyStatus ToVerifyStatus(Status status) {
	switch (status) {
		case Status::OK: return Weaver::VerifyStatus::OK;
		case Status::FAILED: return Weaver::VerifyStatus::FAILED;
		case Status::INCORRECT_KEY: return Weaver::VerifyStatus::INCORRECT_KEY;
		case Status::THROTTLE: return Weaver::VerifyStatus::THROTTLE;
		default: return Weaver::VerifyStatus::INVALID_RESPONSE;
	}
}

Weaver::VerifyResult Weaver::WeaverVerify(uint32_t slot, const void* weaver_key,
		size_t key_buffer_size, std::vector<uint8_t>* payload) {
	using Clock = std::chrono::steady_clock;
	// Decrypt constructs a fresh Weaver for each attempt. Keep per-slot delays in memory,
	// including across those instances, and serialize reads so concurrent attempts cannot race.
	static std::mutex retryMutex;
	static std::map<uint32_t, Clock::time_point> retryAfter;
	if (payload == nullptr)
		return {VerifyStatus::FAILED, 0};
	payload->clear();
	if (weaver_key == nullptr)
		return {VerifyStatus::FAILED, 0};
	std::lock_guard<std::mutex> lock(retryMutex);
	auto retry = retryAfter.find(slot);
	if (retry != retryAfter.end()) {
		if (retry->second == Clock::time_point::max())
			return {VerifyStatus::THROTTLE, 0};
		const auto now = Clock::now();
		if (now < retry->second) {
			const auto remaining = std::chrono::ceil<std::chrono::milliseconds>(retry->second - now);
			return {VerifyStatus::THROTTLE, static_cast<uint32_t>(remaining.count())};
		}
		retryAfter.erase(retry);
	}
	const auto configStatus = GetConfig();
	if (configStatus != VerifyStatus::OK)
		return {configStatus, 0};
	uint32_t slots = 0, keySize = 0, valueSize = 0;
	GetSlots(&slots);
	GetKeySize(&keySize);
	GetValueSize(&valueSize);
	if (slot > INT32_MAX || slot >= slots || keySize > key_buffer_size)
		return {VerifyStatus::FAILED, 0};
	const auto* keyBytes = static_cast<const uint8_t*>(weaver_key);
	std::vector<uint8_t> key(keyBytes, keyBytes + keySize);
	VerifyStatus status = VerifyStatus::INVALID_RESPONSE;
	std::vector<uint8_t> readValue;
	int64_t timeout = 0;
	if (mAidlDevice != nullptr) {
		using AidlWeaver = ::aidl::android::hardware::weaver::IWeaver;
		::aidl::android::hardware::weaver::WeaverReadResponse rep{};
		auto r = mAidlDevice->read(static_cast<int32_t>(slot), key, &rep);
		if (!r.isOk()) {
			// Failed transactions do not supply a valid reply, even if its default status is OK.
			if (r.getExceptionCode() != EX_SERVICE_SPECIFIC)
				return {VerifyStatus::TRANSPORT_ERROR, 0};
			if (r.getServiceSpecificError() == AidlWeaver::STATUS_THROTTLE) {
				retryAfter[slot] = Clock::time_point::max();
				return {VerifyStatus::THROTTLE, 0};
			}
			if (r.getServiceSpecificError() == AidlWeaver::STATUS_INCORRECT_KEY)
				return {VerifyStatus::INCORRECT_KEY, 0};
			return {VerifyStatus::FAILED, 0};
		}
		status = ToVerifyStatus(rep.status);
		timeout = rep.timeout;
		if (timeout < 0 || timeout > INT32_MAX) {
			// An invalid signed AIDL delay must not wrap into permission to retry immediately.
			retryAfter[slot] = Clock::time_point::max();
			return {VerifyStatus::INVALID_RESPONSE, 0};
		}
		readValue = std::move(rep.value);
	} else {
		bool callbackCalled = false;
		const auto ret = mDevice->read(slot, key, [&](WeaverReadStatus s, WeaverReadResponse r) {
			callbackCalled = true;
			status = ToVerifyStatus(s);
			readValue = r.value;
			timeout = r.timeout;
		});
		if (!ret.isOk())
			return {VerifyStatus::TRANSPORT_ERROR, 0};
		if (!callbackCalled)
			return {VerifyStatus::INVALID_RESPONSE, 0};
	}
	const auto timeoutMs = static_cast<uint32_t>(timeout);
	if (timeoutMs != 0) {
		retryAfter[slot] = Clock::now() + std::chrono::milliseconds(timeoutMs);
	} else if (status == VerifyStatus::THROTTLE) {
		// No duration is available. Fail closed for this process, rather than guessing a delay.
		retryAfter[slot] = Clock::time_point::max();
	}
	if (status != VerifyStatus::OK)
		return {status, timeoutMs};
	if (timeoutMs != 0 || readValue.size() != valueSize)
		return {VerifyStatus::INVALID_RESPONSE, timeoutMs};
	*payload = std::move(readValue);
	return {VerifyStatus::OK, 0};
}

}  // namespace vold
}  // namespace android
