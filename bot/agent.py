import asyncio
import json
import logging
import os
import re
from typing import Literal, Optional

from langchain_core.messages import AIMessage, ToolMessage
from langchain_ollama import ChatOllama
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from bot.config import agent_config
from bot.state import BotState
from bot.tools import tools

logger = logging.getLogger(__name__)

_AGENT_MODEL = None
_MAX_RETRIES = int(os.getenv("OLLAMA_RETRIES", "5"))
_RETRY_DELAY = float(os.getenv("OLLAMA_RETRY_DELAY", "3.0"))


def _create_chat_ollama(model: str, temperature: float, num_predict: int) -> ChatOllama:
    return ChatOllama(
        model=model,
        base_url=agent_config.ollama_base_url,
        temperature=temperature,
        num_gpu=agent_config.num_gpu,
        num_predict=num_predict,
    )


def get_agent_model() -> ChatOllama:
    global _AGENT_MODEL
    if _AGENT_MODEL is None:
        _AGENT_MODEL = _create_chat_ollama(
            model=agent_config.model,
            temperature=agent_config.temperature,
            num_predict=4096,
        )
    return _AGENT_MODEL


_AGENT_FINAL_MODEL = None


def get_agent_final_model() -> ChatOllama:
    """Modelo SEM tools bindadas — usado pra gerar a resposta final após a tool rodar.

    O schema das 7 tools (~2k chars) infla o prompt e dobrou o tempo do LLM2 em
    testes (8.8s -> 21.9s). Como a tool já executou, não há por que pagar esse custo.
    """
    global _AGENT_FINAL_MODEL
    if _AGENT_FINAL_MODEL is None:
        _AGENT_FINAL_MODEL = _create_chat_ollama(
            model=agent_config.model,
            temperature=agent_config.temperature,
            num_predict=1024,
        )
    return _AGENT_FINAL_MODEL


def _sanitizar_whatsapp(texto: str) -> str:
    """Remove artefatos que o WhatsApp não renderiza (markdown, JSON colado)."""
    if not texto:
        return ""
    texto = str(texto)
    # Link markdown [texto](url) -> "texto: url"
    texto = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1: \2", texto)
    # Negrito/itálico/strike/code -> texto puro
    texto = re.sub(r"\*{1,3}([^*\n]+)\*{1,3}", r"\1", texto)
    texto = re.sub(r"_{1,3}([^_\n]+)_{1,3}", r"\1", texto)
    texto = re.sub(r"`{1,3}([^`\n]+)`{1,3}", r"\1", texto)
    # Cabeçalhos
    texto = re.sub(r"^#{1,6}\s*", "", texto, flags=re.MULTILINE)
    # Linhas tipo (resposta: "...") / (explicacao: "...") vazadas do JSON
    texto = re.sub(
        r"^\s*[\(\[]\s*(resposta|explicacao|dica|instrucao|modo|fonte|pistas)\s*:\s*.*[\)\]]\s*$",
        "",
        texto,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    return re.sub(r"\n{3,}", "\n\n", texto).strip()


async def _invoke_with_retry(llm, messages, retries=_MAX_RETRIES, delay=_RETRY_DELAY):
    last_error = None
    for attempt in range(retries):
        try:
            return await llm.ainvoke(messages)
        except Exception as e:
            last_error = e
            err_str = str(e).lower()
            if "connection" in err_str or "refused" in err_str or "timeout" in err_str or "eof" in err_str:
                logger.warning(f"Ollama indisponivel (tentativa {attempt+1}/{retries}): {e}")
                if attempt < retries - 1:
                    await asyncio.sleep(delay * (attempt + 1))
                    continue
            raise
    raise last_error


def create_agent(checkpointer: BaseCheckpointSaver | None = None):
    agent_llm = get_agent_model().bind_tools(tools)
    # Sem bind_tools: usado pra gerar a resposta final após a tool já ter rodado.
    final_llm = get_agent_final_model()

    tool_node = ToolNode(tools)

    graph = StateGraph(BotState)

    def classify_intent(state: BotState) -> dict:
        msg = state.get("msg_original", "").lower()

        # Trivia/quiz/curiosidade -> conversa (mantém a personalidade)
        if re.search(
            r"\b(trivia|curiosidade[s]?|quiz|advinha\w*|adivinha\w*|me desafia?\w*|"
            r"pergunta[s]? (de|sobre) pok[eé]mon|sabe(?:r)? uma)\b",
            msg,
        ):
            intent = "chat"
        # Cartas Pokémon (MYP Cards): preços, detalhes, histórico
        elif re.search(
            r"\b(pok[eé]mon|pokemon|pikachu|charizard|blastoise|venusaur|mewtwo|snorlax|"
            r"gengar|cartas?|myp\s*cards|tcg|booster|foil|graded|psa|"
            # produtos selados (caixa/box/ETB/mini box/lata/deck)
            r"caixa[s]?|box|boxes|etb|elite\s*trainer|trainer\s*box|treinador|"
            r"lata[s]?|mega\s*lata|tin[s]?|deck[s]?|baralho|selados?|seladas?|mini\s*box)\b",
            msg,
        ):
            intent = "pokemon"
        elif re.search(
            r"\b(buscar?|pesquisar?|procure|encontre|quanto custa|"
            r"preço\b|precos|preço de|valor do?|mostre|"
            r"quero comprar|me mostre|acha|achei|"
            r"qual o melhor|recomende|promoção|oferta|desconto)\b",
            msg,
        ):
            intent = "busca"
        else:
            intent = "chat"
        logger.info("Intencao classificada: %s", intent)
        return {"intent": intent}

    async def call_model(state: BotState) -> dict:
        intent = state.get("intent")
        if intent == "busca":
            system_content = agent_config.search_system_message
        elif intent == "pokemon":
            system_content = agent_config.pokemon_system_message
        else:
            system_content = agent_config.system_message
        system_msg = {"role": "system", "content": system_content}
        messages = [system_msg] + state.get("messages", [])

        # Se a tool já rodou, gera o texto final SEM tools bindadas — o schema das
        # 7 tools infla o prompt e mais que dobrou o tempo em testes.
        ja_rodou_tool = any(isinstance(m, ToolMessage) for m in state.get("messages", []))
        llm = final_llm if ja_rodou_tool else agent_llm

        response = await _invoke_with_retry(llm, messages)
        return {"messages": [response]}

    def should_continue(state: BotState) -> Literal["tools", "format_response"]:
        result = tools_condition(state)
        if result == "tools":
            return "tools"
        return "format_response"

    async def format_response(state: BotState) -> dict:
        # A resposta do agente (call 2) já sai no estilo certo — o system message do
        # intent (pokemon/busca/chat) define tom e formato. Rodar um LLM de formatação
        # extra custava ~6s e frequentemente descartava conteúdo (provaram os fallbacks).
        # Então usamos a resposta do agente direto + limpeza regex instantânea.
        texto = ""
        for msg in reversed(state.get("messages", [])):
            if isinstance(msg, AIMessage) and getattr(msg, "content", None):
                texto = str(msg.content)
                break

        return {"formatted_message": _sanitizar_whatsapp(texto)}

    graph.add_node("classify_intent", classify_intent)
    graph.add_node("agent", call_model)
    graph.add_node("tools", tool_node)
    graph.add_node("format_response", format_response)

    graph.set_entry_point("classify_intent")

    graph.add_edge("classify_intent", "agent")
    graph.add_conditional_edges("agent", should_continue)
    graph.add_edge("tools", "agent")
    graph.add_edge("format_response", END)

    return graph.compile(checkpointer=checkpointer)
