import time
import random
from client import VirtualeClient

def playbook_inject_and_prepare_reel(instance_name, video_path, filename="ugc_post.mp4"):
    """Ponte direta Remotion -> Galeria do Android com Broadcast de Mídia."""
    client = VirtualeClient()
    inst = client.get_instance_by_name(instance_name)
    if not inst:
        raise ValueError(f"Dispositivo '{instance_name}' não encontrado.")

    inst_id = inst["id"]
    print(f"🎬 [Playbook] Injetando {video_path} em {instance_name}...")
    res = client.inject_video(inst_id, video_path, filename)
    print(f"✔ [Playbook] Vídeo injetado na Galeria: {res.get('gallery_path')}")
    print(f"✔ [Playbook] Broadcast Intent: {res.get('broadcast_output')}")
    return res

def playbook_organic_warmup(instance_name, scroll_count=5):
    """Executa rolagem com pausas orgânicas humanas para aquecimento de conta anti-ban."""
    client = VirtualeClient()
    inst = client.get_instance_by_name(instance_name)
    if not inst:
        raise ValueError(f"Dispositivo '{instance_name}' não encontrado.")

    inst_id = inst["id"]
    print(f"☕ [Playbook] Iniciando Aquecimento Orgânico em {instance_name} ({scroll_count} rolagens)...")

    for i in range(1, scroll_count + 1):
        # Human jitter
        duration = random.randint(280, 480)
        start_y = random.randint(850, 950)
        end_y = random.randint(250, 380)
        start_x = random.randint(340, 380)

        print(f"[{i}/{scroll_count}] Swipe orgânico de ({start_x}, {start_y}) para ({start_x}, {end_y})...")
        client.swipe(inst_id, start_x, start_y, start_x, end_y, duration)

        pause = random.uniform(2.5, 6.0)
        print(f"  Pausa de leitura humana: {pause:.1f}s")
        time.sleep(pause)

    print(f"✔ [Playbook] Aquecimento concluído!")
    return True

def playbook_rotate_ip(instance_name):
    """Aciona rotação de IP do modem 4G/5G móvel."""
    client = VirtualeClient()
    inst = client.get_instance_by_name(instance_name)
    if not inst:
        raise ValueError(f"Dispositivo '{instance_name}' não encontrado.")

    inst_id = inst["id"]
    print(f"🔄 [Playbook] Disparando rotação de IP móvel 4G/5G...")
    res = client.rotate_proxy(inst_id)
    print(f"✔ [Playbook] Resposta da Rotação: {res}")
    return res

if __name__ == "__main__":
    import sys
    action = sys.argv[1] if len(sys.argv) > 1 else "warmup"
    inst = sys.argv[2] if len(sys.argv) > 2 else "mkos-phone-01"

    if action == "warmup":
        playbook_organic_warmup(inst, scroll_count=3)
    elif action == "inject":
        vpath = sys.argv[3] if len(sys.argv) > 3 else "/opt/video-benchmark/test_render.mp4"
        playbook_inject_and_prepare_reel(inst, vpath, "ugc_teste.mp4")
    elif action == "rotate":
        playbook_rotate_ip(inst)
