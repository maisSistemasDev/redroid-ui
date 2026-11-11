#!/usr/bin/env bash
# v4l2loopback: /dev/video0 = Iriun; /dev/video10–17 = câmera das instâncias reDroid.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEVICES="${REDROID_CAMERA_LOOPBACK_DEVICES:-7}"
FIRST_NR="${REDROID_CAMERA_LOOPBACK_FIRST:-10}"
IRIUN_NR="${REDROID_CAMERA_IRIUN_NR:-0}"
MODPROBE_DST="/etc/modprobe.d/v4l2loopback-redroid.conf"
IRIUN_CONFLICT="/etc/modprobe.d/iriunwebcam-options.conf"

if ! command -v modprobe >/dev/null 2>&1; then
  echo "modprobe não encontrado." >&2
  exit 1
fi

if ! modprobe -n v4l2loopback 2>/dev/null; then
  echo "Módulo v4l2loopback não disponível. Instale: sudo apt install v4l2loopback-dkms" >&2
  exit 1
fi

NR_LIST="${IRIUN_NR}"
LABELS="Iriun Webcam"
for ((i = 0; i < DEVICES; i++)); do
  NR_LIST+=",$((FIRST_NR + i))"
  LABELS+=",redroid-$((FIRST_NR + i))"
done
TOTAL=$((DEVICES + 1))

echo "Parando ffmpeg que segura /dev/video*…"
pkill -f 'ffmpeg.* /dev/video' 2>/dev/null || true
sleep 0.5

if lsmod | grep -Eq '^v4l2loopback' || ls /dev/video10 >/dev/null 2>&1; then
  echo "Descarregando v4l2loopback (obrigatório para criar /dev/video${IRIUN_NR})…"
  if ! sudo modprobe -r v4l2loopback 2>/dev/null && ! sudo rmmod v4l2loopback 2>/dev/null; then
    echo "Ainda em uso. Pare instâncias reDroid (Parar) e o Iriun, depois rode de novo." >&2
    echo "Quem segura o módulo:" >&2
    fuser -v /dev/video* 2>&1 || true
    lsmod | grep v4l2 || true
    exit 1
  fi
  sleep 0.3
  if lsmod | grep -Eq '^v4l2loopback'; then
    echo "rmmod não descarregou o módulo. Feche o Iriun e os contêineres Docker com --device /dev/video*." >&2
    exit 1
  fi
fi

echo "Instalando ${MODPROBE_DST} (o Iriun chama 'modprobe v4l2loopback' sem argumentos)…"
sudo cp "${SCRIPT_DIR}/v4l2loopback-redroid.conf" "${MODPROBE_DST}"
sudo chmod 644 "${MODPROBE_DST}"
if [[ -f "${IRIUN_CONFLICT}" ]] && grep -q 'devices=1' "${IRIUN_CONFLICT}"; then
  echo "Desativando ${IRIUN_CONFLICT} (devices=1 conflita com video10–17)."
  sudo mv "${IRIUN_CONFLICT}" "${IRIUN_CONFLICT}.disabled-by-redroid-ui"
fi

echo "Carregando v4l2loopback só via ${MODPROBE_DST} (não passar devices= na linha de comando — soma com o .conf e dá EINVAL)…"
sudo modprobe v4l2loopback

sleep 0.3
if [[ ! -e "/dev/video${IRIUN_NR}" ]]; then
  echo "ERRO: /dev/video${IRIUN_NR} não foi criado." >&2
  ls -l /dev/video* 2>/dev/null || true
  exit 1
fi

if command -v v4l2-ctl >/dev/null 2>&1; then
  echo "--- /dev/video${IRIUN_NR} (Iriun) ---"
  v4l2-ctl --device "/dev/video${IRIUN_NR}" --info || true
fi
ls -l /dev/video* 2>/dev/null || true

echo
echo "Pronto."
echo "  1. Abra o Iriun agora (entrada = /dev/video${IRIUN_NR})."
echo "  2. Religue a instância reDroid (os --device antigos ficam inválidos após o rmmod)."
echo "  3. Modal Câmera → Deep-Live-Cam → webcam de entrada /dev/video${IRIUN_NR}."
