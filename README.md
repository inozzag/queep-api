# Queep API

Firebase Auth, notas/links/imagens, biblioteca paginada, edição, exclusão e outbox transacional. Python 3.12 + FastAPI + Firestore + Cloud Storage. Os apps nunca escolhem o proprietário de uma referência: ele vem do token validado.

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

Os testes usam Auth, Firestore e Storage emulados, incluindo concorrência, isolamento, paginação, edição desatualizada, uploads interrompidos e exclusão durante reenvios. Se os emuladores já estiverem abertos, rode `uv run --env-file .env.example pytest -q`.

## Contrato

- `PUT /v1/references/{UUID}`: captura idempotente de nota/link. Reenviar o mesmo UUID e conteúdo retorna o registro existente.
- `PUT /v1/references/{UUID}/image?title=…`: JPEG/PNG binário, até 12 MiB; valida, normaliza e confirma antes de publicar.
- `GET /v1/references/{UUID}/image[?thumbnail=true]`: imagem/miniatura com autenticação; sem URL pública.
- `GET /v1/references`: lista da conta autenticada, `limit` e cursor opaco.
- `GET /v1/references/{UUID}`: abre referência.
- `PATCH /v1/references/{UUID}`: exige `revision` e `mutation_id`; conflitos retornam 409.
- `DELETE /v1/references/{UUID}?revision=N`: exclui conteúdo e trabalho pendente; mantém só tombstone contra reenvios tardios.

Conteúdo e job são gravados na mesma transação. Links ainda não são visitados. Estado `queued` significa **salvo e aguardando análise**; esta entrega não executa IA nem afirma ter interpretado mídia. Busca semântica e interpretação ainda não estão disponíveis. Regras de mídia e recuperação de falhas em [infra/IMAGES.md](infra/IMAGES.md).

## Próxima entrega

Dispatcher da outbox → Cloud Tasks → worker privado; processamento por revisão; interpretação via OpenRouter e busca. A [comparação de modelos](evaluation/README.md) roda separadamente, com dados sintéticos e orçamento limitado. Antes de produção: cotas, App Check/abuso, métricas sem conteúdo privado, exclusão de conta, retenção de tombstones e integração de assinatura.

O ambiente cloud de desenvolvimento está acessível aos apps, com autenticação Firebase
e sincronização Mac/iPhone verificadas. Estado e comandos em
[infra/CLOUD.md](infra/CLOUD.md). Fora dos emuladores, exige `QUEEP_ENV=production`,
`GOOGLE_CLOUD_PROJECT`, `QUEEP_STORAGE_BUCKET` e identidade Google via ADC; variáveis de emulador são rejeitadas.
Regras de clientes negam acesso direto a Firestore/Storage. A API valida autorização;
Admin SDK não usa essas regras. Contenção transitória em transações retorna 503 com
`Retry-After`; repetir o mesmo identificador e conteúdo é seguro.
