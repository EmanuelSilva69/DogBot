# Prompt para Revisar e Aplicar Fluxo n8n (DogBot)

Use o texto abaixo em um node de IA (ou em uma revisao manual) para validar e propor o melhor fluxo do n8n com as novas adicoes.

---

Voce e um especialista em n8n, integracoes WhatsApp e orquestracao de scrapers Python.

Contexto atual:
- O bot envia comando `buscar <termo>`.
- O endpoint principal do scraper e `POST http://scrapper-python:8000/buscar`.
- O JSON de resposta agora pode conter:
  - `resultados` (lista de produtos em `resultados[].itens[]`)
  - `melhor_oferta` (item de destaque)
  - `fonte`, `termo`, `total_encontrados`
  - Em fallback legado: `resultados` com formato antigo e possivel `fallback_usado=true`.

Objetivo:
- Propor um fluxo n8n robusto e simples para:
  1) receber comando do WhatsApp,
  2) chamar o scraper,
  3) montar resposta final com prioridade em `melhor_oferta`,
  4) incluir ranking visual da promocao (estrelas),
  5) tratar erros e fallback automaticamente.

Requisitos da proposta:
1. Descrever os nodes do n8n em ordem, com nome de cada node e papel.
2. Incluir expressoes do n8n para mapear campos-chave:
   - melhor item: `{{$json.melhor_oferta}}`
   - lista: `{{$json.resultados}}`
   - avaliacao: `{{$json.melhor_oferta.avaliacao}}`
3. Definir regra para estrelas no n8n (Function/Code node):
   - Black Friday => 5 estrelas
   - Promocao Boa => 4 estrelas
   - Preco Normal => 3 estrelas
   - Furada => 1 estrela
4. Definir tratamento de falha:
   - se `resultados` vazio ou erro, retornar mensagem amigavel.
   - se `fallback_usado=true`, adicionar aviso: "fonte secundaria utilizada".
5. Sugerir limite de itens exibidos no WhatsApp (ex.: top 5) para evitar mensagem gigante.
6. Retornar no final:
   - desenho do fluxo (node a node)
   - exemplos de payload de entrada/saida
   - template final de mensagem WhatsApp pronto para usar
   - checklist de testes (sucesso, sem resultados, timeout, fallback)

Responda em portugues, de forma objetiva e aplicavel direto no n8n.

---

## Fluxo-base sugerido (referencia rapida)

1. Webhook/Trigger WhatsApp
2. Node de parse do comando (`buscar <termo>`)
3. HTTP Request para `POST /buscar`
4. IF: tem `resultados`?
5. Code node: calcula estrelas + normaliza mensagem
6. Node de envio WhatsApp
7. Error branch: mensagem de falha amigavel

## Template curto de mensagem

`🏆 Melhor oferta: {{nome}}\n💰 {{preco}}\n🏪 {{loja}}\n🌡️ {{avaliacao}}\n⭐ {{estrelas}}\n🔗 {{link}}\n\nOutras opcoes: {{top_itens}}`
