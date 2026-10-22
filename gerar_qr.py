"""Gera um QR code novo pra conectar o WhatsApp (instância bot-emanuel-v2).

Uso: rode quando estiver com o celular em mãos, escaneie em ~20s.
  python gerar_qr.py
"""
import base64
import sys
import time

import httpx

BASE = "http://localhost:9095"
INSTANCIA = "bot-emanuel-v2"
HDR = {"apikey": "EmanuelSenhaSecreta123"}
SAIDA = "qr_whatsapp.png"


def state() -> str:
    r = httpx.get(f"{BASE}/instance/connectionState/{INSTANCIA}", headers=HDR, timeout=30)
    return r.json().get("instance", {}).get("state", "?")


def main() -> int:
    print("Estado atual:", state())

    r = httpx.get(f"{BASE}/instance/connect/{INSTANCIA}", headers=HDR, timeout=180)
    r.raise_for_status()
    d = r.json()
    b64 = (d.get("base64") or "").split(",")[-1]
    if not b64:
        print("Sem QR na resposta:", str(d)[:300])
        return 1

    with open(SAIDA, "wb") as f:
        f.write(base64.b64decode(b64))

    print(f"\nQR salvo em: {SAIDA}")
    print("Escaneie AGORA: WhatsApp > Aparelhos conectados > Conectar aparelho\n")

    for i in range(12):
        time.sleep(5)
        st = state()
        print(f"  state={st}")
        if st == "open":
            print("\nCONECTADO! O bot já pode enviar mensagens.")
            return 0

    print("\nAinda não conectou. Rode o script de novo pra gerar outro QR.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
