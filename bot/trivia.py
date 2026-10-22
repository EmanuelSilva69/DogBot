"""
Trivia de Pokémon para o bot.

Duas fontes:
  1. Banco local de perguntas/fatos em PT-BR (funciona offline, determinístico).
  2. PokéAPI (https://pokeapi.co) para desafios dinâmicos de adivinhação,
     com fallback para o banco local quando a API falha.
"""

import random
import re
from typing import List, Optional

import httpx

from bot.config import service_config

# ────────────────────────────── Banco local ──────────────────────────────
# Dica de uso pelo agente: apresente a pergunta sem revelar a resposta e
# só confirme/corrija quando o usuário responder.

PERGUNTAS: List[dict] = [
    # ── Tipos ──
    {
        "categoria": "tipos",
        "pergunta": "Qual é o tipo do Pikachu?",
        "resposta": "Elétrico",
        "dica": "Ele solta raios.",
        "explicacao": "Pikachu é do tipo Elétrico puro. A única exceção é a forma de bico do Sul de Paldea (Scarlet/Violet), que é Elétrico/Voador.",
        "dificuldade": "facil",
    },
    {
        "categoria": "tipos",
        "pergunta": "Existe algum tipo Pokémon sem nenhuma fraqueza?",
        "resposta": "Não. Todo tipo tem ao menos uma fraqueza; o mais perto disso é Fantasma, que é imune a Normal e Lutador.",
        "dica": "Pense em imunidades, não em falta de fraqueza.",
        "explicacao": "Não existe tipo sem fraqueza. Fantasma é imune a Normal e Lutador; Voador é imune a Terra; e assim por diante.",
        "dificuldade": "dificil",
    },
    {
        "categoria": "tipos",
        "pergunta": "Qual tipo é super eficaz contra Psíquico?",
        "resposta": "Fantasma e Inseto",
        "dica": "Depois da Geração 1, Psíquico ficou menos quebrado.",
        "explicacao": "Na Geração 1, Fantasma não funcionava por bug. Desde a Geração 2, Fantasma e Inseto são super eficazes contra Psíquico.",
        "dificuldade": "medio",
    },
    {
        "categoria": "tipos",
        "pergunta": "Quantos tipos existem atualmente no Pokémon TCG/jogos?",
        "resposta": "18 tipos",
        "dica": "O último a entrar foi o tipo Fada, na Geração 6.",
        "explicacao": "São 18 tipos: Normal, Fogo, Água, Elétrico, Gelo, Lutador, Veneno, Terra, Voador, Psíquico, Inseto, Pedra, Fantasma, Dragão, Sombrio, Aço, Fada e Estrela (o tipo do Pokémon inicial de Paldea).",
        "dificuldade": "medio",
    },
    {
        "categoria": "tipos",
        "pergunta": "Quais são as fraquezas do tipo Dragão?",
        "resposta": "Dragão, Gelo e Fada",
        "dica": "Fada entrou na Geração 6 justamente para segurar Dragão.",
        "explicacao": "Dragão é fraco contra Dragão, Gelo e Fada. Por muito tempo só Gelo e Dragão seguravam o tipo; o Fada chegou depois como dente extra.",
        "dificuldade": "facil",
    },
    # ── Evoluções ──
    {
        "categoria": "evolucoes",
        "pergunta": "Qual Pokémon precisa de troca para evoluir, entre Eevee, Machop e Charmander?",
        "resposta": "Machop (evolui para Machoke via troca, antes da Pedra do Trovão)",
        "dica": "É um lutador muscular.",
        "explicacao": "Machoke evolui para Machamber/Machamp por troca. Eevee evolui por pedras e Charmander por nível.",
        "dificuldade": "facil",
    },
    {
        "categoria": "evolucoes",
        "pergunta": "Quantas evoluções Eevee tem hoje?",
        "resposta": "8 evoluções (eeveelutions)",
        "dica": "Vaporeon, Jolteon, Flareon, Espeon, Umbreon, Leafeon, Glaceon e Sylveon.",
        "explicacao": "São as 8 eeveelutions, cada uma de um tipo diferente: Água, Elétrico, Fogo, Psíquico, Sombrio, Planta, Gelo e Fada.",
        "dificuldade": "facil",
    },
    {
        "categoria": "evolucoes",
        "pergunta": "Qual Pokémon evolui para Conkeldurr?",
        "resposta": "Gurdurr (que evolui de Timburr)",
        "dica": "A linha toda carrega vigas de concreto.",
        "explicacao": "Timburr → Gurdurr (por troca) → Conkeldurr (por nível, depois da troca).",
        "dificuldade": "medio",
    },
    {
        "categoria": "evolucoes",
        "pergunta": "Qual Pokémon evolui em três evoluções diferentes dependendo do nível de ataque, defesa e velocidade?",
        "resposta": "Tyrogue (Hitmonlee, Hitmonchan e Hitmontop)",
        "dica": "É o precursores dos lutadores da Geração 1.",
        "explicacao": "Tyrogue evolui para Hitmonlee (ataque alto), Hitmonchan (defesa) ou Hitmontop (velocidade), conforme os atributos. Eevee ainda lidera em número total, com 8 eeveelutions.",
        "dificuldade": "dificil",
    },
    # ── Jogos ──
    {
        "categoria": "jogos",
        "pergunta": "Em qual geração foi introduzido o tipo Fada?",
        "resposta": "Geração 6 (X e Y)",
        "dica": "A mesma geração trouxe Mega Evolução e Kalos.",
        "explicacao": "O tipo Fada estreou em Pokémon X e Y (2013), junto com a Mega Evolução e a região de Kalos.",
        "dificuldade": "facil",
    },
    {
        "categoria": "jogos",
        "pergunta": "Qual região é o cenário de Pokémon Rubro e Safira (e Emerald)?",
        "resposta": "Hoenn",
        "dica": "É a região com o Monte Pyre e a Equipe Magma/Aquática.",
        "explicacao": "Hoenn, a terceira região apresentada (Geração 3, 2002).",
        "dificuldade": "facil",
    },
    {
        "categoria": "jogos",
        "pergunta": "Como se chama a cidade onde fica o Laboratório do Professor Carvalho em Kanto?",
        "resposta": "Pallet (Pallet Town)",
        "dica": "Significa 'paleta' em inglês — a cidade 'em branco'.",
        "explicacao": "Pallet Town é a cidade inicial de Red e Blue, no extremo oeste de Kanto.",
        "dificuldade": "facil",
    },
    {
        "categoria": "jogos",
        "pergunta": "Qual é o primeiro jogo Pokémon lançado fora do Japão?",
        "resposta": "Pokémon Red e Blue (1998 na América/Europa)",
        "dica": "Era para o Game Boy.",
        "explicacao": "Red e Blue saíram no Ocidente em 1998; no Japão, Red e Green vieram em 1996.",
        "dificuldade": "medio",
    },
    {
        "categoria": "jogos",
        "pergunta": "Qual é o nome da geração que introduziu a Terra Tera?",
        "resposta": "Geração 9 (Escarlata e Violeta, região Paldea)",
        "dica": "A região é baseada na península ibérica.",
        "explicacao": "A Terastalização estreou em Paldea (Geração 9), junto com raids tera e open world.",
        "dificuldade": "medio",
    },
    {
        "categoria": "jogos",
        "pergunta": "Quantos Pokémon havia na Pokédex da Geração 1?",
        "resposta": "151",
        "dica": "Mewtwo é o 150.",
        "explicacao": "A Pokédex original ia de Bulbasaur (001) a Mew (151).",
        "dificuldade": "facil",
    },
    # ── Curiosidades ──
    {
        "categoria": "curiosidades",
        "pergunta": "Qual Pokémon é famoso por 'falar' o próprio nome?",
        "resposta": "Pikachu... e também Slowpoke, Charmander e outros 'falam' o nome próprio na dublagem original.",
        "dica": "Pikachu é o mais óbvio, mas a resposta é mais ampla.",
        "explicacao": "Na dublagem japonesa, vários Pokémon dizem seu nome (Pikachu, Slowpoke, Charmander...). É uma herança direta do anime.",
        "dificuldade": "medio",
    },
    {
        "categoria": "curiosidades",
        "pergunta": "Qual Pokémon é baseado em um bicho do tamanho de um gato e é do tipo Elétrico?",
        "resposta": "Pikachu (inspirado em pikachū, um roedor do folclore japonês, e em ratos/vespas)",
        "dica": "Ratata não é elétrico.",
        "explicacao": "Pikachu mescla 'pika' (som do raio/relâmpago) e 'chu' (som de ratinho) e tem traços de esquilo/vespa.",
        "dificuldade": "facil",
    },
    {
        "categoria": "curiosidades",
        "pergunta": "O que significa 'Pokémon'?",
        "resposta": "Abreviação de 'Pocket Monsters' (monstros de bolso)",
        "dica": "Bolso + monstro.",
        "explicacao": "Satoshi Tajiri criou a franquia a partir de colecionar insetos; 'Pocket Monsters' virou 'Pokémon' por facilidade de pronúncia.",
        "dificuldade": "facil",
    },
    {
        "categoria": "curiosidades",
        "pergunta": "Qual é o Pokémon mais alto entre os normais da Pokédex?",
        "resposta": "Wailord (14,5 m)",
        "dica": "É uma baleia.",
        "explicacao": "Wailord tem 14,5 m e quase nada de peso. Entre formas especiais, Eternatus Eternamax chega a 200 m, mas é exceção.",
        "dificuldade": "medio",
    },
    {
        "categoria": "curiosidades",
        "pergunta": "Qual Pokémon troca de forma entre uma espada e um escudo vivo?",
        "resposta": "Aegislash",
        "dica": "Ele muda de postura conforme ataca ou defende.",
        "explicacao": "Aegislash alterna entre Forma Lâmina (ataque alto) e Forma Escudo (defesa alta), algo raro entre Pokémon.",
        "dificuldade": "medio",
    },
    {
        "categoria": "curiosidades",
        "pergunta": "Qual Pokémon é conhecido por passar quase o dia inteiro dormindo e só acorda para comer?",
        "resposta": "Snorlax",
        "dica": "Ele costuma bloquear estradas dormindo no meio do caminho.",
        "explicacao": "Snorlax dorme quase 24 horas por dia; nos jogos, ele bloqueava a Rota 12 e só se movia quando acordava com a Flauta.",
        "dificuldade": "facil",
    },
    # ── Animação ──
    {
        "categoria": "animacao",
        "pergunta": "Como se chama a forma especial que Ash e Greninja compartilhavam no anime da Geração 6?",
        "resposta": "Ash-Greninja",
        "dica": "O fenômeno é chamado de 'Bond Phenomenon'.",
        "explicacao": "Ash-Greninja é a forma 'Vínculo' de Greninja, que só aparece quando há laço forte com Ash — e não é uma evolução normal.",
        "dificuldade": "medio",
    },
    {
        "categoria": "animacao",
        "pergunta": "Qual Pokémon voador da Geração 3 Ash capturou e que evoluiu para Swellow?",
        "resposta": "Taillow",
        "dica": "É um pássaro azul e corajoso.",
        "explicacao": "Ash capturou Taillow em Hoenn, que evoluiu para Swellow — um dos Pokémon mais marcantes dele na região.",
        "dificuldade": "medio",
    },
    {
        "categoria": "animacao",
        "pergunta": "Quem é o rival de Ash na região de Kanto no anime?",
        "resposta": "Gary Oak (Professor Oak é avô dele)",
        "dica": "Ele sai de Pallet Town junto com Ash.",
        "explicacao": "Gary Oak é o rival de infância de Ash e aparece em boa parte da série inicial.",
        "dificuldade": "facil",
    },
    {
        "categoria": "animacao",
        "pergunta": "Qual Pokémon lendário aparece no filme de 1998 conhecido como 'O Filme'?",
        "resposta": "Mewtwo",
        "dica": "É um clone de Mew.",
        "explicacao": "O primeiro filme (1998/1999) gira em torno de Mewtwo, o clone criado pela Equipe R.",
        "dificuldade": "facil",
    },
    # ── Cartas / TCG ──
    {
        "categoria": "cartas",
        "pergunta": "O que significa 'HP' na carta Pokémon?",
        "resposta": "Pontos de vida do Pokémon (Health Points)",
        "dica": "Quando zera, o Pokémon é nocauteado.",
        "explicacao": "HP indica quantos pontos de dano o Pokémon suporta antes de ser nocauteado.",
        "dificuldade": "facil",
    },
    {
        "categoria": "cartas",
        "pergunta": "O que é 'Foil' (ou holo) em uma carta Pokémon?",
        "resposta": "Uma versão com acabamento metálico/iridescente na arte",
        "dica": "Brilha na luz.",
        "explicacao": "Cartas foil/holo têm acabamento reflexivo; no TCG elas costumam valer mais que a versão comum da mesma carta.",
        "dificuldade": "facil",
    },
    {
        "categoria": "cartas",
        "pergunta": "O que significa a sigla 'NM' na condição de uma carta?",
        "resposta": "Near Mint — quase nova",
        "dica": "É a melhor condição comum em vendas usadas.",
        "explicacao": "NM (Near Mint) é quase impecável; logo abaixo vem SP (pouco jogada) e LP (jogada), até DM (danificada).",
        "dificuldade": "medio",
    },
    {
        "categoria": "cartas",
        "pergunta": "O que é uma carta 'ex' no TCG Pokémon?",
        "resposta": "Uma carta de Pokémon ex: mais forte, com mais HP, que entrega 2 prêmios ao ser nocauteado",
        "dica": "Ela vem com uma regra escrita na carta.",
        "explicacao": "Pokémon ex (e V, VMAX, VSTAR) dão mais prêmios quando são nocauteados — o risco de usar cartas fortes.",
        "dificuldade": "facil",
    },
    {
        "categoria": "cartas",
        "pergunta": "O que é 'Graduada' (graded) no mundo das cartas?",
        "resposta": "Uma carta avaliada e encapsulada por uma empresa (PSA, BGS, CGC) com nota de 1 a 10",
        "dica": "PSA 10 é o sonho dos colecionadores.",
        "explicacao": "Cartas graduadas são lacradas com nota de autenticidade/estado; PSA 10 (Gem Mint) pode multiplicar o preço.",
        "dificuldade": "medio",
    },
    {
        "categoria": "cartas",
        "pergunta": "Quantas cartas de Prêmio são entregues no início de uma partida TCG Pokémon?",
        "resposta": "6 cartas de prêmio",
        "dica": "É o mesmo número desde 1999.",
        "explicacao": "Cada jogador põe 6 cartas de prêmio; quem pega as 6 primeiro vence. Pokémon ex/V entregam 2.",
        "dificuldade": "facil",
    },
]

FATOS: List[dict] = [
    {"categoria": "curiosidades", "fato": "Gengar sorri porque está sempre escondido atrás de você — e a sombra dele se move sozinha.", "explicacao": "Gengar adora brincar de esconde-esconde e some quando você olha para ele."},
    {"categoria": "curiosidades", "fato": "Slowpoke é lento a ponto de demorar um dia para reagir a uma dor.", "explicacao": "Tanto que no anime ele só percebe quando alguém o morde horas depois."},
    {"categoria": "curiosidades", "fato": "O nome de Snorlax vem de 'snore' (roncar) + 'relax'.", "explicacao": "Ele come, dorme e ronca — a vida boa."},
    {"categoria": "curiosidades", "fato": "Charizard nunca foi um Pokémon inicial em jogo nenhum — Charmander é.", "explicacao": "Muita gente confunde: Charizard é a evolução final, não inicial."},
    {"categoria": "curiosidades", "fato": "Exeggutor tem três cabeças porque o modelo é uma palmeira com três frutos.", "explicacao": "Cada cabeça pensa de um jeito e às vezes se contradizem."},
    {"categoria": "curiosidades", "fato": "O nome 'Pikachu' combina 'pika' (som do relâmpago) com 'chu' (som de ratinho japonês).", "explicacao": "Satoshi Tajiri queria um bichinho pequeno e elétrico — e o nome imitava o próprio som do Pokémon."},
    {"categoria": "jogos", "fato": "Na Geração 1, o tipo Fantasma não funcionava contra Psíquico por causa de um bug de tradução.", "explicacao": "O jogo dizia que Fantasma não afetava Psíquico — e o bug virou regra por muito tempo."},
    {"categoria": "jogos", "fato": "MissingNo. é o 'Pokémon' ausente que podia ser invadido na costa de Cinnabar.", "explicacao": "Ele bagunçava o jogo e podia duplicar itens — lendário entre os players de Geração 1."},
    {"categoria": "jogos", "fato": "O Pikachu com boné, distribuído em eventos presenciais de 1999, virou uma das peças mais cobiçadas dos colecionadores.", "explicacao": "Eventos antigos de distribuição física (que exigiam presença no local) hoje valem fortunas em estado mint."},
    {"categoria": "cartas", "fato": "Charizard 1º Edição (Base Set, 1999) é uma das cartas mais caras do mundo.", "explicacao": "Exemplares PSA 10 já passaram de US$ 400 mil em leilões."},
    {"categoria": "cartas", "fato": "O primeiro Booster Pack Pokémon em inglês foi o Base Set, em 1999.", "explicacao": "Ele trouxe os 102 cards iniciais e definiu o padrão do TCG."},
    {"categoria": "cartas", "fato": "No mercado brasileiro, cartas em Português costumam valer menos que as em Inglês — exceto as raras de edição antiga.", "explicacao": "Colecionadores internacionais valorizam mais as versões em inglês e japonês."},
    {"categoria": "animacao", "fato": "O episódio de 1997 com Porygon gerou convulsões fotossensíveis no Japão por causa dos flashes rápidos.", "explicacao": "O caso 'Dennou Senshi Porygon' mudou as regras de animação da TV japonesa — e Porygon nunca mais teve episódio próprio."},
    {"categoria": "animacao", "fato": "Mewtwo foi cloneado a partir do DNA de Mew na Ilha Cynthya.", "explicacao": "A Equipe R tentou usar Mewtwo como arma — e o filme de 1998 conta essa história."},
    {"categoria": "evolucoes", "fato": "Eevee pode evoluir de oito formas diferentes, dependendo de pedra, amizade, hora e até tipo de move.", "explicacao": "É o Pokémon com mais 'caminhos' de evolução da franquia."},
    {"categoria": "tipos", "fato": "O tipo Fada foi criado justamente para equilibrar Dragão na Geração 6.", "explicacao": "Dragão reinava nos campeonatos; Fada entrou como sua fraqueza natural."},
]


def _limpar_categoria(categoria: Optional[str]) -> Optional[str]:
    if not categoria:
        return None
    alvo = categoria.strip().lower()
    for c in CATEGORIAS:
        if alvo in c or c in alvo:
            return c
    return None


CATEGORIAS: List[str] = sorted({p["categoria"] for p in PERGUNTAS} | {f["categoria"] for f in FATOS})


# ────────────────────────────── Fontes ──────────────────────────────

def _pergunta_local(categoria: Optional[str] = None) -> dict:
    candidatas = [p for p in PERGUNTAS if not categoria or p["categoria"] == categoria]
    if not candidatas:
        candidatas = PERGUNTAS
    p = random.choice(candidatas)
    return {
        "modo": "pergunta",
        "categoria": p["categoria"],
        "pergunta": p["pergunta"],
        "resposta": p["resposta"],
        "dica": p.get("dica"),
        "explicacao": p.get("explicacao"),
        "dificuldade": p.get("dificuldade"),
        "fonte": "banco_local",
    }


def _fato_local(categoria: Optional[str] = None) -> dict:
    candidatos = [f for f in FATOS if not categoria or f["categoria"] == categoria]
    if not candidatos:
        candidatos = FATOS
    f = random.choice(candidatos)
    return {
        "modo": "curiosidade",
        "categoria": f["categoria"],
        "curiosidade": f["fato"],
        "explicacao": f.get("explicacao"),
        "fonte": "banco_local",
    }


async def _desafio_pokeapi() -> dict:
    """Gera um desafio de adivinhação com dados reais da PokéAPI."""
    pokemon_id = random.randint(1, 1025)
    base = service_config.pokeapi_base_url.rstrip("/")

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{base}/pokemon/{pokemon_id}")
        resp.raise_for_status()
        pokemon = resp.json()

        especie = {}
        try:
            resp_sp = await client.get(pokemon.get("species", {}).get("url") or f"{base}/pokemon-species/{pokemon_id}")
            if resp_sp.status_code == 200:
                especie = resp_sp.json()
        except Exception:
            especie = {}

    tipos = [t["type"]["name"] for t in pokemon.get("types", [])]
    altura_m = round(pokemon.get("height", 0) / 10, 1)
    peso_kg = round(pokemon.get("weight", 0) / 10, 1)

    geracao = None
    gen_url = (especie.get("generation") or {}).get("url", "")
    m = re.search(r"/generation/(\d+)/?$", gen_url)
    if m:
        geracao = int(m.group(1))

    texto_jogo = None
    entradas = especie.get("flavor_text_entries") or []
    entradas_idioma = [e for e in entradas if e.get("language", {}).get("name") == "en"]
    if entradas_idioma:
        texto_jogo = re.sub(r"\s+", " ", random.choice(entradas_idioma).get("flavor_text", "")).strip()

    pistas = [
        f"Tipos: {' e '.join(t.capitalize() for t in tipos)}",
        f"Altura: {altura_m} m | Peso: {peso_kg} kg",
    ]
    if geracao:
        pistas.insert(0, f"Pertence à Geração {geracao}")
    if texto_jogo:
        pistas.append(f"Texto dos jogos: {texto_jogo}")

    return {
        "modo": "adivinhar",
        "categoria": "pokeapi",
        "pergunta": "Adivinhe qual é este Pokémon. As pistas estão abaixo:",
        "pistas": pistas,
        "resposta": pokemon.get("name"),
        "dica": f"Dos {len(tipos)} tipos: {', '.join(tipos)}.",
        "explicacao": (
            f"{pokemon.get('name', '?').capitalize()} — "
            f"geração {geracao or 'desconhecida'}, tipos {', '.join(tipos)}, "
            f"{altura_m} m e {peso_kg} kg."
        ),
        "fonte": "pokeapi",
        "nota": (
            "O texto dos jogos vem em inglês — traduza ao apresentar a pista."
            if texto_jogo else None
        ),
        "imagem": (pokemon.get("sprites") or {}).get("other", {}).get("official-artwork", {}).get("front_default"),
    }


# ────────────────────────────── API pública ──────────────────────────────

INSTRUCAO_AGENTE = (
    "Apresente o desafio ao usuário de forma curta e NÃO revele a resposta agora. "
    "Apenas quando o usuário responder é que você confirma ou corrige, usando o campo 'resposta' e 'explicacao'."
)


async def sortear_trivia(categoria: Optional[str] = None, modo: str = "auto") -> dict:
    """
    Monta uma rodada de trivia.

    modo:
      - "pergunta"     -> perguntas do banco local
      - "curiosidade"  -> fato rápido do banco local
      - "adivinhar"    -> desafio dinâmico via PokéAPI
      - "auto"         -> sorteia entre pergunta/adivinhar
    """
    cat = _limpar_categoria(categoria)
    modo = (modo or "auto").strip().lower()

    if modo == "pergunta":
        resultado = _pergunta_local(cat)
    elif modo == "curiosidade":
        resultado = _fato_local(cat)
    elif modo == "adivinhar":
        try:
            resultado = await _desafio_pokeapi()
        except Exception:
            resultado = _pergunta_local(cat)
            resultado["aviso"] = "PokéAPI indisponível — usei o banco local."
    else:
        if random.random() < 0.4:
            try:
                resultado = await _desafio_pokeapi()
            except Exception:
                resultado = _pergunta_local(cat)
        else:
            resultado = _pergunta_local(cat)

    resultado["categorias_disponiveis"] = CATEGORIAS
    resultado["instrucao"] = INSTRUCAO_AGENTE
    return resultado
