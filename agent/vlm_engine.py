import os
import json
import re
import requests

SYSTEM_PROMPT = """Você é um Agente Autônomo de Controle de Dispositivos Android (Mobile VLM Agent).
Sua missão é atingir o OBJETIVO solicitado pelo usuário interagindo com a interface gráfica do Android.

A cada turno, você receberá a imagem atual da tela do smartphone (Screenshot), a janela ativa e o objetivo.
Você deve responder ESTRITAMENTE em formato JSON com uma das seguintes ações:

1. Toque na tela:
{"thought": "explicação do que você está clicando e porquê", "action": "tap", "x": 360, "y": 640}

2. Deslizar (Swipe/Scroll):
{"thought": "rolando a página para ver mais posts", "action": "swipe", "x1": 360, "y1": 900, "x2": 360, "y2": 300, "duration": 350}

3. Digitar texto em campo focado:
{"thought": "digitando legenda da postagem", "action": "type", "text": "Texto a ser digitado"}

4. Enviar tecla do sistema:
{"thought": "voltando para a tela inicial", "action": "keyevent", "key": "HOME"} (Opções: HOME, BACK, ENTER, APP_SWITCH)

5. Aguardar animação/carregamento:
{"thought": "aguardando o vídeo terminar de carregar", "action": "wait", "seconds": 3}

6. Objetivo concluído com sucesso:
{"thought": "o vídeo foi postado e está visível no feed", "action": "done", "summary": "Resumo do que foi feito"}

7. Erro / Bloqueio irrecuperável:
{"thought": "apareceu tela de bloqueio com captcha", "action": "fail", "reason": "Motivo da falha"}

Responda APENAS o JSON válido, sem texto adicional antes ou depois.
"""

class VlmEngine:
    def __init__(self, api_key=None, base_url=None, model="gemini-2.0-flash"):
        self.api_key = api_key or os.getenv("APINEX_API_KEY", "")
        self.base_url = (base_url or os.getenv("APINEX_BASE_URL", "https://api.apinex.bond/v1")).rstrip("/")
        self.model = model or os.getenv("DEFAULT_MODEL", "gemini-2.0-flash")

    def decide_action(self, screenshot_b64, goal, history=None):
        """Sends screenshot to VLM and returns parsed action dict."""
        if not self.api_key:
            return {
                "thought": "Chave de API (APINEX_API_KEY) não configurada. Configure no arquivo .env para execução autônoma.",
                "action": "fail",
                "reason": "Missing APINEX_API_KEY"
            }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        user_content = [
            {"type": "text", "text": f"OBJETIVO: {goal}\n\nAnalise o frame da tela do Android e retorne a próxima ação em JSON."}
        ]

        if screenshot_b64:
            user_content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{screenshot_b64}"
                }
            })

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
        ]

        # Append recent interaction history if present
        if history:
            for h in history[-4:]:
                messages.append({"role": "assistant", "content": json.dumps(h)})

        messages.append({"role": "user", "content": user_content})

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": 500,
        }

        try:
            r = requests.post(f"{self.base_url}/chat/completions", headers=headers, json=payload, timeout=30)
            r.raise_for_status()
            res_json = r.json()
            content = res_json["choices"][0]["message"]["content"].strip()

            # Clean markdown formatting if model wrapped in ```json ... ```
            content_cleaned = re.sub(r"^```(?:json)?", "", content, flags=re.MULTILINE)
            content_cleaned = re.sub(r"```$", "", content_cleaned, flags=re.MULTILINE).strip()

            return json.loads(content_cleaned)
        except Exception as e:
            return {
                "thought": f"Erro na chamada do modelo {self.model}: {str(e)}",
                "action": "fail",
                "reason": str(e)
            }
