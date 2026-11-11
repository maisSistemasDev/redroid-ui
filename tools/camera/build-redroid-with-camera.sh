#!/usr/bin/env bash
#
# Build automatizado: reDroid Android 12 (x86_64 only) + HAL V4L2 (câmera).
#
# Uso:
#   bash build-redroid-with-camera.sh              # fluxo completo (horas)
#   bash build-redroid-with-camera.sh --step sync  # só repo sync
#   bash build-redroid-with-camera.sh --step compile
#   bash build-redroid-with-camera.sh --help
#
# Variáveis:
#   REDROID_SRC=~/redroid
#   REDROID_PRODUCT=redroid_x86_64_only
#   REDROID_IMAGE_TAG=redroid:12-camera
#   REDROID_JOBS=$(nproc)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REDROID_UI_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

REDROID_SRC="${REDROID_SRC:-$HOME/redroid}"
REDROID_PRODUCT="${REDROID_PRODUCT:-redroid_x86_64_only}"
REDROID_IMAGE_TAG="${REDROID_IMAGE_TAG:-redroid:12-camera}"
REDROID_JOBS="${REDROID_JOBS:-$(nproc)}"
PATCHES_DIR="${REDROID_PATCHES_DIR:-$HOME/redroid-patches}"

AOSP_MANIFEST_BRANCH="${AOSP_MANIFEST_BRANCH:-android-12.0.0_r34}"
LOCAL_MANIFESTS_BRANCH="${LOCAL_MANIFESTS_BRANCH:-12.0.0}"

BUILDER_IMAGE="${BUILDER_IMAGE:-redroid-builder}"
DOCKERFILE_URL="https://raw.githubusercontent.com/remote-android/redroid-doc/master/android-builder-docker/Dockerfile"

MIN_FREE_GB="${MIN_FREE_GB:-180}"
LOG_FILE="${REDROID_SRC}/build-redroid-camera.log"

STEP="all"

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \?//'
  echo ""
  echo "Etapas (--step): fetch | sync | patch | builder | compile | import | verify | all"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h | --help) usage; exit 0 ;;
    --step)
      STEP="${2:-all}"
      shift 2
      ;;
    --src)
      REDROID_SRC="$2"
      shift 2
      ;;
    *)
      echo "Argumento desconhecido: $1" >&2
      usage
      exit 1
      ;;
  esac
done

log() {
  echo "[$(date -Iseconds)] $*"
  echo "[$(date -Iseconds)] $*" >>"$LOG_FILE"
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Comando obrigatório ausente: $1" >&2
    exit 1
  }
}

check_docker() {
  need_cmd docker
  if ! docker info >/dev/null 2>&1; then
    echo "Docker não responde. Adicione seu usuário ao grupo docker e faça login de novo." >&2
    exit 1
  fi
}

check_disk() {
  local avail_kb target_dir="$REDROID_SRC"
  mkdir -p "$target_dir"
  avail_kb="$(df -Pk "$target_dir" | awk 'NR==2 {print $4}')"
  local need_kb=$((MIN_FREE_GB * 1024 * 1024))
  if [[ "$avail_kb" -lt "$need_kb" ]]; then
    echo "Pouco espaço em $(df -h "$target_dir" | awk 'NR==2 {print $6}'): need ~${MIN_FREE_GB}GB, livre ~$((avail_kb / 1024 / 1024))GB" >&2
    exit 1
  fi
}

ensure_repo_tool() {
  if command -v repo >/dev/null 2>&1; then
    return
  fi
  mkdir -p "$HOME/.bin"
  curl -fsSL https://storage.googleapis.com/git-repo-downloads/repo -o "$HOME/.bin/repo"
  chmod +x "$HOME/.bin/repo"
  export PATH="$HOME/.bin:$PATH"
  need_cmd repo
}

step_fetch() {
  log "=== fetch: init árvore AOSP + local_manifests reDroid ==="
  ensure_repo_tool
  need_cmd git
  need_cmd curl
  mkdir -p "$REDROID_SRC"
  if [[ ! -d "$REDROID_SRC/.repo" ]]; then
    (
      cd "$REDROID_SRC"
      repo init -u https://android.googlesource.com/platform/manifest --git-lfs --depth=1 -b "$AOSP_MANIFEST_BRANCH"
      git clone https://github.com/remote-android/local_manifests.git .repo/local_manifests -b "$LOCAL_MANIFESTS_BRANCH"
    )
  else
    log "fetch: .repo já existe, pulando repo init"
  fi
}

check_webview_lfs() {
  local apk="$REDROID_SRC/external/chromium-webview/prebuilt/x86_64/webview.apk"
  if [[ ! -f "$apk" ]]; then
    return 0
  fi
  if [[ $(stat -c%s "$apk" 2>/dev/null || echo 0) -lt 1000000 ]]; then
    echo "webview.apk é ponteiro Git LFS (~134 bytes), não o APK real (~350 MB)." >&2
    echo "Instale git-lfs e baixe os binários:" >&2
    echo "  sudo apt install git-lfs && git lfs install" >&2
    echo "  cd $REDROID_SRC/external/chromium-webview/prebuilt/x86_64 && git lfs pull" >&2
    echo "  (APKs ficam em subprojetos LineageOS; ~/redroid não é um único git repo.)" >&2
    exit 1
  fi
}

step_sync() {
  log "=== sync: repo sync (longo) ==="
  ensure_repo_tool
  check_disk
  (
    cd "$REDROID_SRC"
    repo sync -c -j"$REDROID_JOBS"
  )
  pull_chromium_webview_lfs
  check_webview_lfs
}

pull_chromium_webview_lfs() {
  command -v git-lfs >/dev/null 2>&1 || {
    echo "Instale git-lfs: sudo apt install git-lfs && git lfs install" >&2
    return 0
  }
  git lfs install >/dev/null 2>&1 || true
  local base="$REDROID_SRC/external/chromium-webview/prebuilt"
  for arch in x86 x86_64 arm arm64; do
    if [[ -d "$base/$arch/.git" || -L "$base/$arch/.git" ]]; then
      log "git lfs pull → prebuilt/$arch"
      (cd "$base/$arch" && git lfs pull) || true
    fi
  done
}

step_patch() {
  log "=== patch: redroid-patches + câmera V4L2 ==="
  if [[ ! -d "$REDROID_SRC/device/redroid/$REDROID_PRODUCT" ]]; then
    echo "Produto device/redroid/$REDROID_PRODUCT não encontrado. Rode --step sync primeiro." >&2
    exit 1
  fi
  if [[ ! -d "$PATCHES_DIR" ]]; then
    git clone https://github.com/remote-android/redroid-patches.git "$PATCHES_DIR"
  fi
  "$PATCHES_DIR/apply-patch.sh" "$REDROID_SRC" || {
    echo "AVISO: alguns patches redroid falharam; veja log. Continuando patch de câmera…" >&2
  }
  bash "$SCRIPT_DIR/patch-aosp-camera.sh" "$REDROID_SRC" "$REDROID_PRODUCT"
}

step_builder() {
  log "=== builder: imagem Docker redroid-builder ==="
  check_docker
  local df="$REDROID_SRC/Dockerfile.redroid-builder"
  curl -fsSL "$DOCKERFILE_URL" -o "$df"
  docker buildx create --use >/dev/null 2>&1 || true
  docker buildx build \
    --build-arg "userid=$(id -u)" \
    --build-arg "groupid=$(id -g)" \
    --build-arg "username=$(id -un)" \
    -t "$BUILDER_IMAGE" \
    --load \
    -f "$df" \
    "$REDROID_SRC"
}

compile_out_dir() {
  echo "$REDROID_SRC/out/target/product/$REDROID_PRODUCT"
}

step_compile() {
  log "=== compile: m -j$REDROID_JOBS (várias horas) ==="
  check_docker
  check_disk
  if [[ ! -f "$REDROID_SRC/build/envsetup.sh" ]]; then
    echo "Árvore incompleta. Rode sync antes." >&2
    exit 1
  fi
  check_webview_lfs
  local out
  out="$(compile_out_dir)"
  local lunch_target="${REDROID_PRODUCT}-userdebug"
  # ENTRYPOINT padrão do redroid-builder é bash -i (interativo); override para rodar m de verdade.
  docker run --rm --privileged \
    -v "${REDROID_SRC}:/src" \
    --entrypoint /bin/bash \
    "$BUILDER_IMAGE" \
    -c "set -eo pipefail
user=\$(cat /root/username)
chroot --userspec=\${user}:\${user} / /bin/bash -lc '
  set -eo pipefail
  cd /src
  # envsetup.sh do AOSP não tolera nounset (set -u)
  set +u
  . build/envsetup.sh
  lunch ${lunch_target}
  m -j${REDROID_JOBS}
'"
  if [[ ! -f "$out/system.img" ]]; then
    echo "Compile terminou mas $out/system.img não existe." >&2
    echo "Veja erros acima ou rode manualmente:" >&2
    echo "  docker run -it --privileged -v $REDROID_SRC:/src $BUILDER_IMAGE" >&2
    echo "  cd /src && . build/envsetup.sh && lunch ${lunch_target} && m -j${REDROID_JOBS}" >&2
    exit 1
  fi
  log "Compile OK: $out/system.img"
}

step_import() {
  log "=== import: system.img + vendor.img → $REDROID_IMAGE_TAG ==="
  check_docker
  local out
  out="$(compile_out_dir)"
  if [[ ! -f "$out/system.img" ]]; then
    echo "system.img não encontrado em $out — compile falhou ou produto errado." >&2
    exit 1
  fi
  (
    cd "$out"
    mkdir -p system vendor
    sudo mount -o ro system.img system
    sudo mount -o ro vendor.img vendor
    sudo tar --xattrs -c vendor -C system --exclude="./vendor" . \
      | docker import --platform=linux/amd64 \
        -c 'ENTRYPOINT ["/init", "androidboot.hardware=redroid"]' \
        - "$REDROID_IMAGE_TAG"
    sudo umount system vendor
    rmdir system vendor 2>/dev/null || true
  )
  log "Imagem Docker: $REDROID_IMAGE_TAG"
}

install_hal_to_ui_bundle() {
  local cid hal_dest="$REDROID_UI_ROOT/tools/camera/vendor/lib64/hw/camera.v4l2.so"
  mkdir -p "$(dirname "$hal_dest")"
  cid="$(docker create "$REDROID_IMAGE_TAG")"
  local copied=""
  for p in \
    system/lib64/hw/camera.v4l2.so \
    system/lib/hw/camera.v4l2.so \
    vendor/lib64/hw/camera.v4l2.so; do
    if docker cp "${cid}:${p}" "$hal_dest" 2>/dev/null && [[ -s "$hal_dest" ]]; then
      copied="$p"
      break
    fi
  done
  docker rm "$cid" >/dev/null
  if [[ -z "$copied" ]]; then
    echo "Não foi possível copiar camera.v4l2.so para tools/camera/." >&2
    return 1
  fi
  log "HAL copiado de $copied → $hal_dest (bind-mount na UI)"
}

step_verify() {
  log "=== verify: camera.v4l2.so na imagem ==="
  check_docker
  local cid found_path=""
  cid="$(docker create "$REDROID_IMAGE_TAG")"
  while read -r path; do
    found_path="$path"
    break
  done < <(
    docker export "$cid" | tar -t 2>/dev/null | grep -E '(system|vendor)/lib64/hw/camera\.v4l2\.so$' || true
  )
  docker rm "$cid" >/dev/null
  if [[ -z "$found_path" ]]; then
    echo "HAL não encontrado na imagem. Revise patch-aosp-camera e logs de compile." >&2
    exit 1
  fi
  log "OK: $found_path na imagem Docker"
  install_hal_to_ui_bundle || true
  log "Use na redroid-ui: imagem $REDROID_IMAGE_TAG + câmera ativada + setup-host.sh"
}

run_step() {
  case "$1" in
    fetch) step_fetch ;;
    sync) step_sync ;;
    patch) step_patch ;;
    builder) step_builder ;;
    compile) step_compile ;;
    import) step_import ;;
    verify) step_verify ;;
    all)
      step_fetch
      step_sync
      step_patch
      step_builder
      step_compile
      step_import
      step_verify
      ;;
    *)
      echo "Etapa inválida: $1" >&2
      usage
      exit 1
      ;;
  esac
}

mkdir -p "$REDROID_SRC"
: >"$LOG_FILE"
log "Início build reDroid+câmera (step=$STEP) src=$REDROID_SRC"
run_step "$STEP"
log "Concluído."
