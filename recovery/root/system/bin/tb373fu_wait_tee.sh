#!/system/bin/sh
# Wait (at most ~15 s) for the beanpod TEE and KeyMint before the recovery UI
# starts, then note the outcome in the kernel log and /tmp/tb373fu_tee.log
# (the kernel log can wrap before anyone reads it).
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
msg="tb373fu_wait_tee: teei=$(getprop vendor.soter.teei.init) keymint=$(getprop init.svc.vendor.keymint-beanpod) gatekeeper=$(getprop init.svc.vendor.gatekeeper-beanpod) after $i polls"
echo "$msg" > /dev/kmsg
{
    echo "$msg"
    getprop | grep -E "teei|soter|keymint|gatekeeper|keystore|init.svc|crypto|security_patch|version.release"
} > /tmp/tb373fu_tee.log 2>&1
exit 0
