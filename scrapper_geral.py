import json
import os
import re
import sys
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import quote_plus

from fastapi import FastAPI
from pydantic import BaseModel, Field
import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


app = FastAPI(title="Scrapper Geral", version="1.0.0")
DEFAULT_CEP = os.getenv("DEFAULT_CEP", "01001000")
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://searxng:8080/search")
MAX_GERAL_RESULTS = int(os.getenv("GERAL_MAX_RESULTS", "5"))
MAX_REASONABLE_PRICE = float(os.getenv("MAX_REASONABLE_PRICE", "200000"))
MIN_REASONABLE_PRICE = float(os.getenv("MIN_REASONABLE_PRICE", "10"))


class BuscaGeralRequest(BaseModel):
    termo: str = Field(..., min_length=2, description="Termo da busca")
    cep: Optional[str] = Field(default=None, description="CEP para simular frete")


@dataclass
class Oferta:
    produto: str
    loja: str
    preco_produto: float
    valor_frete: float
    preco_total: float
    link: str


def formatar_preco_brl(valor: float) -> str:
    inteiro, decimal = f"{valor:.2f}".split(".")
    inteiro = inteiro[::-1]
    inteiro = ".".join(inteiro[i : i + 3] for i in range(0, len(inteiro), 3))[::-1]
    return f"R$ {inteiro},{decimal}"


def oferta_para_item(oferta: Oferta):
    return {
        "nome": oferta.produto,
        "preco": formatar_preco_brl(oferta.preco_total),
        "loja": oferta.loja,
        "confiavel": True,
        "preco_atual": oferta.preco_total,
        "menor_30d": None,
        "menor_90d": None,
        "avaliacao": "Promocao Boa" if oferta.valor_frete == 0 else "Preco Normal",
        "link": oferta.link,
        "frete": oferta.valor_frete,
        "preco_total": oferta.preco_total,
    }


def ofertas_para_payload(ofertas: List[Oferta], limite: int):
    selecionadas = ofertas[: max(1, limite)]
    itens = [oferta_para_item(o) for o in selecionadas]

    resultados = [
        {
            "url": item["link"],
            "itens": [item],
        }
        for item in itens
    ]

    return {
        "melhor_oferta": itens[0],
        "resultados": resultados,
    }


def normalizar_preco(texto: str) -> Optional[float]:
    """Converte valores monetarios (pt-BR/en-US) para float."""
    if not texto:
        return None

    # Prefere token com centavos, evitando capturar ids/rankings acidentalmente.
    match = re.search(r"(?:R\$\s*)?\d{1,3}(?:\.\d{3})*,\d{2}|(?:R\$\s*)?\d+[\.,]\d{2}", texto)
    if not match:
        match = re.search(r"\d[\d\.,]*", texto)
    if not match:
        return None

    bruto = match.group(0)
    if "," in bruto and "." in bruto:
        if bruto.rfind(",") > bruto.rfind("."):
            bruto = bruto.replace(".", "").replace(",", ".")
        else:
            bruto = bruto.replace(",", "")
    elif "," in bruto:
        bruto = bruto.replace(".", "").replace(",", ".")
    elif bruto.count(".") > 1:
        bruto = bruto.replace(".", "")

    try:
        valor = float(bruto)
        if valor < MIN_REASONABLE_PRICE:
            return None
        if valor > MAX_REASONABLE_PRICE:
            return None
        return valor
    except ValueError:
        return None


def extrair_preco_amazon_card(card) -> Optional[float]:
    candidatos_texto = []

    # Prioridade 1: span.a-offscreen (texto visivel do preco)
    offscreen = card.locator("span.a-offscreen")
    qtd_off = min(offscreen.count(), 4)
    for i in range(qtd_off):
        txt = offscreen.nth(i).inner_text().strip()
        if txt:
            candidatos_texto.append(txt)

    # Prioridade 2: preco dentro de span.a-price (evita capturar numeros de outros contextos)
    price_span = card.locator("span.a-price")
    if price_span.count() > 0:
        for p_idx in range(min(price_span.count(), 3)):
            p = price_span.nth(p_idx)
            whole = p.locator("span.a-price-whole")
            frac = p.locator("span.a-price-fraction")
            if whole.count() > 0:
                p_whole = whole.first.inner_text().strip()
                p_frac = frac.first.inner_text().strip() if frac.count() > 0 else ""
                if p_whole:
                    if p_frac and len(re.sub(r"\D", "", p_frac)) <= 2:
                        candidatos_texto.append(f"{p_whole},{p_frac}")
                    else:
                        candidatos_texto.append(p_whole)

    # Prioridade 3: cores do preco da Amazon (laranja)
    price_symbol = card.locator("span.a-price-symbol, .a-price .a-text-price")
    for i in range(min(price_symbol.count(), 2)):
        txt = price_symbol.nth(i).inner_text().strip()
        if txt:
            candidatos_texto.append(txt)

    for txt in candidatos_texto:
        valor = normalizar_preco(txt)
        if valor is not None:
            return valor

    return None


def _primeiro_texto(locator) -> str:
    try:
        if locator.count() == 0:
            return ""
        return locator.first.inner_text().strip()
    except Exception:
        return ""


def _primeiro_href(locator) -> str:
    try:
        if locator.count() == 0:
            return ""
        href = locator.first.get_attribute("href") or ""
        return href.strip()
    except Exception:
        return ""


def _extrair_preco_multi(texto: str):
    """Tenta extrair preco de um texto usando multiplos padroes."""
    if not texto:
        return None
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


def buscar_urls_searxng(termo: str, limite: int = 6) -> List[dict]:
    """Usa SearXNG como fallback para descobrir URLs e precos de lojas quando ML/Amazon falham."""
    candidatos = []
    vistos_urls = set()
    params_list = [
        {"q": termo, "format": "json", "engines": "google,bing,duckduckgo"},
        {"q": f'"{termo}"', "format": "json", "engines": "google,bing,duckduckgo"},
        {"q": termo, "format": "json", "engines": "qwant,yahoo,startpage"},
    ]

    for params in params_list:
        try:
            resp = requests.get(SEARXNG_URL, params=params, timeout=20)
            if resp.status_code != 200:
                continue
            payload = resp.json()
            for resultado in payload.get("results", []):
                url = (resultado.get("url") or "").strip()
                if not url or url in vistos_urls:
                    continue
                vistos_urls.add(url)

                titulo = (resultado.get("title") or "").strip()
                resumo = (resultado.get("content") or "").strip()
                texto_base = f"{titulo} {resumo}".strip()
                preco = _extrair_preco_multi(texto_base)

                dom = url.lower()
                if any(x in dom for x in ["mercadolivre.", "amazon.", "kabum.", "pichau.", "terabyte.", "shopee.", "shein.", "magazineluiza.", "extra.", "americanas.", "casasbahia."]):
                    prioridade = 1
                else:
                    prioridade = 2

                candidatos.append({
                    "url": url,
                    "titulo": titulo,
                    "preco": preco,
                    "prioridade": prioridade,
                })

                if len(candidatos) >= limite * 3:
                    break
            if len(candidatos) >= limite * 2:
                break
        except Exception:
            continue

    candidatos.sort(key=lambda x: (x["prioridade"], x["preco"] if x["preco"] is not None else float("inf")))
    return candidatos[:limite]


def buscar_top3_mercado_livre(page, nome_produto: str):
    """Busca os 3 primeiros resultados no Mercado Livre."""
    url_busca = f"https://lista.mercadolivre.com.br/{quote_plus(nome_produto)}"
    page.goto(url_busca, wait_until="domcontentloaded", timeout=30000)

    # Fecha possivel banner/cookie caso apareca.
    for seletor in [
        "button[aria-label='Fechar']",
        "button.cookie-consent-banner-opt-out__action",
        "button.andes-modal__close",
    ]:
        try:
            if page.locator(seletor).count() > 0:
                page.locator(seletor).first.click(timeout=1000)
        except Exception:
            pass

    page.wait_for_timeout(1500)
    cards = page.locator("li.ui-search-layout__item")
    if cards.count() == 0:
        cards = page.locator("div.ui-search-result")
    if cards.count() == 0:
        cards = page.locator("article")

    resultados = []
    limite = min(cards.count(), 3)

    for idx in range(limite):
        card = cards.nth(idx)
        try:
            nome = ""
            for s in ["h3", "h2.ui-search-item__title", "a.poly-component__title", "[data-testid='product-title']"]:
                texto = _primeiro_texto(card.locator(s))
                if texto:
                    nome = texto
                    break

            link = ""
            for s in ["a.ui-search-link", "a.poly-component__title", "a", "[data-testid='product-link']"]:
                href = _primeiro_href(card.locator(s))
                if href:
                    link = href
                    break

            preco_texto = ""
            for s in [
                "span.andes-money-amount__fraction",
                "span.price-tag-fraction",
                "span.ui-search-price__part",
                "span.andes-money-amount__currency-symbol",
                "div.andes-money-amount",
            ]:
                texto = _primeiro_texto(card.locator(s))
                if texto:
                    preco_texto = texto
                    break

            preco_produto = normalizar_preco(preco_texto)
            if not nome or not link or preco_produto is None:
                continue

            resultados.append(
                {
                    "produto": nome,
                    "link": link,
                    "preco_produto": preco_produto,
                    "loja": "Mercado Livre",
                }
            )
        except Exception:
            continue

    return resultados


def buscar_top3_shein(page, nome_produto: str):
    url_busca = f"https://pt.shein.com/search/?q={quote_plus(nome_produto)}"
    page.goto(url_busca, wait_until="domcontentloaded", timeout=35000)
    page.wait_for_timeout(3000)

    for seletor in [
        "button:has-text('Aceitar')",
        "button:has-text('OK')",
        "button:has-text('Fechar')",
        "[class*='close']",
        "[class*='cookie']",
    ]:
        try:
            if page.locator(seletor).count() > 0:
                page.locator(seletor).first.click(timeout=1500)
        except Exception:
            pass

    page.wait_for_timeout(1000)
    cards = page.locator("section[id*='product-list'] article, .product-item, [class*='product-card'], [class*='goods-item'], li[class*='product']")
    if cards.count() == 0:
        cards = page.locator("[class*='product-list'] [class*='item'], .search-result-list article")
    if cards.count() == 0:
        cards = page.locator("a[href*='/products/']").locator("..")

    resultados = []
    limite = min(cards.count(), 3)

    for idx in range(limite):
        card = cards.nth(idx)
        try:
            nome = ""
            for s in ["a[class*='title'], a[class*='name'], h3, [class*='product-name'], [class*='goods-title']"]:
                partes = s.split(", ")
                for sel in partes:
                    texto = _primeiro_texto(card.locator(sel))
                    if texto:
                        nome = texto.strip()
                        break
                if nome:
                    break
            if not nome:
                for s in ["img[alt]", "[alt]"]:
                    alt = _primeiro_texto(card.locator(s))
                    if alt:
                        nome = alt.strip()
                        break

            link = ""
            for s in ["a[href*='/products/']", "a[href*='/product-']", "a"]:
                href = _primeiro_href(card.locator(s))
                if href and "/products/" in href.lower():
                    link = href if href.startswith("http") else f"https://pt.shein.com{href}"
                    break
            if not link:
                link = _primeiro_href(card.locator("a"))
                if link and not link.startswith("http"):
                    link = f"https://pt.shein.com{link}"

            preco_texto = ""
            for s in ["[class*='price'] span, [class*='product-price'], span[class*='price'], [class*='c-price'], .price"]:
                partes = s.split(", ")
                for sel in partes:
                    texto = _primeiro_texto(card.locator(sel))
                    if texto and "R$" in texto:
                        preco_texto = texto
                        break
                if preco_texto:
                    break
            if not preco_texto:
                page_content = card.inner_text()
                match = re.search(r"R\$\s*[\d\.,]+", page_content)
                if match:
                    preco_texto = match.group(0)

            preco_produto = normalizar_preco(preco_texto)
            if not nome or not link or preco_produto is None:
                continue

            resultados.append({
                "produto": nome[:120],
                "link": link,
                "preco_produto": preco_produto,
                "loja": "Shein",
            })
        except Exception:
            continue

    return resultados


def buscar_top3_shopee(page, nome_produto: str):
    url_busca = f"https://shopee.com.br/search?keyword={quote_plus(nome_produto)}"
    page.goto(url_busca, wait_until="domcontentloaded", timeout=35000)
    page.wait_for_timeout(3000)

    cards = page.locator("[data-sqe='item'], [data-sqe='name'], div.shopee-search-item-result__item, a[class*='item-link']")
    if cards.count() == 0:
        cards = page.locator("[class*='search-item'] [class*='item'], div[class*='card']")
    if cards.count() == 0:
        cards = page.locator("a[href*='shopee.com.br/product/']").locator("..")

    resultados = []
    limite = min(cards.count(), 3)

    for idx in range(limite):
        card = cards.nth(idx)
        try:
            nome = ""
            for sel in ["div[data-sqe='name']", "[class*='name']", "[class*='title']", "img[alt]"]:
                texto = _primeiro_texto(card.locator(sel))
                if texto:
                    nome = texto.strip()
                    break
            if not nome:
                alt = _primeiro_texto(card.locator("img[alt]"))
                if alt:
                    nome = alt.strip()

            link = ""
            for s in ["a[href*='shopee.com.br/product/']", "a"]:
                href = _primeiro_href(card.locator(s))
                if href:
                    link = href if href.startswith("http") else f"https://shopee.com.br{href}"
                    break

            preco_texto = ""
            for sel in ["[class*='price']", "span[class*='currency']", "[data-sqe='price']"]:
                texto = _primeiro_texto(card.locator(sel))
                if texto:
                    preco_texto = texto
                    break
            if not preco_texto:
                page_content = card.inner_text()
                match = re.search(r"R\$\s*[\d\.,]+", page_content)
                if match:
                    preco_texto = match.group(0)

            preco_produto = normalizar_preco(preco_texto)
            if not nome or not link or preco_produto is None:
                continue

            resultados.append({
                "produto": nome[:120],
                "link": link,
                "preco_produto": preco_produto,
                "loja": "Shopee",
            })
        except Exception:
            continue

    return resultados


def buscar_top3_amazon(page, nome_produto: str):
    """Busca os 3 primeiros resultados na Amazon Brasil."""
    url_busca = f"https://www.amazon.com.br/s?k={quote_plus(nome_produto)}"
    page.goto(url_busca, wait_until="domcontentloaded", timeout=35000)

    # Tentativa de fechar popup/cookie quando existir.
    for seletor in [
        "input[name='accept']",
        "button:has-text('Aceitar')",
        "button:has-text('Continuar comprando')",
    ]:
        try:
            if page.locator(seletor).count() > 0:
                page.locator(seletor).first.click(timeout=1200)
        except Exception:
            pass

    page.wait_for_timeout(1500)
    cards = page.locator("div.s-result-item[data-component-type='s-search-result']")
    if cards.count() == 0:
        cards = page.locator("div[data-component-type='s-search-result']")
    if cards.count() == 0:
        cards = page.locator("div.s-card-container")
    resultados = []
    limite = min(cards.count(), 6)

    for idx in range(limite):
        card = cards.nth(idx)
        try:
            titulo = ""
            for s in ["h2 a span", "h2 span", "[data-cy='title-recipe']", "h2"]:
                texto = _primeiro_texto(card.locator(s))
                if texto:
                    titulo = texto
                    break

            link = ""
            for s in ["h2 a", "a.a-link-normal", "a[href*='/dp/']", "a[href*='/gp/product/']"]:
                href = _primeiro_href(card.locator(s))
                if href:
                    link = href
                    break

            if not titulo or not link:
                continue

            preco_produto = extrair_preco_amazon_card(card)
            if preco_produto is None:
                continue

            link = link if link.startswith("http") else f"https://www.amazon.com.br{link}"
            resultados.append(
                {
                    "produto": titulo,
                    "link": link,
                    "preco_produto": preco_produto,
                    "loja": "Amazon",
                }
            )
            if len(resultados) >= 3:
                break
        except Exception:
            continue

    return resultados


def inserir_cep_calcular_frete(page, cep_destino: str):
    """
    Tenta abrir o componente de frete e inserir o CEP na pagina do produto.
    Comentario importante: os seletores podem variar por categoria/campanha,
    por isso usamos multiplos seletores fallback.
    """
    # Abre modal/painel de frete quando houver CTA visivel.
    gatilhos = [
        "button:has-text('Calcular')",
        "button:has-text('frete')",
        "a:has-text('Calcular')",
        "button:has-text('CEP')",
        "a:has-text('CEP')",
    ]
    for seletor in gatilhos:
        try:
            if page.locator(seletor).count() > 0:
                page.locator(seletor).first.click(timeout=1500)
                page.wait_for_timeout(700)
                break
        except Exception:
            continue

    # Aqui configuramos/inserimos o CEP para simulacao de frete.
    inputs_cep = [
        "input[name*='zip']",
        "input[id*='zip']",
        "input[placeholder*='CEP']",
        "input[placeholder*='cep']",
        "input[data-testid*='zip']",
    ]

    preencheu = False
    for seletor in inputs_cep:
        try:
            if page.locator(seletor).count() > 0:
                campo = page.locator(seletor).first
                campo.click(timeout=1500)
                campo.fill(cep_destino, timeout=1500)
                campo.press("Enter")
                preencheu = True
                page.wait_for_timeout(1800)
                break
        except Exception:
            continue

    if not preencheu:
        raise ValueError("Nao foi possivel localizar campo de CEP para calcular frete")


def inserir_cep_calcular_frete_amazon(page, cep_destino: str):
    """
    Fluxo dedicado da Amazon para definir CEP de entrega.
    Comentario: os seletores variam por sessao/layout, por isso ha fallback.
    """
    gatilhos = [
        "#contextualIngressPtLabel_deliveryShortLine",
        "#glow-ingress-line2",
        "a#nav-global-location-popover-link",
    ]

    abriu_modal = False
    for seletor in gatilhos:
        try:
            if page.locator(seletor).count() > 0:
                page.locator(seletor).first.click(timeout=2000)
                abriu_modal = True
                page.wait_for_timeout(1200)
                break
        except Exception:
            continue

    if not abriu_modal:
        raise ValueError("Nao foi possivel abrir modal de CEP na Amazon")

    # Insercao do CEP na Amazon.
    for seletor in ["#GLUXZipUpdateInput", "input[name='zipCode']", "input#GLUXZipUpdateInput_0"]:
        try:
            if page.locator(seletor).count() > 0:
                campo = page.locator(seletor).first
                campo.click(timeout=2000)
                campo.fill(cep_destino, timeout=2000)
                break
        except Exception:
            continue

    for btn in ["#GLUXZipUpdate", "input.a-button-input", "button:has-text('Aplicar')"]:
        try:
            if page.locator(btn).count() > 0:
                page.locator(btn).first.click(timeout=2000)
                page.wait_for_timeout(2200)
                return
        except Exception:
            continue

    raise ValueError("Nao foi possivel confirmar CEP na Amazon")


def extrair_valor_frete(page) -> float:
    """Extrai valor de frete apos simulacao do CEP."""
    candidatos = [
        "[data-testid*='shipping']",
        "[class*='shipping']",
        "span:has-text('Frete')",
        "p:has-text('Frete')",
        "div:has-text('Frete')",
    ]

    for seletor in candidatos:
        try:
            loc = page.locator(seletor)
            qtd = min(loc.count(), 6)
            for i in range(qtd):
                txt = loc.nth(i).inner_text().strip()
                if not txt:
                    continue
                if "gratis" in txt.lower() or "gratis" in txt.lower().replace("á", "a"):
                    return 0.0
                valor = normalizar_preco(txt)
                if valor is not None:
                    return valor
        except Exception:
            continue

    raise ValueError("Nao foi possivel extrair o valor do frete")


def extrair_valor_frete_amazon(page) -> float:
    """Extrai frete na Amazon apos atualizacao de CEP."""
    candidatos = [
        "#mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE",
        "#deliveryBlockMessage",
        "#ddmDeliveryMessage",
        "span:has-text('Entrega')",
    ]

    for seletor in candidatos:
        try:
            loc = page.locator(seletor)
            qtd = min(loc.count(), 4)
            for i in range(qtd):
                txt = loc.nth(i).inner_text().strip()
                if not txt:
                    continue
                txt_low = txt.lower().replace("á", "a")
                if "gratis" in txt_low or "grátis" in txt_low:
                    return 0.0
                valor = normalizar_preco(txt)
                if valor is not None:
                    return valor
        except Exception:
            continue

    raise ValueError("Nao foi possivel extrair o valor do frete na Amazon")


def calcular_ofertas(nome_produto: str, cep_destino: str) -> List[Oferta]:
    """Busca em multiplos marketplaces e retorna ofertas ordenadas por menor preco total."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(locale="pt-BR")
        page = context.new_page()

        try:
            resultados = []
            resultados.extend(buscar_top3_mercado_livre(page, nome_produto))
            resultados.extend(buscar_top3_amazon(page, nome_produto))
            resultados.extend(buscar_top3_shein(page, nome_produto))
            resultados.extend(buscar_top3_shopee(page, nome_produto))

            # Fallback de descoberta via SearXNG quando marketplaces nao produzem cards uteis.
            if not resultados:
                urls_fallback = buscar_urls_searxng(nome_produto, limite=8)
                for item_fb in urls_fallback:
                    url = item_fb["url"]
                    dom = url.lower()
                    if "mercadolivre" in dom:
                        loja = "Mercado Livre"
                    elif "amazon" in dom:
                        loja = "Amazon"
                    elif "shein" in dom:
                        loja = "Shein"
                    elif "shopee" in dom:
                        loja = "Shopee"
                    elif "kabum" in dom:
                        loja = "Kabum"
                    elif "pichau" in dom:
                        loja = "Pichau"
                    elif "terabyte" in dom:
                        loja = "Terabyte"
                    else:
                        loja = "Busca Web"
                    resultados.append({
                        "produto": item_fb.get("titulo") or nome_produto,
                        "link": url,
                        "preco_produto": item_fb["preco"] if item_fb["preco"] is not None else 0.0,
                        "loja": loja,
                    })

            if not resultados:
                raise ValueError("Produto nao encontrado nos marketplaces")

            ofertas_validas: List[Oferta] = []
            erros_frete = []

            for resultado in resultados:
                page_prod = context.new_page()
                try:
                    page_prod.goto(resultado["link"], wait_until="domcontentloaded", timeout=30000)
                    page_prod.wait_for_timeout(1200)

                    loja = resultado.get("loja", "Mercado Livre")
                    if loja == "Amazon":
                        inserir_cep_calcular_frete_amazon(page_prod, cep_destino)
                        valor_frete = extrair_valor_frete_amazon(page_prod)
                    elif loja == "Mercado Livre":
                        inserir_cep_calcular_frete(page_prod, cep_destino)
                        valor_frete = extrair_valor_frete(page_prod)
                    else:
                        texto_pagina = page_prod.locator("body").inner_text(timeout=3000)
                        preco_texto = None
                        for padrao in [
                            r"R\$\s*[\d\.]+,\d{2}",
                            r"\$\s*[\d\.,]+",
                        ]:
                            m = re.search(padrao, texto_pagina)
                            if m:
                                preco_texto = m.group(0)
                                break
                        preco_encontrado = normalizar_preco(preco_texto) if preco_texto else None
                        valor_frete = 0.0
                        if preco_encontrado is not None and resultado.get("preco_produto", 0.0) <= 0:
                            resultado["preco_produto"] = preco_encontrado

                    preco_total = resultado["preco_produto"] + valor_frete

                    ofertas_validas.append(
                        Oferta(
                            produto=resultado["produto"],
                            loja=loja,
                            preco_produto=round(resultado["preco_produto"], 2),
                            valor_frete=round(valor_frete, 2),
                            preco_total=round(preco_total, 2),
                            link=resultado["link"],
                        )
                    )
                except (PlaywrightTimeoutError, ValueError, Exception) as exc:
                    erros_frete.append(f"{resultado['produto']}: {exc}")
                finally:
                    page_prod.close()

            if not ofertas_validas:
                raise ValueError(
                    "Nao foi possivel calcular frete para os resultados encontrados. "
                    + " | ".join(erros_frete[:3])
                )

            ofertas_validas.sort(key=lambda x: x.preco_total)
            return ofertas_validas
        finally:
            context.close()
            browser.close()


@app.get("/health")
def health():
    return {"status": "ok", "service": "scrapper-geral"}


@app.post("/buscar_geral")
def buscar_geral(payload: BuscaGeralRequest):
    termo = payload.termo.strip()
    cep = (payload.cep or DEFAULT_CEP).strip()

    try:
        ofertas = calcular_ofertas(termo, cep)
        formatado = ofertas_para_payload(ofertas, MAX_GERAL_RESULTS)
        return {
            "fonte": "marketplaces_geral",
            "termo": termo,
            "cep": cep,
            "total_encontrados": len(formatado["resultados"]),
            "fallback_usado": False,
            **formatado,
        }
    except Exception as exc:
        return {
            "fonte": "marketplaces_geral",
            "termo": termo,
            "cep": cep,
            "total_encontrados": 0,
            "fallback_usado": False,
            "resultados": [],
            "erro": str(exc),
        }


def main():
    try:
        if len(sys.argv) < 3:
            raise ValueError("Uso: python scrapper_geral.py <nome_produto> <cep_destino>")

        nome_produto = sys.argv[1].strip()
        cep_destino = sys.argv[2].strip()

        melhor = calcular_ofertas(nome_produto, cep_destino)[0]
        saida = {
            "produto": melhor.produto,
            "loja": melhor.loja,
            "preco_produto": melhor.preco_produto,
            "valor_frete": melhor.valor_frete,
            "preco_total": melhor.preco_total,
            "link": melhor.link,
        }
        print(json.dumps(saida, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"erro": str(e)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
