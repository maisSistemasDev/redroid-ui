#!/usr/bin/env bash
# Troca o WebView (com.android.webview) das imagens locais com câmera
# pelo APK x86_64 do Android 12 oficial. A tag original não é alterada.
#
#   bash tools/camera/replace-webview.sh
#   bash tools/camera/replace-webview.sh redroid:12-camera_mindthegapps
#
# Saída: <tag>-wv  (ex.: redroid:12-camera-wv)
set -euo pipefail

SRC_IMAGE="${REDROID_WEBVIEW_SRC:-redroid/redroid:12.0.0_64only-latest}"
WEBVIEW_PATHS=(
  /system/product/app/webview/webview.apk
  /system/app/webview/webview.apk
)

if docker info >/dev/null 2>&1; then
  DOCKER=(docker)
elif sudo -n docker info >/dev/null 2>&1; then
  DOCKER=(sudo -n docker)
else
  echo "docker não está acessível (nem direto, nem com sudo -n)." >&2
  exit 1
fi

if [[ $# -gt 0 ]]; then
  BASES=("$@")
else
  BASES=()
  for tag in redroid:12-camera redroid:12-camera_mindthegapps; do
    if "${DOCKER[@]}" image inspect "$tag" >/dev/null 2>&1; then
      BASES+=("$tag")
    fi
  done
fi

if [[ ${#BASES[@]} -eq 0 ]]; then
  echo "Nenhuma imagem redroid:12-camera encontrada." >&2
  exit 1
fi

mkdir -p "${HOME}/.cache"
work="$(mktemp -d "${HOME}/.cache/redroid-webview.XXXXXX")"
cleanup() {
  if [[ -n "${src_cid:-}" ]]; then
    "${DOCKER[@]}" rm -f "$src_cid" >/dev/null 2>&1 || true
  fi
  rm -rf "$work"
}
trap cleanup EXIT

echo "Baixando WebView de ${SRC_IMAGE}…"
"${DOCKER[@]}" pull "$SRC_IMAGE"
src_cid="$("${DOCKER[@]}" create "$SRC_IMAGE")"

apk=""
for path in "${WEBVIEW_PATHS[@]}"; do
  if "${DOCKER[@]}" cp "${src_cid}:${path}" "$work/webview.apk" 2>/dev/null; then
    apk="$path"
    break
  fi
done
"${DOCKER[@]}" rm -f "$src_cid" >/dev/null
src_cid=""

if [[ -z "$apk" ]] || [[ ! -s "$work/webview.apk" ]]; then
  echo "webview.apk não encontrado em ${SRC_IMAGE}." >&2
  exit 1
fi
if ! head -c 2 "$work/webview.apk" | grep -q $'PK'; then
  echo "O arquivo copiado de ${apk} não é um APK." >&2
  exit 1
fi
echo "APK: ${apk} ($(du -h "$work/webview.apk" | awk '{print $1}'))"

cat >"$work/patch.py" <<'PY'
import io
import sys
import tarfile

apk_path, out_path = sys.argv[1], sys.argv[2]
apk = open(apk_path, "rb").read()
replace = {"system/product/app/webview/webview.apk"}
src = tarfile.open(fileobj=sys.stdin.buffer, mode="r|*")
dst = tarfile.open(out_path, "w")
replaced = False
try:
    for member in src:
        name = member.name.lstrip("./")
        oat = (
            name == "system/product/app/webview/oat"
            or name.startswith("system/product/app/webview/oat/")
            or name == "system/app/webview/oat"
            or name.startswith("system/app/webview/oat/")
        )
        if oat:
            if member.isfile():
                blob = src.extractfile(member)
                if blob is not None:
                    while blob.read(1024 * 1024):
                        pass
            continue
        if name in replace and member.isfile():
            blob = src.extractfile(member)
            if blob is not None:
                while blob.read(1024 * 1024):
                    pass
            member.size = len(apk)
            dst.addfile(member, io.BytesIO(apk))
            replaced = True
            continue
        if member.isfile():
            dst.addfile(member, src.extractfile(member))
        else:
            dst.addfile(member)
finally:
    dst.close()
    src.close()
if not replaced:
    sys.exit("webview.apk não estava na imagem de destino")
PY

patch_image() {
  local base="$1"
  local out="${base}-wv"
  local cid entry tar
  echo "Exportando ${base}…"
  cid="$("${DOCKER[@]}" create "$base")"
  tar="$work/patched.tar"
  rm -f "$tar"
  "${DOCKER[@]}" export "$cid" | python3 "$work/patch.py" "$work/webview.apk" "$tar"
  "${DOCKER[@]}" rm -f "$cid" >/dev/null
  entry="$("${DOCKER[@]}" image inspect "$base" --format '{{json .Config.Entrypoint}}')"
  echo "Importando ${out}…"
  "${DOCKER[@]}" import --change "ENTRYPOINT ${entry}" "$tar" "$out" >/dev/null
  rm -f "$tar"
  echo "Pronta: ${out}"
}

for base in "${BASES[@]}"; do
  if ! "${DOCKER[@]}" image inspect "$base" >/dev/null 2>&1; then
    echo "Imagem ausente, pulando: ${base}" >&2
    continue
  fi
  patch_image "$base"
done
