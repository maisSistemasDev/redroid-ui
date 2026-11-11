#!/usr/bin/env bash
# Cuttlefish no host: install | fetch | start | stop | status
set -euo pipefail

CF_HOME="${CF_HOME:-$HOME/cuttlefish}"
# aosp-main não publica imagens no bucket público; os branches *-gsi publicam.
CF_BUILD="${CF_BUILD:-aosp-android14-gsi/aosp_cf_x86_64_phone-userdebug}"
CF_CPUS="${CF_CPUS:-2}"
CF_MEMORY_MB="${CF_MEMORY_MB:-4096}"
CF_GPU_MODE="${CF_GPU_MODE:-auto}"

cmd_install() {
  sudo curl -fsSL https://us-apt.pkg.dev/doc/repo-signing-key.gpg \
    -o /etc/apt/trusted.gpg.d/artifact-registry.asc
  sudo chmod a+r /etc/apt/trusted.gpg.d/artifact-registry.asc
  echo "deb https://us-apt.pkg.dev/projects/android-cuttlefish-artifacts android-cuttlefish main" \
    | sudo tee /etc/apt/sources.list.d/artifact-registry.list >/dev/null
  sudo apt-get update
  sudo apt-get install -y cuttlefish-base cuttlefish-user
  sudo usermod -aG kvm,cvdnetwork,render "$USER"
  echo "Pronto. Reinicie o computador para os grupos e módulos valerem."
}

cmd_fetch() {
  command -v cvd >/dev/null || { echo "cvd não encontrado; rode: $0 install"; exit 1; }
  mkdir -p "$CF_HOME"
  cd "$CF_HOME"
  cvd fetch --default_build="$CF_BUILD" --target_directory="$CF_HOME"
}

cmd_start() {
  [ -x "$CF_HOME/bin/launch_cvd" ] || { echo "Imagem ausente; rode: $0 fetch"; exit 1; }
  cd "$CF_HOME"
  HOME="$CF_HOME" ./bin/launch_cvd --daemon \
    --cpus="$CF_CPUS" --memory_mb="$CF_MEMORY_MB" --gpu_mode="$CF_GPU_MODE" \
    --report_anonymous_usage_stats=n
  adb connect 127.0.0.1:6520 || true
  echo "ADB: 127.0.0.1:6520 | WebRTC: https://localhost:8443"
}

cmd_stop() {
  cd "$CF_HOME"
  HOME="$CF_HOME" ./bin/stop_cvd || true
}

cmd_status() {
  groups | tr ' ' '\n' | grep -E '^(kvm|cvdnetwork|render)$' || echo "sem grupos kvm/cvdnetwork/render"
  command -v cvd >/dev/null && echo "cvd: $(command -v cvd)" || echo "cvd: não instalado"
  [ -x "$CF_HOME/bin/launch_cvd" ] && echo "imagem: $CF_HOME" || echo "imagem: não baixada"
  adb devices | grep 6520 || echo "cvd não está rodando"
}

case "${1:-status}" in
  install) cmd_install ;;
  fetch) cmd_fetch ;;
  start) cmd_start ;;
  stop) cmd_stop ;;
  status) cmd_status ;;
  *) echo "uso: $0 {install|fetch|start|stop|status}"; exit 1 ;;
esac
