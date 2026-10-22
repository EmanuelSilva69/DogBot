"""
Serviço de Busca, Validação e Aplicação de Cupons
Testa cupons em múltiplos marketplaces: Mercado Livre, Amazon, Kabum, etc.
FastAPI service running on port 9098
"""

import asyncio
import logging
from typing import Optional, Dict
from dataclasses import dataclass
from urllib.parse import urlparse
from datetime import datetime
from enum import Enum
import re

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from playwright.async_api import async_playwright, Page

# ============================================================================
# CONFIGURAÇÃO
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Serviço de Cupons", version="1.0.0")

# Constantes
TIMEOUT_TESTE = 30000  # 30s para teste de cupom
MAX_CUPONS_TESTAR = 10  # Máximo de cupons a testar por busca
MARKETPLACES = ["mercado-livre", "amazon", "kabum", "terabyte", "pichau", "shopee", "extra"]

# ============================================================================
# MODELOS DE DADOS
# ============================================================================

class MarketplaceEnum(str, Enum):
    MERCADO_LIVRE = "mercado-livre"
    AMAZON = "amazon"
    KABUM = "kabum"
    TERABYTE = "terabyte"
    PICHAU = "pichau"
    SHOPEE = "shopee"
    EXTRA = "extra"

@dataclass
class Cupom:
    codigo: str
    marketplace: str
    percentual: float
    valor_minimo: float = 0
    descricao: str = ""
    ativo: bool = True
    data_expiracao: Optional[str] = None

class BuscaCupomRequest(BaseModel):
    termo_busca: str = ""
    marketplace: Optional[str] = None  # Se None, testa todos
    valor_carrinho: float = 100.0
    url_produto: Optional[str] = None

class ResultadoCupom(BaseModel):
    cupom_valido: Optional[str]
    marketplace: str
    desconto: float
    percentual: float
    cupons_testados: int
    tempo_busca: float
    preco_original: Optional[float] = None
    preco_final: Optional[float] = None
    url_produto: Optional[str] = None
    item_adicionado: bool = False
    cupons_sugeridos: list[dict] = []

# ============================================================================
# BASE DE CUPONS LOCAL
# ============================================================================

CUPONS_BASE = {
    "mercado-livre": [
        {"codigo": "PRIMEIRA", "percentual": 10, "valor_minimo": 50},
        {"codigo": "TECH20", "percentual": 20, "valor_minimo": 100},
        {"codigo": "FRETEGRATIS", "percentual": 5, "valor_minimo": 150},
        {"codigo": "CLIENTE", "percentual": 15, "valor_minimo": 200},
        {"codigo": "DESCONTO30", "percentual": 30, "valor_minimo": 300},
        {"codigo": "SUPER", "percentual": 25, "valor_minimo": 250},
        {"codigo": "CUPOM15", "percentual": 15, "valor_minimo": 75},
        {"codigo": "BLACKFRIDAY", "percentual": 40, "valor_minimo": 500},
    ],
    "amazon": [
        {"codigo": "TECHLOVER", "percentual": 15, "valor_minimo": 50},
        {"codigo": "AMAZON20", "percentual": 20, "valor_minimo": 100},
        {"codigo": "FRETE5", "percentual": 5, "valor_minimo": 80},
        {"codigo": "INOVA25", "percentual": 25, "valor_minimo": 200},
        {"codigo": "PRIMEIRA10", "percentual": 10, "valor_minimo": 30},
        {"codigo": "TECH30", "percentual": 30, "valor_minimo": 300},
    ],
    "kabum": [
        {"codigo": "WELCOME15", "percentual": 15, "valor_minimo": 100},
        {"codigo": "TECH50", "percentual": 50, "valor_minimo": 200},
        {"codigo": "KABUM20", "percentual": 20, "valor_minimo": 150},
        {"codigo": "FRETEGRATIS", "percentual": 10, "valor_minimo": 100},
        {"codigo": "GAMERS30", "percentual": 30, "valor_minimo": 250},
        {"codigo": "SUPER25", "percentual": 25, "valor_minimo": 180},
    ],
    "terabyte": [
        {"codigo": "TERA10", "percentual": 10, "valor_minimo": 100},
        {"codigo": "TERA15", "percentual": 15, "valor_minimo": 200},
        {"codigo": "FRETEFREE", "percentual": 5, "valor_minimo": 150},
        {"codigo": "MONTA20", "percentual": 20, "valor_minimo": 500},
    ],
    "pichau": [
        {"codigo": "PICHAU10", "percentual": 10, "valor_minimo": 100},
        {"codigo": "SETUP15", "percentual": 15, "valor_minimo": 250},
        {"codigo": "PCGAMER20", "percentual": 20, "valor_minimo": 600},
        {"codigo": "FRETEGRATIS", "percentual": 5, "valor_minimo": 150},
    ],
    "shopee": [
        {"codigo": "SHOPEETECH", "percentual": 15, "valor_minimo": 50},
        {"codigo": "SUPER20", "percentual": 20, "valor_minimo": 100},
        {"codigo": "FRETE10", "percentual": 10, "valor_minimo": 80},
        {"codigo": "MEGA30", "percentual": 30, "valor_minimo": 300},
    ],
    "extra": [
        {"codigo": "EXTRA15", "percentual": 15, "valor_minimo": 100},
        {"codigo": "TECH20", "percentual": 20, "valor_minimo": 150},
        {"codigo": "DESCONTO25", "percentual": 25, "valor_minimo": 200},
    ]
}


def detectar_marketplace_por_url(url: str) -> Optional[str]:
    """Detecta marketplace a partir do domínio do link."""
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return None

    if "mercadolivre" in host:
        return "mercado-livre"
    if "amazon" in host:
        return "amazon"
    if "kabum" in host:
        return "kabum"
    if "terabyte" in host:
        return "terabyte"
    if "pichau" in host:
        return "pichau"
    if "shopee" in host:
        return "shopee"
    if "extra" in host:
        return "extra"
    return None


def normalizar_preco(texto: str) -> Optional[float]:
    if not texto:
        return None
    match = re.search(r"(?:R\$\s*)?\d{1,3}(?:\.\d{3})*,\d{2}|(?:R\$\s*)?\d+[\.,]\d{2}", texto)
    if not match:
        return None
    bruto = match.group(0).replace("R$", "").replace(" ", "").strip()
    if "," in bruto:
        bruto = bruto.replace(".", "").replace(",", ".")
    try:
        valor = float(bruto)
        if valor <= 0:
            return None
        return valor
    except Exception:
        return None


def gerar_sugestoes_cupons(cupons: list[dict], valor_base: float, limite: int = 3) -> list[dict]:
    """Gera cupons para aplicação manual com desconto estimado."""
    validos = [c for c in cupons if c.get("valor_minimo", 0) <= valor_base]
    validos = sorted(validos, key=lambda x: x.get("percentual", 0), reverse=True)[:limite]
    saida = []
    for c in validos:
        percentual = float(c.get("percentual", 0))
        desconto = round((percentual / 100.0) * valor_base, 2)
        saida.append({
            "codigo": c.get("codigo"),
            "percentual": percentual,
            "desconto_estimado": desconto,
            "valor_minimo": c.get("valor_minimo", 0),
        })
    return saida


async def tentar_fechar_popups(page: Page) -> None:
    """Tenta fechar banners/modais que bloqueiam clique em botões."""
    acoes = [
        "button:has-text('Aceitar')",
        "button:has-text('Entendi')",
        "button:has-text('Fechar')",
        "button:has-text('Continuar')",
        "button[aria-label='Fechar']",
        "[data-testid='close']",
    ]
    for seletor in acoes:
        try:
            elem = await page.query_selector(seletor)
            if elem:
                await elem.click(timeout=1200)
                await asyncio.sleep(0.2)
        except Exception:
            continue


async def primeiro_elemento(page: Page, seletores: list[str]):
    for seletor in seletores:
        try:
            elem = await page.query_selector(seletor)
            if elem:
                return elem
        except Exception:
            continue
    return None


async def abrir_link_e_adicionar_ao_carrinho(page: Page, marketplace: str, url_produto: str) -> bool:
    """Abre link do item e tenta adicionar ao carrinho de forma específica por loja."""
    try:
        await page.goto(url_produto, timeout=TIMEOUT_TESTE, wait_until="domcontentloaded")
        await asyncio.sleep(2)
        await tentar_fechar_popups(page)

        botoes_por_marketplace = {
            "mercado-livre": [
                "button:has-text('Adicionar ao carrinho')",
                "button:has-text('Adicionar carrinho')",
                "button[data-testid='action:buy']",
            ],
            "amazon": [
                "#add-to-cart-button",
                "input#add-to-cart-button",
                "button:has-text('Adicionar ao carrinho')",
            ],
            "kabum": [
                "button:has-text('COMPRAR')",
                "button:has-text('Adicionar ao carrinho')",
                "button:has-text('Comprar')",
                "button[data-testid*='buy']",
                "button[data-testid*='add-to-cart']",
                "[id*='buy-button']",
            ],
            "terabyte": [
                "button:has-text('Comprar')",
                "button:has-text('Adicionar ao carrinho')",
                "a:has-text('Comprar')",
                "button[data-testid*='buy']",
            ],
            "pichau": [
                "button:has-text('Comprar')",
                "button:has-text('Adicionar ao carrinho')",
                "a:has-text('Comprar')",
                "button[data-testid*='buy']",
            ],
            "shopee": [
                "button:has-text('Adicionar ao carrinho')",
                "button:has-text('Comprar agora')",
            ],
            "extra": [
                "button:has-text('Adicionar ao carrinho')",
                "button:has-text('Comprar')",
            ],
        }

        seletores = botoes_por_marketplace.get(marketplace, [])
        for seletor in seletores:
            try:
                botao = await page.query_selector(seletor)
                if botao:
                    await botao.click()
                    await asyncio.sleep(2)
                    await tentar_fechar_popups(page)
                    return True
            except Exception:
                continue

        # Fallback por texto para páginas dinâmicas da Kabum
        if marketplace == "kabum":
            try:
                btn = page.get_by_role("button", name=re.compile("comprar|adicionar", re.I)).first
                await btn.click(timeout=2500)
                await asyncio.sleep(2)
                await tentar_fechar_popups(page)
                return True
            except Exception:
                pass

        return False
    except Exception as e:
        logger.warning(f"Falha ao adicionar item no carrinho: {str(e)}")
        return False


async def ir_para_carrinho(page: Page, marketplace: str) -> None:
    urls = {
        "mercado-livre": "https://www.mercadolivre.com.br/carrinho",
        "amazon": "https://www.amazon.com.br/gp/cart/view.html",
        "kabum": "https://www.kabum.com.br/carrinho",
        "terabyte": "https://www.terabyteshop.com.br/carrinho",
        "pichau": "https://www.pichau.com.br/checkout/cart/",
        "shopee": "https://shopee.com.br/cart",
        "extra": "https://www.extra.com.br/carrinho/",
    }
    await page.goto(urls[marketplace], timeout=TIMEOUT_TESTE, wait_until="domcontentloaded")
    await asyncio.sleep(2)
    await tentar_fechar_popups(page)


async def ir_para_checkout_kabum(page: Page) -> bool:
    """Cupom da Kabum costuma aparecer no checkout; tenta chegar nessa etapa."""
    urls_checkout = [
        "https://www.kabum.com.br/checkout",
        "https://www.kabum.com.br/checkout/carrinho",
    ]

    for url in urls_checkout:
        try:
            await page.goto(url, timeout=TIMEOUT_TESTE, wait_until="domcontentloaded")
            await asyncio.sleep(2)
            await tentar_fechar_popups(page)

            html = (await page.content()).lower()
            if "login" in html and "kabum" in html and "checkout" in page.url.lower():
                logger.warning("[KABUM] Checkout exige login para continuar")
                return True

            # Se existe algo relacionado a cupom já no checkout, sucesso.
            cupom_marker = await primeiro_elemento(page, [
                "input[placeholder*='cupom']",
                "input[placeholder*='código']",
                "input[name='coupon']",
                "button:has-text('Cupom')",
                "[data-testid*='coupon']",
            ])
            if cupom_marker:
                logger.info("[KABUM] Checkout carregado com área de cupom")
                return True
        except Exception:
            continue

    # Fallback: ir ao carrinho e tentar clicar em finalizar compra.
    try:
        await ir_para_carrinho(page, "kabum")
        btn_checkout = await primeiro_elemento(page, [
            "button:has-text('Finalizar compra')",
            "a:has-text('Finalizar compra')",
            "button:has-text('Ir para checkout')",
            "a[href*='checkout']",
            "button[data-testid*='checkout']",
        ])
        if btn_checkout:
            await btn_checkout.click(timeout=3000)
            await asyncio.sleep(2)
            await tentar_fechar_popups(page)
            logger.info("[KABUM] Redirecionado do carrinho para checkout")
            return True
    except Exception:
        pass

    logger.warning("[KABUM] Nao foi possivel abrir checkout")
    return False


async def capturar_valor_carrinho(page: Page, marketplace: str) -> Optional[float]:
    seletores_preco = {
        "mercado-livre": [
            "[data-testid='summary-total-value']",
            ".ui-receipt-summary__price",
            ".andes-money-amount__fraction",
        ],
        "amazon": [
            "#sc-subtotal-amount-activecart span",
            ".sc-price",
            ".a-color-price",
        ],
        "kabum": [
            "[data-testid='total-price']",
            ".final-price",
            ".priceTotal",
        ],
        "terabyte": [
            ".order-total .price",
            ".totals .price",
            "[data-th='Total']",
        ],
        "pichau": [
            ".grand.totals .price",
            ".totals .amount",
            ".cart-totals .price",
        ],
        "shopee": [
            ".shopee-cart-summary__subtotal-amount",
            ".checkout-payment-setting__payment-methods-tab",
        ],
        "extra": [
            ".summary-total .value",
            ".total-value",
        ],
    }

    for seletor in seletores_preco.get(marketplace, []):
        try:
            elem = await page.query_selector(seletor)
            if not elem:
                continue
            txt = await elem.inner_text()
            valor = normalizar_preco(txt)
            if valor:
                return valor
        except Exception:
            continue
    return None

# ============================================================================
# FUNÇÕES DE TESTE POR MARKETPLACE
# ============================================================================

async def testar_cupom_mercado_livre(page: Page, cupom: str, valor: float) -> bool:
    """Testa cupom no Mercado Livre via Playwright"""
    try:
        logger.info(f"[ML] Testando cupom {cupom}")
        
        await ir_para_carrinho(page, "mercado-livre")
        
        # Procura campo de cupom (pode estar em vários lugares)
        cupom_input = None
        seletores = [
            "input[aria-label*='Cupom']",
            "input[placeholder*='Cupom']",
            "input[placeholder*='cupom']",
            ".discount-code-input",
            "input#cupom",
        ]
        
        for seletor in seletores:
            try:
                elem = await page.query_selector(seletor)
                if elem:
                    cupom_input = elem
                    break
            except:
                pass
        
        if not cupom_input:
            logger.warning(f"[ML] Campo de cupom não encontrado para {cupom}")
            return False
        
        # Preenche cupom
        await cupom_input.fill(cupom)
        await asyncio.sleep(1)
        
        # Procura botão aplicar
        botoes = ["Aplicar", "aplicar", "Usar", "OK", "Confirmar"]
        for texto in botoes:
            try:
                btn = await page.get_by_role("button", name=texto, exact=False)
                if btn:
                    await btn.click()
                    await asyncio.sleep(2)
                    break
            except:
                pass
        
        # Verifica se foi aplicado (método: procura mensagem de sucesso ou desconto aplicado)
        try:
            await page.wait_for_selector(
                ".success-message, .discount-applied, [data-testid='discount-applied']",
                timeout=5000
            )
            logger.info(f"[ML] ✓ Cupom {cupom} válido!")
            return True
        except:
            logger.warning(f"[ML] ✗ Cupom {cupom} rejeitado")
            return False
            
    except Exception as e:
        logger.error(f"[ML] Erro testando {cupom}: {str(e)}")
        return False

async def testar_cupom_amazon(page: Page, cupom: str, valor: float) -> bool:
    """Testa cupom na Amazon"""
    try:
        logger.info(f"[AMAZON] Testando cupom {cupom}")
        
        await ir_para_carrinho(page, "amazon")
        
        # Procura campo de cupom (Amazon USA vs Brasil)
        seletores = [
            "input[aria-label*='Cardápio']",
            "input[aria-label*='Cupom']",
            "input[name='promoCode']",
            "input[placeholder*='cupom']",
            "#promo-code-box",
        ]
        
        cupom_input = None
        for seletor in seletores:
            try:
                elem = await page.query_selector(seletor)
                if elem:
                    cupom_input = elem
                    break
            except:
                pass
        
        if not cupom_input:
            logger.warning(f"[AMAZON] Campo de cupom não encontrado para {cupom}")
            return False
        
        await cupom_input.fill(cupom)
        await asyncio.sleep(1)
        
        # Procura botão aplicar
        try:
            btn = await page.get_by_role("button", name=re.compile("aplicar|Apply|submit", re.I))
            await btn.click()
            await asyncio.sleep(2)
        except:
            pass
        
        # Verifica sucesso
        try:
            await page.wait_for_selector(
                ".a-alert-success, .promoCode-success, [data-testid='promo-success']",
                timeout=5000
            )
            logger.info(f"[AMAZON] ✓ Cupom {cupom} válido!")
            return True
        except:
            logger.warning(f"[AMAZON] ✗ Cupom {cupom} rejeitado")
            return False
            
    except Exception as e:
        logger.error(f"[AMAZON] Erro testando {cupom}: {str(e)}")
        return False

async def testar_cupom_kabum(page: Page, cupom: str, valor: float) -> bool:
    """Testa cupom na Kabum"""
    try:
        logger.info(f"[KABUM] Testando cupom {cupom}")
        
        chegou_checkout = await ir_para_checkout_kabum(page)
        if not chegou_checkout:
            await ir_para_carrinho(page, "kabum")

        html_atual = (await page.content()).lower()
        if "seu carrinho está vazio" in html_atual or "carrinho vazio" in html_atual:
            logger.warning("[KABUM] Carrinho vazio; cupom indisponivel")
            return False
        if "entrar" in html_atual and "login" in html_atual and "checkout" in page.url.lower():
            logger.warning("[KABUM] Bloqueado por login no checkout")
            return False

        # Em alguns layouts o campo de cupom fica colapsado atrás de um CTA.
        for abrir in [
            "button:has-text('Cupom')",
            "button:has-text('cupom')",
            "button:has-text('Adicionar cupom')",
            "button:has-text('Inserir cupom')",
            "button:has-text('Vale-presente')",
            "button:has-text('Código de desconto')",
            "[data-testid*='coupon']",
        ]:
            try:
                elem = await page.query_selector(abrir)
                if elem:
                    await elem.click(timeout=1500)
                    await asyncio.sleep(0.8)
            except Exception:
                continue
        
        # Procura campo de cupom Kabum
        seletores = [
            "input[placeholder*='cupom']",
            "input[placeholder*='Cupom']",
            "input[placeholder*='código']",
            "input[placeholder*='codigo']",
            "input[name='coupon']",
            "input[name='couponCode']",
            "input[name='voucher']",
            ".coupon-input",
            "input[id*='coupon']",
            "input[id*='cupom']",
            "[data-testid*='coupon'] input",
        ]

        cupom_input = await primeiro_elemento(page, seletores)
        
        if not cupom_input:
            logger.warning(f"[KABUM] Campo de cupom não encontrado para {cupom}")
            return False
        
        await cupom_input.fill(cupom)
        await asyncio.sleep(1)
        
        # Procura botão aplicar
        try:
            btn = page.get_by_role("button", name=re.compile("aplicar|usar|ok|validar", re.I)).first
            await btn.click(timeout=2500)
            await asyncio.sleep(2)
        except Exception:
            btn = await primeiro_elemento(page, [
                "button[data-testid*='coupon']",
                "button[id*='coupon']",
                "button:has-text('Aplicar')",
            ])
            if btn:
                try:
                    await btn.click(timeout=2500)
                    await asyncio.sleep(2)
                except Exception:
                    pass
        
        # Verifica sucesso (Kabum mostra desconto na tela)
        try:
            await page.wait_for_selector(
                ".coupon-success, .discount-message, [data-testid='coupon-applied'], .text-success, [class*='success']",
                timeout=5000
            )
            logger.info(f"[KABUM] ✓ Cupom {cupom} válido!")
            return True
        except:
            logger.warning(f"[KABUM] ✗ Cupom {cupom} rejeitado")
            return False
            
    except Exception as e:
        logger.error(f"[KABUM] Erro testando {cupom}: {str(e)}")
        return False

async def testar_cupom_shopee(page: Page, cupom: str, valor: float) -> bool:
    """Testa cupom na Shopee"""
    try:
        logger.info(f"[SHOPEE] Testando cupom {cupom}")
        
        await ir_para_carrinho(page, "shopee")
        
        seletores = [
            "input[placeholder*='cupom']",
            "input[placeholder*='voucher']",
            ".voucher-input",
            "input[name='voucher']",
        ]
        
        cupom_input = None
        for seletor in seletores:
            try:
                elem = await page.query_selector(seletor)
                if elem:
                    cupom_input = elem
                    break
            except:
                pass
        
        if not cupom_input:
            logger.warning(f"[SHOPEE] Campo de cupom não encontrado para {cupom}")
            return False
        
        await cupom_input.fill(cupom)
        await asyncio.sleep(1)
        
        # Botão aplicar
        try:
            btn = await page.get_by_role("button", name=re.compile("aplicar|usar", re.I))
            await btn.click()
            await asyncio.sleep(2)
        except:
            pass
        
        try:
            await page.wait_for_selector(
                ".voucher-success, [data-testid='voucher-applied']",
                timeout=5000
            )
            logger.info(f"[SHOPEE] ✓ Cupom {cupom} válido!")
            return True
        except:
            logger.warning(f"[SHOPEE] ✗ Cupom {cupom} rejeitado")
            return False
            
    except Exception as e:
        logger.error(f"[SHOPEE] Erro testando {cupom}: {str(e)}")
        return False

async def testar_cupom_extra(page: Page, cupom: str, valor: float) -> bool:
    """Testa cupom na Extra"""
    try:
        logger.info(f"[EXTRA] Testando cupom {cupom}")
        
        await ir_para_carrinho(page, "extra")
        
        seletores = [
            "input[placeholder*='cupom']",
            "input[placeholder*='Cupom']",
            "input[name='coupon']",
            ".coupon-field",
        ]
        
        cupom_input = None
        for seletor in seletores:
            try:
                elem = await page.query_selector(seletor)
                if elem:
                    cupom_input = elem
                    break
            except:
                pass
        
        if not cupom_input:
            logger.warning(f"[EXTRA] Campo de cupom não encontrado para {cupom}")
            return False
        
        await cupom_input.fill(cupom)
        await asyncio.sleep(1)
        
        try:
            btn = await page.get_by_role("button", name=re.compile("aplicar|usar", re.I))
            await btn.click()
            await asyncio.sleep(2)
        except:
            pass
        
        try:
            await page.wait_for_selector(
                ".success, [data-testid='coupon-success']",
                timeout=5000
            )
            logger.info(f"[EXTRA] ✓ Cupom {cupom} válido!")
            return True
        except:
            logger.warning(f"[EXTRA] ✗ Cupom {cupom} rejeitado")
            return False
            
    except Exception as e:
        logger.error(f"[EXTRA] Erro testando {cupom}: {str(e)}")
        return False


async def testar_cupom_terabyte(page: Page, cupom: str, valor: float) -> bool:
    """Teste de cupom na Terabyte (campos variam por etapa de checkout)."""
    try:
        logger.info(f"[TERABYTE] Testando cupom {cupom}")
        await ir_para_carrinho(page, "terabyte")

        cupom_input = await primeiro_elemento(page, [
            "input[name='coupon_code']",
            "input[name='coupon']",
            "input[placeholder*='cupom']",
            "input[id*='coupon']",
        ])
        if not cupom_input:
            logger.warning(f"[TERABYTE] Campo de cupom não encontrado para {cupom}")
            return False

        await cupom_input.fill(cupom)
        await asyncio.sleep(0.7)
        btn = await primeiro_elemento(page, [
            "button:has-text('Aplicar')",
            "button:has-text('Usar')",
            "button[title*='Apply']",
        ])
        if btn:
            await btn.click(timeout=2000)

        await asyncio.sleep(1.5)
        ok = await primeiro_elemento(page, [
            ".message-success",
            ".coupon-success",
            ".success",
        ])
        if ok:
            logger.info(f"[TERABYTE] ✓ Cupom {cupom} válido!")
            return True
        logger.warning(f"[TERABYTE] ✗ Cupom {cupom} rejeitado")
        return False
    except Exception as e:
        logger.error(f"[TERABYTE] Erro testando {cupom}: {str(e)}")
        return False


async def testar_cupom_pichau(page: Page, cupom: str, valor: float) -> bool:
    """Teste de cupom na Pichau (campos variam por etapa de checkout)."""
    try:
        logger.info(f"[PICHAU] Testando cupom {cupom}")
        await ir_para_carrinho(page, "pichau")

        cupom_input = await primeiro_elemento(page, [
            "input[name='coupon_code']",
            "input[name='coupon']",
            "input[placeholder*='cupom']",
            "input[id*='coupon']",
        ])
        if not cupom_input:
            logger.warning(f"[PICHAU] Campo de cupom não encontrado para {cupom}")
            return False

        await cupom_input.fill(cupom)
        await asyncio.sleep(0.7)
        btn = await primeiro_elemento(page, [
            "button:has-text('Aplicar')",
            "button:has-text('Usar')",
            "button[title*='Apply']",
        ])
        if btn:
            await btn.click(timeout=2000)

        await asyncio.sleep(1.5)
        ok = await primeiro_elemento(page, [
            ".message-success",
            ".coupon-success",
            ".success",
        ])
        if ok:
            logger.info(f"[PICHAU] ✓ Cupom {cupom} válido!")
            return True
        logger.warning(f"[PICHAU] ✗ Cupom {cupom} rejeitado")
        return False
    except Exception as e:
        logger.error(f"[PICHAU] Erro testando {cupom}: {str(e)}")
        return False

# ============================================================================
# LÓGICA DE BUSCA E TESTE
# ============================================================================

async def buscar_melhor_cupom(
    marketplace: str,
    valor_carrinho: float,
    url_produto: Optional[str] = None,
) -> ResultadoCupom:
    """Busca e testa cupons para um marketplace específico"""
    inicio = datetime.now()
    
    if marketplace not in CUPONS_BASE:
        raise ValueError(f"Marketplace {marketplace} não suportado")
    
    cupons = CUPONS_BASE[marketplace]
    # Filtra por valor mínimo
    cupons_validos = [c for c in cupons if c["valor_minimo"] <= valor_carrinho]
    cupons_validos = sorted(cupons_validos, key=lambda x: x["percentual"], reverse=True)
    cupons_validos = cupons_validos[:MAX_CUPONS_TESTAR]
    
    if not cupons_validos:
        logger.warning(f"Nenhum cupom disponível para {marketplace} com valor >= R$ {valor_carrinho}")
        tempo = (datetime.now() - inicio).total_seconds()
        return ResultadoCupom(
            cupom_valido=None,
            marketplace=marketplace,
            desconto=0,
            percentual=0,
            cupons_testados=0,
            tempo_busca=tempo
        )
    
    # Testa cupons
    browser = None
    preco_base = valor_carrinho
    item_adicionado = False
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(viewport={"width": 1280, "height": 720})
            page = await context.new_page()

            # Fluxo completo por link: abre o produto e tenta adicionar ao carrinho antes dos cupons.
            if url_produto:
                item_adicionado = await abrir_link_e_adicionar_ao_carrinho(page, marketplace, url_produto)
                await ir_para_carrinho(page, marketplace)
                preco_detectado = await capturar_valor_carrinho(page, marketplace)
                if preco_detectado:
                    preco_base = preco_detectado
            
            # Seleciona função de teste baseada no marketplace
            funcoes_teste = {
                "mercado-livre": testar_cupom_mercado_livre,
                "amazon": testar_cupom_amazon,
                "kabum": testar_cupom_kabum,
                "terabyte": testar_cupom_terabyte,
                "pichau": testar_cupom_pichau,
                "shopee": testar_cupom_shopee,
                "extra": testar_cupom_extra,
            }
            
            funcao_teste = funcoes_teste.get(marketplace)
            if not funcao_teste:
                raise ValueError(f"Função de teste não implementada para {marketplace}")
            
            # Testa cada cupom
            for cupom_data in cupons_validos:
                try:
                    valido = await funcao_teste(page, cupom_data["codigo"], preco_base)
                    if valido:
                        tempo = (datetime.now() - inicio).total_seconds()
                        desconto = (cupom_data["percentual"] / 100) * preco_base
                        preco_final = max(0.0, preco_base - desconto)
                        
                        logger.info(f"✓ Cupom encontrado: {cupom_data['codigo']} " +
                                  f"({cupom_data['percentual']}% = R$ {desconto:.2f})")
                        
                        return ResultadoCupom(
                            cupom_valido=cupom_data["codigo"],
                            marketplace=marketplace,
                            desconto=round(desconto, 2),
                            percentual=cupom_data["percentual"],
                            cupons_testados=cupons_validos.index(cupom_data) + 1,
                            tempo_busca=tempo,
                            preco_original=round(preco_base, 2),
                            preco_final=round(preco_final, 2),
                            url_produto=url_produto,
                            item_adicionado=item_adicionado,
                        )
                except Exception as e:
                    logger.warning(f"Erro ao testar {cupom_data['codigo']}: {str(e)}")
                    continue
            
            # Se nenhum cupom funcionou
            tempo = (datetime.now() - inicio).total_seconds()
            logger.warning(f"Nenhum cupom válido encontrado após testar {len(cupons_validos)}")
            
            return ResultadoCupom(
                cupom_valido=None,
                marketplace=marketplace,
                desconto=0,
                percentual=0,
                cupons_testados=len(cupons_validos),
                tempo_busca=tempo,
                preco_original=round(preco_base, 2) if preco_base else None,
                preco_final=round(preco_base, 2) if preco_base else None,
                url_produto=url_produto,
                item_adicionado=item_adicionado,
                cupons_sugeridos=gerar_sugestoes_cupons(cupons, preco_base),
            )
    
    except Exception as e:
        logger.error(f"Erro buscando cupom para {marketplace}: {str(e)}")
        tempo = (datetime.now() - inicio).total_seconds()
        return ResultadoCupom(
            cupom_valido=None,
            marketplace=marketplace,
            desconto=0,
            percentual=0,
            cupons_testados=0,
            tempo_busca=tempo,
            preco_original=round(preco_base, 2) if preco_base else None,
            preco_final=round(preco_base, 2) if preco_base else None,
            url_produto=url_produto,
            item_adicionado=item_adicionado,
            cupons_sugeridos=gerar_sugestoes_cupons(cupons, preco_base),
        )
    
    finally:
        if browser:
            await browser.close()

# ============================================================================
# ENDPOINTS FastAPI
# ============================================================================

@app.get("/health")
async def health():
    """Health check endpoint"""
    return {"status": "ok", "service": "cupons"}

@app.post("/testar_cupons")
async def testar_cupons(request: BuscaCupomRequest) -> Dict:
    """
    Busca e testa cupons para um ou múltiplos marketplaces
    
    Exemplo:
    {
        "termo_busca": "fone bluetooth",
        "marketplace": "kabum",  # ou None para testar todas
        "valor_carrinho": 150.0
    }
    
    Retorna:
    {
        "melhores_cupons": [
            {
                "cupom_valido": "TECH50",
                "marketplace": "kabum",
                "desconto": 75.0,
                "percentual": 50,
                "cupons_testados": 3,
                "tempo_busca": 45.2
            }
        ],
        "melhor_desconto": 75.0,
        "marketplaces_testados": ["kabum"]
    }
    """
    
    logger.info(f"Busca de cupons: termo={request.termo_busca} marketplace={request.marketplace or 'auto'}")

    marketplace_deduzido = None
    if request.url_produto:
        marketplace_deduzido = detectar_marketplace_por_url(request.url_produto)
        if not marketplace_deduzido:
            raise HTTPException(status_code=400, detail="Nao foi possivel detectar marketplace pela URL enviada")
    
    # Define quais marketplaces testar
    marketplaces_para_testar = []
    if request.marketplace:
        if request.marketplace not in CUPONS_BASE:
            raise HTTPException(status_code=400, detail=f"Marketplace {request.marketplace} nao suportado")
        marketplaces_para_testar = [request.marketplace]
    elif marketplace_deduzido:
        marketplaces_para_testar = [marketplace_deduzido]
    else:
        marketplaces_para_testar = list(CUPONS_BASE.keys())
    
    # Testa em paralelo
    resultados = await asyncio.gather(
        *[
            buscar_melhor_cupom(
                mp,
                request.valor_carrinho,
                request.url_produto,
            )
            for mp in marketplaces_para_testar
        ],
        return_exceptions=True
    )
    
    # Processa resultados
    resultados_lista = []
    melhor_desconto = 0
    melhor_resultado = None
    
    for resultado in resultados:
        if isinstance(resultado, Exception):
            logger.error(f"Erro processando resultado: {str(resultado)}")
            continue
        
        resultados_lista.append(resultado.dict())
        if resultado.cupom_valido and resultado.desconto > melhor_desconto:
            melhor_desconto = resultado.desconto
            melhor_resultado = resultado
    
    return {
        "termo_busca": request.termo_busca,
        "url_produto": request.url_produto,
        "valor_carrinho": request.valor_carrinho,
        "melhores_cupons": resultados_lista,
        "melhor_cupom": melhor_resultado.dict() if melhor_resultado else None,
        "cupons_sugeridos_manuais": {
            r["marketplace"]: r.get("cupons_sugeridos", [])
            for r in resultados_lista
            if r.get("cupons_sugeridos")
        },
        "melhor_desconto": round(melhor_desconto, 2),
        "marketplaces_testados": marketplaces_para_testar,
        "marketplace_detectado": marketplace_deduzido,
        "timestamp": datetime.now().isoformat()
    }

@app.get("/listar_cupons/{marketplace}")
async def listar_cupons(marketplace: str):
    """Lista os cupons disponíveis para um marketplace"""
    if marketplace not in CUPONS_BASE:
        raise HTTPException(status_code=404, detail=f"Marketplace não encontrado: {marketplace}")
    
    cupons = CUPONS_BASE[marketplace]
    return {
        "marketplace": marketplace,
        "total_cupons": len(cupons),
        "cupons": cupons
    }

@app.get("/listar_marketplaces")
async def listar_marketplaces():
    """Lista todos os marketplaces suportados"""
    return {
        "marketplaces_suportados": list(CUPONS_BASE.keys()),
        "total": len(CUPONS_BASE)
    }

# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9098, log_level="info")
