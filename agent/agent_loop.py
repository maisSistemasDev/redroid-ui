import os
import sys
import time
import json
from client import VirtualeClient
from vlm_engine import VlmEngine

def run_agent(instance_name, goal, max_steps=20):
    client = VirtualeClient()
    inst = client.get_instance_by_name(instance_name)
    if not inst:
        print(f"[-] Instância '{instance_name}' não encontrada.")
        return False

    inst_id = inst["id"]
    print(f"\n=======================================================")
    print(f"🤖 AGENTE MKOS INICIADO")
    print(f"📱 Dispositivo: {instance_name} (ID: {inst_id})")
    print(f"🎯 Objetivo: {goal}")
    print(f"=======================================================\n")

    vlm = VlmEngine()
    history = []

    for step in range(1, max_steps + 1):
        print(f"\n--- [PASSO {step}/{max_steps}] ---")

        # 1. Capture screen
        t0 = time.time()
        print("📸 Capturando tela via screencap...")
        try:
            b64_screen = client.screenshot_base64(inst_id)
            print(f"✔ Tela obtida em {int((time.time() - t0)*1000)}ms")
        except Exception as e:
            print(f"❌ Falha ao obter screenshot: {e}")
            time.sleep(2)
            continue

        # 2. VLM Decision
        print("🧠 Consultando modelo VLM...")
        t1 = time.time()
        decision = vlm.decide_action(b64_screen, goal, history=history)
        print(f"✔ Decisão tomada em {int((time.time() - t1)*1000)}ms")

        thought = decision.get("thought", "Sem pensamento")
        action = decision.get("action", "wait")
        print(f"💡 Pensamento: {thought}")
        print(f"⚡ Ação decidida: {action.upper()}")

        history.append(decision)

        # 3. Execution
        if action == "done":
            print(f"\n🎉 OBJETIVO CONCLUÍDO COM SUCESSO!")
            print(f"Resumo: {decision.get('summary', 'Concluído')}")
            return True

        if action == "fail":
            print(f"\n❌ FALHA NO OBJETIVO: {decision.get('reason', 'Desconhecido')}")
            return False

        if action == "tap":
            x, y = decision.get("x", 360), decision.get("y", 640)
            print(f"👉 Executando Toque em ({x}, {y})")
            client.tap(inst_id, x, y)
            time.sleep(1.5)

        elif action == "swipe":
            x1, y1 = decision.get("x1", 360), decision.get("y1", 900)
            x2, y2 = decision.get("x2", 360), decision.get("y2", 300)
            dur = decision.get("duration", 300)
            print(f"👆 Executando Swipe de ({x1},{y1}) para ({x2},{y2}) em {dur}ms")
            client.swipe(inst_id, x1, y1, x2, y2, dur)
            time.sleep(2.0)

        elif action == "type":
            txt = decision.get("text", "")
            print(f"⌨ Digitando texto: '{txt}'")
            client.type_text(inst_id, txt)
            time.sleep(1.5)

        elif action == "keyevent":
            key = decision.get("key", "HOME")
            print(f"🔘 Pressionando tecla de sistema: {key}")
            client.keyevent(inst_id, key)
            time.sleep(1.0)

        elif action == "wait":
            sec = decision.get("seconds", 2)
            print(f"⏳ Aguardando {sec}s...")
            time.sleep(sec)

    print("\n⚠️ Limite de passos atingido sem conclusão definitiva.")
    return False

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "mkos-phone-01"
    goal_arg = sys.argv[2] if len(sys.argv) > 2 else "Abra a tela inicial e verifique se o launcher está ativo."
    run_agent(target, goal_arg)
