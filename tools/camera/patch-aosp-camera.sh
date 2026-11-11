#!/usr/bin/env bash
# Aplica USE_CAMERA_V4L2_HAL + provider 2.4 no device.mk.
# HAL fica em /system/lib64/hw/ (AOSP não permite vendor linkar libcamera_client).
# Na runtime a redroid-ui faz bind-mount para /system/lib64/hw/ (não /vendor).
# Uso: bash patch-aosp-camera.sh ~/redroid [redroid_x86_64_only|redroid_x86_64]
set -euo pipefail

SRC="${1:-}"
PRODUCT="${2:-redroid_x86_64_only}"
if [[ -z "$SRC" || ! -d "$SRC/device/redroid/$PRODUCT" ]]; then
  echo "Uso: $0 /caminho/para/redroid [redroid_x86_64_only]" >&2
  echo "Produto não encontrado: device/redroid/$PRODUCT" >&2
  exit 1
fi

DEVICE_MK="$SRC/device/redroid/$PRODUCT/device.mk"
CAMERA_MK="$SRC/hardware/libhardware/modules/camera/3_4/Android.mk"
VINTF_FRAG="$SRC/device/redroid/$PRODUCT/camera.v4l2.vintf.xml"
VINTF_MARKER="# redroid-ui: VINTF camera manifest"

MARKER="# redroid-ui: V4L2 camera"
if grep -q "$MARKER" "$DEVICE_MK" 2>/dev/null; then
  echo "device.mk já patchado (bloco câmera)."
else
  cat >>"$DEVICE_MK" <<EOF

$MARKER
USE_CAMERA_V4L2_HAL := true
PRODUCT_PROPERTY_OVERRIDES += ro.hardware.camera=v4l2
PRODUCT_PACKAGES += \\
    android.hardware.camera.provider@2.4-impl \\
    android.hardware.camera.provider@2.4-service_64 \\
    camera.device@1.0-impl \\
    camera.device@3.2-impl \\
    camera.device@3.4-impl \\
    camera.v4l2
EOF
  echo "Patch aplicado em $DEVICE_MK"
fi

# Android 12+: VINTF não pode ir em PRODUCT_COPY_FILES
sed -i '/camera\.v4l2\.vintf\.xml.*PRODUCT_COPY_FILES/d' "$DEVICE_MK" 2>/dev/null || true
sed -i '/camera\.v4l2\.vintf\.xml:\$(TARGET_COPY_OUT_VENDOR)/d' "$DEVICE_MK" 2>/dev/null || true
sed -i '/camera\.v4l2\.vintf\.xml:vendor\/etc\/vintf/d' "$DEVICE_MK" 2>/dev/null || true
if grep -q 'PRODUCT_COPY_FILES.*camera\.v4l2\.vintf' "$DEVICE_MK" 2>/dev/null; then
  perl -i -0pe 's/PRODUCT_COPY_FILES \+= \\\n\s*device\/redroid\/[^ ]+\/camera\.v4l2\.vintf\.xml:[^\n]+\n//g' "$DEVICE_MK"
fi

sed -i '/DEVICE_MANIFEST_FILE.*camera\.v4l2\.vintf\.xml/d' "$DEVICE_MK" 2>/dev/null || true
sed -i "/$VINTF_MARKER/d" "$DEVICE_MK" 2>/dev/null || true
echo "VINTF camera: use pacotes camera.provider (sem fragmento DEVICE_MANIFEST_FILE duplicado)."

if [[ -f "$CAMERA_MK" ]]; then
  if grep -q 'LOCAL_VENDOR_MODULE := true' "$CAMERA_MK" 2>/dev/null; then
    sed -i '/LOCAL_VENDOR_MODULE := true/d' "$CAMERA_MK"
    echo "Removido LOCAL_VENDOR_MODULE (evita erro vendor↔platform no Android 12)."
  fi
  echo "HAL camera.v4l2 permanece módulo system (sem LOCAL_VENDOR_MODULE)."
else
  echo "AVISO: $CAMERA_MK não existe (sync incompleto?)." >&2
fi

if [[ ! -f "$VINTF_FRAG" ]]; then
  cat >"$VINTF_FRAG" <<'XML'
<!-- redroid-ui: legacy camera provider for V4L2 HAL -->
<manifest version="1.0" type="device">
    <hal format="hidl">
        <name>android.hardware.camera.provider</name>
        <transport>hwbinder</transport>
        <version>2.4</version>
        <interface>
            <name>ICameraProvider</name>
            <instance>legacy/0</instance>
        </interface>
        <fqname>@2.4::ICameraProvider/legacy/0</fqname>
    </hal>
</manifest>
XML
  echo "Criado $VINTF_FRAG"
fi

echo ""
echo "Próximo: lunch ${PRODUCT}-userdebug && m -j\$(nproc)"
