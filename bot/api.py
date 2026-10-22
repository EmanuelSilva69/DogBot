import asyncio
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from langchain_core.messages import HumanMessage
from bot.agent import create_agent
from bot.config import agent_config, service_config
from bot.context import set_current_user
from bot.memory import consultar_memoria_contato, migrar_banco, registrar_ou_atualizar_contato

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

_agent = None


async def tarefa_configuracao_webhook():
    await asyncio.sleep(2)
    url_set = f"{service_config.evolution_base_url}/webhook/set/{service_config.instance_name}"
    headers = {"apikey": service_config.evolution_api_token, "Content-Type": "application/json"}
    payload = {
        "webhook": {
            "enabled": True,
            "url": f"http://bot-emanuel-langgraph:8001/webhook",
            "webhook_by_events": False,
            "events": ["MESSAGES_UPSERT"],
        }
    }
    for i in range(10):
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url_set, json=payload, headers=headers)
            if response.status_code in (200, 201):
                logger.info(f"Webhook auto-configurado (Status: {response.status_code})")
                return
            logger.warning(f"Tentativa {i+1}: Evolution retornou {response.status_code}")
        except Exception:
            logger.warning(f"Tentativa {i+1}: Evolution API offline...")
        await asyncio.sleep(5)
    logger.error("Nao foi possivel configurar o webhook automaticamente.")


async def get_agent():
    global _agent
    if _agent is None:
        checkpointer = None
        try:
            from bot.memory import get_postgres_saver
            checkpointer = await get_postgres_saver()
        except Exception as e:
            logger.warning(f"Memoria PostgreSQL indisponivel, usando volatil: {e}")
        _agent = create_agent(checkpointer=checkpointer)
    return _agent


def limpar_jid(jid: str) -> str:
    jid = jid.strip().replace(" ", "")
    if "@" in jid:
        return jid
    if jid:
        return f"{jid}@s.whatsapp.net"
    return ""


def extrair_dados_mensagem(payload: dict) -> dict:
    body = payload.get("body", payload)
    data = body.get("data", body)
    message = data.get("message", {})
    msg = (
        message.get("conversation")
        or message.get("extendedTextMessage", {}).get("text", "")
        or ""
    )
    key = data.get("key", {})
    lid = key.get("participant") or key.get("remoteJid", "")
    push_name = data.get("pushName", "Usuário")
    message_id = key.get("id", "")
    remote_jid = key.get("remoteJid", "")
    is_group = "@g.us" in remote_jid

    # participantAlt contém o JID real do remetente (ex: 559882777091@s.whatsapp.net)
    participant_alt = data.get("participantAlt") or body.get("participantAlt") or ""

    # JID do remetente para envio de PV (prioriza participant_alt, fallback lid)
    sender_private_jid = limpar_jid(participant_alt) if participant_alt else ""
    if not sender_private_jid:
        sender_private_jid = limpar_jid(lid)

    target_private_jid = ""
    context_info = (
        message.get("contextInfo")
        or message.get("extendedTextMessage", {}).get("contextInfo", {})
        or data.get("contextInfo", {})
    )
    mentions = context_info.get("mentionedJid", [])
    if mentions:
        bot_lid_value = service_config.bot_lid
        filtered = [j for j in mentions if bot_lid_value not in j]
        if filtered:
            target_private_jid = limpar_jid(filtered[0])

    if not target_private_jid:
        manual_matches = list(re.finditer(r"@(\d{10,15})", msg))
        bot_lid_num = service_config.bot_lid.split("@")[0]
        for m in manual_matches:
            if bot_lid_num not in m.group(1):
                target_private_jid = limpar_jid(m.group(1))
                break

    real_number = (
        target_private_jid.replace("@s.whatsapp.net", "").replace("@lid", "")
        if target_private_jid
        else None
    )

    termo = re.sub(r"@\d+", "", msg)
    termo = re.sub(r"\b(dogbot|pokebot)\b", "", termo, flags=re.IGNORECASE)
    termo = re.sub(r"\b(busca|buscar|busque|pesquisa|pesquisar)\b", "", termo, flags=re.IGNORECASE)
    termo = re.sub(r"para meu amigo|pra meu amigo|mande no privado dele|no privado", "", termo, flags=re.IGNORECASE)
    termo = termo.strip()

    return {
        "lid": lid,
        "remote_jid": remote_jid,
        "message_id": message_id,
        "real_number": real_number,
        "termo": termo,
        "push_name": push_name,
        "is_group": is_group,
        "target_private_jid": target_private_jid,
        "sender_private_jid": sender_private_jid,
        "msg_original": msg,
        "mentioned_jid": mentions,
        "participant_alt": participant_alt,
    }


async def enviar_whatsapp(jid: str, texto: str, quoted_id: Optional[str] = None) -> bool:
    url = f"{service_config.evolution_base_url}/message/sendText/{service_config.instance_name}"
    headers = {
        "apikey": service_config.evolution_api_token,
        "Content-Type": "application/json",
    }
    if "@" not in jid:
        jid = f"{jid}@s.whatsapp.net"

    payload = {"number": jid, "text": texto}
    if quoted_id:
        payload["quoted"] = {"key": {"id": quoted_id}}

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, headers=headers, timeout=30)
            if resp.status_code in (200, 201):
                logger.info(f"Mensagem enviada para {jid}")
                return True
            logger.error(f"Erro Evolution: {resp.status_code} {resp.text}")
            return False
    except Exception as e:
        logger.error(f"Falha ao enviar mensagem: {e}")
        return False


def _fmt_brl(valor) -> str:
    if valor is None:
        return "N/A"
    try:
        return f"R$ {float(valor):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return str(valor)


def formatar_aviso_descontos(lista: list) -> str:
    """Monta o texto do aviso de desconto (texto puro p/ WhatsApp)."""
    linhas = ["🔔 Achei desconto nas suas cartas! ⚡"]
    for ev in lista:
        nome = ev.get("nome") or "Carta"
        preco = _fmt_brl(ev.get("preco_atual"))
        sinais = ev.get("sinais") or []
        motivos = []
        if "queda" in sinais and ev.get("queda_pct") is not None:
            motivos.append(f"caiu {ev['queda_pct']}% desde a última vez")
        if "promocao" in sinais and ev.get("promo_pct") is not None:
            motivos.append(f"promoção de {ev['promo_pct']}%")
        if "tcg_player" in sinais and ev.get("tcg_pct") is not None:
            motivos.append(f"{ev['tcg_pct']}% abaixo do mercado (TCG Player)")
        motivo_txt = "; ".join(motivos) or "desconto encontrado"
        anterior = _fmt_brl(ev.get("preco_anterior"))
        linhas.append(f"\n• {nome}")
        linhas.append(f"  Agora: {preco} ({motivo_txt})")
        if ev.get("preco_anterior"):
            linhas.append(f"  Antes: {anterior}")
        if ev.get("url"):
            linhas.append(f"  {ev['url']}")
    linhas.append("\nMe pede se quiser mais detalhes ou o histórico! ⚡")
    return "\n".join(linhas)


async def checar_e_enviar_descontos():
    """Consulta o scrapper pelos descontos detectados e avisa cada usuário no PV."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            service_config.scrapper_pokemon_verificar_url, timeout=240
        )
        resp.raise_for_status()
        dados = resp.json()

    eventos = dados.get("eventos") or []
    if not eventos:
        logger.info(
            "Descontos: nenhum novo (%s cartas verificadas).",
            dados.get("verificados", 0),
        )
        return

    por_usuario: dict = {}
    for ev in eventos:
        jid = ev.get("user_jid")
        if jid:
            por_usuario.setdefault(jid, []).append(ev)

    enviados = 0
    for jid, lista in por_usuario.items():
        ok = await enviar_whatsapp(jid, formatar_aviso_descontos(lista))
        if ok:
            enviados += 1

    logger.info(
        "Descontos: %d evento(s) em %d usuário(s); %d notificação(ões) enviada(s).",
        len(eventos), len(por_usuario), enviados,
    )


async def tarefa_monitorar_descontos():
    """Roda em background a cada N minutos: verifica preços e avisa de desconto."""
    if not service_config.desconto_ativo:
        logger.info("Monitoramento de descontos DESATIVADO (DESCONTO_ATIVO=0).")
        return

    await asyncio.sleep(30)  # dá tempo dos serviços subirem
    intervalo = max(1, service_config.desconto_intervalo_min) * 60
    logger.info(
        "Monitoramento de descontos ativo: verificando a cada %d min.",
        service_config.desconto_intervalo_min,
    )
    while True:
        try:
            await checar_e_enviar_descontos()
        except Exception as e:
            logger.warning("Falha no ciclo de descontos: %s", e)
        await asyncio.sleep(intervalo)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Iniciando Bot Emanuel - LangGraph Agent")
    asyncio.create_task(tarefa_configuracao_webhook())
    asyncio.create_task(migrar_banco())
    asyncio.create_task(tarefa_monitorar_descontos())
    yield
    logger.info("Desligando Bot Emanuel")


app = FastAPI(title="Bot Emanuel - LangGraph", lifespan=lifespan)


@app.post("/webhook")
async def webhook(request: Request):
    payload = await request.json()

    print("\n" + "=" * 50)
    print("WEBHOOK RECEBIDO:")
    print(json.dumps(payload, indent=2))
    print("=" * 50 + "\n")

    try:
        data_extraida = extrair_dados_mensagem(payload)
    except Exception as e:
        logger.error(f"Erro extraindo dados: {e}")
        return {"status": "erro_extracao"}

    from_me = (
        payload.get("body", payload)
        .get("data", payload)
        .get("key", {})
        .get("fromMe")
    )
    if from_me is True:
        return {"status": "ignorado"}

    if data_extraida["is_group"]:
        bot_jid = f"{service_config.bot_number}@s.whatsapp.net"
        bot_lid = service_config.bot_lid
        mencionado = (
            bot_jid in data_extraida["mentioned_jid"]
            or bot_lid in data_extraida["mentioned_jid"]
        )
        logger.info(
            "Grupo | mencionado_jid=%s | bot_jid=%s | bot_lid=%s | msg=%s",
            data_extraida["mentioned_jid"],
            bot_jid,
            bot_lid,
            data_extraida["msg_original"][:80],
        )
        palavras_chave = re.search(
            r"\b(dogbot|pokebot)\b",
            data_extraida["msg_original"],
            re.IGNORECASE,
        )
        if not mencionado and not palavras_chave:
            return {"status": "ignorado"}

    if not data_extraida["termo"]:
        return {"status": "sem_termo"}

    # Resolve LIDs para JIDs reais via DB
    target_jid = data_extraida["target_private_jid"]
    if target_jid and target_jid.endswith("@lid"):
        try:
            contato = await consultar_memoria_contato(target_jid)
            if contato and contato.get("real_jid"):
                target_jid = contato["real_jid"]
                logger.info("LID %s resolvido para JID %s", data_extraida["target_private_jid"], target_jid)
            else:
                logger.warning("LID %s nao encontrado no DB, ignorando envio privado", target_jid)
                target_jid = ""
        except Exception as e:
            logger.warning("Erro resolvendo LID %s: %s", target_jid, e)
            target_jid = ""
    data_extraida["target_private_jid"] = target_jid

    sender_jid = data_extraida["sender_private_jid"]
    if sender_jid and sender_jid.endswith("@lid"):
        try:
            contato = await consultar_memoria_contato(sender_jid)
            if contato and contato.get("real_jid"):
                sender_jid = contato["real_jid"]
                logger.info("Sender LID %s resolvido para JID %s", data_extraida["sender_private_jid"], sender_jid)
            else:
                sender_jid = ""
        except Exception as e:
            logger.warning("Erro resolvendo sender LID %s: %s", sender_jid, e)
            sender_jid = ""
    data_extraida["sender_private_jid"] = sender_jid

    lid = data_extraida["lid"]

    try:
        real_jid = data_extraida.get("participant_alt", "") or None
        await registrar_ou_atualizar_contato(
            lid=lid,
            numero_real=real_jid.replace("@s.whatsapp.net", "") if real_jid else None,
            nome_usuario=data_extraida["push_name"],
            real_jid=real_jid,
        )
    except Exception as e:
        logger.warning(f"Falha ao registrar contato: {e}")

    agent = await get_agent()

    # Contexto do usuário atual: as tools usam para auto-monitorar cartas e
    # gerenciar a watchlist de quem falou com o bot.
    set_current_user(data_extraida.get("sender_private_jid") or "")

    config = {"configurable": {"thread_id": lid}}

    initial_state = {
        "messages": [HumanMessage(content=data_extraida["msg_original"])],
        "lid": lid,
        "remote_jid": data_extraida["remote_jid"],
        "message_id": data_extraida["message_id"],
        "push_name": data_extraida["push_name"],
        "is_group": data_extraida["is_group"],
        "real_number": data_extraida["real_number"],
        "target_private_jid": data_extraida["target_private_jid"],
        "msg_original": data_extraida["msg_original"],
        "termo": data_extraida["termo"],
        "intent": "",
        "search_result": None,
        "coupon_result": None,
        "formatted_message": "",
    }

    try:
        result = await agent.ainvoke(initial_state, config)
        formatted_msg = result.get("formatted_message", "")

        if not formatted_msg:
            for m in reversed(result.get("messages", [])):
                if hasattr(m, "content") and m.content:
                    formatted_msg = m.content
                    break

        if not formatted_msg:
            logger.warning("Nenhuma mensagem formatada encontrada")
            return {"status": "sem_resposta"}

        logger.info(
            "RESPOSTA GERADA (intent=%s):\n%s",
            result.get("intent"),
            formatted_msg,
        )

        bot_foi_mencionado = (
            f"{service_config.bot_number}@s.whatsapp.net" in data_extraida["mentioned_jid"]
            or service_config.bot_lid in data_extraida["mentioned_jid"]
        )

        if data_extraida["is_group"] and bot_foi_mencionado:
            # Bot foi @mencionado → responde no PV de quem mandou
            pv_jid = data_extraida["sender_private_jid"]
            if pv_jid and "@s.whatsapp.net" in pv_jid:
                sucesso = await enviar_whatsapp(pv_jid, formatted_msg, quoted_id=data_extraida["message_id"])
                if sucesso:
                    aviso = (
                        f"✅ *Resultado enviado no seu privado!* "
                        f"@{data_extraida['push_name']}, dá uma olhada lá. ⚡"
                    )
                    await enviar_whatsapp(data_extraida["remote_jid"], aviso, quoted_id=data_extraida["message_id"])
                else:
                    await enviar_whatsapp(
                        data_extraida["remote_jid"],
                        f"@{data_extraida['push_name']} não consegui te enviar no privado, "
                        f"verifica se você me bloqueou ou se sua privacidade impede "
                        f"mensagens de desconhecidos. ⚡",
                        quoted_id=data_extraida["message_id"],
                    )
            else:
                await enviar_whatsapp(data_extraida["remote_jid"], formatted_msg, quoted_id=data_extraida["message_id"])

        elif data_extraida["is_group"] and data_extraida["target_private_jid"]:
            # Alguém pediu "busca X para @fulano" → envia PV pro @fulano
            msg_privado = (
                f"⚡ *Oi! O {data_extraida['push_name']} te mandou um recado pelo grupo* "
                f"e pediu essa resposta:\n\n"
                f"{formatted_msg}"
            )
            sucesso = await enviar_whatsapp(
                data_extraida["target_private_jid"],
                msg_privado,
                quoted_id=data_extraida["message_id"],
            )
            if sucesso:
                aviso = (
                    f"✅ *Busca concluída!* @{data_extraida['real_number']}, mandei os "
                    f"resultados lá no seu privado como o {data_extraida['push_name']} pediu! ⚡"
                )
                await enviar_whatsapp(data_extraida["remote_jid"], aviso, quoted_id=data_extraida["message_id"])
            else:
                logger.warning("Falha ao enviar privado para %s, respondendo no grupo", data_extraida["target_private_jid"])
                await enviar_whatsapp(data_extraida["remote_jid"], formatted_msg, quoted_id=data_extraida["message_id"])

        elif data_extraida["is_group"]:
            # Keyword "pokebot" sem @mention → responde no grupo
            await enviar_whatsapp(data_extraida["remote_jid"], formatted_msg, quoted_id=data_extraida["message_id"])

        else:
            # Mensagem direta (PV) → responde normal
            await enviar_whatsapp(data_extraida["remote_jid"], formatted_msg, quoted_id=data_extraida["message_id"])

        return {"status": "sucesso"}

    except Exception as e:
        logger.error(f"Erro no fluxo do agente: {e}", exc_info=True)
        await enviar_whatsapp(
            data_extraida["remote_jid"],
            "Ops, meu sistema deu uma travada aqui. Tenta de novo mais tarde. ⚡",
            quoted_id=data_extraida["message_id"],
        )
        return {"status": "erro_agente"}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "bot-emanuel-langgraph"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
