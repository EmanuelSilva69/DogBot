import json
import re
import sys
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import quote_plus

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


@dataclass
class Oferta:
    produto: str
    loja: str
    preco_produto: float
    valor_frete: float
    preco_total: float
    link: str


def normalizar_preco(texto: str) -> Optional[float]:
    """Converte valores monetarios (pt-BR/en-US) para float."""
    if not texto:
        return None

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
        return valor if valor >= 0 else None
    except ValueError:
        return None


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

    resultados = []
    limite = min(cards.count(), 3)

    for idx in range(limite):
        card = cards.nth(idx)
        try:
            nome = ""
            for s in ["h3", "h2.ui-search-item__title", "a.poly-component__title"]:
                loc = card.locator(s)
                if loc.count() > 0:
                    nome = loc.first.inner_text().strip()
                    if nome:
                        break

            link = ""
            for s in ["a.ui-search-link", "a.poly-component__title", "a"]:
                loc = card.locator(s)
                if loc.count() > 0:
                    href = loc.first.get_attribute("href")
                    if href:
                        link = href
                        break

            preco_texto = ""
            for s in [
                "span.andes-money-amount__fraction",
                "span.price-tag-fraction",
                "span.ui-search-price__part",
            ]:
                loc = card.locator(s)
                if loc.count() > 0:
                    preco_texto = loc.first.inner_text().strip()
                    if preco_texto:
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
    resultados = []
    limite = min(cards.count(), 6)

    for idx in range(limite):
        card = cards.nth(idx)
        try:
            titulo_loc = card.locator("h2 a span")
            link_loc = card.locator("h2 a")
            if titulo_loc.count() == 0 or link_loc.count() == 0:
                continue

            nome = titulo_loc.first.inner_text().strip()
            href = link_loc.first.get_attribute("href")
            if not nome or not href:
                continue

            preco_whole = card.locator("span.a-price-whole")
            preco_frac = card.locator("span.a-price-fraction")
            preco_texto = ""
            if preco_whole.count() > 0:
                preco_texto = preco_whole.first.inner_text().strip()
                if preco_frac.count() > 0:
                    preco_texto = f"{preco_texto},{preco_frac.first.inner_text().strip()}"

            preco_produto = normalizar_preco(preco_texto)
            if preco_produto is None:
                continue

            link = href if href.startswith("http") else f"https://www.amazon.com.br{href}"
            resultados.append(
                {
                    "produto": nome,
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


def calcular_melhor_oferta(nome_produto: str, cep_destino: str) -> Oferta:
    """Busca em multiplos marketplaces e retorna a melhor pelo menor preco total."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(locale="pt-BR")
        page = context.new_page()

        try:
            resultados_ml = buscar_top3_mercado_livre(page, nome_produto)
            resultados_amz = buscar_top3_amazon(page, nome_produto)
            resultados = resultados_ml + resultados_amz
            if not resultados:
                raise ValueError("Produto nao encontrado em Mercado Livre/Amazon")

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
                    else:
                        inserir_cep_calcular_frete(page_prod, cep_destino)
                        valor_frete = extrair_valor_frete(page_prod)

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
            return ofertas_validas[0]
        finally:
            context.close()
            browser.close()


def main():
    try:
        if len(sys.argv) < 3:
            raise ValueError("Uso: python scrapper_geral.py <nome_produto> <cep_destino>")

        nome_produto = sys.argv[1].strip()
        cep_destino = sys.argv[2].strip()

        melhor = calcular_melhor_oferta(nome_produto, cep_destino)
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
