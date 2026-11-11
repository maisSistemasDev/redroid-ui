#!/usr/bin/env bash
# Compila só android.hardware.camera.provider@2.4-service_64 e copia para tools/camera (bind-mount na UI).
# Não reimporta imagem Docker — recrie a instância na UI depois.
set -euo pipefail

REDROID_SRC="${REDROID_SRC:-$HOME/redroid}"
BUILDER="${BUILDER:-redroid-builder}"
PRODUCT="${REDROID_PRODUCT:-redroid_x86_64_only}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$SCRIPT_DIR/vendor/bin/hw/android.hardware.camera.provider@2.4-service_64"
DEST_RC="$SCRIPT_DIR/vendor/etc/init/camera.provider.v4l2.rc"

if [[ ! -f "$REDROID_SRC/build/envsetup.sh" ]]; then
  echo "Árvore AOSP não encontrada em $REDROID_SRC" >&2
  exit 1
fi

echo "=== Compilando módulo (minutos, não horas) ==="
docker run --rm --privileged \
  -v "${REDROID_SRC}:/src" \
  --entrypoint /bin/bash \
  "$BUILDER" \
  -c "set -eo pipefail
user=\$(cat /root/username)
chroot --userspec=\${user}:\${user} / /bin/bash -lc '
  set -eo pipefail
  set +u
  cd /src
  . build/envsetup.sh
  lunch ${PRODUCT}-userdebug
  m camera.v4l2 android.hardware.camera.provider@2.4-service_64
'"

OUT_BIN="$REDROID_SRC/out/target/product/$PRODUCT/vendor/bin/hw/android.hardware.camera.provider@2.4-service_64"
if [[ ! -f "$OUT_BIN" ]]; then
  echo "Binário não encontrado: $OUT_BIN" >&2
  exit 1
fi

mkdir -p "$(dirname "$DEST")"
cp -f "$OUT_BIN" "$DEST"
chmod +x "$DEST"
echo "Copiado → $DEST ($(du -h "$DEST" | cut -f1))"

SRC_RC="$REDROID_SRC/hardware/interfaces/camera/provider/2.4/default/android.hardware.camera.provider@2.4-service_64.rc"
mkdir -p "$(dirname "$DEST_RC")"
if [[ -f "$SRC_RC" ]]; then
  cp -f "$SRC_RC" "$DEST_RC"
  echo "Init RC → $DEST_RC"
fi

HAL_SO="$REDROID_SRC/out/target/product/$PRODUCT/system/lib64/hw/camera.v4l2.so"
if [[ ! -f "$HAL_SO" ]]; then
  HAL_SO="$REDROID_SRC/out/target/product/$PRODUCT/vendor/lib64/hw/camera.v4l2.so"
fi
if [[ -f "$HAL_SO" ]]; then
  DEST_HAL="$SCRIPT_DIR/vendor/lib64/hw/camera.v4l2.so"
  mkdir -p "$(dirname "$DEST_HAL")"
if [[ -f "$DEST_HAL" ]]; then
  # Preserva inode (bind-mount no contêiner já aberto).
  cat "$HAL_SO" > "$DEST_HAL"
else
  cp -f "$HAL_SO" "$DEST_HAL"
fi
  echo "HAL → $DEST_HAL ($(du -h "$DEST_HAL" | cut -f1))"
fi

echo ""
echo "Próximo: reinicie npm start se a API estiver no ar; recrie a instância (Aplicar câmera)."
echo "Teste: adb shell lshal | grep camera.provider"
