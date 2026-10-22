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
from urllib.parse import urlparse
from urllib.parse import quote_plus

try:
    from playwright.async_api import async_playwright
except Exception:
    async_playwright = None

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
PROMOTECH_SEED_URLS = [
    "https://promotech.app.br/products/simple/gpu/rx-9060-xt-16gb",
]


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
        # Tenta URL direta de busca
        f"{PROMOTECH_BASE_URL}/?q={termo_q}",
        f"{PROMOTECH_BASE_URL}/search?q={termo_q}",
    ]

    links = set()
    for endpoint in candidatos:
        try:
            print(f"[DEBUG API] Tentando: {endpoint}")
            resp = requests.get(endpoint, headers=PROMOTECH_HEADERS, timeout=10)
            if resp.status_code != 200:
                print(f"[DEBUG API] Status {resp.status_code} para {endpoint}")
                continue
            payload = resp.json()
            caminhos = set()
            _coletar_urls_produto(payload, caminhos)
            links.update(urljoin(PROMOTECH_BASE_URL, caminho) for caminho in caminhos)
            if links:
                print(f"[DEBUG API] ✅ Encontrou {len(links)} links em {endpoint}")
                return list(links)
        except Exception as e:
            print(f"[DEBUG API] Erro em {endpoint}: {e}")
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

    MAX_REASONABLE_PRICE = float(os.getenv("MAX_REASONABLE_PRICE", "50000"))
    MIN_REASONABLE_PRICE = float(os.getenv("MIN_REASONABLE_PRICE", "20"))

    try:
        preco = float(bruto)
        if preco <= 0 or preco < MIN_REASONABLE_PRICE or preco > MAX_REASONABLE_PRICE:
            return None
        return preco
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
    lojas: Optional[List[str]] = [
        "mercadolivre.com.br",
        "kabum.com.br",
        "pichau.com.br",
        "terabyteshop.com.br",
    ]

class Produto(BaseModel):
    nome: str = Field(description="O nome completo do produto")
    preco: str = Field(description="O preço à vista encontrado")
    loja: str = Field(description="O nome da loja onde foi encontrado")


def normalizar_texto(texto: str) -> str:
    """Normaliza acentos/caixa para melhorar matching de termos."""
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return " ".join(texto.lower().split())


def _extrair_preco_multi(texto):
    """Tenta extrair preco de um texto usando multiplos padroes."""
    padroes = [
        r"R\$\s*[\d\.,]+",
        r"US\$\s*[\d\.,]+",
        r"\$\s*[\d\.,]+",
        r"(?:por|apenas|a\ vista|à vista)\s*R\$\s*[\d\.,]+",
        r"\b\d{1,3}(?:\.\d{3})*,\d{2}\b",
        r"\b\d{1,3}(?:,\d{3})*\.\d{2}\b",
    ]
    for padrao in padroes:
        match = re.search(padrao, texto, re.IGNORECASE)
        if match:
            val = normalizar_preco(match.group(0))
            if val is not None:
                return val
    return None


def extrair_item_rapido_searx(resultado_busca, loja_padrao):
    """
    Extrai item de forma rapida sem LLM a partir de title/content do SearXNG.
    Retorna None quando nao encontra preco minimamente confiavel.
    """
    titulo = (resultado_busca.get("title") or "").strip()
    resumo = (resultado_busca.get("content") or "").strip()
    url = (resultado_busca.get("url") or "").strip()
    if not titulo or not url:
        return None

    texto_base = f"{titulo} {resumo}".strip()
    preco_atual = _extrair_preco_multi(texto_base)

    nome = re.sub(r"\s*[-|].*?(?=R\$|\d+[,.]\d)", "", titulo).strip()
    if not nome or len(nome) < 5:
        nome = re.sub(r"\s*[-|].*$", "", titulo).strip()
    if not nome:
        nome = titulo

    return {
        "url": url,
        "nome": nome,
        "preco_atual": round(preco_atual, 2) if preco_atual is not None else None,
        "loja": loja_padrao,
        "confiavel": False,
    }


def _extrair_price_texto(texto: str):
    match = re.search(r"R\$\s*\d[\d\.]*,\d{2}|\d[\d\.]*,\d{2}", texto or "")
    if not match:
        return None
    return normalizar_preco(match.group(0))


def _extrair_json_ld_produtos(soup: BeautifulSoup, url_base: str, loja_padrao: str, limite: int = 5):
    itens = []
    vistos = set()

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        conteudo = script.string or script.get_text(strip=True)
        if not conteudo:
            continue
        try:
            payload = json.loads(conteudo)
        except Exception:
            continue

        blocos = payload if isinstance(payload, list) else [payload]
        for bloco in blocos:
            if not isinstance(bloco, dict):
                continue

            tipo = bloco.get("@type")
            if tipo == "Product" or (isinstance(tipo, list) and "Product" in tipo):
                nome = bloco.get("name") or bloco.get("headline") or bloco.get("title")
                if not nome:
                    continue

                url_item = bloco.get("url") or url_base
                ofertas = bloco.get("offers") or {}
                if isinstance(ofertas, list):
                    ofertas = ofertas[0] if ofertas else {}

                preco = ofertas.get("price") if isinstance(ofertas, dict) else None
                if preco is None:
                    preco = bloco.get("price")

                preco_atual = normalizar_preco(preco) if preco is not None else None
                chave = normalizar_texto(f"{nome}::{url_item}")
                if not chave or chave in vistos:
                    continue
                vistos.add(chave)
                itens.append(
                    {
                        "url": url_item,
                        "nome": nome,
                        "preco_atual": round(preco_atual, 2) if preco_atual is not None else None,
                        "loja": loja_padrao,
                        "confiavel": preco_atual is not None,
                    }
                )
                if len(itens) >= limite:
                    return itens

    return itens


def _extrair_cards_genericos(soup: BeautifulSoup, url_base: str, loja_padrao: str, limite: int = 5):
    itens = []
    vistos = set()

    candidatos = soup.find_all(["article", "li", "div"])
    for bloco in candidatos:
        texto_bloco = bloco.get_text(" ", strip=True)
        if not texto_bloco:
            continue

        preco_atual = _extrair_price_texto(texto_bloco)
        if preco_atual is None:
            continue

        link = None
        for anchor in bloco.find_all("a", href=True):
            href = (anchor.get("href") or "").strip()
            if href:
                link = urljoin(url_base, href)
                break

        if not link:
            continue

        titulo = None
        for tag_nome in ["h1", "h2", "h3", "h4", "p", "span", "strong"]:
            tag = bloco.find(tag_nome)
            if tag:
                texto = tag.get_text(" ", strip=True)
                if texto and len(texto) >= 6:
                    titulo = texto
                    break

        if not titulo:
            primeiro_anchor = bloco.find("a")
            if primeiro_anchor:
                texto = primeiro_anchor.get_text(" ", strip=True)
                if texto:
                    titulo = texto

        if not titulo:
            titulo = texto_bloco[:120]

        chave = normalizar_texto(f"{titulo}::{link}")
        if chave in vistos:
            continue
        vistos.add(chave)

        itens.append(
            {
                "url": link,
                "nome": titulo,
                "preco_atual": round(preco_atual, 2),
                "loja": loja_padrao,
                "confiavel": False,
            }
        )
        if len(itens) >= limite:
            break

    return itens


async def extrair_ofertas_sem_ia_da_url(url: str, loja_padrao: str, limite: int = 5):
    """Extrai múltiplas ofertas de uma URL sem IA, com fallback Playwright quando necessário."""
    html = ""
    headers = {
        "User-Agent": PROMOTECH_HEADERS["User-Agent"],
        "Accept-Language": "pt-BR,pt;q=0.9",
    }

    try:
        resp = requests.get(url, headers=headers, timeout=20)
        if resp.status_code == 200:
            html = resp.text or ""
    except Exception:
        html = ""

    if not html and async_playwright is not None:
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(user_agent=headers["User-Agent"], locale="pt-BR")
                page = await context.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                html = await page.content()
                await browser.close()
        except Exception:
            html = ""

    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    itens = _extrair_json_ld_produtos(soup, url, loja_padrao, limite=limite)
    if len(itens) < limite:
        extras = _extrair_cards_genericos(soup, url, loja_padrao, limite=limite)
        vistos = {normalizar_texto(f"{i['nome']}::{i['url']}") for i in itens}
        for item in extras:
            chave = normalizar_texto(f"{item['nome']}::{item['url']}")
            if chave in vistos:
                continue
            vistos.add(chave)
            itens.append(item)
            if len(itens) >= limite:
                break

    return itens[:limite]


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


def extrair_slugs_json_categoria(html):
    """
    Promotech embute dados de produtos em JSON no HTML da categoria.
    Extrai pares (nome, slug) para montar URLs de produto sem depender de <a href>.
    """
    pares = []
    vistos = set()
    padrao = re.compile(r'"name":"([^"\\]+)".{0,500}?"slug":"([a-z0-9\-]+)"', re.IGNORECASE)
    for match in padrao.finditer(html):
        nome = match.group(1).strip()
        slug = match.group(2).strip().lower()
        chave = (nome, slug)
        if slug and chave not in vistos:
            vistos.add(chave)
            pares.append(chave)
    return pares


def encontrar_links_produtos_promotech(termo, limite=10):
    """
    Retorna uma lista ordenada de links de produtos mais aderentes ao termo.
    Isso permite comparar varios produtos parecidos (ex.: 10 modelos RTX 4090).
    """
    candidatos = []
    termo_lower = termo.lower()

    # 1) Tenta API interna/hidratacao para maior robustez.
    fontes_links = []
    fontes_links.extend(buscar_links_promotech_via_api(termo))
    print(f"[DEBUG] API encontrou {len(fontes_links)} links para '{termo}'")

    # Seed explícita para garantir presença de páginas estratégicas no ranking TECH.
    fontes_links.extend(PROMOTECH_SEED_URLS)
    
    for categoria in PROMOTECH_CATEGORIAS:
        url_categoria = f"{PROMOTECH_BASE_URL}/products/simple/{categoria}"
        links_categoria = buscar_links_promotech_via_hidratacao(url_categoria)
        fontes_links.extend(links_categoria)
        print(f"[DEBUG] Categoria '{categoria}': {len(links_categoria)} links")

    vistos = set()
    for link in fontes_links:
        if link in vistos:
            continue
        vistos.add(link)
        slug = link.rstrip("/").split("/")[-1].replace("-", " ")
        score = score_correspondencia(termo, slug)
        # Threshold reduzido de 0.35 para 0.25 para capturar mais produtos
        if score >= 0.25:
            candidatos.append((score, link))
            print(f"[DEBUG] Slug match: '{slug}' -> score={score:.2f}")

    # 2) Fallback para parsing HTML de listagens quando API/hidratacao falhar.
    if len(candidatos) < max(3, limite // 2):
        print(f"[DEBUG] Buscando fallback HTML. Candidatos atuais: {len(candidatos)}")
        for categoria in PROMOTECH_CATEGORIAS:
            url_categoria = f"{PROMOTECH_BASE_URL}/products/simple/{categoria}"
            try:
                html = baixar_html_promotech(url_categoria)
            except Exception as e:
                print(f"[DEBUG] Erro ao buscar categoria '{categoria}': {e}")
                continue

            # 2.1) Estratégia principal do fallback: slugs vindos de JSON embutido.
            for nome_json, slug in extrair_slugs_json_categoria(html):
                link = f"{PROMOTECH_BASE_URL}/products/simple/{categoria}/{slug}"
                if link in vistos:
                    continue

                score_json = max(
                    score_correspondencia(termo, nome_json),
                    score_correspondencia(termo, slug.replace("-", " ")),
                )
                if score_json >= 0.20:
                    vistos.add(link)
                    candidatos.append((score_json, link))
                    print(f"[DEBUG] JSON slug match: '{nome_json}' ({slug}) -> score={score_json:.2f}")

            # 2.2) Fallback extra por anchors (quando existir).
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
                # Threshold reduzido de 0.45 para 0.20 para ser mais permissivo
                if score >= 0.20:
                    vistos.add(link)
                    candidatos.append((score, link))
                    print(f"[DEBUG] HTML match: '{nome_candidato}' -> score={score:.2f}")

    print(f"[DEBUG] Total de candidatos após filtragem: {len(candidatos)}")
    candidatos.sort(key=lambda x: x[0], reverse=True)
    return [link for _, link in candidatos[:limite]]


def combinar_resultados_promotech_legado(resposta_promotech, resposta_legado, termo, limite=10):
    """Combina fontes TECH: Promotech + legado (Kabum/Pichau/Terabyte via SearXNG+crawl4ai)."""
    combinados = []
    vistos = set()

    for fonte_nome, resposta in (("promotech", resposta_promotech), ("legado", resposta_legado)):
        if not isinstance(resposta, dict):
            continue
        for bloco in resposta.get("resultados", []) or []:
            itens = bloco.get("itens", []) or []
            if not itens:
                continue
            item = itens[0]
            chave = (item.get("link") or bloco.get("url") or "", item.get("nome") or "")
            if chave in vistos:
                continue
            vistos.add(chave)
            combinados.append({
                "fonte_item": fonte_nome,
                "url": bloco.get("url") or item.get("link"),
                "itens": [item],
            })

    combinados.sort(
        key=lambda r: r["itens"][0].get("preco_atual")
        if r.get("itens") and r["itens"][0].get("preco_atual") is not None
        else float("inf")
    )
    combinados = combinados[:limite]

    saida = {
        "fonte": "promotech+legado_crawl4ai",
        "termo": termo,
        "total_encontrados": len(combinados),
        "resultados": combinados,
    }
    if combinados:
        saida["melhor_oferta"] = combinados[0]["itens"][0]
    return saida


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
        print(f"\n═══ BUSCA PROMOTECH: '{termo_limpo}' ═══")
        limite_produtos = int(os.getenv("PROMOTECH_MAX_RESULTS", "10"))
        links_produtos = encontrar_links_produtos_promotech(termo_limpo, limite=limite_produtos)
        print(f"[RESULTADO] {len(links_produtos)} links encontrados para '{termo_limpo}'")
        
        if not links_produtos:
            erro_msg = (
                f"❌ Nenhum produto encontrado no Promotech para '{termo_limpo}'. "
                "Verificando: API endpoints, categorias, score matching. "
                "Se o site renderiza via JS, tente Playwright/Selenium ou acione fallback crawl4ai."
            )
            print(erro_msg)
            return {"resultados": [], "erro": erro_msg}

        conn = conectar_banco_historico()

        ofertas_processadas = []
        for idx, link_produto in enumerate(links_produtos, 1):
            try:
                print(f"  [{idx}/{len(links_produtos)}] Processando: {link_produto}")
                oferta = extrair_melhor_oferta_promotech(link_produto)
                if not oferta:
                    print(f"    ⚠️ Nao conseguiu extrair oferta deste link")
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
                print(f"    ✅ {oferta['produto']} - {oferta['loja']}")
            except Exception as e:
                print(f"    ❌ Erro: {e}")
                continue

        if not ofertas_processadas:
            erro_msg = (
                f"❌ Encontrou {len(links_produtos)} links mas nao conseguiu extrair ofertas validas. "
                "Isso pode significar que: a estrutura HTML mudou, o site bloqueou o scraper, "
                "ou os links encontrados nao sao validos."
            )
            print(erro_msg)
            return {
                "resultados": [],
                "erro": erro_msg,
            }

        # Ordena pelo preco atual para destacar melhor oferta.
        ofertas_processadas.sort(
            key=lambda r: r["itens"][0].get("preco_atual")
            if r.get("itens") and r["itens"][0].get("preco_atual") is not None
            else float("inf")
        )

        resultado = {
            "fonte": "promotech",
            "termo": termo_limpo,
            "total_encontrados": len(ofertas_processadas),
            "resultados": ofertas_processadas,
            "melhor_oferta": ofertas_processadas[0]["itens"][0],
        }
        print(f"✅ PROMOTECH: {len(ofertas_processadas)} produtos processados com sucesso\n")
        return resultado
    except Exception as e:
        erro_msg = f"❌ Erro geral ao processar busca no Promotech: {e}"
        print(erro_msg)
        return {"resultados": [], "erro": erro_msg}
    finally:
        if conn is not None:
            conn.close()

@app.post("/buscar")
async def buscar_dados(req: RequisicaoBusca):
    resposta_legado = await buscar_dados_legado(req)

    # Fonte primária TECH: Mercado Livre, Kabum, Pichau e Terabyte.
    if isinstance(resposta_legado, dict) and resposta_legado.get("resultados"):
        resposta_legado["fallback_usado"] = False
        resposta_legado["fonte"] = "mercado-livre+kabum+pichau+terabyte"
        return resposta_legado

    # Fallback com IA fica desligado por padrão para manter o fluxo sem LLM.
    if os.getenv("TECH_ALLOW_AI_FALLBACK", "0") == "1":
        resposta_promotech = processar_busca_promotech(req.termo)
        if resposta_promotech.get("resultados"):
            resposta_promotech["fallback_usado"] = True
            resposta_promotech["fonte"] = "promotech_fallback"
            return resposta_promotech

    retorno = {
        "fonte": "mercado-livre+kabum+pichau+terabyte",
        "termo": req.termo,
        "resultados": [],
        "total_encontrados": 0,
    }
    if isinstance(resposta_legado, dict) and resposta_legado.get("erro"):
        retorno["erro_legado"] = resposta_legado.get("erro")
    return retorno


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

    # Detecta onde o código está rodando para acessar o SearXNG corretamente
    if os.path.exists('/.dockerenv'):
        # No Docker, usa a rede interna (nome do container e porta original)
        searxng_url = SEARXNG_URL_DOCKER
        print(f" SearXNG: Rodando no Docker! URL: {searxng_url}")
    else:
        # Local no VS Code, usa o localhost e a porta exposta (9092)
        searxng_url = SEARXNG_URL_LOCAL
        print(f" SearXNG: Rodando Local! URL: {searxng_url}")
    
    urls_para_raspar = []
    itens_rapidos = []
    vistos_urls = set()
    limite_por_loja = int(os.getenv("TECH_URLS_PER_SITE", "20"))
    limite_total = int(os.getenv("TECH_MAX_RESULTS", "80"))

    async with httpx.AsyncClient() as client:
        try:
            for loja in req.lojas:
                query_completa = f'{req.termo} site:{loja}'
                print(f"🔎 SearXNG TECH -> {loja}: {query_completa}")
                resposta_busca = await client.get(
                    searxng_url,
                    params={"q": query_completa, "format": "json", "engines": "google,bing,duckduckgo"},
                    timeout=20.0,
                )

                if resposta_busca.status_code != 200:
                    print(f"⚠️ Motor indisponível para {loja}: HTTP {resposta_busca.status_code}")
                    continue

                dados_busca = resposta_busca.json()

                if len(dados_busca.get('results', [])) == 0:
                    print(f"⚠️ {loja} sem resultados no Google/Bing. Tentando engines alternativos...")
                    resposta_busca = await client.get(
                        searxng_url,
                        params={"q": query_completa, "format": "json", "engines": "qwant,yahoo,startpage"},
                        timeout=20.0,
                    )
                    dados_busca = resposta_busca.json()

                resultados_loja = 0
                for res in dados_busca.get('results', []):
                    url = res.get('url')
                    if not url or url in vistos_urls:
                        continue
                    vistos_urls.add(url)
                    urls_para_raspar.append(url)

                    item_rapido = extrair_item_rapido_searx(res, loja)
                    if item_rapido:
                        itens_rapidos.append(item_rapido)

                    resultados_loja += 1
                    if resultados_loja >= limite_por_loja or len(urls_para_raspar) >= limite_total:
                        break

                print(f"✅ {loja}: {resultados_loja} URLs adicionadas")
                if len(urls_para_raspar) >= limite_total:
                    break

        except Exception as e:
            conn.close()
            return {"resultados": [], "erro": f"Erro de conexão: {e}"}
    
    if not urls_para_raspar:
        # Fallback amplo: tenta buscar sem restricao de loja para nao retornar vazio
        # e reduzir a chance de cair apenas em 1 item do Promotech.
        try:
            async with httpx.AsyncClient() as client:
                query_ampla = req.termo
                print(f"🔎 Fallback amplo TECH: {query_ampla}")
                resposta_busca = await client.get(
                    searxng_url,
                    params={"q": query_ampla, "format": "json", "engines": "google,bing,duckduckgo"},
                    timeout=20.0,
                )

                if resposta_busca.status_code == 200:
                    dados_busca = resposta_busca.json()
                    if len(dados_busca.get("results", [])) == 0:
                        resposta_busca = await client.get(
                            searxng_url,
                            params={"q": query_ampla, "format": "json", "engines": "qwant,yahoo,startpage"},
                            timeout=20.0,
                        )
                        dados_busca = resposta_busca.json()

                    limite_amplo = int(os.getenv("TECH_BROAD_URLS", "20"))
                    for res in dados_busca.get("results", []):
                        url = res.get("url")
                        if not url or url in vistos_urls:
                            continue
                        vistos_urls.add(url)
                        urls_para_raspar.append(url)

                        host = "web"
                        try:
                            host = urlparse(url).netloc.replace("www.", "") or "web"
                        except Exception:
                            pass

                        item_rapido = extrair_item_rapido_searx(res, host)
                        if item_rapido:
                            itens_rapidos.append(item_rapido)

                        if len(urls_para_raspar) >= limite_amplo:
                            break
        except Exception as e_fallback:
            print(f"⚠️ Fallback amplo falhou: {e_fallback}")

    if not urls_para_raspar and not itens_rapidos:
        conn.close()
        return {"resultados": []}

    # Organiza candidatos rapidos sem IA (mais veloz e com maior volume de itens).
    itens_rapidos_unicos = []
    vistos_nomes = set()
    for item in itens_rapidos:
        chave = normalizar_texto(item.get("nome", ""))
        if not chave or chave in vistos_nomes:
            continue
        vistos_nomes.add(chave)
        itens_rapidos_unicos.append(item)

    itens_rapidos_unicos.sort(
        key=lambda x: x.get("preco_atual") if x.get("preco_atual") is not None else float("inf")
    )

    resultados_finais = []

    # Preenche com itens rapidos sem IA para garantir volume inicial.
    for item in itens_rapidos_unicos:
        try:
            historico = {
                "menor_30d": None,
                "menor_90d": None,
                "media_30d": None,
            }
            if item.get("preco_atual") is not None:
                id_produto = f"{item['url']}::{item['nome'].strip().lower()}"
                registrar_preco(conn, id_produto, item["nome"], item["preco_atual"])
                historico = analisar_historico_preco(conn, id_produto)

            avaliacao = (
                classificar_oferta(
                    item["preco_atual"],
                    historico.get("media_30d"),
                    historico.get("menor_90d"),
                )
                if item.get("preco_atual") is not None
                else "Preço não identificado no snippet da busca rápida."
            )

            resultados_finais.append(
                {
                    "url": item["url"],
                    "nome": item["nome"],
                    "preco_atual": item["preco_atual"],
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
                    "contexto_ia": "Aja como um cachorro amigavel listando opcoes coletadas rapidamente.",
                    "loja": item.get("loja"),
                    "confiavel": item.get("confiavel", False),
                }
            )
        except Exception as e_item_rapido:
            print(f"⚠️ Erro ao processar item rapido: {e_item_rapido}")
            continue

    if urls_para_raspar:
        limite_itens_por_url = int(os.getenv("TECH_ITEMS_PER_URL", "5"))

        for url in urls_para_raspar:
            try:
                host = urlparse(url).netloc.replace("www.", "") or "web"
            except Exception:
                host = "web"

            print(f"🕸️ [SEM IA] Extraindo HTML de: {url}")
            itens_url = await extrair_ofertas_sem_ia_da_url(url, host, limite=limite_itens_por_url)

            for item in itens_url:
                try:
                    nome_produto = item.get("nome", "Produto sem nome")
                    preco_atual = item.get("preco_atual")
                    avaliacao = "Preço não identificado no HTML."

                    if preco_atual is not None:
                        id_produto = f"{item['url']}::{nome_produto.strip().lower()}"
                        registrar_preco(conn, id_produto, nome_produto, preco_atual)
                        historico = analisar_historico_preco(conn, id_produto)
                        avaliacao = classificar_oferta(
                            preco_atual,
                            historico.get("media_30d"),
                            historico.get("menor_90d"),
                        )

                    resultados_finais.append(
                        {
                            "url": item["url"],
                            "nome": nome_produto,
                            "preco_atual": preco_atual,
                            "menor_30d": None,
                            "menor_90d": None,
                            "media_30d": None,
                            "avaliacao": avaliacao,
                            "contexto_ia": contexto_ia_por_avaliacao(avaliacao),
                            "loja": item.get("loja", host),
                            "confiavel": item.get("confiavel", False),
                        }
                    )
                except Exception as e_item:
                    print(f"⚠️ Erro ao processar item sem IA: {e_item}")
                    continue

    conn.close()

    # Normaliza saída do legado para o mesmo formato do fluxo Promotech,
    # evitando mensagem vazia no n8n quando houver fallback.
    if resultados_finais:
        resultados_finais.sort(
            key=lambda x: x.get("preco_atual")
            if x.get("preco_atual") is not None
            else float("inf")
        )

    resultados_compativeis = []
    vistos_link_saida = set()
    for item in resultados_finais:
        link_item = item.get("url")
        if not link_item or link_item in vistos_link_saida:
            continue
        vistos_link_saida.add(link_item)

        loja = item.get("loja") or "Fonte Web"
        if loja == "Fonte Web":
            try:
                host = urlparse(link_item).netloc
                if host:
                    loja = host.replace("www.", "")
            except Exception:
                pass

        item_compat = {
            "nome": item.get("nome", "Produto sem nome"),
            "preco": _formatar_preco_brl(item.get("preco_atual")),
            "loja": loja,
            "confiavel": item.get("confiavel", True),
            "preco_atual": item.get("preco_atual"),
            "menor_30d": item.get("menor_30d"),
            "menor_90d": item.get("menor_90d"),
            "avaliacao": item.get("avaliacao"),
            "link": link_item,
            "contexto_ia": item.get("contexto_ia"),
        }

        resultados_compativeis.append(
            {
                "url": link_item,
                "itens": [item_compat],
            }
        )

    resposta = {
        "resultados": resultados_compativeis,
    }
    if resultados_compativeis:
        resposta["melhor_oferta"] = resultados_compativeis[0]["itens"][0]
        resposta["primeira_oferta"] = {
            "preco_atual": resultados_finais[0].get("preco_atual"),
            "menor_30d": resultados_finais[0].get("menor_30d"),
            "menor_90d": resultados_finais[0].get("menor_90d"),
            "avaliacao": resultados_finais[0].get("avaliacao"),
            "contexto_ia": resultados_finais[0].get("contexto_ia"),
        }

    return resposta

    

if __name__ == "__main__":
    # Modo CLI: imprime apenas JSON no console (sem logs extras), ideal para n8n.
    if len(sys.argv) > 1:
        termo_cli = " ".join(sys.argv[1:])
        print(json.dumps(processar_busca_promotech(termo_cli), ensure_ascii=False))
    else:
        uvicorn.run(app, host="0.0.0.0", port=8000)