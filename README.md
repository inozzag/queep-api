# Queep API

Primeira entrega: Firebase Auth, notas/links, biblioteca paginada, edição, exclusão e outbox transacional. Python 3.12 + FastAPI + Firestore. Os apps nunca escolhem o proprietário de uma referência: ele vem do token validado.

## Rodar

Pré-requisitos: `uv`, Firebase CLI e Java 21+. `uv sync --locked`, depois, em dois terminais:

```sh
make emulators
make dev
```

API: http://127.0.0.1:8000/docs · emuladores: http://127.0.0.1:4000. Projeto fictício `demo-queep`; nenhuma credencial de produção é necessária. Dados locais são descartados quando os emuladores param.

## Verificar

```sh
make lint
make test
```

Os testes usam Auth e Firestore reais emulados, incluindo concorrência, isolamento de contas, paginação, edição desatualizada e processamento após exclusão. Se os emuladores já estiverem abertos, rode `uv run --env-file .env.example pytest -q`.

## Contrato

- `PUT /v1/references/{UUID}`: captura idempotente de nota/link. Reenviar o mesmo UUID e conteúdo retorna o registro existente.
- `GET /v1/references`: lista da conta autenticada, `limit` e cursor opaco.
- `GET /v1/references/{UUID}`: abre referência.
- `PATCH /v1/references/{UUID}`: exige `revision` e `mutation_id`; conflitos retornam 409.
- `DELETE /v1/references/{UUID}?revision=N`: exclui conteúdo e trabalho pendente; mantém só tombstone contra reenvios tardios.

Conteúdo e job são gravados na mesma transação. Links ainda não são visitados. Estado `queued` significa **salvo e aguardando análise**; esta entrega não executa IA nem afirma ter interpretado mídia. API não oferece upload ou busca semântica ainda.

## Próxima entrega

Storage com limites e uploads autorizados; dispatcher da outbox → Cloud Tasks → worker privado; processamento por revisão; avaliação Gemini/embeddings e busca. Antes de produção: cotas, App Check/abuso, métricas sem conteúdo privado, exclusão de conta, retenção de tombstones e integração de assinatura.

Deploy ainda não executado. Produção exige `QUEEP_ENV=production`, `GOOGLE_CLOUD_PROJECT` e identidade Google via ADC; variáveis de emulador são rejeitadas. Regras de clientes negam acesso direto a Firestore/Storage nesta etapa. A API valida autorização; Admin SDK não usa essas regras.
