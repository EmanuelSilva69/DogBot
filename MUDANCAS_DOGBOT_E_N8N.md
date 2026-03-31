# Mudancas Implementadas no DogBot

## 1) Scraper especifico (scrapper.py)

### Estrategia atual
- Endpoint principal `POST /buscar` agora usa Promotech como fonte principal.
- Quando Promotech falha ou retorna vazio, existe fallback automatico para o fluxo legado (`/buscar_legado`) com `crawl4ai`.
- Fluxo legado foi preservado para uso futuro e cenarios de extracao multi-site.

### Coleta e robustez
- Tentativa de encontrar produtos por:
  - API interna do Promotech (quando disponivel)
  - JSON de hidratacao (`__NEXT_DATA__`)
  - fallback para parsing HTML
- Busca multiproduto no Promotech (padrao ate 10 itens, configuravel por `PROMOTECH_MAX_RESULTS`).

### Historico e termometro
- Persistencia SQLite em `prices_history.db`.
- Em Docker, caminho preferencial via `PRICE_DB_PATH=/app/data/prices_history.db` (com fallback automatico no codigo).
- Tabela `historico` para armazenar snapshots por termo/produto.
- Calculos por produto:
  - menor_30d
  - menor_90d
  - media_30d
  - avaliacao (Furada, Preco Normal, Promocao Boa, Nivel Black Friday)

### Formato de resposta do endpoint /buscar
- Retorna lista em `resultados` no formato compativel com o bot:
  - `resultados[].url`
  - `resultados[].itens[]`
  - em cada item: `nome`, `preco`, `loja`, `confiavel`, `preco_atual`, `menor_30d`, `menor_90d`, `avaliacao`, `link`
- Tambem retorna `melhor_oferta` para destaque rapido no bot/n8n.

## 2) Scraper geral (scrapper_geral.py)

### Objetivo
- Busca de produtos de uso geral em marketplaces com navegador headless (Playwright).

### Capacidades atuais
- Busca e comparacao em:
  - Mercado Livre
  - Amazon
- Coleta de ate 3 resultados por marketplace (quando disponiveis).
- Simulacao de CEP e tentativa de extracao de frete por pagina de produto.
- Escolha automatica da melhor oferta por `preco_total = preco_produto + valor_frete`.

### Saida
- JSON unico no console com melhor oferta (ou erro estruturado).

## 3) Bot WhatsApp (bot-whatsapp.py)

### Melhorias de mensagem
- Agora destaca bloco de `MELHOR OFERTA AGORA` no topo da resposta.
- Mostra termometro textual e nota visual por estrelas.
- Continua listando os demais itens encontrados.

## 4) Infra Docker

### Dockerfile
- Base Playwright oficial para suportar automacao de navegador.
- Instala dependencias via `requirements.txt`.
- Banco em `/app/data` para persistencia.

### docker-compose.yml
- Servico `ollama` adicionado na mesma rede Docker.
- `scrapper-python` reforcado para Playwright:
  - `init: true`
  - `shm_size: "1gb"`
- Healthchecks adicionados em servicos criticos.
- Dependencias com `condition` para reduzir race conditions de subida.
- Volume `./data:/app/data` para persistencia do SQLite.

## 5) Dependencias (requirements.txt)
- `fastapi`
- `uvicorn`
- `pydantic`
- `httpx`
- `requests`
- `requests-toolbelt`
- `beautifulsoup4`
- `playwright`
- `crawl4ai`
- `python-socketio[client]`
- `websocket-client`

## 6) Observacoes de operacao
- Se `crawl4ai` falhar em ambiente local (principalmente Python muito novo), usar Docker para execucao do legado.
- Rodar no host (apenas uma vez):
  - `python -m playwright install chromium`
- Para atualizar stack:
  - `docker compose down`
  - `docker compose build --no-cache scrapper-python`
  - `docker compose up -d`
