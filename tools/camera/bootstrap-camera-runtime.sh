#!/system/bin/sh
# Executado via: adb shell su 0 sh < bootstrap-camera-runtime.sh
# (paths com @ quebram adb shell sh -c)
MARKER=redroid-ui-vendor-camera
LD=/linkerconfig/ld.config.txt
PROVIDER=/vendor/bin/hw/android.hardware.camera.provider@2.4-service_64
LOG=/data/local/tmp/cam-provider.log

if [ -f "$LD" ] && ! grep -q "$MARKER" "$LD"; then
  sed -i "/^\[vendor\]/,/^\[product\]/{
/namespace.default.link.system.shared_libs += libfdtrack.so/a namespace.default.link.system.shared_libs += libcamera_client.so # '"$MARKER"'
/namespace.default.link.system.shared_libs += libfdtrack.so/a namespace.default.link.system.shared_libs += libcamera_metadata.so # '"$MARKER"'
/namespace.default.link.system.shared_libs += libfdtrack.so/a namespace.default.link.system.shared_libs += libchrome.so # '"$MARKER"'
/namespace.default.link.system.shared_libs += libfdtrack.so/a namespace.default.link.system.shared_libs += libexif.so # '"$MARKER"'
}" "$LD"
fi

if [ -n "$V4L2_DEV" ]; then
  setprop vendor.v4l2.device "$V4L2_DEV"
fi

if [ ! -f /vendor/lib64/hw/camera.v4l2.so ] && [ -f /system/lib64/hw/camera.v4l2.so ]; then
  mkdir -p /vendor/lib64/hw 2>/dev/null || true
  cp /system/lib64/hw/camera.v4l2.so /vendor/lib64/hw/camera.v4l2.so 2>/dev/null || true
fi

[ -x "$PROVIDER" ] || exit 2

provider_pid() {
  ps -A 2>/dev/null | grep -v grep | grep 'camera.provider@2.4-service' | awk '{print $2}' | head -1
}

CUR=$(getprop vendor.v4l2.device)
RUNNING=$(provider_pid)
NEED_RESTART=1
if [ -n "$RUNNING" ]; then
  if [ -z "$V4L2_DEV" ] || [ "$CUR" = "$V4L2_DEV" ]; then
    NEED_RESTART=0
  fi
fi

if [ "$NEED_RESTART" = 1 ]; then
  ps -A 2>/dev/null | grep -v grep | grep 'camera.provider@2.4-service' | awk '{print $2}' | while read pid; do
    kill "$pid" 2>/dev/null || true
  done
  sleep 1
  trap '' HUP
  setsid "$PROVIDER" </dev/null >>"$LOG" 2>&1 &
  sleep 2
fi
[ -n "$(provider_pid)" ] || exit 3
