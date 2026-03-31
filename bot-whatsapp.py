import logging
import requests
import re
from contextlib import asynccontextmanager
import json
from fastapi import FastAPI, Request
import uvicorn
import time
import os # Adicione o import no topo
# Configuração de Logs
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
GEMINI_KEY = os.getenv("GEMINI_KEY", "")
# --- CONFIGURAÇÕES ---
# URLs Híbridas: Prioriza o ambiente Docker, mas aceita localhost como fallback
EVOLUTION_BASE_URL = os.getenv("EVOLUTION_URL", "http://localhost:9095")
# Nota: Externamente seu scraper está na 9094, internamente na 8000
SCRAPER_URL = os.getenv("SCRAPER_URL", "http://localhost:8000/buscar")
EVOLUTION_API_TOKEN = os.getenv("EVOLUTION_API_TOKEN", "")
INSTANCE_NAME = os.getenv("INSTANCE_NAME", "bot-emanuel-v2")
# --- O NOVO GERENCIADOR DE VIDA (LIFESPAN) ---
import asyncio # Precisamos de asyncio para a tarefa de fundo

async def tarefa_configuracao_webhook():
    """Roda em paralelo para não travar o servidor"""
    # Dá um tempo para o Uvicorn abrir a porta 8001 primeiro
    await asyncio.sleep(2) 
    
    url_set = f"{EVOLUTION_BASE_URL}/webhook/set/{INSTANCE_NAME}"
    headers = {"apikey": EVOLUTION_API_TOKEN, "Content-Type": "application/json"}
    payload = {
        "enabled": True,
        "url": "http://bot-whatsapp:8001/webhook",
        "webhook_by_events": False,
        "events": ["MESSAGES_UPSERT"]
    }

    for i in range(10):
        try:
            # Usamos loop.run_in_executor para não travar o loop de eventos com requests
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, lambda: requests.post(url_set, json=payload, headers=headers))
            
            # ACEITANDO 200 (OK) OU 201 (CREATED)
            if response.status_code in [200, 201]:
                logger.info(f"✅ Webhook auto-configurado com sucesso (Status: {response.status_code})!")
                return
            else:
                logger.warning(f"⏳ Tentativa {i+1}: Evolution retornou {response.status_code}. Aguardando...")
        except Exception:
            logger.warning(f"⏳ Tentativa {i+1}: Evolution API ainda offline...")
        
        await asyncio.sleep(5)
    logger.error("❌ Não foi possível configurar o webhook automaticamente.")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Dispara a configuração em segundo plano e LIBERA o servidor
    asyncio.create_task(tarefa_configuracao_webhook())
    yield 
    logger.info("🛑 Desligando bot...")

app = FastAPI(lifespan=lifespan)


def nota_estrelas_promocao(avaliacao: str) -> str:
    """Converte o termômetro textual em uma nota visual com estrelas."""
    texto = (avaliacao or "").lower()
    if "black friday" in texto:
        return "★★★★★"
    if "promoção boa" in texto or "promocao boa" in texto:
        return "★★★★☆"
    if "preço normal" in texto or "preco normal" in texto:
        return "★★★☆☆"
    if "furada" in texto:
        return "★☆☆☆☆"
    return "★★★☆☆"


def enviar_mensagem(jid, texto):
    url = f"{EVOLUTION_BASE_URL}/message/sendText/{INSTANCE_NAME}"
    headers = {"apikey": EVOLUTION_API_TOKEN, "Content-Type": "application/json"}
    
    # Garante o sufixo correto
    if "@" not in jid:
        jid = f"{jid}@s.whatsapp.net"

    payload = {
        "number": jid,
        "textMessage": {"text": texto}
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers)
        if response.status_code in [200, 201]:
            logger.info(f"🚀 Mensagem enviada para: {jid}")
        else:
            logger.error(f"❌ Erro na Evolution: {response.text}")
    except Exception as e:
        logger.error(f"❌ Falha de conexão: {e}")

@app.post("/webhook")
async def receber_webhook(request: Request):
    # 1. Captura o JSON bruto
    payload = await request.json()
    
    # --- O "PRINT" QUE VOCÊ PEDIU ---
    # Isso vai mostrar a estrutura completa no seu terminal
    print("\n" + "="*50)
    print("📦 WEBHOOK RECEBIDO - PAYLOAD COMPLETO:")
    print(json.dumps(payload, indent=2)) # indent=2 deixa o JSON bonito e legível
    print("="*50 + "\n")
    # -------------------------------

    try:
        message_data = payload.get('data', {})
        
        # Filtro: Ignora se a mensagem foi enviada pelo próprio bot
        if message_data.get('key', {}).get('fromMe') is True:
            return {"status": "ignorado"}

        # 2. EXTRAÇÃO AUTOMÁTICA (O Detetive)
        # Vamos buscar o JID real em todos os lugares possíveis que a Evolution usa
        candidatos = [
            payload.get('sender'),                       # Raiz do JSON
            message_data.get('sender'),                  # Dentro de 'data'
            message_data.get('key', {}).get('participant'), # Se for grupo ou dispositivo pareado
            message_data.get('key', {}).get('remoteJid')   # Último caso (@lid)
        ]
        
        remote_jid = None
        for jid in candidatos:
            if jid and "@s.whatsapp.net" in jid:
                remote_jid = jid
                logger.info(f"🎯 JID Real encontrado: {remote_jid}")
                break
        
        # Se não achou nenhum @s.whatsapp.net, usa o que tiver disponível
        if not remote_jid:
            remote_jid = message_data.get('key', {}).get('remoteJid')
            logger.warning(f"⚠️ Apenas JID mascarado (@lid) disponível: {remote_jid}")

        # 3. Processamento do Texto
        message_text = message_data.get('message', {}).get('conversation') or \
                       message_data.get('message', {}).get('extendedTextMessage', {}).get('text', '')

        if message_text.lower().startswith("buscar"):
            # Lógica do número manual mantida para redundância
            numero_manual = re.search(r"@(\d{10,15})", message_text)
            
            if numero_manual:
                remote_jid = numero_manual.group(1)
                logger.info(f"📱 Usando número manual: {remote_jid}")
                termo = re.sub(r"@\d+", "", message_text).lower().replace("buscar", "").strip()
            else:
                termo = message_text.lower().replace("buscar", "").strip()

            logger.info(f"🔎 Iniciando Scrapper para: {termo}")

            # 4. Chamada ao Scrapper (porta 8000)
            try:
                resp = requests.post(SCRAPER_URL, json={"termo": termo}, timeout=150)
                dados = resp.json()
                
                resultados = dados.get('resultados', [])
                if not resultados:
                    enviar_mensagem(remote_jid, f"⚠️ Nada encontrado para '{termo}'.")
                    return {"status": "vazio"}

                texto_resp = f"🔍 *RESULTADOS:* {termo.upper()}\n\n"

                melhor_item = dados.get("melhor_oferta")
                if not melhor_item and resultados and resultados[0].get("itens"):
                    melhor_item = resultados[0]["itens"][0]

                if melhor_item:
                    avaliacao = melhor_item.get("avaliacao", "Sem avaliação")
                    estrelas = nota_estrelas_promocao(avaliacao)
                    nome_melhor = melhor_item.get("nome", "Produto")
                    preco_melhor = melhor_item.get("preco", "N/A")
                    loja_melhor = melhor_item.get("loja", "Loja não informada")
                    link_melhor = melhor_item.get("link") or resultados[0].get("url", "")

                    texto_resp += (
                        "🏆 *MELHOR OFERTA AGORA*\n"
                        f"📦 *{nome_melhor}*\n"
                        f"🏪 {loja_melhor}\n"
                        f"💰 {preco_melhor}\n"
                        f"🌡️ {avaliacao}\n"
                        f"⭐ Nota: {estrelas}\n"
                        f"🔗 {link_melhor}\n"
                        f"{'='*18}\n\n"
                    )

                for res in resultados:
                    for item in res.get('itens', []):
                        if item.get('nome') and item.get('preco') and item.get('nome') != "null":
                            avaliacao_item = item.get("avaliacao", "Sem avaliação")
                            estrelas_item = nota_estrelas_promocao(avaliacao_item)
                            link_item = item.get("link") or res.get("url", "")
                            texto_resp += (
                                f"📦 *{item['nome']}*\n"
                                f"💰 {item['preco']}\n"
                                f"🌡️ {avaliacao_item}\n"
                                f"⭐ {estrelas_item}\n"
                                f"🔗 {link_item}\n"
                                f"{'-'*15}\n"
                            )

                enviar_mensagem(remote_jid, texto_resp)
                return {"status": "sucesso"}

            except Exception as e:
                logger.error(f"❌ Erro no Scrapper: {e}")
                return {"status": "erro_scrapper"}

    except Exception as e:
        logger.info(f"❌ Erro Geral: {e}")
        return {"status": "erro"}
    
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)