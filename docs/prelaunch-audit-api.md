# Auditoria pré-lançamento — Queep API

Escopo: branch `develop` deste repositório (`queep-api`). Clientes iOS/Mac estão fora daqui. Nenhum número de qualidade, latência ou custo foi medido nesta auditoria. Onde aparece percentual, duração ou dólar, a fonte é um arquivo versionado e a data está junto.

O que o código sustenta do radar conhecido:

- Workers de análise tratam nota, imagem, página, post social, clipe social e arquivo de áudio/vídeo por caminhos diferentes. Vários desses caminhos publicam `status=ready` com evidência visual ausente, e o retry do usuário só existe para `status=error`.
- A busca em linguagem natural é um vizinho vetorial curto seguido de um filtro de um modelo. Posts e páginas, que são `kind=link`, saem do resultado quando a consulta pede vídeo. Não há busca lexical, limiar de distância nem filtro de arquivados no índice.

## Como ler cada achado

Severidade: **Blocker** (corrigir antes de empurrar aquisição em cima de análise + busca), **High**, **Medium**, **Low**.

Cada achado traz área, evidência (caminho no repo), o que fazer antes do lançamento e o que pode esperar.

---

## 1. Blocker — Busca em linguagem natural

**Área:** `POST /v1/search`

**Evidência**

O contrato público promete até 10 candidatos conferidos por IA (`README.md`, `infra/AI.md`). A implementação em `queep/search.py` faz outra coisa, em sequência:

1. Lê **todos** os documentos `ready` do mesmo `embedding_space` só para saber se existe algum não arquivado (`search_with_context`, por volta das linhas 26–33). O índice vetorial em `infra/firestore.indexes.json` indexa `deleted`, `status`, `embedding_space` e `embedding`. `archived_at` não entra nesse índice.
2. Só então cobra a cota (`queep/budget.py`, `rate_limit`) e gera o embedding da consulta.
3. Pede os 50 vizinhos por cosseno, **sem limiar de distância** (`find_nearest`, `limit=50`).
4. Descarta revisão antiga, status diferente de `ready` e arquivados **depois** da busca (`current_references`) e corta em 10.
5. Manda esses 10 ao verificador (`queep/ai.py`, `verify`). Se o modelo disser que a consulta exige um formato, a API joga fora qualquer item cujo `kind` não seja exatamente esse.

Consequências diretas no código:

- **Consulta de vídeo não alcança o acervo real de Free e Pro.** O prompt do verificador manda preencher `requested_kind=video` para “Vídeos sobre X” (`VERIFY_PROMPT` em `queep/ai.py`). A API aplica `by_id[identifier].kind == kind`. YouTube, Vimeo, X, Instagram, Dribbble, Behance e Pinterest são salvos como `kind=link` (`queep/models.py`, `Capture`; `queep/sources.py`, `fetch`). Arquivo de áudio/vídeo só existe no Max: `features.media_av` é verdadeiro apenas para `plan == "max"` (`queep/plans.py`). Uma busca natural por vídeo, no plano em que a pessoa não pode enviar o arquivo, devolve lista vazia mesmo com o link certo no acervo.
- **Arquivados ocupam a vizinhança.** O teste `test_semantic_search_never_returns_or_spends_on_archived_references` garante que o item arquivado não volta na resposta. Ele continua elegível no `find_nearest`. Se os vizinhos mais próximos estiverem arquivados, os 10 que chegam ao verificador são o que sobrou, ou ninguém.
- **Trecho usado na conferência ignora a palavra curta.** `queep/sources.py`, `evidence`, ranqueia parágrafos com termos de `\w{4,}`. “CRM”, “UI”, “app”, “cor” não contam. O verificador ainda vê título e resumo cortados (texto 600 bytes, `page_excerpt` 600, fala 1400 em `AI.verify`), então o assunto pode existir no item e ficar fora do trecho.
- **Falha do verificador derruba a busca inteira.** `queep/main.py` transforma `AIError`, `FailedPrecondition`, `DeadlineExceeded` e `ServiceUnavailable` em 503, sem `Retry-After`. Não há devolução dos vizinhos vetoriais. A cota mensal já foi incrementada em `rate_limit` antes do embedding e da conferência. Resposta vazia legítima e falha de provedor chegam parecidas: `items=[]` ou 503, com `candidate_limit` fixo em 10 (`queep/models.py`, `SearchResults`).
- **O documento vetorial não é o item.** `document()` em `queep/ai.py` corta texto de nota (~3500 bytes), texto de página (~3200) e transcrição. `infra/AI.md` declara fora desta entrega a busca exaustiva, o filtro textual complementar e a localização em trechos longos. A consulta só aceita `query` e `space_id` (`Search`). Não há filtro por tipo, data ou cor.

**O que a avaliação versionada realmente mede**

`evaluation/RESULTADO.md` (11/09/2026) descreve 34 referências sintéticas (28 notas, 2 links parciais, 4 imagens gráficas) e uma conferência Gemini 3.1 com precisão/recall de conjunto 100% em 20 consultas retestadas e em 10 adicionais. O próprio arquivo diz que a amostra é pequena e autorada, que o reteste já tinha sido visto no diagnóstico, e que essas porcentagens não comprovam desempenho geral. `evaluation/README.md` ainda afirma que a engine “ainda será integrada” e que não houve, naquela rodada, avaliação de áudio/vídeo, fotos reais ou captura de páginas. Os smokes posteriores (`evaluation/results/cloud-ai-smoke.json`, `cloud-sources-smoke.json`, `cloud-media-smoke.json`, citados em `infra/AI.md`, `infra/SOURCES.md`, `infra/MEDIA.md`) são amostras de 5 a 7 consultas. A pipeline de produção também divergiu do limiar usado na calibração: `evaluation/README.md` fala em ajustar limiar; `search.py` não aplica limiar nenhum.

**Antes do lançamento**

Tratar busca como promessa de aquisição só depois de, no mínimo:

- parar de excluir `kind=link` quando a pessoa pede vídeo/imagem de um post ou página;
- tirar arquivados da consulta vetorial (índice novo), em vez de filtrar depois do top 50;
- definir um comportamento explícito quando o verificador falha (não queimar a cota; responder com `Retry-After`);
- medir de novo, com acervo de links visuais, posts e consultas curtas em português, sem reaproveitar as 32 consultas de setembro.

**Adiar**

Ranking com score na resposta, destaque do trecho, filtro por data. Isso melhora o produto e não é o furo que zera o resultado.

---

## 2. Blocker — Workers de análise inconsistentes

**Área:** `queep/worker.py`, `queep/ai.py`, `queep/media.py`, `queep/sources.py`, `queep/jobs.py`

O outbox em si é sólido: conteúdo e job na mesma transação (`queep/repository.py`, `save`), lease de 360 s, no máximo 3 tentativas, publicação só se `run_id`, revisão, token e versão da conta ainda batem (`queep/jobs.py`, `claim` / `current` / `finish`). O problema é o que cada tipo de item coloca dentro dessa publicação.

**Evidência — o mesmo job, regras diferentes**

| Material | O que o worker faz | Efeito na busca |
| --- | --- | --- |
| Nota / imagem enviada | `ai.interpret` com o texto ou o JPEG (`process_work`) | Entra no vetor com a interpretação |
| Página (`content_kind=page`) | Baixa `og:image` e favicon (`page_previews` em `queep/sources.py`) e grava o card, mas **não** manda pixel ao modelo: `frames` só cresce com `if not page`, e a chamada é `ai.interpret(ref, None if profile or page else ...)` | O card existe na UI; a aparência fica vazia porque o prompt de site pede `appearance` vazio salvo descrição no texto |
| Post social (`metadata`: X, Instagram, Dribbble, Behance, Pinterest) | Até 4 previews; foto que falha é logada e ignorada (`except Exception` em autor, clipe e preview) | Pode ficar `ready` sem as imagens que a pessoa salvou |
| Primeiro clipe do post | No máximo 15 s e 3 frames (`social_evidence` em `queep/media.py`, `SOCIAL_SECONDS = 15`). Os frames do clipe são **acrescentados depois** dos pôsteres, e `interpret` só usa `frames[:4]` | Carrossel de 4 stills empurra a amostragem do vídeo para fora do modelo. A fala ainda pode ir no mesmo request, junto com as imagens |
| Arquivo de áudio/vídeo (Max) | Caminho separado: transcrição, interpretação do texto **como nota**, visão das cenas **sem** título nem fala (`interpret_media`). Até 8 frames em 40 s de leitura (`FRAME_COUNT = 8`) | É o único vídeo em que fala e cena são de propósito isoladas |
| YouTube / Vimeo | Só oEmbed: título e autor (`social_endpoint` + `video_metadata`). Sem frame e sem fala | Link `ready` com limitação explícita |
| Perfil sem bio | `Interpretation` vazia, `coverage=partial`, e mesmo assim gera vetor | Perfil “analisado” sem conteúdo |
| TikTok, LinkedIn, Threads | Não há adapter em `queep/social/__init__.py`. Caem em `fetch_site` | Página JS tende a `state=unavailable` e análise parcial do título |

`infra/SOURCES.md` ainda diz que sites genéricos não baixam imagem da página. O código baixa o card e não o interpreta. `infra/MEDIA.md` documenta YouTube, Vimeo, X e Instagram; o código também tem Dribbble, Behance e Pinterest. O onboarding aceita `tiktok` e `linkedin` como origem da pessoa (`queep/models.py`, `OnboardingProfile`) e não há leitor para esses hosts.

Retry só reabre `status=error` (`Jobs.retry`). Falha de foto, clipe ou leitura de página não levanta: `read()` converte `SourceError` em fonte `unavailable` e o job termina `ready`. A pessoa não tem como pedir outra análise. Só erro de IA, `HTTPException` (incluindo mídia 415) ou exceção genérica chamam `finish(..., error=...)` e voltam para a fila, até 3 vezes (`worker.py`, `process_work`).

Há um segundo retry só para post do X sem bundle (`tweet_status_id`, `sleep(0.6)`, segunda leitura). Instagram e as outras redes não têm esse passo.

**Evidência — tempo contra o timeout**

`infra/AI.md` e `infra/MEDIA.md`: worker com timeout 300 s, Cloud Tasks com `dispatch_deadline` 300 s (`queep/dispatch.py`), lease 360 s. Uma mídia enviada soma extração (teto 40 s), transcrição (timeout 90 s), interpretação do texto e visão (timeout 90 s) e embedding. Um post social soma leitura da página (teto 20 s em `queep/sources.py`, `TOTAL_TIMEOUT`), até 4 fotos com 3 tentativas (`load_photo`) e o mesmo bloco de modelo. Estourar 300 s mata a request com o lease ainda válido; a retentativa da mesma task não reentra no `claim` enquanto `lease_until` não passa. O dispatcher minuto a minuto é quem recupera, e a tentativa já foi contada. Três estouros seguidos gravam `status=error`.

`GET` de leitura da biblioteca não participa desse fluxo. Captura continua idempotente. O furo é a qualidade do `ready`, não a perda do arquivo.

**Antes do lançamento**

- Uma regra só para “o que entra no vetor”: still, pôster e frame de clipe, com o mesmo teto, para página, post e arquivo.
- Separar fala e visão também no post social, como `interpret_media` já faz no arquivo. Hoje o post manda `speech` e imagens no mesmo `chat` (`source()` + `frames` dentro de `interpret`).
- Falha de mídia que a pessoa espera ver (foto do post, clipe) precisa ir para `error` com retry, ou para `coverage=partial` com uma ação de reler. Publicar `ready` e esconder o botão de retry deixa o item ruim para sempre, até uma edição.
- Confirmar que o timeout de 300 s aguenta o pior caso que o plano Max pode enviar (5 min / 25 MiB, `infra/MEDIA.md`) mais as chamadas de modelo. Se não aguentar, o job precisa falhar uma vez com causa estável, sem queimar as 3 tentativas num 415 determinístico.

**Adiar**

Reindexar `analysis_version` (`queep-interpretation-v6-post-photos` em `queep/ai.py`) para itens antigos de desenvolvimento. Conta nova no lançamento nasce na versão atual. Troca futura de embedding continua exigindo espaço novo e reindexação, como `infra/AI.md` já diz. `Repository.complete` publica `ready` sem vetor e não é chamado por nenhum fluxo; pode sair depois.

---

## 3. High — `GET /v1/account` e leituras sem o mesmo contrato de falha

**Área:** conta, paywall, transporte

**Evidência**

`GET /v1/account` (`queep/main.py`, `account_status`) é o único GET de conta que a sessão suspensa consegue usar. `require_open` (`queep/account_state.py`) responde 401 “Esta conta foi encerrada” em todo o resto enquanto o estado não é `active`, inclusive `scheduled` nos 30 dias. O cancelamento depende desse GET para mostrar `delete_at` e `can_restore` (`queep/accounts.py`, `status`; `infra/ACCOUNTS.md`).

Esse GET não passa por `Repository.transact`. Transações mapeiam `Aborted`, `DeadlineExceeded` e `ServiceUnavailable` para 503 com `Retry-After: 1` (`queep/repository.py`). O `get()` cru do documento de exclusão deixa essas falhas virarem 500, sem `Retry-After`.

`account_claims` e `user` só capturam `InvalidIdTokenError`, `RevokedIdTokenError`, `UserDisabledError`, `UserNotFoundError` e `ValueError`. Os dois chamam `verify_id_token(..., check_revoked=True)`, que consulta o Identity Toolkit. Erro de transporte nessa consulta não está nesse `except`. Vira 500. Não há `exception_handler` em `queep/main.py`.

O OpenAPI versionado descreve o 200 desses GETs com schema vazio: `/v1/account`, `/v1/usage`, `/v1/account/deletion` e o cancelamento (`contracts/openapi.json`). O corpo real de conta é `status`, `version`, `delete_at`, `can_restore`. O de uso é `plan`, `resets_at`, `counters` (`queep/usage.py`, `snapshot`). Cliente gerado a partir do contrato não vê o formato. `GET /v1/account/avatar` está como `application/json` e devolve bytes ou 204.

`GET /v1/references` e `GET /v1/references/{id}` também leem Firestore fora de `transact`. A assimetria é mais grave na conta porque, com a biblioteca bloqueada, esse é o endpoint que distingue “sessão morta”, “exclusão agendada” e “rede ruim”.

**Antes do lançamento**

Mapear falha transitória de Auth e de Firestore nesses GETs para 503 com `Retry-After`, no mesmo espírito das escritas. Publicar o schema de `GET /v1/account` e `GET /v1/usage` no OpenAPI. O app precisa tratar 500 sem corpo como falha de transporte e continuar capaz de abrir a tela de recuperar conta.

**Adiar**

Unificar todos os GETs de mídia. Vários já traduzem `GoogleAPICallError` para 503 (`queep/images.py`, `queep/media.py`, `queep/accounts.py` no avatar).

---

## 4. High — Paywall depende de RevenueCat até no Free, e o sync pode devolver o plano antigo

**Área:** `GET /v1/billing/access`, `POST /v1/billing/sync`, `GET /v1/usage`, cotas

**Evidência**

O que a API já oferece para a tela de plano, e que o cliente consegue usar sem inventar regra:

- `Access`: `plan`, `active`, `expires_at`, `will_renew`, `features.spaces`, `features.media_av`, `limits` (`queep/billing.py`, `queep/plans.py`).
- `GET /v1/usage`: usado/limite de operações, buscas, segundos de mídia, itens, storage, upload e spaces, mais `resets_at`.
- 402 com `code` (`plan_limit`, `storage_limit`, `media_av_required`), `resource`, `required_plan`, `resets_at` (`queep/plans.py`, `denied`).
- Free continua com biblioteca. `Billing.require` sem feature não exige `active=True`. Captura e busca passam no Free até a cota (`PaidUID` em `queep/main.py` chama `require(uid)` sem feature). A/V de arquivo exige Max. Spaces exigem Pro (`require_space_change`).

Furos para o cliente:

- Em `QUEEP_BILLING_MODE=required` (default em `queep/config.py`), `access()` consulta o RevenueCat quando o cache vence. Falha vira 503 “Não foi possível confirmar a assinatura”, com `Retry-After: 10`, e não concede acesso (`queep/billing.py`, `customer` / `unavailable`). `GET /v1/usage`, captura, retry e busca chamam esse caminho. Free sem cache válido para de salvar e de buscar se o fornecedor cai. `GET /v1/account` não chama billing, então a conta pode responder 200 e o uso/busca responder 503 na mesma abertura.
- `POST /v1/billing/sync` com `force=True` devolve o cache se `checked_at` tem menos de 10 s (`access`, comentário “Client refresh is capped…”). Compra imediata depois de um `GET /v1/billing/access` pode ler o plano anterior até o webhook invalidar `valid_until` ou passarem 10 s. `infra/BILLING.md` documenta o teto. `infra/APPLE-LAUNCH.md` pede conferir latência de compra/restore e lista sandbox Apple como pendente.
- Preço, trial e gestão da assinatura não estão na API. `infra/PLANS.md` coloca preço no StoreKit. Não falta endpoint de catálogo; falta o cliente não tratar `active=false` como biblioteca bloqueada.
- O 402 não tem schema no OpenAPI.

Cotas no código (`queep/plans.py`): Free 12 operações e 8 buscas/mês; Pro 100 e 50; Max 200 e 80; A/V 0 / 0 / 2400 s; itens 100 / 800 / 2500. O teto diário de abuso é outro (`QUEEP_SEARCH_DAILY_LIMIT` default 100, `queep/config.py`). Para Free e Pro, o mensal estoura antes do diário.

**Antes do lançamento**

Combinar com o app: 503 de billing é retry, não “virou Free”; sync dentro de 10 s pode repetir o snapshot anterior; paywall lê `plan` + `usage` + corpo do 402. Executar o aceite externo que `infra/APPLE-LAUNCH.md` ainda lista como pendente (13ª captura Free, 101ª Pro, restore, reembolso, Max→Pro). Este repo não contém o resultado desses testes.

**Adiar**

Endpoint de preço. Loja já é a fonte.

---

## 5. High — Teto global de IA de desenvolvimento

**Área:** orçamento

**Evidência**

`QUEEP_AI_DAILY_BUDGET_USD` default é `1.0` (`queep/config.py`). `Budget.reserve` soma US$ 0,01 por chamada e responde 429 para **todas** as contas quando o dia estoura (`queep/budget.py`). `infra/AI.md` chama isso de limite de desenvolvimento, compartilhado, e não de teto comercial. A cota por plano é outra camada (`reserve_operation`, `rate_limit`).

Não há neste repositório o valor efetivo do Cloud Run de produção. Se o serviço público subir com o default, análise e busca param no primeiro dólar do dia, com 429, independente do plano.

**Antes do lançamento**

Confirmar o valor publicado na API e no worker. O default do código não serve como teto de lançamento.

**Adiar**

Recalcular margem anual. `infra/APPLE-LAUNCH.md` já mostra Pro anual ~24,84% e Max anual ~13,42% no pior mix da issue, contra a meta de 40%, e pede decisão do autor antes de vender anuais. São contas daquele documento, não uma medição nova.

---

## 6. Medium — Cota de busca cobrada antes da resposta

**Área:** quotas

**Evidência**

`search_with_context` chama `ai.budget.rate_limit` e só depois `embed` e `verify`. O 503 do endpoint não desfaz o incremento em `users/{uid}/usage/{YYYY-MM}.searches`. Biblioteca vazia ou toda arquivada retorna antes da cobrança (coberto por `test_empty_library_avoids_paid_search` e `test_semantic_search_never_returns_or_spends_on_archived_references`).

**Antes do lançamento**

Se a busca for aberta no beta, estornar ou só contar depois de uma resposta estável. Com 8 buscas no Free, um 503 repetido esgota o mês.

**Adiar**

Se a busca continuar atrás de flag até o achado 1.

---

## 7. Medium — Exclusão de conta e RevenueCat

**Área:** conta / loja

**Evidência**

`QUEEP_ACCOUNT_DELETION_ENABLED` default é `False`. Com a flag desligada, `POST /v1/account/deletion` responde 503 “A exclusão de conta ainda não está disponível.” (`queep/main.py`). `infra/ACCOUNTS.md` diz que o dev foi ligado em 11/09/2026. Produção não está provada por este checkout.

A limpeza Queep (Auth, referências, jobs, storage) existe (`Accounts.process`, `queep/cleanup_images.py`). Customer RevenueCat não é apagado nesse fluxo: o código grava `provider_deletions` com `state=pending` e o comentário diz que a limpeza do fornecedor foi adiada. `erase_customer` existe e é testado, e nenhum sweep chama essa coleção. `infra/ACCOUNTS.md` e `infra/BILLING.md` registram a mesma decisão.

**Antes do lançamento**

Flag ligada no serviço que o app de loja usa. Decidir se `provider_deletions` precisa de um worker antes da revisão da Apple. O código deixa o customer no RevenueCat.

**Adiar**

Nada, se a revisão da loja aceitar a fila sem consumidor. Isso é decisão de política, já escrita nos docs, não um bug novo.

---

## 8. Medium — Leitores sociais pela metade

**Área:** captura de link

**Evidência**

Adapters registrados: Twitter, Instagram, Dribbble, Behance, Pinterest (`queep/social/__init__.py`). YouTube e Vimeo param no oEmbed. TikTok e LinkedIn não têm leitor, embora o onboarding os liste como origem. Perfil lê bio e para; shots, pins e posts do perfil ficam de fora (limitações em cada `parse`). Login, JS e bloqueio preservam o link com `coverage=partial` (`queep/sources.py`, `read`). Isso está alinhado com `infra/SOURCES.md` para página genérica e é insuficiente para uma biblioteca visual cujo hábito é salvar o post, não colar o arquivo.

**Antes do lançamento**

Não anunciar “salva de qualquer rede”. Lista suportada no produto: as cinco redes com adapter, mais metadado de YouTube/Vimeo. TikTok fica explícito como link sem leitura.

**Adiar**

Novo adapter, desde que a busca (achado 1) e a visão (achado 2) já tratem `kind=link` como item visual.

---

## 9. Low — Código e docs que não descrevem o fluxo atual

**Área:** legado

**Evidência**

- `Repository.complete` marca `ready` e apaga o job sem embedding. Nenhum caller no pacote.
- `Jobs.bootstrap` reescreve outbox antigo sem `run_id`. Caminho de migração, ainda ativo no dispatcher.
- Ledger de arquivo continua em `image_uploads` / prefixo `images/v1` para áudio e vídeo (`queep/media.py`). Funciona; o nome é histórico.
- `evaluation/README.md` diz que a engine não foi integrada. `queep/worker.py` e `queep/main.py` já a usam.
- `infra/BILLING.md`, seção “Antes de cobrar”, ainda diz que cotas de captura precisam ser decididas. `infra/PLANS.md` já fixa Free/Pro/Max.
- `infra/PLANS.md`, “Integração com GAZ-116”, diz que Spaces não tem API neste checkout. `queep/main.py` já expõe CRUD de spaces e atribuição.
- Dois webhooks RevenueCat do TestFlight apontam para URLs diferentes do mesmo serviço (`infra/APPLE-LAUNCH.md`). Vale conferir na hora do deploy; não é bug de código.

**Antes do lançamento**

Nada disso bloqueia aquisição. Evitar que suporte ou o app leiam o README de avaliação como estado atual.

**Adiar**

Apagar `complete` e atualizar os parágrafos velhos.

---

## O que já está em condição de lançar

Isto não é pendência. Está no código e nos testes do repo:

- Captura idempotente, edição com `revision` + `mutation_id`, tombstone no delete.
- Worker privado, task sem conteúdo, publicação condicional à revisão.
- Planos Free/Pro/Max, 402 com recurso e plano exigido, webhook que invalida cache em vez de aplicar o evento fora de ordem.
- Arquivado sai da resposta da busca e da biblioteca ativa. Não sai do índice vetorial (achado 1).
- Isolamento por UID do token. Sem URL pública de mídia.

---

## Linha de corte — API

**Corrigir antes de empurrar aquisição (análise + busca):**

1. Busca: `kind=link` (post, página, YouTube) precisa poder responder a uma consulta visual/de vídeo. Arquivado não pode consumir os 50 vizinhos. Falha do verificador não pode gastar a cota nem parecer “não achei nada” sem `Retry-After`. Nova medição em acervo de referências visuais; os percentuais de `evaluation/RESULTADO.md` não servem como aceite.
2. Workers: página, post e arquivo têm de indexar a mesma classe de evidência visual, ou o `ready` parcial tem de ser reprocessável. Hoje foto/clipe que falha silenciosamente fica `ready` e `POST /v1/references/{id}/retry` não roda. Fala e visão do post social não podem continuar no mesmo request enquanto o arquivo enviado as separa. O timeout de 300 s tem de caber no pior item do Max, ou a falha tem de ser uma tentativa só.
3. Operação: o serviço público não pode estar no default de US$ 1/dia de `QUEEP_AI_DAILY_BUDGET_USD`. Confirmar o valor no deploy, não no default do `Settings`.

**Corrigir antes da build de loja que abre conta e paywall (pode ser o mesmo corte se a aquisição já pede login):**

4. `GET /v1/account` e `GET /v1/usage`: 503 com `Retry-After` em falha transitória de Auth/Firestore, e schema no OpenAPI. O app não pode tratar 500 desse GET como sessão inválida.
5. Cliente de paywall: usar `plan`, `features`, `limits`, `usage` e o corpo do 402; tratar 503 de billing como retry; não confiar num único `POST /v1/billing/sync` nos 10 s seguintes a um access cacheado. Ligar `QUEEP_ACCOUNT_DELETION_ENABLED` no serviço da loja. Sandbox de compra ainda está pendente em `infra/APPLE-LAUNCH.md`.

**Pode esperar a primeira versão no ar:**

- Reindexação de itens antigos de dev, preço na API, adapters de TikTok, score de relevância na resposta, worker de `provider_deletions` se a política da loja aceitar a fila já documentada, limpeza de `Repository.complete` e dos parágrafos desatualizados em `evaluation/README.md`, `infra/BILLING.md` e `infra/PLANS.md`.

Sem os itens 1 e 2, captura e biblioteca podem ir a um beta fechado. Busca em linguagem natural e “o Queep entende o que você salvou” não podem ser a frase de aquisição.
