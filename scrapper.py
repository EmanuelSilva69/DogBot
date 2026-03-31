from fastapi import FastAPI
from pydantic import BaseModel, Field
import json
import uvicorn
from typing import Optional, List 
import os # Adicione o import no topo
import re
import sqlite3
from datetime import datetime
import requests
import sys
import unicodedata
import importlib
from bs4 import BeautifulSoup
from difflib import SequenceMatcher
from urllib.parse import urljoin
from urllib.parse import quote_plus

# Se estiver no Docker, usará 'searxng:8080'. Se estiver fora, usa 'localhost:9092'

app = FastAPI()

if os.getenv("PRICE_DB_PATH"):
    DB_PATH = os.getenv("PRICE_DB_PATH")
elif os.path.exists("/.dockerenv"):
    DB_PATH = "/app/data/prices_history.db"
else:
    DB_PATH = "prices_history.db"

PROMOTECH_BASE_URL = os.getenv("PROMOTECH_BASE_URL", "https://promotech.app.br")
SEARXNG_URL_DOCKER = os.getenv("SEARXNG_URL", "http://searxng:8080/search")
SEARXNG_URL_LOCAL = os.getenv("SEARXNG_URL_LOCAL", "http://localhost:9092/search")
OLLAMA_BASE_URL_DOCKER = os.getenv("OLLAMA_BASE_URL_DOCKER", "http://host.docker.internal:11434")
OLLAMA_BASE_URL_LOCAL = os.getenv("OLLAMA_BASE_URL_LOCAL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "ollama/qwen2.5-coder:7b")
PROMOTECH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9",
}
PROMOTECH_CATEGORIAS = ["gpu", "cpu", "ram", "monitor", "console", "iphone"]


def _coletar_urls_produto(obj, acumulador):
    """Varre JSON recursivamente para coletar links de produto do Promotech."""
    if isinstance(obj, dict):
        for valor in obj.values():
            _coletar_urls_produto(valor, acumulador)
    elif isinstance(obj, list):
        for item in obj:
            _coletar_urls_produto(item, acumulador)
    elif isinstance(obj, str):
        if obj.startswith("/products/simple/") and obj.count("/") >= 4:
            acumulador.add(obj)


def _extrair_next_data_json(html):
    """Extrai o payload __NEXT_DATA__ quando disponível."""
    match = re.search(
        r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if not match:
        return None

    try:
        return json.loads(match.group(1))
    except Exception:
        return None


def buscar_links_promotech_via_hidratacao(url):
    """Tenta extrair links de produto do JSON de hidratação do Next.js."""
    try:
        html = baixar_html_promotech(url)
        next_data = _extrair_next_data_json(html)
        if not next_data:
            return []

        encontrados = set()
        _coletar_urls_produto(next_data, encontrados)
        return [urljoin(PROMOTECH_BASE_URL, caminho) for caminho in encontrados]
    except Exception:
        return []


def buscar_links_promotech_via_api(termo):
    """
    Tenta endpoints comuns de API interna do Promotech.
    Se algum endpoint existir, reduz dependência de parsing HTML.
    """
    termo_q = quote_plus(termo)
    candidatos = [
        f"{PROMOTECH_BASE_URL}/api/search?q={termo_q}",
        f"{PROMOTECH_BASE_URL}/api/search?query={termo_q}",
        f"{PROMOTECH_BASE_URL}/api/products/search?q={termo_q}",
        f"{PROMOTECH_BASE_URL}/api/v1/search?q={termo_q}",
    ]

    links = set()
    for endpoint in candidatos:
        try:
            resp = requests.get(endpoint, headers=PROMOTECH_HEADERS, timeout=10)
            if resp.status_code != 200:
                continue
            payload = resp.json()
            caminhos = set()
            _coletar_urls_produto(payload, caminhos)
            for caminho in caminhos:
                links.add(urljoin(PROMOTECH_BASE_URL, caminho))
        except Exception:
            continue

    return list(links)


def conectar_banco():
    """
    Conecta (ou cria) o banco SQLite local e garante a estrutura mínima
    para armazenar histórico de preços.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS prices_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            id_produto TEXT NOT NULL,
            nome TEXT NOT NULL,
            preco REAL NOT NULL,
            data_coleta TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_prices_history_produto_data
        ON prices_history (id_produto, data_coleta)
        """
    )
    conn.commit()
    return conn


def normalizar_preco(valor):
    """
    Converte strings monetárias no formato BR para float.
    Exemplos aceitos: "R$ 1.500,00", "1500", "1,500.00".
    """
    if isinstance(valor, (int, float)):
        return float(valor)

    if not valor:
        return None

    texto = str(valor)
    match = re.search(r"\d[\d\.,]*", texto)
    if not match:
        return None

    bruto = match.group(0)

    # Trata separadores em formatos comuns (pt-BR e en-US)
    if "," in bruto and "." in bruto:
        # Se a última vírgula vem depois do último ponto, assume pt-BR: 1.234,56
        if bruto.rfind(",") > bruto.rfind("."):
            bruto = bruto.replace(".", "").replace(",", ".")
        else:
            # Formato en-US: 1,234.56
            bruto = bruto.replace(",", "")
    elif "," in bruto:
        bruto = bruto.replace(".", "").replace(",", ".")
    elif bruto.count(".") > 1:
        # Ex: 1.234.567 -> remove separadores de milhar
        bruto = bruto.replace(".", "")

    try:
        preco = float(bruto)
        return preco if preco > 0 else None
    except ValueError:
        return None


def registrar_preco(conn, id_produto, nome, preco):
    """Insere uma nova coleta de preço no histórico."""
    conn.execute(
        """
        INSERT INTO prices_history (id_produto, nome, preco, data_coleta)
        VALUES (?, ?, ?, ?)
        """,
        (id_produto, nome, float(preco), datetime.utcnow().isoformat()),
    )
    conn.commit()


def analisar_historico_preco(conn, id_produto):
    """
    Retorna métricas para o produto:
    - menor_30d
    - menor_90d
    - media_30d
    """
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT
            MIN(CASE WHEN datetime(data_coleta) >= datetime('now', '-30 day') THEN preco END) AS menor_30d,
            MIN(CASE WHEN datetime(data_coleta) >= datetime('now', '-90 day') THEN preco END) AS menor_90d,
            AVG(CASE WHEN datetime(data_coleta) >= datetime('now', '-30 day') THEN preco END) AS media_30d
        FROM prices_history
        WHERE id_produto = ?
        """,
        (id_produto,),
    )
    row = cursor.fetchone()
    if not row:
        return {"menor_30d": None, "menor_90d": None, "media_30d": None}

    return {
        "menor_30d": float(row[0]) if row[0] is not None else None,
        "menor_90d": float(row[1]) if row[1] is not None else None,
        "media_30d": float(row[2]) if row[2] is not None else None,
    }


def classificar_oferta(preco_atual, media_30d, menor_90d):
    """
    Classifica a oferta em 4 níveis conforme regra de negócio.
    """
    if media_30d is None or media_30d <= 0:
        return "Preço Normal. Produto novo no histórico: ainda não há base de comparação suficiente."

    if preco_atual > media_30d:
        return "Furada. O preço atual está acima da média dos últimos 30 dias."

    desconto = (media_30d - preco_atual) / media_30d
    menor_90d_valido = menor_90d is not None and menor_90d > 0

    if desconto > 0.20 or (menor_90d_valido and preco_atual <= menor_90d):
        return "Nível Black Friday! Preço muito abaixo da média ou no menor patamar dos últimos 90 dias."

    if 0.10 <= desconto <= 0.20:
        alvo_black = menor_90d if menor_90d_valido else round(preco_atual * 0.95, 2)
        return (
            "Promoção Boa. Vale a pena nesse preço! "
            f"Se cair para {alvo_black:.2f}, é Black Friday."
        )

    return "Preço Normal. Valor dentro da faixa média recente."


def contexto_ia_por_avaliacao(avaliacao):
    """Define um contexto pronto para o n8n orientar o LLM de resposta final."""
    if "Black Friday" in avaliacao:
        return "Aja como um cachorro muito animado comemorando uma super oferta imperdível."
    if "Promoção Boa" in avaliacao:
        return "Aja como um cachorro feliz dando uma boa notícia sobre essa oferta."
    if "Furada" in avaliacao:
        return "Aja como um cachorro protetor alertando o tutor para esperar um preço melhor."
    return "Aja como um cachorro amigável explicando o cenário de preço de forma clara."


# Função de limpeza para evitar o envio de "lixo" ou campos vazios para o bot
def limpar_resultados(itens):
    # Filtra apenas itens que tenham nome e preço válidos e que não sejam strings 'null'
    validos = [i for i in itens if i.get('nome') and i.get('preco') and i.get('nome') != "null"]
    return validos[:3] # Retorna apenas os 3 melhores para o WhatsApp não ficar gigante

class RequisicaoBusca(BaseModel):
    termo: str
    lojas: Optional[List[str]] = ["promotech.app.br", "kabum.com.br"]

class Produto(BaseModel):
    nome: str = Field(description="O nome completo do produto")
    preco: str = Field(description="O preço à vista encontrado")
    loja: str = Field(description="O nome da loja onde foi encontrado")


def normalizar_texto(texto: str) -> str:
    """Normaliza acentos/caixa para melhorar matching de termos."""
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return " ".join(texto.lower().split())


def conectar_banco_historico():
    """Conecta/cria o SQLite e garante a tabela historico solicitada."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS historico (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            termo_busca TEXT NOT NULL,
            nome_produto TEXT NOT NULL,
            preco REAL NOT NULL,
            loja TEXT NOT NULL,
            data_coleta TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_historico_termo_data
        ON historico (termo_busca, data_coleta)
        """
    )
    conn.commit()
    return conn


def inserir_historico(conn, termo_busca, nome_produto, preco, loja):
    """Insere snapshot do preço atual para compor série histórica."""
    conn.execute(
        """
        INSERT INTO historico (termo_busca, nome_produto, preco, loja, data_coleta)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            normalizar_texto(termo_busca),
            nome_produto,
            float(preco),
            loja,
            datetime.utcnow().isoformat(),
        ),
    )
    conn.commit()


def analisar_historico_termo(conn, termo_busca):
    """
    Calcula métricas de janela para o termo:
    - menor_30d: MIN(preco) em 30 dias
    - menor_90d: MIN(preco) em 90 dias
    - media_30d: AVG(preco) em 30 dias
    """
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT
            MIN(CASE WHEN datetime(data_coleta) >= datetime('now', '-30 day') THEN preco END),
            MIN(CASE WHEN datetime(data_coleta) >= datetime('now', '-90 day') THEN preco END),
            AVG(CASE WHEN datetime(data_coleta) >= datetime('now', '-30 day') THEN preco END),
            COUNT(CASE WHEN datetime(data_coleta) >= datetime('now', '-30 day') THEN 1 END)
        FROM historico
        WHERE termo_busca = ?
        """,
        (normalizar_texto(termo_busca),),
    )
    row = cursor.fetchone()
    if not row:
        return {"menor_30d": None, "menor_90d": None, "media_30d": None, "qtd_30d": 0}

    return {
        "menor_30d": float(row[0]) if row[0] is not None else None,
        "menor_90d": float(row[1]) if row[1] is not None else None,
        "media_30d": float(row[2]) if row[2] is not None else None,
        "qtd_30d": int(row[3]) if row[3] is not None else 0,
    }


def classificar_oferta_termometro(preco_atual, media_30d, menor_90d, qtd_30d, confiavel):
    """
    Lógica matemática do termômetro:
    desconto = (media_30d - preco_atual) / media_30d
    """
    if media_30d is None or media_30d <= 0 or qtd_30d <= 1:
        texto = (
            "Primeira vez que busco esse produto, ainda não tenho histórico de preços, "
            f"mas o valor atual é {preco_atual:.2f}"
        )
        if not confiavel:
            texto += ". Atenção: vendedor/loja com reputação duvidosa."
        return texto

    if preco_atual > media_30d:
        avaliacao = "Furada: preço atual acima da média dos últimos 30 dias."
    else:
        desconto = (media_30d - preco_atual) / media_30d
        if desconto > 0.20 or (menor_90d is not None and preco_atual <= menor_90d):
            avaliacao = "Nível Black Friday: mais de 20% abaixo da média ou no menor preço dos últimos 90 dias."
        elif 0.10 <= desconto <= 0.20:
            alvo = menor_90d if menor_90d is not None else round(preco_atual * 0.95, 2)
            avaliacao = f"Promoção Boa. Vale a pena nesse preço! Se cair para {alvo:.2f}, é Black Friday."
        else:
            avaliacao = "Preço Normal: preço atual muito próximo da média dos últimos 30 dias."

    if not confiavel:
        avaliacao += " Atenção: vendedor/loja com reputação duvidosa."
    return avaliacao


def baixar_html_promotech(url):
    """Baixa HTML do Promotech com headers de navegador para reduzir bloqueios."""
    resp = requests.get(url, headers=PROMOTECH_HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text


def score_correspondencia(termo, nome_produto):
    termo_n = normalizar_texto(termo)
    nome_n = normalizar_texto(nome_produto)
    if termo_n in nome_n:
        return 1.0
    return SequenceMatcher(None, termo_n, nome_n).ratio()


def encontrar_links_produtos_promotech(termo, limite=10):
    """
    Retorna uma lista ordenada de links de produtos mais aderentes ao termo.
    Isso permite comparar varios produtos parecidos (ex.: 10 modelos RTX 4090).
    """
    candidatos = []

    # 1) Tenta API interna/hidratacao para maior robustez.
    fontes_links = []
    fontes_links.extend(buscar_links_promotech_via_api(termo))
    for categoria in PROMOTECH_CATEGORIAS:
        url_categoria = f"{PROMOTECH_BASE_URL}/products/simple/{categoria}"
        fontes_links.extend(buscar_links_promotech_via_hidratacao(url_categoria))

    vistos = set()
    for link in fontes_links:
        if link in vistos:
            continue
        vistos.add(link)
        slug = link.rstrip("/").split("/")[-1].replace("-", " ")
        score = score_correspondencia(termo, slug)
        if score >= 0.35:
            candidatos.append((score, link))

    # 2) Fallback para parsing HTML de listagens quando API/hidratacao falhar.
    if len(candidatos) < max(3, limite // 2):
        for categoria in PROMOTECH_CATEGORIAS:
            url_categoria = f"{PROMOTECH_BASE_URL}/products/simple/{categoria}"
            try:
                html = baixar_html_promotech(url_categoria)
            except Exception:
                continue

            soup = BeautifulSoup(html, "html.parser")
            for anchor in soup.find_all("a", href=True):
                href = anchor.get("href", "")
                if not href.startswith("/products/simple/") or href.count("/") < 4:
                    continue

                link = urljoin(PROMOTECH_BASE_URL, href)
                if link in vistos:
                    continue

                nome_candidato = " ".join(anchor.stripped_strings)
                if not nome_candidato:
                    continue

                score = score_correspondencia(termo, nome_candidato)
                if score >= 0.45:
                    vistos.add(link)
                    candidatos.append((score, link))

    candidatos.sort(key=lambda x: x[0], reverse=True)
    return [link for _, link in candidatos[:limite]]


def extrair_melhor_oferta_promotech(link_produto):
    """Extrai nome, preço à vista, loja, confiabilidade e link de compra."""
    html = baixar_html_promotech(link_produto)
    soup = BeautifulSoup(html, "html.parser")

    titulo = soup.find("h1")
    nome_produto = " ".join(titulo.stripped_strings) if titulo else "Produto não identificado"
    texto = soup.get_text(" ", strip=True)

    preco_match = re.search(r"R\$\s*[\d\.]+,\d{2}", texto)
    preco_atual = normalizar_preco(preco_match.group(0)) if preco_match else None
    if preco_atual is None:
        return None

    loja = "Loja não identificada"
    loja_match = re.search(r"À vista\s*[•\-]\s*([^\d\|]+?)\s+PromoScore", texto, flags=re.IGNORECASE)
    if loja_match:
        loja = loja_match.group(1).strip()

    promo_score = None
    score_match = re.search(r"PromoScore\s*:?\s*(\d{1,3})", texto, flags=re.IGNORECASE)
    if score_match:
        promo_score = int(score_match.group(1))

    confiavel = True if promo_score is None else promo_score >= 40

    link_oferta = link_produto
    for anchor in soup.find_all("a", href=True):
        rotulo = " ".join(anchor.stripped_strings).upper()
        if "COMPRAR" in rotulo:
            link_oferta = anchor.get("href")
            break

    return {
        "produto": nome_produto,
        "loja": loja,
        "confiavel": confiavel,
        "preco_atual": round(preco_atual, 2),
        "link": link_oferta,
    }


def _formatar_preco_brl(valor):
    if valor is None:
        return "N/A"
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def processar_busca_promotech(termo):
    """Pipeline Promotech + SQLite + Termômetro comparando varios produtos."""
    termo_limpo = (termo or "").strip()
    if not termo_limpo:
        return {"resultados": [], "erro": "Termo de busca inválido."}

    conn = None
    try:
        limite_produtos = int(os.getenv("PROMOTECH_MAX_RESULTS", "10"))
        links_produtos = encontrar_links_produtos_promotech(termo_limpo, limite=limite_produtos)
        if not links_produtos:
            return {
                "resultados": [],
                "erro": (
                    "Nenhum produto encontrado no Promotech. "
                    "Se o site estiver renderizando via JS, tente Playwright/Selenium ou API interna."
                ),
            }

        conn = conectar_banco_historico()

        ofertas_processadas = []
        for link_produto in links_produtos:
            try:
                oferta = extrair_melhor_oferta_promotech(link_produto)
                if not oferta:
                    continue

                inserir_historico(
                    conn,
                    termo_busca=termo_limpo,
                    nome_produto=oferta["produto"],
                    preco=oferta["preco_atual"],
                    loja=oferta["loja"],
                )

                # Analise por item (termo + nome) para evitar mistura de produtos distintos.
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT
                        MIN(CASE WHEN datetime(data_coleta) >= datetime('now', '-30 day') THEN preco END),
                        MIN(CASE WHEN datetime(data_coleta) >= datetime('now', '-90 day') THEN preco END),
                        AVG(CASE WHEN datetime(data_coleta) >= datetime('now', '-30 day') THEN preco END),
                        COUNT(CASE WHEN datetime(data_coleta) >= datetime('now', '-30 day') THEN 1 END)
                    FROM historico
                    WHERE termo_busca = ? AND nome_produto = ?
                    """,
                    (normalizar_texto(termo_limpo), oferta["produto"]),
                )
                row = cursor.fetchone() or (None, None, None, 0)
                metricas_item = {
                    "menor_30d": float(row[0]) if row[0] is not None else None,
                    "menor_90d": float(row[1]) if row[1] is not None else None,
                    "media_30d": float(row[2]) if row[2] is not None else None,
                    "qtd_30d": int(row[3]) if row[3] is not None else 0,
                }

                avaliacao = classificar_oferta_termometro(
                    preco_atual=oferta["preco_atual"],
                    media_30d=metricas_item["media_30d"],
                    menor_90d=metricas_item["menor_90d"],
                    qtd_30d=metricas_item["qtd_30d"],
                    confiavel=oferta["confiavel"],
                )

                ofertas_processadas.append(
                    {
                        "url": link_produto,
                        "itens": [
                            {
                                "nome": oferta["produto"],
                                "preco": _formatar_preco_brl(oferta["preco_atual"]),
                                "loja": oferta["loja"],
                                "confiavel": oferta["confiavel"],
                                "preco_atual": oferta["preco_atual"],
                                "menor_30d": round(metricas_item["menor_30d"], 2)
                                if metricas_item["menor_30d"] is not None
                                else None,
                                "menor_90d": round(metricas_item["menor_90d"], 2)
                                if metricas_item["menor_90d"] is not None
                                else None,
                                "avaliacao": avaliacao,
                                "link": oferta["link"],
                            }
                        ],
                    }
                )
            except Exception:
                continue

        if not ofertas_processadas:
            return {
                "resultados": [],
                "erro": "Nao foi possivel extrair ofertas validas do Promotech para o termo informado.",
            }

        # Ordena pelo preco atual para destacar melhor oferta.
        ofertas_processadas.sort(
            key=lambda r: r["itens"][0].get("preco_atual")
            if r.get("itens") and r["itens"][0].get("preco_atual") is not None
            else float("inf")
        )

        return {
            "fonte": "promotech",
            "termo": termo_limpo,
            "total_encontrados": len(ofertas_processadas),
            "resultados": ofertas_processadas,
            "melhor_oferta": ofertas_processadas[0]["itens"][0],
        }
    except Exception as e:
        return {"resultados": [], "erro": f"Erro ao processar busca no Promotech: {e}"}
    finally:
        if conn is not None:
            conn.close()

@app.post("/buscar")
async def buscar_dados(req: RequisicaoBusca):
    resposta_promotech = processar_busca_promotech(req.termo)
    if resposta_promotech.get("resultados"):
        return resposta_promotech

    # Fallback hibrido: se Promotech falhar/nao retornar itens, tenta fluxo legado.
    resposta_legado = await buscar_dados_legado(req)
    if isinstance(resposta_legado, dict):
        resposta_legado["fallback_usado"] = True
        resposta_legado["fonte"] = "legado_crawl4ai"
        if resposta_promotech.get("erro"):
            resposta_legado["erro_promotech"] = resposta_promotech.get("erro")
    return resposta_legado


@app.post("/buscar_legado")
async def buscar_dados_legado(req: RequisicaoBusca):
    try:
        import httpx
        crawl4ai_mod = importlib.import_module("crawl4ai")
        AsyncWebCrawler = crawl4ai_mod.AsyncWebCrawler
        CrawlerRunConfig = crawl4ai_mod.CrawlerRunConfig
        CacheMode = crawl4ai_mod.CacheMode
        LLMConfig = crawl4ai_mod.LLMConfig
        LLMExtractionStrategy = crawl4ai_mod.LLMExtractionStrategy
    except Exception as e:
        return {
            "resultados": [],
            "erro": (
                "Fluxo legado indisponível neste ambiente. "
                "Instale httpx + crawl4ai (preferencialmente em Python 3.10/3.11). "
                f"Detalhe: {e}"
            ),
        }

    try:
        conn = conectar_banco()
    except Exception as e:
        return {"resultados": [], "erro": f"Falha ao iniciar banco de histórico: {e}"}

    sites_formatados = " OR ".join([f"site:{loja}" for loja in req.lojas])
    query_completa = f'{req.termo} ({sites_formatados})'
    
    # Detecta onde o código está rodando para acessar o SearXNG corretamente
    if os.path.exists('/.dockerenv'):
        # No Docker, usa a rede interna (nome do container e porta original)
        searxng_url = SEARXNG_URL_DOCKER
        print(f" SearXNG: Rodando no Docker! URL: {searxng_url}")
    else:
        # Local no VS Code, usa o localhost e a porta exposta (9092)
        searxng_url = SEARXNG_URL_LOCAL
        print(f" SearXNG: Rodando Local! URL: {searxng_url}")
    
    async with httpx.AsyncClient() as client:
        try:
            # TENTATIVA 1: Deixa o SearXNG usar os originais (Google, DuckDuckGo, etc)
            resposta_busca = await client.get(searxng_url, params={"q": query_completa, "format": "json"}, timeout=20.0)
            
            if resposta_busca.status_code != 200:
                conn.close()
                return {"resultados": [], "erro": "Motor de busca indisponível"}
            
            dados_busca = resposta_busca.json()
            
            # Se os originais bloquearam e a lista veio vazia, acionamos o Plano B!
            if len(dados_busca.get('results', [])) == 0:
                print("⚠️ Buscadores originais bloqueados (Anti-Bot). Acionando Plano B (Bing, Yahoo)...")
                
                # TENTATIVA 2: Forçando motores alternativos
                resposta_busca = await client.get(
                    searxng_url, 
                    params={"q": query_completa, "format": "json", "engines": "bing,yahoo,qwant"}, 
                    timeout=20.0
                )
                dados_busca = resposta_busca.json()
                print(f"✅ Plano B encontrou {len(dados_busca.get('results', []))} resultados!")
            else:
                print(f"✅ Buscadores originais funcionaram! {len(dados_busca.get('results', []))} resultados.")

        except Exception as e:
            conn.close()
            return {"resultados": [], "erro": f"Erro de conexão: {e}"}
    
    # Pega as 3 primeiras URLs relevantes
    urls_para_raspar = [res['url'] for res in dados_busca.get('results', [])[:3]]
    
    if not urls_para_raspar:
        conn.close()
        return {"resultados": []}

    # Detecta automaticamente se o código está dentro do Docker ou rodando local (VS Code)
    if os.path.exists('/.dockerenv'):
        url_ollama = OLLAMA_BASE_URL_DOCKER
        print("🐳 Rodando no Docker! Conectando ao Ollama via host.docker.internal...")
    else:
        url_ollama = OLLAMA_BASE_URL_LOCAL
        print("💻 Rodando Local! Conectando ao Ollama via localhost...")
    
    llm_config = LLMConfig(
        provider=OLLAMA_MODEL,
        base_url=url_ollama 
    )
    
    estrategia = LLMExtractionStrategy(
        llm_config=llm_config,
        schema=Produto.model_json_schema(),
        extraction_type="schema",
        # Prompt atualizado para forçar o modelo a ser mais preciso:
        instruction="Você é um assistente de extração de preços. Extraia APENAS o produto principal da página. O campo 'preco' DEVE conter apenas valores monetários numéricos (ex: R$ 1.500,00). Ignore produtos recomendados, ignore banners e ignore preços em texto como 'oferta'.",
        input_format="markdown",
        apply_chunking=True
    )
    run_config = CrawlerRunConfig(
        extraction_strategy=estrategia, 
        cache_mode=CacheMode.BYPASS 
    )
  
    resultados_finais = [] 

    async with AsyncWebCrawler() as crawler:
        for url in urls_para_raspar:
            print(f"🤖 [OLLAMA] Extraindo de: {url}")
            
            # Agora ele vai direto no Ollama sem tentar o Gemini antes
            resultado = await crawler.arun(url=url, config=run_config)
            
            if resultado.success:
                try:
                    dados = json.loads(resultado.extracted_content)
                    if isinstance(dados, dict): dados = [dados]
                    
                    # Filtra e limpa os resultados
                    dados_limpos = limpar_resultados(dados)
                    if dados_limpos:
                        for item in dados_limpos:
                            try:
                                preco_atual = normalizar_preco(item.get("preco"))
                                if preco_atual is None:
                                    continue

                                nome_produto = item.get("nome", "Produto sem nome")
                                # Usa URL + nome para identificar o produto no histórico
                                id_produto = f"{url}::{nome_produto.strip().lower()}"

                                registrar_preco(conn, id_produto, nome_produto, preco_atual)

                                historico = analisar_historico_preco(conn, id_produto)
                                avaliacao = classificar_oferta(
                                    preco_atual,
                                    historico.get("media_30d"),
                                    historico.get("menor_90d"),
                                )

                                resultados_finais.append(
                                    {
                                        "url": url,
                                        "nome": nome_produto,
                                        "preco_atual": round(preco_atual, 2),
                                        "menor_30d": round(historico["menor_30d"], 2)
                                        if historico["menor_30d"] is not None
                                        else None,
                                        "menor_90d": round(historico["menor_90d"], 2)
                                        if historico["menor_90d"] is not None
                                        else None,
                                        "media_30d": round(historico["media_30d"], 2)
                                        if historico["media_30d"] is not None
                                        else None,
                                        "avaliacao": avaliacao,
                                        "contexto_ia": contexto_ia_por_avaliacao(avaliacao),
                                    }
                                )
                            except Exception as e_item:
                                print(f"⚠️ Erro ao registrar/analisar histórico: {e_item}")
                                continue
                except Exception as e:
                    print(f"⚠️ Erro ao processar JSON: {e}")
                    continue

    conn.close()

    # Mantém compatibilidade com retorno em lista e já entrega um objeto padrão pronto para o n8n.
    resposta = {"resultados": resultados_finais}
    if resultados_finais:
        resposta["primeira_oferta"] = {
            "preco_atual": resultados_finais[0]["preco_atual"],
            "menor_30d": resultados_finais[0]["menor_30d"],
            "menor_90d": resultados_finais[0]["menor_90d"],
            "avaliacao": resultados_finais[0]["avaliacao"],
            "contexto_ia": resultados_finais[0]["contexto_ia"],
        }

    return resposta

    

if __name__ == "__main__":
    # Modo CLI: imprime apenas JSON no console (sem logs extras), ideal para n8n.
    if len(sys.argv) > 1:
        termo_cli = " ".join(sys.argv[1:])
        print(json.dumps(processar_busca_promotech(termo_cli), ensure_ascii=False))
    else:
        uvicorn.run(app, host="0.0.0.0", port=8000)