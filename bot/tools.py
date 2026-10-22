import asyncio
from typing import Optional, Type

import httpx
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from bot.config import service_config
from bot.context import get_current_user
from bot.trivia import sortear_trivia


class SearchTechInput(BaseModel):
    termo: str = Field(description="Termo de busca para hardware/eletrônicos")


class SearchTechTool(BaseTool):
    name: str = "search_tech"
    description: str = (
        "Busca por produtos de hardware, eletrônicos, computadores, jogos e tecnologia em geral. "
        "Use esta ferramenta quando o usuário perguntar sobre placas de vídeo, processadores, "
        "monitores, periféricos, consoles, ou qualquer item de tecnologia. "
        "Marketplaces: Mercado Livre, Kabum, Pichau, Terabyte."
    )
    args_schema: Type[BaseModel] = SearchTechInput

    def _run(self, termo: str) -> dict:
        raise NotImplementedError("Use async version")

    async def _arun(self, termo: str) -> dict:
        url = service_config.scrapper_tech_url
        timeout = service_config.scrapper_timeout
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    url,
                    json={"termo": termo},
                    timeout=timeout,
                )
                resp.raise_for_status()
                data = resp.json()
                if not data.get("resultados"):
                    return {"mensagem": f"Não encontrei ofertas para *{termo.upper()}* no momento.", "resultados": []}
                return data
        except httpx.TimeoutException:
            return {"erro": f"O scrapper de tecnologia demorou muito para responder (timeout de {timeout}s). Tente novamente com um termo mais específico.", "resultados": []}
        except httpx.HTTPStatusError as e:
            return {"erro": f"Erro no servidor do scrapper: {e.response.status_code}", "resultados": []}
        except Exception as e:
            return {"erro": f"Erro inesperado ao buscar tecnologia: {str(e)}", "resultados": []}


class SearchGeneralInput(BaseModel):
    termo: str = Field(description="Termo de busca geral")


class SearchGeneralTool(BaseTool):
    name: str = "search_general"
    description: str = (
        "Busca produtos e itens em marketplaces gerais como Mercado Livre, Amazon, "
        "Shein, Shopee e outros. "
        "Use para qualquer busca que não seja especificamente de hardware/eletrônicos, "
        "como roupas, livros, móveis, alimentos, cosméticos, brinquedos, etc."
    )
    args_schema: Type[BaseModel] = SearchGeneralInput

    def _run(self, termo: str) -> dict:
        raise NotImplementedError("Use async version")

    async def _arun(self, termo: str) -> dict:
        url = service_config.scrapper_geral_url
        timeout = service_config.scrapper_timeout
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    url,
                    json={"termo": termo},
                    timeout=timeout,
                )
                resp.raise_for_status()
                data = resp.json()
                if not data.get("resultados"):
                    return {"mensagem": f"Não encontrei ofertas para *{termo.upper()}* no momento.", "resultados": []}
                return data
        except httpx.TimeoutException:
            return {"erro": f"O scrapper geral demorou muito para responder (timeout de {timeout}s). Tente novamente.", "resultados": []}
        except httpx.HTTPStatusError as e:
            return {"erro": f"Erro no servidor do scrapper geral: {e.response.status_code}", "resultados": []}
        except Exception as e:
            return {"erro": f"Erro inesperado ao buscar geral: {str(e)}", "resultados": []}


class CheckCouponsInput(BaseModel):
    termo_busca: str = Field(description="Termo da busca para encontrar cupons relacionados")
    url_produto: Optional[str] = Field(default=None, description="URL do produto para testar cupons no carrinho")
    valor_carrinho: float = Field(default=100.0, description="Valor do carrinho para calcular descontos")


class CheckCouponsTool(BaseTool):
    name: str = "check_coupons"
    description: str = (
        "Testa cupons de desconto em marketplaces. Use quando o usuário perguntar sobre cupons, "
        "descontos, ou códigos promocionais. Pode testar cupons para Mercado Livre, Amazon, Kabum, "
        "Terabyte, Pichau, Shopee, Extra e outros marketplaces."
    )
    args_schema: Type[BaseModel] = CheckCouponsInput

    def _run(self, termo_busca: str, url_produto: Optional[str] = None, valor_carrinho: float = 100.0) -> dict:
        raise NotImplementedError("Use async version")

    async def _arun(self, termo_busca: str, url_produto: Optional[str] = None, valor_carrinho: float = 100.0) -> dict:
        url = service_config.cupons_url
        timeout = service_config.cupons_timeout
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    url,
                    json={
                        "termo_busca": termo_busca,
                        "url_produto": url_produto or "",
                        "valor_carrinho": valor_carrinho,
                    },
                    timeout=timeout,
                )
                resp.raise_for_status()
                return resp.json()
        except httpx.TimeoutException:
            return {"erro": f"O serviço de cupons demorou muito (timeout de {timeout}s).", "melhor_cupom": None}
        except httpx.HTTPStatusError as e:
            return {"erro": f"Erro no servidor de cupons: {e.response.status_code}", "melhor_cupom": None}
        except Exception as e:
            return {"erro": f"Erro inesperado ao buscar cupons: {str(e)}", "melhor_cupom": None}


class SearchPokemonInput(BaseModel):
    termo: str = Field(description="Nome da carta ou termo de busca (ex.: 'charizard', 'pikachu ex')")
    preco_minimo: Optional[float] = Field(default=None, description="Preço mínimo em R$")
    preco_maximo: Optional[float] = Field(default=None, description="Preço máximo em R$")
    pagina: int = Field(default=1, ge=1, le=50, description="Página dos resultados (1 = primeira)")
    tipo: str = Field(
        default="carta",
        description=(
            "'carta' para carta avulsa (padrão). 'selado' para produto lacrado: "
            "caixa/box, mini box, booster, ETB (caixa de treinador), lata/tin ou deck."
        ),
    )


def _resposta_compacta(dados: dict) -> dict:
    """Reduz o JSON do scrapper aos campos úteis pro LLM.

    O retorno cru tem ~22 campos por carta (vários null/irrelevantes). Cortar isso
    encolhe o prompt do LLM2 e acelera bastante o processamento.
    """
    if not isinstance(dados, dict):
        return dados

    compacto = {"fonte": dados.get("fonte"), "tipo": dados.get("tipo"),
                "termo": dados.get("termo"), "total_encontrados": dados.get("total_encontrados")}

    resultados = []
    for r in dados.get("resultados") or []:
        resultados.append({
            "carta": r.get("titulo_completo") or r.get("nome"),
            "edicao": r.get("edicao_sigla") or r.get("edicao"),
            "preco": r.get("preco"),
            "link": r.get("url"),
        })
    compacto["resultados"] = resultados

    if dados.get("erro"):
        compacto["erro"] = dados["erro"]
    return compacto


class SearchPokemonTool(BaseTool):
    name: str = "search_pokemon"
    description: str = (
        "Busca cartas Pokémon à venda no MYP Cards com preço, edição, número, condição e link. "
        "Use para 'quanto custa charizard', 'pikachu ex barata', 'cartas até R$ 50'. "
        "Também busca produtos SELADOS (caixa/box, mini box, booster, ETB/caixa de treinador, "
        "lata/tin, deck): use tipo='selado' quando pedirem 'caixa do charizard', "
        "'quanto custa um booster box', 'caixa de treinador', 'elite trainer box', 'lata'. "
        "Passe no 'termo' só o nome do produto/pokémon (ex.: 'charizard'), sem 'caixa'/'box'."
    )
    args_schema: Type[BaseModel] = SearchPokemonInput

    def _run(self, termo: str, preco_minimo: Optional[float] = None,
             preco_maximo: Optional[float] = None, pagina: int = 1,
             tipo: str = "carta") -> dict:
        raise NotImplementedError("Use async version")

    async def _arun(self, termo: str, preco_minimo: Optional[float] = None,
                    preco_maximo: Optional[float] = None, pagina: int = 1,
                    tipo: str = "carta") -> dict:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    service_config.scrapper_pokemon_buscar_url,
                    json={
                        "termo": termo,
                        "preco_minimo": preco_minimo,
                        "preco_maximo": preco_maximo,
                        "pagina": pagina,
                        "tipo": tipo or "carta",
                        # Busca na seção geral de selados (sem subcategoria estreita).
                        "subcategoria": None,
                        # Se houver usuário no contexto, o scrapper monitora o
                        # melhor resultado e avisa quando baixar.
                        "user_jid": get_current_user() or None,
                    },
                    timeout=service_config.pokemon_timeout,
                )
                resp.raise_for_status()
                return _resposta_compacta(resp.json())
        except httpx.TimeoutException:
            return {"erro": f"O consulta de cartas demorou demais (timeout de {service_config.pokemon_timeout}s). Tente com um termo mais simples.", "resultados": []}
        except httpx.HTTPStatusError as e:
            return {"erro": f"Erro no servidor de cartas: {e.response.status_code}", "resultados": []}
        except Exception as e:
            return {"erro": f"Erro inesperado ao buscar cartas: {e}", "resultados": []}


class PokemonCardDetailsInput(BaseModel):
    carta: str = Field(
        description="Nome da carta (ex.: 'Mega Charizard X ex') ou URL completa da página do produto"
    )
    limite_ofertas: int = Field(default=8, ge=1, le=20, description="Quantidade de ofertas (vendedores) a listar")


def _resposta_compacta_detalhes(dados: dict) -> dict:
    """Reduz o JSON de detalhes aos campos úteis pro LLM (corta imagem, resumo, campos nulos)."""
    if not isinstance(dados, dict) or dados.get("erro"):
        return dados

    compacto = {
        "carta": dados.get("nome"),
        "edicao": dados.get("edicao"),
        "preco_menor": dados.get("preco_menor"),
        "preco_medio": dados.get("preco_medio"),
        "preco_tcg_player": dados.get("preco_tcg_player"),
        "tendencia_30d": (dados.get("tendencia") or {}).get("texto"),
        "qtd_ofertas": dados.get("qtd_ofertas"),
        "detalhes": dados.get("detalhes"),
    }

    ofertas = []
    for o in (dados.get("ofertas") or [])[:6]:
        ofertas.append({
            "vendedor": o.get("vendedor"),
            "preco": o.get("preco"),
            "condicao": o.get("condicao"),
            "idioma": o.get("idioma"),
            "qtd": o.get("quantidade"),
            "link_mensagem": o.get("mensagem_vendedor"),
        })
    compacto["ofertas"] = ofertas

    if dados.get("descricao"):
        compacto["descricao"] = str(dados["descricao"])[:200]

    alts = [{"carta": a.get("nome"), "preco": a.get("preco"), "link": a.get("url")}
            for a in (dados.get("alternativas") or [])[:3]]
    if alts:
        compacto["alternativas"] = alts

    return compacto


class PokemonCardDetailsTool(BaseTool):
    name: str = "pokemon_card_details"
    description: str = (
        "Analisa uma carta Pokémon específica no MYP Cards e devolve: menor preço, preço médio, "
        "preço de referência TCG Player, tendência de 30 dias, quantidade de ofertas, os vendedores "
        "mais baratos (condição, idioma, versão e quantidade) e detalhes da carta (HP, tipo, "
        "raridade, data de lançamento e descrição dos ataques). "
        "Use quando o usuário pedir detalhes, comparar vendedores ou perguntar sobre uma carta exata."
    )
    args_schema: Type[BaseModel] = PokemonCardDetailsInput

    def _run(self, carta: str, limite_ofertas: int = 8) -> dict:
        raise NotImplementedError("Use async version")

    async def _arun(self, carta: str, limite_ofertas: int = 8) -> dict:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    service_config.scrapper_pokemon_detalhar_url,
                    json={
                        "carta": carta,
                        "limite_ofertas": limite_ofertas,
                        # Monitora a carta para o usuário avisar quando baixar.
                        "user_jid": get_current_user() or None,
                    },
                    timeout=service_config.pokemon_timeout,
                )
                resp.raise_for_status()
                return _resposta_compacta_detalhes(resp.json())
        except httpx.TimeoutException:
            return {"erro": f"A análise da carta demorou demais (timeout de {service_config.pokemon_timeout}s). Tente novamente."}
        except httpx.HTTPStatusError as e:
            return {"erro": f"Erro no servidor de cartas: {e.response.status_code}"}
        except Exception as e:
            return {"erro": f"Erro inesperado ao analisar a carta: {e}"}


class PokemonPriceHistoryInput(BaseModel):
    carta: str = Field(description="Nome da carta ou URL da página do produto")
    dias: int = Field(default=90, ge=7, le=730, description="Janela do histórico em dias")


class PokemonPriceHistoryTool(BaseTool):
    name: str = "pokemon_price_history"
    description: str = (
        "Histórico de preço de uma carta Pokémon do MYP Cards. Registra uma coleta do preço atual "
        "e devolve a série histórica acumulada (menor/maior/média, variação e evolução), além da "
        "tendência de 30 dias exibida pelo próprio site. "
        "Use quando o usuário perguntar 'histórico de preço', 'subiu ou caiu', 'vale a pena esperar'."
    )
    args_schema: Type[BaseModel] = PokemonPriceHistoryInput

    def _run(self, carta: str, dias: int = 90) -> dict:
        raise NotImplementedError("Use async version")

    async def _arun(self, carta: str, dias: int = 90) -> dict:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    service_config.scrapper_pokemon_historico_url,
                    json={"carta": carta, "dias": dias},
                    timeout=service_config.pokemon_timeout,
                )
                resp.raise_for_status()
                return resp.json()
        except httpx.TimeoutException:
            return {"erro": f"O histórico de preço demorou demais (timeout de {service_config.pokemon_timeout}s). Tente novamente."}
        except httpx.HTTPStatusError as e:
            return {"erro": f"Erro no servidor de cartas: {e.response.status_code}"}
        except Exception as e:
            return {"erro": f"Erro inesperado ao consultar o histórico: {e}"}


class PokemonTriviaInput(BaseModel):
    categoria: Optional[str] = Field(
        default=None,
        description="Categoria opcional: tipos, evolucoes, jogos, curiosidades, animacao ou cartas",
    )
    modo: str = Field(
        default="auto",
        description="'pergunta', 'curiosidade', 'adivinhar' (desafio via PokéAPI) ou 'auto'",
    )


class PokemonTriviaTool(BaseTool):
    name: str = "pokemon_trivia"
    description: str = (
        "Gera uma rodada de trivia/quiz ou curiosidade de Pokémon (perguntas em português, "
        "desafios de adivinhação com pistas via PokéAPI e fatos rápidos). "
        "Use quando pedirem trivia, quiz, curiosidade, 'me desafia com um pokemon' ou 'me conta algo sobre pokemon'. "
        "Retorna a pergunta e a resposta certa: apresente só a pergunta e revele a resposta depois que o usuário responder."
    )
    args_schema: Type[BaseModel] = PokemonTriviaInput

    def _run(self, categoria: Optional[str] = None, modo: str = "auto") -> dict:
        raise NotImplementedError("Use async version")

    async def _arun(self, categoria: Optional[str] = None, modo: str = "auto") -> dict:
        try:
            return await sortear_trivia(categoria=categoria, modo=modo)
        except Exception as e:
            return {"erro": f"Erro ao montar a trivia: {e}"}


class ManageWatchlistInput(BaseModel):
    acao: str = Field(
        description=(
            "O que fazer: 'adicionar' (monitorar carta para avisar de desconto), "
            "'remover' (parar de monitorar) ou 'listar' (mostrar cartas monitoradas)."
        )
    )
    carta: Optional[str] = Field(
        default=None, description="Nome da carta (necessário para adicionar ou remover)"
    )


class ManageWatchlistTool(BaseTool):
    name: str = "manage_watchlist"
    description: str = (
        "Gerencia a lista de cartas que o usuário quer monitorar para receber aviso de desconto. "
        "Use quando disserem 'avise quando o charizard baixar', 'me avisa se o preço cair', "
        "'para de avisar fulano', 'remova X do monitoramento' ou 'quais cartas você monitora pra mim'."
    )
    args_schema: Type[BaseModel] = ManageWatchlistInput

    def _run(self, acao: str, carta: Optional[str] = None) -> dict:
        raise NotImplementedError("Use async version")

    async def _arun(self, acao: str, carta: Optional[str] = None) -> dict:
        user_jid = get_current_user()
        if not user_jid:
            return {"erro": "Não consegui identificar seu número para monitorar cartas."}

        acao_n = (acao or "").strip().lower()
        try:
            async with httpx.AsyncClient() as client:
                if acao_n in ("listar", "list", "mostrar", "ver", "quais"):
                    resp = await client.post(
                        service_config.scrapper_pokemon_watchlist_list_url,
                        json={"user_jid": user_jid},
                        timeout=30,
                    )
                elif acao_n in ("remover", "parar", "cancelar", "desligar", "tirar"):
                    if not carta:
                        return {"erro": "Diga qual carta eu paro de monitorar (ex.: 'para de avisar charizard')."}
                    resp = await client.post(
                        service_config.scrapper_pokemon_watchlist_remove_url,
                        json={"user_jid": user_jid, "termo": carta},
                        timeout=30,
                    )
                else:  # adicionar (padrão)
                    if not carta:
                        return {"erro": "Diga qual carta monitorar (ex.: 'avise quando o charizard baixar')."}
                    resp = await client.post(
                        service_config.scrapper_pokemon_watchlist_add_url,
                        json={"user_jid": user_jid, "carta": carta},
                        timeout=service_config.pokemon_timeout,
                    )
                resp.raise_for_status()
                return resp.json()
        except httpx.TimeoutException:
            return {"erro": "O monitoramento demorou demais. Tente de novo."}
        except httpx.HTTPStatusError as e:
            return {"erro": f"Erro no servidor de monitoramento: {e.response.status_code}"}
        except Exception as e:
            return {"erro": f"Erro inesperado no monitoramento: {e}"}


tools: list[BaseTool] = [
    SearchTechTool(),
    SearchGeneralTool(),
    CheckCouponsTool(),
    SearchPokemonTool(),
    PokemonCardDetailsTool(),
    PokemonPriceHistoryTool(),
    PokemonTriviaTool(),
    ManageWatchlistTool(),
]
