#
# Copyright (C) 2026 The Android Open Source Project
# Copyright (C) 2026 The OrangeFox Recovery Project
#
# SPDX-License-Identifier: Apache-2.0
#
# OrangeFox Recovery for Lenovo Idea Tab Pro (TB373FU)
# MediaTek Dimensity 8300 (MT6897 / MT8792), GKI 6.1, vendor_boot v4

# AOSP base, 64-bit only (recovery carries no 32-bit userspace)
$(call inherit-product, $(SRC_TARGET_DIR)/product/core_64_bit_only.mk)
$(call inherit-product, $(SRC_TARGET_DIR)/product/base.mk)
$(call inherit-product, $(SRC_TARGET_DIR)/product/emulated_storage.mk)

# Device-specific
$(call inherit-product, device/lenovo/TB373FU/device.mk)

PRODUCT_DEVICE := TB373FU
PRODUCT_NAME := fox_TB373FU
PRODUCT_BRAND := Lenovo
PRODUCT_MODEL := TB373FU
PRODUCT_MANUFACTURER := Lenovo
