import requests
import base64
import time

# Configurações do seu ambiente Docker em São Luís
URL_BASE = "http://localhost:9095" 
API_KEY = "EmanuelSenhaSecreta123"
NOVA_INSTANCE = "bot-emanuel-v2"

def gerenciar_instancia():
    headers = {
        "apikey": API_KEY,
        "Content-Type": "application/json"
    }

    # 1. Tenta criar a instância (Equivalente ao seu CURL)
    payload_create = {
        "instanceName": NOVA_INSTANCE,
        "token": API_KEY,
        "qrcode": True
    }
    
    print(f"🛠️ Criando/Verificando instância: {NOVA_INSTANCE}...")
    requests.post(f"{URL_BASE}/instance/create", json=payload_create, headers=headers)
    
    # Aguarda um segundo para o servidor processar a criação
    time.sleep(1)

    # 2. Solicita o QR Code para conexão
    url_connect = f"{URL_BASE}/instance/connect/{NOVA_INSTANCE}"
    
    try:
        response = requests.get(url_connect, headers={"apikey": API_KEY})
        data = response.json()
        
        if data.get("status") == "OPEN" or data.get("instance", {}).get("state") == "open":
            print(f"✅ A instância '{NOVA_INSTANCE}' já está conectada e pronta!")
            return

        # Pega o base64 do QR Code
        qr_base64 = data.get("base64")
        
        if qr_base64:
            if "," in qr_base64:
                qr_base64 = qr_base64.split(",")[1]
            
            with open("qrcode_v2.png", "wb") as fh:
                fh.write(base64.b64decode(qr_base64))
            
            print("🖼️ QR Code gerado! Abra o arquivo 'qrcode_v2.png' e escaneie no WhatsApp.")
        else:
            print(f"⚠️ Resposta inesperada da API: {data}")
            
    except Exception as e:
        print(f"❌ Erro na comunicação: {e}")

if __name__ == "__main__":
    gerenciar_instancia()