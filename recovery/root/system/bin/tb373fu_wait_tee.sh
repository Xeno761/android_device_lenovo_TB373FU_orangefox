#!/system/bin/sh
# A failed profile leaves crypto disabled but must not prevent UI/ADB startup.
if [ "$(getprop vendor.recovery.crypto.profile)" != "configured" ]; then
    setprop vendor.recovery.crypto.profile blocked
    echo "ROM crypto profile unavailable; leaving data encrypted" > /tmp/tb373fu_tee.log
    exit 0
fi

# Wait at most ~15 s for the configured Beanpod services, then verify one
# existing metadata record without generating, upgrading or replacing keys.
i=0
while [ $i -lt 30 ]; do
    if [ "$(getprop vendor.soter.teei.init)" = "INIT_OK" ] &&
       [ "$(getprop init.svc.vendor.keymint-beanpod)" = "running" ]; then
        break
    fi
    sleep 0.5
    i=$((i + 1))
done
# Let KeyMint finish registering with servicemanager.
sleep 1
if [ $i -ge 30 ]; then
    setprop vendor.recovery.crypto.profile blocked
elif ! /system/bin/tb373fu_crypto_profile --verify > /tmp/tb373fu_crypto_verify.log 2>&1; then
    setprop vendor.recovery.crypto.profile blocked
fi
msg="tb373fu_wait_tee: profile=$(getprop vendor.recovery.crypto.profile) teei=$(getprop vendor.soter.teei.init) keymint=$(getprop init.svc.vendor.keymint-beanpod) gatekeeper=$(getprop init.svc.vendor.gatekeeper-beanpod) after $i polls"
echo "$msg" > /dev/kmsg
{
    echo "$msg"
    getprop | grep -E "teei|soter|keymint|gatekeeper|keystore|init.svc|crypto|security_patch|version.release"
} > /tmp/tb373fu_tee.log 2>&1
exit 0
