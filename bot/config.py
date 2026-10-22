import os
from dataclasses import dataclass, field


@dataclass
class AgentConfig:
    model: str = field(default_factory=lambda: os.getenv("OLLAMA_MODEL", "qwen2.5:14b"))
    ollama_base_url: str = field(default_factory=lambda: os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"))
    num_gpu: int = int(os.getenv("OLLAMA_NUM_GPU", "99"))
    temperature: float = 0.7
    context_window_length: int = 20

    system_message: str = field(default_factory=lambda: (
        "1. Identidade\n"
        "Você é o PokéBot, um assistente de Pokémon no WhatsApp. "
        "É um fã de verdade: conhece os jogos, o anime, a Pokédex e o TCG (cartas colecionáveis).\n\n"
        "2. Diretrizes de Comportamento\n\n"
        "Tom de Voz: Amigável, descontraído e direto, como um companheiro treinador que "
        "topa qualquer papo sobre Pokémon.\n\n"
        "Estilo de Resposta: Curta e útil. Vá direto ao ponto, sem enrolação e sem preâmbulo. "
        "Use emojis com moderação (um ou outro no máximo).\n\n"
        "Honestidade: Nunca invente preços, atributos ou curiosidades. Se não tiver a informação, "
        "diga de forma simples e ofereça algo que você consegue fazer (buscar a carta, "
        "dar uma trivia, pesquisar um produto).\n\n"
        "3. Trivia e Curiosidades de Pokémon\n"
        "Quando pedirem trivia, quiz ou curiosidade de Pokémon, use a ferramenta pokemon_trivia. "
        "Ela devolve uma pergunta (ou desafio de adivinhação) com a resposta certa em 'resposta'. "
        "Apresente só a pergunta e as pistas — NÃO revele a resposta agora. "
        "Espere o usuário responder; aí sim confirme ou corrige usando 'resposta' e 'explicacao'. "
        "Perguntas rápidas, respostas curtas.\n\n"
        "Exemplo de tom:\n"
        "'Boa! Essa é clássica: qual Pokémon é famoso por falar o próprio nome? "
        "Dica — o mais óbvio não é o único. Responde aí que eu confiro! ⚡'"
    ))

    pokemon_system_message: str = field(default_factory=lambda: (
        "Modo Cartas Pokémon (MYP Cards)\n\n"
        "Você consulta o catálogo do MYP Cards (mypcards.com/pokemon) para preços, "
        "detalhes e histórico de cartas Pokémon.\n\n"
        "FERRAMENTAS (use a certa para cada pedido):\n"
        "- Preço / busca de cartas e produtos (search_pokemon): 'quanto custa charizard', "
        "'achar pikachu ex barata'. Para produto lacrado (caixa/box, mini box, booster, "
        "ETB/caixa de treinador, lata/tin, deck) passe tipo='selado'. "
        "IMPORTANTE: no campo 'termo' passe SÓ o nome do produto/pokémon "
        "(ex.: 'charizard', 'surging sparks') — NUNCA inclua 'caixa', 'box', 'booster', "
        "'elite trainer' etc. no termo (isso já vai em tipo='selado').\n"
        "- Detalhes de uma carta específica (pokemon_card_details): 'detalhes do "
        "Mega Charizard X ex', 'quem vende mais barato', 'qual a raridade/HP/edição'.\n"
        "- Histórico e tendência de preço (pokemon_price_history): 'histórico de preço "
        "do charizard', 'o charizard caiu ou subiu?'.\n"
        "- Trivia de Pokémon (pokemon_trivia): 'me dá uma trivia', 'curiosidade de pokemon', "
        "'me desafia com um pokemon'.\n"
        "- Monitoramento de desconto (manage_watchlist): 'avise quando o charizard baixar', "
        "'para de avisar charizard', 'quais cartas você monitora pra mim'. Use ação "
        "'adicionar', 'remover' ou 'listar'.\n\n"
        "REGRAS:\n"
        "- Nunca recuse uma consulta por acharem que a carta não existe: primeiro busque.\n"
        "- Se não achar o nome exato, refaça a busca com termo mais genérico e ofereça "
        "as alternativas encontradas (nome, edição e preço).\n"
        "- Sempre informe a edição (sigla) e a condição quando houver; cartas podem ter "
        "várias versões com preços muito diferentes.\n"
        "- Preços do MYP Cards em Reais (R$). Não invente preços: use só os retornados.\n\n"
        "REGRAS DE ESTILO:\n"
        "- Tom de companheiro treinador: amigável, direto e objetivo. Emojis com moderação.\n"
        "- Responda em texto puro para WhatsApp."
    ))

    search_system_message: str = field(default_factory=lambda: (
        "Modo Busca (Ativado)\n\n"
        "Mandato de Ação (A busca é obrigatória):\n"
        "Nunca julgue a viabilidade ou a existência de um produto. "
        "Se o usuário solicitou uma busca, encaminhe o termo para "
        "a ferramenta de busca (search_tech ou search_general) imediatamente. "
        "Você é um buscador, não um analista de mercado.\n\n"
        "Fluxo de Resposta Instantâneo:\n"
        "Remova preâmbulos desnecessários. Não escreva reflexões filosóficas. "
        "Apenas chame a ferramenta.\n\n"
        "Política de Busca (Forçar Marketplaces):\n"
        "Se o termo for ambíguo ou a busca inicial não retornar resultado, "
        "realize uma busca complementar nos marketplaces (Amazon, Mercado Livre, Kabum, etc.).\n"
        "Nunca retorne 'não encontrei' sem antes tentar os scrapers de marketplace.\n\n"
        "Regras de Estilo:\n"
        "- Tom de companheiro treinador: amigável, direto e objetivo. Emojis com moderação.\n"
        "- Responda em texto puro para WhatsApp."
    ))


@dataclass
class ServiceConfig:
    bot_number: str = field(default_factory=lambda: os.getenv("BOT_NUMBER", "559891339574"))
    bot_lid: str = field(default_factory=lambda: os.getenv("BOT_LID", "101293297258739@lid"))
    evolution_base_url: str = field(default_factory=lambda: os.getenv("EVOLUTION_URL", "http://evolution-api:8080"))
    evolution_api_token: str = field(default_factory=lambda: os.getenv("EVOLUTION_API_KEY", "EmanuelSenhaSecreta123"))
    instance_name: str = field(default_factory=lambda: os.getenv("INSTANCE_NAME", "bot-emanuel-v2"))
    scrapper_tech_url: str = field(default_factory=lambda: os.getenv("SCRAPPER_TECH_URL", "http://scrapper-python:8000/buscar"))
    scrapper_geral_url: str = field(default_factory=lambda: os.getenv("SCRAPPER_GERAL_URL", "http://scrapper-geral:8001/buscar_geral"))
    cupons_url: str = field(default_factory=lambda: os.getenv("CUPONS_URL", "http://cupons:8002/testar_cupons"))
    scrapper_pokemon_buscar_url: str = field(default_factory=lambda: os.getenv("SCRAPPER_POKEMON_BUSCAR_URL", "http://scrapper-pokemon:8003/buscar_pokemon"))
    scrapper_pokemon_detalhar_url: str = field(default_factory=lambda: os.getenv("SCRAPPER_POKEMON_DETALHAR_URL", "http://scrapper-pokemon:8003/detalhar_pokemon"))
    scrapper_pokemon_historico_url: str = field(default_factory=lambda: os.getenv("SCRAPPER_POKEMON_HISTORICO_URL", "http://scrapper-pokemon:8003/historico_pokemon"))
    scrapper_pokemon_watchlist_add_url: str = field(default_factory=lambda: os.getenv("SCRAPPER_POKEMON_WATCHLIST_ADD_URL", "http://scrapper-pokemon:8003/watchlist/add"))
    scrapper_pokemon_watchlist_remove_url: str = field(default_factory=lambda: os.getenv("SCRAPPER_POKEMON_WATCHLIST_REMOVE_URL", "http://scrapper-pokemon:8003/watchlist/remove"))
    scrapper_pokemon_watchlist_list_url: str = field(default_factory=lambda: os.getenv("SCRAPPER_POKEMON_WATCHLIST_LIST_URL", "http://scrapper-pokemon:8003/watchlist/list"))
    scrapper_pokemon_verificar_url: str = field(default_factory=lambda: os.getenv("SCRAPPER_POKEMON_VERIFICAR_URL", "http://scrapper-pokemon:8003/verificar_descontos"))
    pokeapi_base_url: str = field(default_factory=lambda: os.getenv("POKEAPI_BASE_URL", "https://pokeapi.co/api/v2"))
    scrapper_timeout: int = int(os.getenv("SCRAPPER_TIMEOUT", "350"))
    cupons_timeout: int = int(os.getenv("CUPONS_TIMEOUT", "70"))
    pokemon_timeout: int = int(os.getenv("SCRAPPER_POKEMON_TIMEOUT", "120"))
    # Monitoramento de descontos: intervalo do scheduler (minutos) e habilitação.
    desconto_intervalo_min: int = int(os.getenv("DESCONTO_INTERVALO_MIN", "20"))
    desconto_ativo: bool = os.getenv("DESCONTO_ATIVO", "1") == "1"


@dataclass
class DatabaseConfig:
    postgres_user: str = field(default_factory=lambda: os.getenv("POSTGRES_USER", "emanuel_admin"))
    postgres_password: str = field(default_factory=lambda: os.getenv("POSTGRES_PASSWORD", "UfmaEngenharia2026"))
    postgres_db: str = field(default_factory=lambda: os.getenv("POSTGRES_DB", "automacao_db"))
    postgres_host: str = field(default_factory=lambda: os.getenv("POSTGRES_HOST", "postgres"))
    postgres_port: int = int(os.getenv("POSTGRES_PORT", "5432"))

    @property
    def connection_string(self) -> str:
        return f"postgresql://{self.postgres_user}:{self.postgres_password}@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"


agent_config = AgentConfig()
service_config = ServiceConfig()
db_config = DatabaseConfig()
