# Queep API

Firebase Auth, notas/links/imagens, biblioteca, interpretação assíncrona e busca semântica. Python 3.12 + FastAPI + Firestore + Cloud Storage + Cloud Tasks + OpenRouter. O proprietário vem do token validado.

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
- `POST /v1/search`: `{ "query": "interfaces claras de CRM" }`; até 10 candidatos, conferidos por IA, sem inventar resultados fora do acervo.
- `POST /v1/references/{UUID}/retry`: `{ "revision": N }`; nova tentativa para análise em erro.
- `GET /v1/account`: estado, versão e prazo de exclusão da conta autenticada.
- `POST /v1/account/deletion`: autenticação recente, operação/versão; agenda exclusão em 30 dias ou inicia imediatamente. `POST /v1/account/deletion/cancel` recupera antes do prazo. [Contrato, ativação e limites](infra/ACCOUNTS.md).

Conteúdo e job são gravados na mesma transação. O worker privado interpreta notas/imagens e grava o vetor da revisão atual; entregas duplicadas ou antigas não republicam conteúdo. Links públicos/READMEs são extraídos quando acessíveis; cobertura parcial é sinalizada. Regras de mídia em [infra/IMAGES.md](infra/IMAGES.md) e operação de IA em [infra/AI.md](infra/AI.md).

## Próxima entrega

Leitura de páginas públicas/READMEs implementada; cobertura e limites em [SOURCES.md](infra/SOURCES.md). Próximos passos: fontes sociais/vídeos e avaliação em acervos maiores. A [comparação de modelos](evaluation/README.md) usa dados sintéticos e orçamento limitado. Antes de produção: cotas por plano, App Check/abuso, limpeza de fornecedores após exclusão, retenção e integração Apple IAP. O teto de desenvolvimento não substitui os limites comerciais.

O ambiente cloud de desenvolvimento está acessível aos apps, com autenticação Firebase
e sincronização Mac/iPhone verificadas. Estado e comandos em
[infra/CLOUD.md](infra/CLOUD.md). Fora dos emuladores, exige `QUEEP_ENV=production`,
`GOOGLE_CLOUD_PROJECT`, `QUEEP_STORAGE_BUCKET` e identidade Google via ADC; variáveis de emulador são rejeitadas.
Regras de clientes negam acesso direto a Firestore/Storage. A API valida autorização;
Admin SDK não usa essas regras. Contenção transitória em transações retorna 503 com
`Retry-After`; repetir o mesmo identificador e conteúdo é seguro.
