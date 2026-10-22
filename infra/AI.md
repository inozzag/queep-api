# IA — desenvolvimento

## Fluxo e limites

Captura confirma conteúdo/outbox → Cloud Tasks → `queep-worker` privado → análise e
vetor publicados na revisão atual. Dispatcher agendado a cada minuto recupera falhas
de envio. Jobs usam lease de 360 s, até 3 tentativas e identificadores diferentes por
execução/revisão. Reenvio de tarefa não repete trabalho concluído; edição/exclusão
invalida o processamento anterior. O botão de nova tentativa abre outra execução,
também quando o post ficou pronto sem a foto ou o clipe que a fonte ofereceu.

`POST /v1/search` usa apenas documentos prontos do proprietário no mesmo espaço de
embeddings. Ordena o acervo ativo já carregado (vetor fundido com palavra-chave e sinônimos PT/EN) e devolve uma janela (teto 60,
`candidate_limit` na resposta; o cliente que limitar a grade por esse campo passa a
mostrar a lista maior). Consulta curta sem cor (1–2 palavras: interface, web design, mobile) mantém quem cita o tema e só acrescenta vizinhos acima da média de cosseno dessa consulta mais um desvio, no máximo 4. Os 10 primeiros da fusão ficam na ordem da fusão; o restante segue o cosseno. O teto é 40. "Log in" repetido no chrome da página não conta como citação, então login fica perto de quantos itens realmente falam de login. Consulta com cor fica
só nos itens que citam essa cor em `colors`/`brand` ou, em itens antigos, na
aparência e no texto da leitura; se ninguém cita, voltam 5 quase-acertos, não a
biblioteca inteira. A conferência roda só na cabeça (6 itens, até 8 KB, timeout
de 20 s). O modelo devolve índices curtos (`[0,2,5]`), não UUIDs, em JSON compacto,
com teto de 300 tokens. Ela só tira contradição explícita; o restante volta sem passar pelo modelo.
Se a conferência falha ou estoura o tempo, a lista ordenada volta mesmo assim, a
busca mensal não é debitada e qualquer erro do provedor sem payload de uso
libera a reserva de US$ 0,01 (não só o tempo esgotado). Revalida revisões e exclusões antes de
responder. Consultas usam POST para não expor o texto em URLs.
O verificador informa também se a consulta exige um formato (`requested_kind`). A
API confere os tipos reais antes de devolver os IDs da cabeça; um pedido explícito
de vídeo não pode retornar áudio, mesmo que o modelo o inclua. "Somente vídeos"
tira nota, imagem, áudio e página comum da cabeça; posts, páginas e YouTube
continuam elegíveis numa busca visual ou de vídeo sem essa restrição. Assunto e
formato são separados: ferramentas para criar vídeos não precisam ser arquivos de vídeo.

| Etapa | Modelo / rota |
| --- | --- |
| Notas, imagens e texto de links | `google/gemini-3.1-flash-lite` / `google-vertex/global` |
| Transcrição e cenas de vídeo, em chamadas separadas | `google/gemini-3.1-flash-lite` / `google-vertex/global` |
| Vetores de 768 dimensões | `google/gemini-embedding-2` / `google-vertex/us` |
| Conferência da busca | `google/gemini-3.1-flash-lite` / `google-vertex/global` |

Em 20/10/2026 a OpenRouter tirou a rota `google-vertex` do `gemini-2.5-flash-lite`; toda
interpretação passou a voltar 404. A interpretação e a transcrição usam desde então o
`gemini-3.1-flash-lite` em `google-vertex/global` (ZDR), o mesmo da conferência.

### Leitura no aparelho

Os apps Apple leem no próprio aparelho, sem custo de token, o que vão enviar:
OCR do Vision (imagens), transcrição do Speech com o início de cada trecho (áudio/vídeo)
e cores medidas nos pixels. O app manda `PUT /v1/references/{id}/device-reading`
**antes** do upload do arquivo; o worker lê `users/{uid}/device_readings/{id}`.
Com transcrição do aparelho, a chamada paga de transcrição não acontece e
`transcript_segments` fica salvo no item. OCR e cores entram como evidência na
interpretação, que ainda precisa ver a imagem. Sem leitura, nada muda.
A leitura expira por TTL em `expires_at` (2 dias); a exclusão da conta apaga a coleção.
Ativar o TTL uma vez:
`gcloud firestore fields ttls update expires_at --collection-group=device_readings --enable-ttl --project queep-dev-hstlx`.

`POST /v1/search` aceita `verify: false`: o app confere a cabeça da lista com o
Foundation Models no aparelho (iPhone 15 Pro ou mais novo, Mac/iPad M1+, com Apple
Intelligence ativo) e a API não chama o verificador pago. Aparelhos sem o modelo
continuam com `verify: true`.

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

Imagens são reduzidas a 1536 px para interpretação. Até quatro imagens seguem juntas
para o modelo; amostras do clipe ocupam vagas desse teto e não ficam depois dos
pôsteres. Fotos e pôsteres de vídeo
anexados a um post social seguem o mesmo caminho, mesmo quando o texto do post
não pôde ser lido: entram na interpretação, no documento vetorial e na conferência
da busca. O arquivo de vídeo do post no X não é baixado. Sites enviam a imagem
de preview para a mesma interpretação. A versão `queep-interpretation-v8` acrescenta
`ui_types`, `colors`, `brand`, `on_screen_text` e, em vídeo, `hook`, `style` e
`transcript_summary`. O texto do vetor (até 6.800 bytes) escreve esses campos,
a aparência e a nota antes do texto da página e da transcrição. A conferência da
cabeça recebe os mesmos campos. Links públicos recebem extração de texto, com
cobertura parcial e limites descritos em [SOURCES.md](SOURCES.md).
Áudio/vídeo de até cinco minutos usam transcrição e amostragem visual; limites em
[MEDIA.md](MEDIA.md). Busca exaustiva, filtro textual complementar e localização de
trechos longos permanecem fora desta entrega.

## Releitura para a v8

Itens antigos continuam no espaço `gemini-embedding-2:768:document-v1` (768 dimensões).
O comando abaixo regrava a leitura ou só o vetor. O padrão é dry-run: imprime o
plano e o custo, sem chamar modelo e sem gravar. Não há credencial neste
repositório; rode numa máquina com ADC no projeto `queep-dev-hstlx`.

```sh
# plano (não grava)
uv run --env-file .env.evaluation python -m queep.backfill --uid UID

# só o vetor, a partir dos campos já salvos (aparência e nota incluídas).
# Não marca analysis_version, então uma leitura completa depois ainda ocorre.
uv run --env-file .env.evaluation python -m queep.backfill --uid UID --reembed-only --apply --max-cost-usd 0.05

# enfileira no worker já publicado. A coleção é QUEEP_JOB_COLLECTION
# (jobs no dev, jobs_testflight no TestFlight). O worker relê a página,
# manda a imagem de preview e publica a v8. --enqueue exige --require-version
# igual à ANALYSIS_VERSION servida. --enqueue --dry-run só mostra.
uv run --env-file .env.evaluation python -m queep.backfill \
  --uid UID --enqueue --require-version queep-interpretation-v8

# coortes de fetch (erro, site vazio, reel só com autor, pôster sem motion).
# A seleção é queep.refetch.classify, a mesma de python -m queep.reprocess.
uv run --env-file .env.evaluation python -m queep.backfill \
  --uid UID --scope fetch --exclude-host dribbble.com
```

Variáveis: `QUEEP_ENV=production`, `GOOGLE_CLOUD_PROJECT=queep-dev-hstlx`,
`QUEEP_STORAGE_BUCKET` do mesmo projeto, `QUEEP_AI_ENABLED=true` e
`OPENROUTER_API_KEY` só no `--apply` local. Sem `FIRESTORE_EMULATOR_HOST` nem
as outras variáveis de emulador. `--apply` local não baixa a imagem de preview;
páginas com leitura vazia precisam de `--enqueue` depois que o worker v8 estiver
no ar. `--limit` corta a leva. `--force` inclui quem já está na v8. Itens
apagados, em fila ou em processamento ficam de fora. Uma falha de item não
interrompe a leva. A segunda execução pula quem já chegou na v8 (ou, no modo
barato, quem já tem `embedding_build=queep-embed-v8-fields`).

Custo estimado do provedor, não a reserva: US$ 0,0035 por post/página,
US$ 0,010 por vídeo/reel (transcrição + visão) e US$ 0,0002 no `--reembed-only`.
Para as 58 referências do acervo (53 posts/páginas e 5 clipes): cerca de
US$ 0,24 na leitura completa e US$ 0,012 só no vetor. Cada chamada reserva
US$ 0,01 em `ai_budget/{data}` antes do acerto (US$ 0,02 por item comum,
US$ 0,04 por clipe). Com o teto de US$ 1/dia, a leitura completa para por volta
de 50 itens e continua no dia seguinte. `--max-cost-usd` (padrão 0,50) é um
segundo teto, pelo custo estimado. O comando também lê `used` do dia e não
começa um item que não caiba na reserva.

## Avaliação das consultas do acervo

`eval/owner_queries.json` guarda só identificadores (sem texto dos itens). O
script compara a lista fundida com a ordem só por cosseno e imprime recall@10,
recall@30 e a quantidade devolvida. Rótulo vazio (`login`, `logos verdes`,
`carro app logo azul`) imprime a contagem: uma lista curta é aceitável. Três
consultas sem rótulo saem para leitura qualitativa. Não debita busca mensal.
Cada consulta gasta um embedding (~US$ 0,0002). `--checker` acrescenta a
conferência.

```sh
uv run --env-file .env.evaluation python eval/recall.py --uid UID
```

Mesmas variáveis do `--apply` do backfill, no projeto `queep-dev-hstlx`. Os
pesos e o corte de cauda em `queep/ranking.py` são a calibração inicial; ajuste
depois dessa rodada. Os testes em `tests/test_recall_eval.py` cobrem, sem
credencial, consulta ampla, cor só na aparência, termo só na legenda, falha da
conferência, cor errada e expansão PT/EN.

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
