# Spaces — GAZ-116

Coleções pessoais, sem hierarquia. `space_ids` é uma associação múltipla; “Tudo”
continua incluindo todas as referências ativas, inclusive as que pertencem a Spaces.
Arquivar não remove vínculos: itens arquivados não aparecem nem contam no Space ativo.

## Contrato

- `GET /v1/spaces`: array ordenado com `reference_count` derivado dos itens ativos.
- `POST /v1/spaces {name}`: 201; trim, 1–40 caracteres; unicidade sem distinção de
  caixa por conta; 409 para nome repetido; 402 para cota 0/15/40 (Free/Pro/Max).
- `PATCH /v1/spaces/{id} {name?, sort_index?}`: renomeia e/ou move a posição;
  normaliza a ordem dos demais Spaces na mesma transação.
- `DELETE /v1/spaces/{id}`: 204; esconde imediatamente, impede novas associações e
  remove somente os vínculos em lotes transacionais de até 400 referências.
- `POST /v1/references/{id}/spaces {add: [], remove: []}`: atômico e idempotente;
  404 para Space inexistente ou de outra conta. Não altera revisão de conteúdo,
  não enfileira IA nem consome operações.
- `GET /v1/references?space_id=UUID`: mantém paginação, ordem e política de arquivados.
  Cursor de um Space não pode ser reutilizado em outro filtro.
- `POST /v1/search {query, space_id?}`: filtra candidatos antes da busca vetorial e
  revalida vínculos antes de devolver os resultados.
- `PUT /v1/references/{UUID}` aceita `space_ids`; `POST /v1/references` também cria
  com UUID gerado no servidor. Clientes continuam usando PUT para retries idempotentes.
- Uploads `/image` e `/media` aceitam parâmetros repetidos `space_ids=UUID`.
  A seleção é validada antes de reservar uso e novamente na confirmação do upload.

## Persistência e concorrência

`users/{uid}/spaces/{UUID}` contém nome, `name_key`, posição e timestamps. A
transação compartilha o documento de inventário da conta para serializar nomes e
cotas, inclusive quando a coleção está vazia. Alterar conteúdo preserva os vínculos.
As referências antigas sem `space_ids` continuam na inbox, sem migração obrigatória.

Exclusão registra um trabalho durável em `space_cleanup`. A resposta agenda limpeza
em background; `/tasks/dispatch` no worker retoma trabalhos interrompidos. Os lotes
releem documentos, preservando exclusões e alterações concorrentes. A exclusão da
conta também remove Spaces e trabalhos de limpeza. Publicar os índices compostos
de `firestore.indexes.json` junto da API.

## Decisões de produto

Mantido “Spaces” na navegação e “Space” nas ações. Sobre a divergência com GAZ-115,
prevalece a regra explícita da GAZ-116: criar exige cota disponível; atribuir/remover
vínculos com Spaces existentes continua permitido, inclusive após downgrade.
O helper legado `plans.require_space_change` não governa estas rotas.

## Validação

`tests/test_spaces.py` cobre CRUD, nomes, ordem, cotas dos três planos, downgrade,
concorrência, captura, edição, isolamento, paginação, arquivados, busca, imagem e
retomada da limpeza de mais de 500 vínculos. Usa apenas Firebase emulado.
