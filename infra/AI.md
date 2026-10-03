# IA — desenvolvimento

## Fluxo e limites

Captura confirma conteúdo/outbox → Cloud Tasks → `queep-worker` privado → análise e
vetor publicados na revisão atual. Dispatcher agendado a cada minuto recupera falhas
de envio. Jobs usam lease de 360 s, até 3 tentativas e identificadores diferentes por
execução/revisão. Reenvio de tarefa não repete trabalho concluído; edição/exclusão
invalida o processamento anterior. O botão de nova tentativa abre outra execução.

`POST /v1/search` usa apenas documentos prontos do proprietário no mesmo espaço de
embeddings. Seleciona 10 candidatos e confere correspondência; revalida revisões e
exclusões antes de responder. Pode retornar vazio. Não garante todos os resultados
de uma biblioteca grande. Consultas usam POST para não expor o texto em URLs.
O verificador informa também se a consulta exige um formato (`requested_kind`). A
API confere os tipos reais antes de devolver os IDs; um pedido explícito de vídeo
não pode retornar áudio, mesmo que o modelo o inclua na seleção. Assunto e formato
são separados: ferramentas para criar vídeos não precisam ser arquivos de vídeo.

| Etapa | Modelo / rota |
| --- | --- |
| Notas, imagens e texto de links | `google/gemini-2.5-flash-lite` / `google-vertex` |
| Transcrição e cenas de vídeo, em chamadas separadas | `google/gemini-2.5-flash-lite` / `google-vertex` |
| Vetores de 768 dimensões | `google/gemini-embedding-2` / `google-vertex/us` |
| Conferência da busca | `google/gemini-3.1-flash-lite` / `google-vertex/global` |

Configuração versionada em `queep/ai.py`. Rotas fixas, sem fallback de provedor,
ZDR exigido e coleta negada. A chave fica em `queep-openrouter`, Secret Manager;
somente identidades da API e worker leem o segredo. Não há chave de IA no Swift.
Tratamento em nuvem não é criptografia ponta a ponta: os provedores recebem o
material necessário à análise. Firestore/Storage continuam privados.

Limite de desenvolvimento: US$ 1/dia compartilhado, reservas de US$ 0,01 antes de
cada chamada, acerto pelo custo reportado; custo desconhecido mantém a reserva.
São controles conservadores, não um teto de faturamento Firebase. Há também limite
de 100 buscas/conta/dia e intervalo mínimo de 1 s. Registros de custo não incluem
conteúdo; `ai_calls` expira por TTL após 30 dias. Chave OpenRouter tem limite próprio.

Imagens são reduzidas a 1536 px para interpretação. Fotos e pôsteres de vídeo
anexados a um post social seguem o mesmo caminho, mesmo quando o texto do post
não pôde ser lido: entram na interpretação, no documento vetorial e na conferência
da busca. O arquivo de vídeo do post no X não é baixado. O documento vetorial
começa com o que o item é e para que serve (`portrait`), seguido de uso, assuntos,
nomes, aparência e um trecho curto do texto ou da fala. Notas longas recebem
análise parcial. A conferência usa esse retrato junto com trechos de até 600 bytes
por nota. Links públicos recebem extração de texto, com cobertura parcial e limites
descritos em [SOURCES.md](SOURCES.md). A capa de um site entra na interpretação
para a busca por hero, branding e composição.
Áudio/vídeo de até cinco minutos usam transcrição e amostragem visual; limites em
[MEDIA.md](MEDIA.md). Busca exaustiva, filtro textual complementar e localização de
trechos longos permanecem fora desta entrega.

## Publicar

1. `uv run --env-file .env.evaluation python infra/prepare-ai-dev.py` cria identidades,
   fila e segredo se ausentes. Arquivo local privado; não imprimir nem versionar.
2. `firebase deploy --only firestore:indexes --project queep-dev-hstlx --account gazzoni@hstlx.com --non-interactive`.
   Aguardar índices vetoriais em `READY` antes de habilitar buscas.
3. `bash infra/deploy-dev.sh --public` publica a API preservando configurações de IA.
4. `bash infra/deploy-ai-dev.sh` usa o mesmo digest no worker e habilita IA na API.
5. `bash infra/deploy-image-cleanup-dev.sh` atualiza a limpeza de imagens.

Scripts fixam projeto `queep-dev-hstlx`, região `southamerica-east1` e conta
`gazzoni@hstlx.com`. Worker: escala 0–1, concorrência 1, timeout 300 s. Fila: 0,2
entregas/s, 2 simultâneas. Apenas `queep-task-invoker` invoca o worker; o container
também verifica assinatura/audiência/email OIDC. A API pública não monta suas rotas.

Para pausar IA, desabilitar `QUEEP_AI_ENABLED` nos dois serviços e pausar o Scheduler
e a fila; capturas continuam preservadas. Reativar e repetir análises em erro quando
necessário. Não trocar modelo de embeddings sem migrar o espaço e reindexar.

## Verificação desta entrega

- 65 testes locais de API/avaliação, incluindo vetores, isolamento, orçamento,
  repetição da fila, expiração de lease, revisão/exclusão e resposta vazia.
- Smoke cloud: 6 referências sintéticas (4 notas, 1 link, 1 imagem), 7 consultas
  PT/EN com conjuntos esperados completos na amostra; isolamento, acesso privado
  e exclusão depois de indexar. [Registro](../evaluation/results/cloud-ai-smoke.json).
- Rodada cloud após leitura de fontes: processamento pendente concluído em 9,06 s
  após os uploads; buscas entre 2,47 e 4,03 s. Amostra pequena; não é um SLA nem garantia de qualidade
  para todo tipo de material. Houve 429 transitório do provedor em outra referência;
  a nova tentativa pelo app concluiu. Uma chamada diagnóstica custou US$ 0,0003081.
- Antes da rodada final, o smoke aguardou um índice ainda em construção e uma
  expectativa foi corrigida: a imagem de teste também era uma interface clara de CRM.
  Nenhuma alteração de prompt foi feita para acomodar esse resultado.

Executar: `uv run python scripts/smoke-ai-cloud.py`. O script usa apenas conteúdo
sintético e remove suas contas/referências. Avaliações anteriores estão em
[RESULTADO.md](../evaluation/RESULTADO.md). Fotos pessoais não fazem parte do benchmark.
