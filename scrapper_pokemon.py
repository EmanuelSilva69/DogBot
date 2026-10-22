"""
Scraper do MYP Cards — cartas Pokémon (https://mypcards.com/pokemon).

Expõe três endpoints consumidos pelo bot (bot/tools.py):

  POST /buscar_pokemon    -> busca de cartas com preço, condição, vendedor etc.
  POST /detalhar_pokemon  -> análise profunda de uma carta específica
  POST /historico_pokemon -> série histórica de preços (coleta local + tendência do site)
  GET  /health            -> healthcheck do Docker

O site do MYP Cards renderiza tudo no servidor (HTML), então usamos
requests + BeautifulSoup, com fallback Playwright quando o requests falhar.
"""

import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import List, Optional
from urllib.parse import urljoin

import requests
import uvicorn
from bs4 import BeautifulSoup
from fastapi import FastAPI
from pydantic import BaseModel, Field

try:
    from playwright.sync_api import sync_playwright
except Exception:  # playwright é opcional (fallback)
    sync_playwright = None

app = FastAPI(title="Scraper MYP Cards - Pokemon")

# ────────────────────────────── Configuração ──────────────────────────────

if os.getenv("PRICE_DB_PATH"):
    DB_PATH = os.getenv("PRICE_DB_PATH")
elif os.path.exists("/.dockerenv"):
    DB_PATH = "/app/data/prices_history.db"
else:
    DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "prices_history.db")

MYP_BASE_URL = os.getenv("MYP_BASE_URL", "https://mypcards.com").rstrip("/")
MYP_TIMEOUT = int(os.getenv("MYP_TIMEOUT", "40"))
MYP_MAX_PAGINAS_OFERTAS = int(os.getenv("MYP_MAX_PAGINAS_OFERTAS", "2"))
MYP_CACHE_TTL = int(os.getenv("MYP_CACHE_TTL", "180"))  # segundos

# ── Monitoramento de descontos ──
# Queda mínima (%), vs. última coleta e vs. preço TCG Player, para avisar.
DESCONTO_MIN_PCT = float(os.getenv("DESCONTO_MIN_PCT", "15"))
DESCONTO_TCG_PCT = float(os.getenv("DESCONTO_TCG_PCT", "20"))
# Máx. de cartas distintas verificadas por ciclo do scheduler (protege o site).
WATCH_MAX_POR_CICLO = int(os.getenv("WATCH_MAX_POR_CICLO", "40"))
# Intervalo entre requisições ao MYP dentro de um ciclo (segundos, ser cortês).
WATCH_DELAY_REQ = float(os.getenv("WATCH_DELAY_REQ", "0.4"))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Cache simples em memória para não martelar o site no mesmo ciclo de conversa.
_CACHE: dict = {}


# ────────────────────────────── Helpers de preço ──────────────────────────────

def normalizar_preco(valor) -> Optional[float]:
    """Converte 'R$ 1.234,56', '1234.56', 1000 etc. em float. Aceita valores baixos (cartas)."""
    if isinstance(valor, (int, float)):
        return float(valor)
    if not valor:
        return None

    match = re.search(r"\d[\d\.,]*", str(valor))
    if not match:
        return None

    bruto = match.group(0)
    if "," in bruto and "." in bruto:
        # pt-BR: 1.234,56 | en-US: 1,234.56
        if bruto.rfind(",") > bruto.rfind("."):
            bruto = bruto.replace(".", "").replace(",", ".")
        else:
            bruto = bruto.replace(",", "")
    elif "," in bruto:
        # Última vírgula é decimal: 1234,56 (também cobre 1.234,56 sem ponto...)
        if bruto.count(",") == 1 and len(bruto.split(",")[-1]) <= 2:
            bruto = bruto.replace(".", "").replace(",", ".")
        else:
            bruto = bruto.replace(",", "")
    elif bruto.count(".") > 1:
        bruto = bruto.replace(".", "")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})", bruto):
        # "1.234" sem decimais -> separador de milhar
        bruto = bruto.replace(".", "")

    try:
        preco = float(bruto)
    except ValueError:
        return None
    if preco <= 0 or preco > 5_000_000:
        return None
    return round(preco, 2)


def formatar_brl(valor: Optional[float]) -> str:
    if valor is None:
        return "N/A"
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def normalizar_texto(texto: str) -> str:
    """Normaliza acentos/caixa para comparação de termos."""
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return " ".join(texto.lower().split())


# ── Selados (caixas, boosters, ETB, mini box, lata, deck) ──
# O MYP guarda produtos selados em /outros/pokemon/selados[/<subcategoria>],
# separados das cartas avulsas (/pokemon).
_SELADO_SYN = (
    "selado", "selada", "box", "caixa", "booster", "etb", "lata", "tin",
    "deck", "mini box", "treinador", "premium collection", "master deck",
)
# Subcategorias reais do site (slug -> sinônimos que mapeiam pra elas)
_SELADO_SUBS = {
    "booster": ("booster", "pacote", "pack"),
    "caixa-de-booster": ("caixa de booster", "caixa booster", "booster box", "caixa de boosters"),
    "lata-mega-lata": ("lata", "mega lata", "tin", "tins"),
    "decks-promocionais": ("deck promocional", "deck", "baralho", "decks"),
}


def eh_produto_selado(tipo: str) -> bool:
    """Detecta se o pedido é por produto selado (caixa/booster/ETB/mini box/lata/deck)."""
    t = normalizar_texto(tipo or "")
    if not t:
        return False
    return any(s in t for s in _SELADO_SYN)


def _url_selados(subcategoria: Optional[str]) -> str:
    """Monta a URL da seção de selados, aplicando subcategoria quando reconhecida."""
    base = f"{MYP_BASE_URL}/outros/pokemon/selados"
    sub_n = normalizar_texto(subcategoria or "")
    if not sub_n:
        return base
    for slug, sins in _SELADO_SUBS.items():
        if sub_n == slug or sub_n in sins:
            return f"{base}/{slug}"
    return base


# Palavras de tipo/conector removidas do termo em busca de selado (o tipo já
# escopa a seção; o site busca melhor só pelo nome do produto).
_SELADO_LIMPAR = {
    "caixa", "caixas", "box", "boxes", "booster", "boosters", "etb", "elite",
    "trainer", "treinador", "lata", "latas", "tin", "tins", "deck", "decks",
    "baralho", "baralhos", "mini", "selado", "selada", "selados", "seladas",
    "lacrado", "lacrada", "lacrados", "lacradas", "produto", "produtos",
    "de", "da", "do", "dos", "das", "para", "pra", "com", "e",
}


def _limpar_termo_selado(termo: str) -> str:
    """Reduz o termo ao nome do produto (ex.: 'caixa do charizard' -> 'charizard')."""
    palavras = normalizar_texto(termo or "").split()
    sobra = [p for p in palavras if p not in _SELADO_LIMPAR]
    return " ".join(sobra)


# ────────────────────────────── HTTP ──────────────────────────────

def _baixar_html_com_playwright(url: str) -> str:
    """Fallback headless quando o requests é bloqueado/falha."""
    if sync_playwright is None:
        return ""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=HEADERS["User-Agent"], locale="pt-BR")
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=MYP_TIMEOUT * 1000)
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            html = page.content()
            browser.close()
            return html or ""
    except Exception as e:
        print(f"[MYP] Playwright falhou em {url}: {e}")
        return ""


def baixar_html(url: str, params: Optional[dict] = None, usar_cache: bool = True) -> str:
    """Baixa o HTML de uma página do MYP Cards (com cache TTL e retry)."""
    cache_key = f"{url}?{sorted((params or {}).items())}" if params else url
    if usar_cache:
        entrada = _CACHE.get(cache_key)
        if entrada and (datetime.now(timezone.utc) - entrada[0]).total_seconds() < MYP_CACHE_TTL:
            return entrada[1]

    html = ""
    for tentativa in range(3):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=MYP_TIMEOUT)
            if resp.status_code == 200 and resp.text:
                html = resp.text
                break
            print(f"[MYP] HTTP {resp.status_code} em {resp.url}")
        except Exception as e:
            print(f"[MYP] Tentativa {tentativa + 1} falhou em {url}: {e}")
            if tentativa < 2:
                time.sleep(2 * (tentativa + 1))
    if not html:
        html = _baixar_html_com_playwright(url if not params else requests.Request("GET", url, params=params).prepare().url)

    if html and usar_cache:
        _CACHE[cache_key] = (datetime.now(timezone.utc), html)
    return html


# ────────────────────────────── Banco (histórico) ──────────────────────────────

def conectar_banco():
    """SQLite com a série histórica de preços das cartas do MYP Cards."""
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS myp_price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            produto_id TEXT NOT NULL,
            nome TEXT NOT NULL,
            preco_menor REAL NOT NULL,
            preco_medio REAL,
            preco_tcg REAL,
            qtd_ofertas INTEGER,
            data_coleta TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_myp_history_produto_data
        ON myp_price_history (produto_id, data_coleta)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS myp_watchlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_jid TEXT NOT NULL,
            produto_id TEXT NOT NULL,
            nome TEXT NOT NULL,
            url TEXT,
            preco_ref REAL,
            ultima_coleta REAL,
            notificado_preco REAL,
            ativo INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_checked_at TEXT,
            UNIQUE(user_jid, produto_id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_myp_watch_user
        ON myp_watchlist (user_jid, ativo)
        """
    )
    conn.commit()
    return conn


def registrar_coleta(conn, produto_id: str, nome: str, preco_menor: float,
                     preco_medio: Optional[float], preco_tcg: Optional[float],
                     qtd_ofertas: Optional[int]) -> bool:
    """
    Registra um snapshot. Evita duplicidade: se já existe coleta do mesmo
    produto nas últimas 2 horas, não insere de novo (mantém a série limpa).
    """
    janela = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    existe = conn.execute(
        "SELECT 1 FROM myp_price_history WHERE produto_id = ? AND data_coleta >= ? LIMIT 1",
        (produto_id, janela),
    ).fetchone()
    if existe:
        return False

    conn.execute(
        """
        INSERT INTO myp_price_history
            (produto_id, nome, preco_menor, preco_medio, preco_tcg, qtd_ofertas, data_coleta)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            produto_id,
            nome,
            float(preco_menor),
            float(preco_medio) if preco_medio is not None else None,
            float(preco_tcg) if preco_tcg is not None else None,
            int(qtd_ofertas) if qtd_ofertas is not None else None,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    return True


def analisar_historico(conn, produto_id: str, dias: int = 90) -> dict:
    """Métricas da série local de preços (menor preço da carta por coleta)."""
    desde = (datetime.now(timezone.utc) - timedelta(days=dias)).isoformat()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT preco_menor, data_coleta, qtd_ofertas
        FROM myp_price_history
        WHERE produto_id = ? AND data_coleta >= ?
        ORDER BY data_coleta ASC
        """,
        (produto_id, desde),
    )
    linhas = cursor.fetchall()
    if not linhas:
        return {
            "qtd_coletas": 0,
            "primeira_coleta": None,
            "ultima_coleta": None,
            "menor_preco": None,
            "maior_preco": None,
            "media_preco": None,
            "preco_primeira_coleta": None,
            "preco_ultima_coleta": None,
            "variacao_pct": None,
            "serie": [],
            "observacao": (
                "Ainda não tenho coletas salvas desta carta. "
                "A coleta de hoje foi registrada; consulte de novo mais tarde para montar a série."
            ),
        }

    precos = [float(l[0]) for l in linhas]
    primeira, ultima = linhas[0], linhas[-1]
    variação = None
    if len(precos) >= 2 and primeira[0]:
        variação = round((precos[-1] - primeira[0]) / primeira[0] * 100, 1)

    serie = [
        {"data": l[1][:10], "preco": float(l[0]), "ofertas": l[2]}
        for l in linhas[-12:]
    ]

    return {
        "qtd_coletas": len(precos),
        "primeira_coleta": primeira[1][:10],
        "ultima_coleta": ultima[1][:10],
        "menor_preco": round(min(precos), 2),
        "maior_preco": round(max(precos), 2),
        "media_preco": round(sum(precos) / len(precos), 2),
        "preco_primeira_coleta": round(float(primeira[0]), 2),
        "preco_ultima_coleta": round(float(ultima[0]), 2),
        "variacao_pct": variação,
        "serie": serie,
        "observacao": None,
    }


# ────────────────────── Watchlist (monitoramento de descontos) ──────────────────────

def _extrair_produto_id(url: str) -> str:
    """Extrai o id estável do produto a partir da URL (ou usa a própria URL)."""
    m = re.search(r"/produto/(\d+)", url or "")
    return m.group(1) if m else (url or "")


def adicionar_watch(conn, user_jid: str, produto_id: str, nome: str,
                    url: Optional[str], preco_ref: Optional[float]) -> dict:
    """Registra (ou reativa) uma carta na watchlist de um usuário.

    Idempotente: se já existe, atualiza nome/url e reativa, mas preserva o
    histórico de notificação para não re-avisar o mesmo preço.
    """
    if not user_jid or not produto_id:
        return {"ok": False, "erro": "user_jid e produto_id são obrigatórios."}

    agora = datetime.now(timezone.utc).isoformat()
    existe = conn.execute(
        "SELECT id, ativo FROM myp_watchlist WHERE user_jid = ? AND produto_id = ?",
        (user_jid, produto_id),
    ).fetchone()

    if existe:
        conn.execute(
            """
            UPDATE myp_watchlist
            SET nome = ?, url = COALESCE(?, url), ativo = 1,
                preco_ref = COALESCE(?, preco_ref)
            WHERE id = ?
            """,
            (nome, url, preco_ref, existe[0]),
        )
        conn.commit()
        return {"ok": True, "reativada": True, "nome": nome}

    conn.execute(
        """
        INSERT INTO myp_watchlist
            (user_jid, produto_id, nome, url, preco_ref, notificado_preco, ativo, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 1, ?)
        """,
        (user_jid, produto_id, nome, url, preco_ref, preco_ref, agora),
    )
    conn.commit()
    return {"ok": True, "reativada": False, "nome": nome}


def remover_watch(conn, user_jid: str, termo: str) -> dict:
    """Remove da watchlist do usuário as cartas cujo nome casa com o termo."""
    termo_n = normalizar_texto(termo)
    linhas = conn.execute(
        "SELECT id, nome FROM myp_watchlist WHERE user_jid = ? AND ativo = 1",
        (user_jid,),
    ).fetchall()

    removidas = []
    for rid, rnome in linhas:
        if termo_n and termo_n in normalizar_texto(rnome):
            conn.execute("UPDATE myp_watchlist SET ativo = 0 WHERE id = ?", (rid,))
            removidas.append(rnome)
    conn.commit()
    return {"ok": True, "removidas": removidas}


def listar_watch(conn, user_jid: str) -> List[dict]:
    linhas = conn.execute(
        """
        SELECT nome, url, preco_ref, ultima_coleta, last_checked_at
        FROM myp_watchlist
        WHERE user_jid = ? AND ativo = 1
        ORDER BY created_at DESC
        """,
        (user_jid,),
    ).fetchall()
    return [
        {
            "nome": l[0],
            "url": l[1],
            "preco_ref": l[2],
            "ultima_coleta": l[3],
            "ultima_verificacao": (l[4][:10] if l[4] else None),
        }
        for l in linhas
    ]


def _sinais_desconto(detalhe: dict, preco_atual: float,
                     baseline: Optional[float]) -> dict:
    """Avalia os 3 sinais de desconto de uma carta e devolve o resumo."""
    sinais = []
    preco_de = None

    # 1) Queda vs. baseline (última coleta / preço de referência do usuário)
    queda_pct = None
    if baseline and baseline > 0 and preco_atual < baseline:
        queda_pct = round((baseline - preco_atual) / baseline * 100, 1)
        if queda_pct >= DESCONTO_MIN_PCT:
            sinais.append("queda")

    # 2) Badge de promoção do próprio MYP (oferta com preço "de" maior)
    melhor = detalhe.get("melhor_oferta") or {}
    preco_de = melhor.get("preco_de")
    preco_de_valor = normalizar_preco(preco_de) if preco_de else None
    promo_pct = None
    if preco_de_valor and preco_de_valor > preco_atual:
        promo_pct = round((preco_de_valor - preco_atual) / preco_de_valor * 100, 1)
        if promo_pct >= DESCONTO_MIN_PCT:
            sinais.append("promocao")

    # 3) Abaixo do preço de mercado TCG Player
    tcg_pct = None
    tcg = detalhe.get("preco_tcg_valor")
    if tcg and tcg > 0 and preco_atual < tcg:
        tcg_pct = round((tcg - preco_atual) / tcg * 100, 1)
        if tcg_pct >= DESCONTO_TCG_PCT:
            sinais.append("tcg_player")

    return {
        "sinais": sinais,
        "queda_pct": queda_pct,
        "promo_pct": promo_pct,
        "tcg_pct": tcg_pct,
        "preco_tcg": tcg,
        "preco_de": preco_de,
    }


def verificar_descontos() -> dict:
    """Percorre a watchlist, refaz o preço de cada carta e detecta descontos.

    Compara com a última coleta do usuário (queda), com o preço "de" do site
    (promoção) e com o preço TCG Player. Atualiza a base e devolve os eventos
    para o bot notificar cada usuário no privado.
    """
    conn = conectar_banco()
    try:
        linhas = conn.execute(
            """
            SELECT id, user_jid, produto_id, nome, url, ultima_coleta, notificado_preco
            FROM myp_watchlist
            WHERE ativo = 1
            ORDER BY last_checked_at IS NULL DESC, last_checked_at ASC
            LIMIT ?
            """,
            (WATCH_MAX_POR_CICLO,),
        ).fetchall()

        eventos = []
        verificados = 0
        agora = datetime.now(timezone.utc).isoformat()

        # Agrupa por url p/ não repetir scraping quando vários usuários assistem a mesma carta
        por_url: dict = {}
        for lid_, user_jid, produto_id, nome, url, ultima_coleta, notif_preco in linhas:
            chave = url or produto_id
            por_url.setdefault(chave, []).append(
                (user_jid, nome, ultima_coleta, notif_preco)
            )

        for chave, users in por_url.items():
            url = chave if str(chave).startswith("http") else None
            termo = None if url else chave  # sem url -> usa produto_id/nome como termo
            try:
                detalhe = resolver_carta(url or termo, limite_ofertas=3)
            except Exception as e:
                print(f"[MYP] Erro verificando '{chave}': {e}")
                detalhe = {"erro": str(e)}
            if detalhe.get("erro") or detalhe.get("preco_menor_valor") is None:
                continue

            verificados += 1
            preco_atual = float(detalhe["preco_menor_valor"])
            produto_id = _extrair_produto_id(detalhe.get("url") or chave)
            nome_carta = detalhe.get("titulo_completo") or detalhe.get("nome")
            url_carta = detalhe.get("url") or (chave if str(chave).startswith("http") else None)

            for user_jid, nome_user, ultima_coleta, notif_preco in users:
                baseline = ultima_coleta if ultima_coleta is not None else notif_preco
                resumo = _sinais_desconto(detalhe, preco_atual, baseline)

                # Anti-spam: só avisa se caiu abaixo do último preço avisado.
                ja_avisou_mais_barato = (
                    notif_preco is not None and preco_atual >= notif_preco
                )

                if resumo["sinais"] and not ja_avisou_mais_barato:
                    eventos.append({
                        "user_jid": user_jid,
                        "nome": nome_carta or nome_user,
                        "url": url_carta,
                        "preco_atual": preco_atual,
                        "preco_anterior": baseline,
                        "queda_pct": resumo["queda_pct"],
                        "promo_pct": resumo["promo_pct"],
                        "tcg_pct": resumo["tcg_pct"],
                        "preco_tcg": resumo["preco_tcg"],
                        "preco_de": resumo["preco_de"],
                        "sinais": resumo["sinais"],
                    })
                    novo_notif = preco_atual
                else:
                    novo_notif = notif_preco

                conn.execute(
                    """
                    UPDATE myp_watchlist
                    SET ultima_coleta = ?, notificado_preco = ?, last_checked_at = ?
                    WHERE user_jid = ? AND produto_id = ?
                    """,
                    (preco_atual, novo_notif, agora, user_jid, produto_id),
                )

            # registra snapshot no histórico global (série de preços)
            registrar_coleta(
                conn,
                produto_id=produto_id,
                nome=nome_carta,
                preco_menor=preco_atual,
                preco_medio=detalhe.get("preco_medio_valor"),
                preco_tcg=detalhe.get("preco_tcg_valor"),
                qtd_ofertas=detalhe.get("qtd_ofertas"),
            )

            if WATCH_DELAY_REQ:
                time.sleep(WATCH_DELAY_REQ)  # cortês com o MYP Cards

        conn.commit()
        return {"verificados": verificados, "eventos": eventos}
    finally:
        conn.close()


def _auto_watch(user_jid: str, url: Optional[str], nome: Optional[str],
                preco_ref: Optional[float]) -> None:
    """Registra em silêncio uma carta na watchlist do usuário (auto-monitoramento)."""
    if not user_jid or not url:
        return
    try:
        conn = conectar_banco()
        try:
            adicionar_watch(conn, user_jid, _extrair_produto_id(url), nome or url, url, preco_ref)
        finally:
            conn.close()
    except Exception as e:
        print(f"[MYP] Falha ao auto-monitorar '{nome}': {e}")


# ────────────────────────────── Parsing: listagem ──────────────────────────────

def _extrair_numero(titulo: str) -> Optional[str]:
    m = re.search(r"\((\d+(?:\s*\/\s*|\s*-\s*)\d+[a-z]?)\)\s*$", titulo or "", re.IGNORECASE)
    return m.group(1).strip() if m else None


def _limpar_nome(titulo: str) -> str:
    return re.sub(r"\s*\([^)]*\)\s*$", "", titulo or "").strip() or (titulo or "").strip()


def _extrair_cards(soup: BeautifulSoup) -> List[dict]:
    """Extrai os cards da listagem (cards avulsos + itens marketplace)."""
    itens = []
    for card in soup.select("div.card"):
        anchor = card.select_one("a.card-img-link")
        if not anchor or not anchor.get("href"):
            continue

        href = anchor["href"]
        url = urljoin(MYP_BASE_URL, href)
        eh_card_avulso = href.startswith("/pokemon/produto/")

        h3 = card.select_one(".card-name h3")
        titulo = (h3.get("title") if h3 else None) or (h3.get_text(" ", strip=True) if h3 else "")
        if not titulo:
            titulo = (card.get("data-produto") or "").strip()
        if not titulo:
            continue

        edicao_el = card.select_one(".card-edicao")
        edicao_sigla = (edicao_el.get_text(strip=True) if edicao_el else "") or None
        edicao_nome = (edicao_el.get("title") if edicao_el else None) or edicao_sigla

        preco_el = card.select_one(".card-preco")
        preco_valor = normalizar_preco(preco_el.get_text(" ", strip=True)) if preco_el else None

        preco_de = None
        promo_el = card.select_one("p.text-sm.vermelho span[title]")
        if promo_el and promo_el.get("title"):
            preco_de = normalizar_preco(promo_el.get_text(" ", strip=True)) or None

        # data-resumo: JSON com condição, idioma, versão, vendedor, graduação
        resumo = {}
        resumo_raw = card.get("data-resumo")
        if resumo_raw:
            try:
                resumo = json.loads(resumo_raw)
            except Exception:
                resumo = {}

        qualidade = (resumo.get("qualidade") or {}).get("nome")
        idioma = (resumo.get("idioma") or {}).get("nome")
        versao = resumo.get("versao")
        graduacao = (resumo.get("graduacao") or {}).get("rotulo")
        vendedor = (resumo.get("vendedor") or {}).get("nick") or card.get("data-vendedor")
        tipo_vendedor = (resumo.get("vendedor") or {}).get("tipo")
        quantidade = resumo.get("quantidade")
        observacao = resumo.get("obs")

        # fallback quando o card não tem data-resumo
        if not qualidade:
            chip = card.select_one(".card-qualidade .chip-inline, .card-qualidade span[title]")
            if chip:
                qualidade = chip.get("title")
        if not idioma:
            cond = card.select_one(".card-condicao span")
            if cond:
                idioma = cond.get_text(strip=True)
        if not quantidade:
            qtd_el = card.select_one(".card-estatistica .quantidade-num")
            if qtd_el:
                quantidade = f"{qtd_el.get_text(strip=True)} un."

        badge = card.select_one(".badges-top-left-corner")
        promocao = bool(badge and "promoção" in badge.get_text(" ", strip=True).lower())

        img = card.select_one("img.card-thumb")

        itens.append({
            "nome": _limpar_nome(titulo),
            "titulo_completo": titulo.strip(),
            "numero": _extrair_numero(titulo),
            "codigo": card.get("data-ga-item-id"),
            "edicao": edicao_nome,
            "edicao_sigla": edicao_sigla,
            "preco": formatar_brl(preco_valor),
            "preco_valor": preco_valor,
            "preco_de": formatar_brl(preco_de) if preco_de else None,
            "condicao": qualidade,
            "idioma": idioma,
            "versao": versao,
            "graduacao": graduacao,
            "quantidade": quantidade,
            "vendedor": vendedor,
            "tipo_vendedor": tipo_vendedor,
            "observacao": observacao,
            "promocao": promocao,
            "url": url,
            "imagem": img.get("src") if img else None,
            "card_avulso": eh_card_avulso,
        })
    return itens


def buscar_cartas(termo: str, preco_minimo: Optional[float] = None,
                  preco_maximo: Optional[float] = None, pagina: int = 1,
                  limite: int = 6, tipo: str = "carta",
                  subcategoria: Optional[str] = None) -> dict:
    """Busca no catálogo do MYP Cards e devolve cartas/produtos ranqueados.

    tipo='carta' (padrão): seção /pokemon (cartas avulsas).
    tipo='selado' (box, booster, ETB, mini box, lata, deck): seção
    /outros/pokemon/selados, com subcategoria opcional p/ afinar.
    """
    termo = (termo or "").strip()
    if not termo:
        return {"resultados": [], "erro": "Termo de busca vazio."}

    selado = eh_produto_selado(tipo)
    if selado:
        # O tipo já escopa a seção; busca melhor só pelo nome do produto.
        termo = _limpar_termo_selado(termo) or termo
        url = _url_selados(subcategoria)
        params = {"ProdutoSearch[query]": termo}
    else:
        url = f"{MYP_BASE_URL}/pokemon"
        params = {
            "ProdutoSearch[query]": termo,
            "ProdutoSearch[marca]": "pokemon",
        }

    if pagina and pagina > 1:
        params["page"] = pagina
    if preco_minimo is not None:
        params["ProdutoSearch[precoMinimo]"] = f"{preco_minimo:.2f}".replace(".", ",")
    if preco_maximo is not None:
        params["ProdutoSearch[precoMaximo]"] = f"{preco_maximo:.2f}".replace(".", ",")

    html = baixar_html(url, params=params)
    if not html:
        return {"resultados": [], "erro": "Não consegui acessar o MYP Cards agora (site bloqueou ou está fora)."}

    soup = BeautifulSoup(html, "html.parser")
    itens = _extrair_cards(soup)

    # Filtragem local de preço (reforço do filtro do site)
    filtrados = []
    for item in itens:
        preco = item.get("preco_valor")
        if preco_minimo is not None and preco is not None and preco < preco_minimo:
            continue
        if preco_maximo is not None and preco is not None and preco > preco_maximo:
            continue
        filtrados.append(item)

    # Relevância: cards avulsos primeiro e depois a ordem do próprio site
    filtrados.sort(key=lambda i: not i.get("card_avulso"))

    termo_n = normalizar_texto(termo)
    for item in filtrados:
        alvo = normalizar_texto(f"{item['nome']} {item.get('numero') or ''} {item.get('edicao') or ''}")
        item["relevancia"] = round(
            1.0 if termo_n and termo_n in alvo else SequenceMatcher(None, termo_n, alvo).ratio(), 3
        )

    filtrados.sort(key=lambda i: (not i.get("card_avulso"), -i.get("relevancia", 0)))
    resultados = filtrados[:limite]

    if not resultados:
        rotulo = "produto selado (caixa/booster/ETB)" if selado else "carta"
        return {
            "resultados": [],
            "erro": f"Nenhum {rotulo} encontrado para '{termo}' no MYP Cards.",
            "dica": "Tente um termo mais genérico (ex.: 'charizard' em vez do nome completo).",
        }

    com_preco = [r for r in resultados if r.get("preco_valor") is not None]
    melhor = min(com_preco, key=lambda r: r["preco_valor"]) if com_preco else None

    return {
        "fonte": "mypcards",
        "tipo": "selado" if selado else "carta",
        "termo": termo,
        "pagina": pagina,
        "total_encontrados": len(filtrados),
        "resultados": resultados,
        "melhor_oferta": melhor,
        "total_pagina": len(itens),
    }


# ────────────────────────────── Parsing: produto ──────────────────────────────

def _extrair_tendencia(soup: BeautifulSoup) -> Optional[dict]:
    el = soup.select_one(".produto-tendencia")
    if not el:
        return None
    titulo = el.get("title") or ""
    texto = el.get_text(" ", strip=True)
    classes = el.get("class") or []

    direcao = "estavel"
    if any("baixa" in c for c in classes):
        direcao = "baixa"
    elif any("alta" in c for c in classes):
        direcao = "alta"

    variacao = None
    m = re.search(r"(\d+[,.]\d+|\d+)\s*%", texto) or re.search(r"(\d+[,.]\d+|\d+)\s*%", titulo)
    if m:
        variacao = float(m.group(1).replace(",", "."))

    return {
        "direcao": direcao,
        "variacao_pct": variacao,
        "texto": texto,
        "descricao": titulo.replace("Ver histórico de preço", "").strip(" ."),
    }


def _extrair_ofertas(soup: BeautifulSoup, url: str, limite: int = 10) -> List[dict]:
    """Ofertas (vendedores) da página do produto, da mais barata para a mais cara."""
    produto_id = None
    m_id = re.search(r"/produto/(\d+)", url or "")
    if m_id:
        produto_id = m_id.group(1)

    ofertas = []
    for tr in soup.select("table tbody tr[data-key]"):
        preco_td = tr.select_one("td.estoque-lista-precoestoque")
        if not preco_td:
            continue

        preco_promo_el = preco_td.select_one(".moeda-promocao")
        preco_promo_txt = preco_promo_el.get_text(strip=True) if preco_promo_el else None
        texto_preco = preco_td.get_text(" ", strip=True)
        if preco_promo_txt:
            texto_preco = texto_preco.replace(preco_promo_txt, "", 1)
        preco = normalizar_preco(texto_preco)
        if preco is None:
            continue

        preco_antigo = normalizar_preco(preco_promo_txt) if preco_promo_txt else None

        vendedor_a = tr.select_one("a.nome-vendedor-apelido")
        vendedor = vendedor_a.get_text(strip=True) if vendedor_a else None

        tipo_vendedor_el = tr.select_one(".vendedor-identidade span[title]")
        tipo_vendedor = tipo_vendedor_el.get("title") if tipo_vendedor_el else None

        chip = tr.select_one(".qualidade-texto .chip-inline")
        flag = tr.select_one(".qualidade-texto .flag-icon")

        versao_td = tr.select_one("td.estoque-lista-nomeenfoil")
        qtd_td = tr.select_one("td.estoque-lista-quantidadeestoque")
        obs = tr.select_one(".badge-obs")

        ofertas.append({
            "vendedor": vendedor,
            "tipo_vendedor": tipo_vendedor,
            "preco": formatar_brl(preco),
            "preco_valor": preco,
            "preco_de": formatar_brl(preco_antigo) if preco_antigo else None,
            "condicao": chip.get("title") if chip else None,
            "idioma": flag.get("title") if flag else None,
            "versao": versao_td.get_text(strip=True) if versao_td else None,
            "quantidade": qtd_td.get_text(strip=True) if qtd_td else None,
            "observacao": obs.get("title") if obs else None,
            "mensagem_vendedor": (
                f"{MYP_BASE_URL}/{vendedor}/mensagem?idproduto={produto_id}"
                if vendedor and produto_id
                else (f"{MYP_BASE_URL}/{vendedor}/mensagem" if vendedor else None)
            ),
        })

    # remove duplicatas (mesmo vendedor + preço + versão) e ordena por preço
    vistos, unicas = set(), []
    for oferta in ofertas:
        chave = (oferta.get("vendedor"), oferta.get("preco_valor"), oferta.get("versao"))
        if chave in vistos:
            continue
        vistos.add(chave)
        unicas.append(oferta)

    unicas.sort(key=lambda o: o["preco_valor"])
    return unicas[:limite]


def _extrair_detalhes(soup: BeautifulSoup) -> dict:
    detalhes = {}
    for block in soup.select("#produto-detalhe .view-field"):
        label = block.find("label")
        if label:
            nome = label.extract().get_text(strip=True)
            detalhes[nome] = block.get_text(" ", strip=True).replace("\xa0", "").strip()

    descricao = None
    desc_el = soup.select_one("#produto-detalhe .descricao-content .descricao")
    if desc_el:
        descricao = re.sub(r"\s+", " ", desc_el.get_text(" ", strip=True)).strip()
    return {"campos": detalhes, "descricao": descricao}


def _extrair_contador_abas(soup: BeautifulSoup, aba: str) -> Optional[int]:
    btn = soup.select_one(f'button.abas-aba[data-aba="{aba}"]')
    if not btn:
        return None
    m = re.search(r"\((\d+)\)", btn.get_text(" ", strip=True))
    return int(m.group(1)) if m else None


def analisar_produto(html: str, url: str, limite_ofertas: int = 10) -> Optional[dict]:
    """Extrai tudo o que a página de produto oferece sem login."""
    soup = BeautifulSoup(html, "html.parser")

    h1 = soup.select_one("h1#produto-nome")
    nome = h1.get_text(strip=True) if h1 else None
    if not nome:
        return None

    # JSON-LD (fonte confiável de preço e SKU)
    jsonld = {}
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            payload = json.loads(script.string or "")
        except Exception:
            continue
        if isinstance(payload, dict) and payload.get("@type") == "Product":
            jsonld = payload
            break

    # Código e edição
    codigo, edicao, edicao_url = None, None, None
    for block in soup.select("#produto-codigo .view-field"):
        label = block.find("label")
        if not label:
            continue
        rotulo = label.extract().get_text(strip=True)
        valor = block.get_text(" ", strip=True)
        if rotulo.lower() == "código":
            codigo = valor
        elif rotulo.lower() == "edição":
            edicao = valor
            a = block.find("a", href=True)
            if a:
                edicao_url = urljoin(MYP_BASE_URL, a["href"])

    # Resumo de preços
    preco_menor = preco_medio = preco_tcg = None
    menor_el = soup.select_one(".resumo-preco--menor")
    if menor_el:
        preco_menor = normalizar_preco(menor_el.get_text(" ", strip=True))
        m = re.search(r"Preço médio\s*([\d\.,]+)", menor_el.get("title") or "")
        if m:
            preco_medio = normalizar_preco(m.group(1))
    tcg_el = soup.select_one(".resumo-preco--tcg")
    if tcg_el:
        preco_tcg = normalizar_preco(tcg_el.get_text(" ", strip=True))

    if preco_menor is None:
        preco_menor = normalizar_preco((jsonld.get("offers") or {}).get("price"))

    ofertas = _extrair_ofertas(soup, url, limite=limite_ofertas)
    detalhes = _extrair_detalhes(soup)
    tendencia = _extrair_tendencia(soup)

    meta = soup.find("meta", attrs={"name": "description"})
    resumo_texto = meta.get("content") if meta else None

    wishlist = None
    wl = soup.select_one(".lista-desejo h3")
    if wl:
        m = re.search(r"(\d+)", wl.get_text(" ", strip=True))
        if m:
            wishlist = int(m.group(1))

    img = jsonld.get("image")
    if not img:
        og = soup.find("meta", attrs={"property": "og:image"})
        img = og.get("content") if og else None

    titulo = nome.strip()
    return {
        "fonte": "mypcards",
        "url": url,
        "nome": _limpar_nome(titulo),
        "titulo_completo": titulo,
        "numero": _extrair_numero(titulo),
        "codigo": codigo or jsonld.get("sku"),
        "edicao": edicao,
        "edicao_url": edicao_url,
        "tipo_produto": jsonld.get("product_type"),
        "imagem": img,
        "preco_menor": formatar_brl(preco_menor),
        "preco_menor_valor": preco_menor,
        "preco_medio": formatar_brl(preco_medio),
        "preco_medio_valor": preco_medio,
        "preco_tcg_player": formatar_brl(preco_tcg),
        "preco_tcg_valor": preco_tcg,
        "tendencia": tendencia,
        "qtd_ofertas": _extrair_contador_abas(soup, "ofertas"),
        "qtd_graduadas": _extrair_contador_abas(soup, "graduadas"),
        "ofertas_visiveis": len(ofertas),
        "ofertas": ofertas,
        "melhor_oferta": ofertas[0] if ofertas else None,
        "detalhes": detalhes.get("campos", {}),
        "descricao": detalhes.get("descricao"),
        "wishlist": wishlist,
        "resumo": resumo_texto,
    }


# ────────────────────────────── Resolução de carta ──────────────────────────────

def eh_url_produto(carta: str) -> bool:
    """Reconhece URL absoluta ou relativa (ex.: /pokemon/produto/300731/charizard ou
    /outros/produto/313041/box) do MYP Cards."""
    texto = (carta or "").strip()
    if "/produto/" not in texto:
        return False
    if texto.startswith(("/pokemon/produto/", "/outros/produto/", "/produto/")):
        return True
    return bool(re.match(r"^https?://", texto)) and "mypcards.com" in texto.lower()


def normalizar_url_produto(carta: str) -> str:
    """Converte caminhos relativos do MYP Cards em URL absoluta."""
    texto = (carta or "").strip()
    if texto.startswith(("/",)) and not texto.startswith("//"):
        return f"{MYP_BASE_URL}{texto}"
    return texto


def resolver_carta(carta: str, limite_ofertas: int = 10) -> dict:
    """
    Aceita URL direta (absoluta ou relativa) ou nome da carta.
    Quando é nome, busca no catálogo e escolhe o melhor match.
    """
    carta = (carta or "").strip()
    if not carta:
        return {"erro": "Informe o nome ou o link da carta."}

    if eh_url_produto(carta):
        url = normalizar_url_produto(carta)
        html = baixar_html(url)
        if not html:
            return {"erro": "Não consegui abrir a página da carta no MYP Cards."}
        detalhe = analisar_produto(html, url, limite_ofertas=limite_ofertas)
        if not detalhe:
            return {"erro": "A página da carta não retornou dados."}
        detalhe["alternativas"] = []
        return detalhe

    busca = buscar_cartas(carta, limite=20)
    candidatos = busca.get("resultados") or []
    if not candidatos:
        return {
            "erro": busca.get("erro") or f"Nenhuma carta encontrada para '{carta}'.",
            "dica": busca.get("dica"),
        }

    termo_n = normalizar_texto(carta)
    melhor = max(
        candidatos,
        key=lambda c: (
            0.5 if c.get("card_avulso") else 0.0
        ) + c.get("relevancia", 0),
    )

    html = baixar_html(melhor["url"])
    detalhe = analisar_produto(html, melhor["url"], limite_ofertas=limite_ofertas) if html else None
    if not detalhe:
        return {"erro": f"Encontrei '{melhor['nome']}' mas não consegui abrir a página."}

    # Alternativas caso o usuário tenha pedido outra versão da carta
    detalhe["alternativas"] = [
        {
            "nome": c["titulo_completo"],
            "preco": c["preco"],
            "edicao": c.get("edicao"),
            "url": c["url"],
        }
        for c in candidatos
        if c["url"] != melhor["url"]
    ][:5]
    detalhe["termo_busca"] = termo_n
    return detalhe


# ────────────────────────────── Endpoints ──────────────────────────────

class RequisicaoBuscaPokemon(BaseModel):
    termo: str = Field(description="Nome da carta ou termo de busca (ex.: charizard)")
    preco_minimo: Optional[float] = Field(default=None, description="Preço mínimo (R$)")
    preco_maximo: Optional[float] = Field(default=None, description="Preço máximo (R$)")
    pagina: int = Field(default=1, ge=1, le=100)
    limite: int = Field(default=6, ge=1, le=20)
    tipo: str = Field(default="carta", description="'carta' (avulsa) ou 'selado' (caixa, booster, ETB, mini box, lata, deck)")
    subcategoria: Optional[str] = Field(default=None, description="Selado: afinar p/ booster, caixa-de-booster, lata-mega-lata ou decks-promocionais")
    user_jid: Optional[str] = Field(default=None, description="Se presente, monitora o melhor resultado p/ avisar de desconto")


class RequisicaoCarta(BaseModel):
    carta: str = Field(description="Nome da carta (ex.: 'Mega Charizard X ex') ou URL do produto")
    limite_ofertas: int = Field(default=10, ge=1, le=30)
    user_jid: Optional[str] = Field(default=None, description="Se presente, monitora a carta p/ avisar de desconto")


class RequisicaoHistorico(BaseModel):
    carta: str = Field(description="Nome da carta ou URL do produto")
    dias: int = Field(default=90, ge=7, le=730, description="Janela do histórico local (dias)")


class RequisicaoWatchAdd(BaseModel):
    user_jid: str = Field(description="JID do usuário que quer ser avisado (ex.: 5598...@s.whatsapp.net)")
    url: Optional[str] = Field(default=None, description="URL do produto (caminho rápido, sem novo scraping)")
    carta: Optional[str] = Field(default=None, description="Nome da carta (resolve a URL automaticamente)")
    preco_ref: Optional[float] = Field(default=None, description="Preço atual conhecido (baseline para detectar queda)")


class RequisicaoWatchRemove(BaseModel):
    user_jid: str = Field(description="JID do usuário")
    termo: str = Field(description="Nome (ou parte) da carta a parar de monitorar")


class RequisicaoWatchList(BaseModel):
    user_jid: str = Field(description="JID do usuário")


@app.get("/health")
def health():
    return {"status": "ok", "service": "scrapper-pokemon", "base": MYP_BASE_URL}


@app.post("/buscar_pokemon")
def buscar_pokemon(req: RequisicaoBuscaPokemon):
    try:
        resultado = buscar_cartas(
            req.termo,
            preco_minimo=req.preco_minimo,
            preco_maximo=req.preco_maximo,
            pagina=req.pagina,
            limite=req.limite,
            tipo=req.tipo,
            subcategoria=req.subcategoria,
        )
        # Auto-monitora o melhor resultado para avisar o usuário de desconto.
        if req.user_jid and resultado.get("resultados"):
            top = resultado["resultados"][0]
            _auto_watch(req.user_jid, top.get("url"),
                        top.get("titulo_completo") or top.get("nome"),
                        top.get("preco_valor"))
        return resultado
    except Exception as e:
        print(f"[MYP] Erro em /buscar_pokemon: {e}")
        return {"resultados": [], "erro": f"Erro ao buscar cartas: {e}"}


@app.post("/detalhar_pokemon")
def detalhar_pokemon(req: RequisicaoCarta):
    try:
        resultado = resolver_carta(req.carta, limite_ofertas=req.limite_ofertas)
        if req.user_jid and not resultado.get("erro") and resultado.get("preco_menor_valor") is not None:
            _auto_watch(req.user_jid, resultado.get("url"),
                        resultado.get("titulo_completo") or resultado.get("nome"),
                        resultado.get("preco_menor_valor"))
        return resultado
    except Exception as e:
        print(f"[MYP] Erro em /detalhar_pokemon: {e}")
        return {"erro": f"Erro ao analisar a carta: {e}"}


@app.post("/historico_pokemon")
def historico_pokemon(req: RequisicaoHistorico):
    """
    Coleta o preço atual da carta, registra no SQLite local e devolve a série
    histórica + a tendência exibida pelo próprio MYP Cards.
    """
    try:
        detalhe = resolver_carta(req.carta, limite_ofertas=3)
        if detalhe.get("erro"):
            return detalhe

        preco_atual = detalhe.get("preco_menor_valor")
        if preco_atual is None:
            return {"erro": "Não consegui identificar o preço atual desta carta."}

        produto_id = detalhe.get("codigo") or detalhe.get("url")
        conn = conectar_banco()
        try:
            nova_coleta = registrar_coleta(
                conn,
                produto_id=produto_id,
                nome=detalhe.get("titulo_completo") or detalhe.get("nome"),
                preco_menor=preco_atual,
                preco_medio=detalhe.get("preco_medio_valor"),
                preco_tcg=detalhe.get("preco_tcg_valor"),
                qtd_ofertas=detalhe.get("qtd_ofertas"),
            )
            historico = analisar_historico(conn, produto_id, dias=req.dias)
        finally:
            conn.close()

        return {
            "fonte": "mypcards",
            "carta": {
                "nome": detalhe.get("titulo_completo") or detalhe.get("nome"),
                "url": detalhe.get("url"),
                "edicao": detalhe.get("edicao"),
            },
            "preco_atual": detalhe.get("preco_menor"),
            "preco_atual_valor": preco_atual,
            "preco_medio_site": detalhe.get("preco_medio"),
            "preco_tcg_player": detalhe.get("preco_tcg_player"),
            "qtd_ofertas": detalhe.get("qtd_ofertas"),
            "tendencia_site": detalhe.get("tendencia"),
            "historico_local": historico,
            "nova_coleta": nova_coleta,
            "observacao": (
                "Histórico local construído a cada consulta ao bot. "
                "O gráfico completo do MYP Cards exige login no site."
            ),
        }
    except Exception as e:
        print(f"[MYP] Erro em /historico_pokemon: {e}")
        return {"erro": f"Erro ao montar o histórico: {e}"}


@app.post("/watchlist/add")
def watchlist_add(req: RequisicaoWatchAdd):
    """Adiciona (ou reativa) uma carta na watchlist do usuário para avisar de desconto."""
    try:
        url = (req.url or "").strip()
        nome = None
        preco_ref = req.preco_ref

        if url:
            # Caminho rápido: URL já veio do search/detalhe — não refaz scraping.
            produto_id = _extrair_produto_id(url)
            nome = req.carta or produto_id
        elif req.carta:
            detalhe = resolver_carta(req.carta, limite_ofertas=2)
            if detalhe.get("erro"):
                return {"ok": False, "erro": detalhe["erro"]}
            url = detalhe.get("url")
            produto_id = _extrair_produto_id(url or req.carta)
            nome = detalhe.get("titulo_completo") or detalhe.get("nome")
            if preco_ref is None:
                preco_ref = detalhe.get("preco_menor_valor")
        else:
            return {"ok": False, "erro": "Informe 'url' ou 'carta'."}

        conn = conectar_banco()
        try:
            resultado = adicionar_watch(conn, req.user_jid, produto_id, nome, url, preco_ref)
        finally:
            conn.close()

        resultado["url"] = url
        return resultado
    except Exception as e:
        print(f"[MYP] Erro em /watchlist/add: {e}")
        return {"ok": False, "erro": f"Erro ao adicionar à watchlist: {e}"}


@app.post("/watchlist/remove")
def watchlist_remove(req: RequisicaoWatchRemove):
    try:
        conn = conectar_banco()
        try:
            return remover_watch(conn, req.user_jid, req.termo)
        finally:
            conn.close()
    except Exception as e:
        print(f"[MYP] Erro em /watchlist/remove: {e}")
        return {"ok": False, "erro": f"Erro ao remover: {e}"}


@app.post("/watchlist/list")
def watchlist_list(req: RequisicaoWatchList):
    try:
        conn = conectar_banco()
        try:
            itens = listar_watch(conn, req.user_jid)
        finally:
            conn.close()
        return {"ok": True, "user_jid": req.user_jid, "itens": itens, "total": len(itens)}
    except Exception as e:
        print(f"[MYP] Erro em /watchlist/list: {e}")
        return {"ok": False, "erro": f"Erro ao listar: {e}"}


@app.post("/verificar_descontos")
def verificar_descontos_endpoint():
    """Percorre a watchlist, refaz preços e devolve os descontos encontrados (para o bot avisar)."""
    try:
        return verificar_descontos()
    except Exception as e:
        print(f"[MYP] Erro em /verificar_descontos: {e}")
        return {"verificados": 0, "eventos": [], "erro": f"Erro ao verificar descontos: {e}"}


if __name__ == "__main__":
    # Modo CLI: python scrapper_pokemon.py "charizard" -> JSON no console
    if len(sys.argv) > 1:
        termo_cli = " ".join(sys.argv[1:])
        print(json.dumps(buscar_cartas(termo_cli), ensure_ascii=False, indent=2))
    else:
        uvicorn.run(app, host="0.0.0.0", port=8003)
