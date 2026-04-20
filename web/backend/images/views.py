from __future__ import annotations

import re

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from instances.services import (
    ANDROID_VERSIONS,
    DockerService,
    get_build_status,
    get_host_arch,
    get_spoof_status,
    start_build,
    start_spoof,
)


@api_view(["GET"])
def list_images(request):
    try:
        images = DockerService.list_images()
    except RuntimeError as e:
        return Response({"detail": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    return Response({"images": images, "arch": get_host_arch()})


@api_view(["DELETE"])
def remove_image(request, image_id):
    if not re.match(r"^[a-zA-Z0-9:._/-]+$", image_id):
        return Response({"detail": "ID/tag de imagem inválido."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        DockerService.remove_image(image_id)
    except RuntimeError as e:
        return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
    return Response({"status": "removed", "image": image_id})


@api_view(["POST"])
def build_image(request):
    params = dict(request.data)
    av = params.get("android_version", "11.0.0")
    if av not in ANDROID_VERSIONS:
        return Response(
            {"detail": f"Versão Android inválida. Opções: {', '.join(ANDROID_VERSIONS)}"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    gapps_variant = str(params.get("gapps_variant", "mindthegapps")).strip().lower()
    if gapps_variant not in {"none", "gapps", "litegapps", "mindthegapps"}:
        return Response(
            {"detail": "gapps_variant inválido. Use: none, gapps, litegapps ou mindthegapps."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    params["gapps"] = gapps_variant == "gapps"
    params["litegapps"] = gapps_variant == "litegapps"
    params["mindthegapps"] = gapps_variant == "mindthegapps"

    params["houdini"] = str(params.get("houdini", "1")).lower() in {"1", "true", "yes", "on"}
    params["auto_spoof"] = str(params.get("auto_spoof", "1")).lower() in {"1", "true", "yes", "on"}
    params["cleanup_intermediate"] = str(params.get("cleanup_intermediate", "1")).lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    spoof_base_image = str(params.get("spoof_base_image", "")).strip()
    if spoof_base_image:
        if not re.match(r"^[a-zA-Z0-9:._/-]+$", spoof_base_image):
            return Response({"detail": "spoof_base_image inválida."}, status=status.HTTP_400_BAD_REQUEST)
        params["spoof_base_image"] = spoof_base_image

    ok = start_build(dict(params))
    if not ok:
        return Response({"detail": "Já existe um build em andamento."}, status=status.HTTP_409_CONFLICT)
    return Response({"status": "started"})


@api_view(["GET"])
def build_status(request):
    return Response(get_build_status())


@api_view(["POST"])
def spoof_image(request):
    base_image = request.data.get("base_image", "")
    if not re.match(r"^[a-zA-Z0-9:._/-]+$", base_image):
        return Response({"detail": "Tag de imagem inválida."}, status=status.HTTP_400_BAD_REQUEST)
    ok = start_spoof(base_image)
    if not ok:
        return Response({"detail": "Já existe um spoof em andamento."}, status=status.HTTP_409_CONFLICT)
    return Response({"status": "started"})


@api_view(["GET"])
def spoof_status(request):
    return Response(get_spoof_status())
