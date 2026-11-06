#
# SPDX-License-Identifier: Apache-2.0
#

PRODUCT_SOONG_NAMESPACES += device/lenovo/TB373FU

# API levels (stock launched on Android 14)
PRODUCT_SHIPPING_API_LEVEL := 34
BOARD_SHIPPING_API_LEVEL := 34
PRODUCT_OTA_ENFORCE_VINTF_KERNEL_REQUIREMENTS := false

# Dynamic partitions + Virtual A/B with compression (matches the ROM)
PRODUCT_USE_DYNAMIC_PARTITIONS := true
$(call inherit-product, $(SRC_TARGET_DIR)/product/virtual_ab_ota/compression.mk)

AB_OTA_UPDATER := true
AB_OTA_PARTITIONS += \
    boot \
    vendor_boot \
    init_boot \
    dtbo \
    odm \
    odm_dlkm \
    product \
    system \
    system_dlkm \
    system_ext \
    vbmeta \
    vbmeta_system \
    vbmeta_vendor \
    vendor \
    vendor_dlkm

# Boot control: the misc-backed AOSP implementation, same as LineageOS recovery
# on this device.
PRODUCT_PACKAGES += \
    android.hardware.boot-service.default_recovery

# A/B OTA install, snapshots, fastbootd
PRODUCT_PACKAGES += \
    update_engine \
    update_engine_client \
    update_engine_sideload \
    update_verifier \
    snapuserd \
    checkpoint_gc \
    fastbootd \
    android.hardware.fastboot-service.example_recovery
