# Imagens — contrato da primeira versão

- App aceita JPEG, PNG e HEIC estáticos de até 32 MiB. Converte orientação e reduz
  o maior lado para 4096 px; remove metadados de origem. HEIC vira JPEG/PNG.
- API aceita JPEG/PNG de até 12 MiB e 20 milhões de pixels; decodifica o arquivo
  inteiro, rejeita animações, normaliza e cria miniatura de até 1440 px. Preserva alpha.
  Miniaturas antigas de 560 px ganham uma prévia `card` de até 1440 px na primeira leitura.
- Arquivos são versões otimizadas para referências, não cópias arquivísticas dos
  originais. Leitura de texto, interpretação e embeddings são assíncronos; [IA](AI.md).
- O upload passa pela API. Vídeos curtos usam o mesmo ciclo de vida, com limites
  próprios em [MEDIA.md](MEDIA.md). Upload direto/resumível de arquivos maiores fica para depois.

## Consistência

1. Reserva transacional do UUID e hash do envio em `image_uploads`; placeholder
   oculto em `references` impede colisão com outro tipo de referência.
2. Imagem e miniatura imutáveis no Storage, com precondição de geração zero.
3. Confirmação transacional publica referência e outbox apenas após ambos os objetos.
   Repetição com mesmo UUID, título e bytes retorna a referência; conteúdo diferente
   recebe 409. A imagem não pode ser convertida em nota/link pela edição.
4. Exclusão grava tombstone e pedido de limpeza juntos; a API tenta remover objetos.
   Falha no Storage mantém o pedido persistido. Leituras já são bloqueadas.
5. Job privado a cada 5 minutos: reservas abandonadas por 1 hora viram tombstones;
   exclusões são repetidas. Mantém a reserva de limpeza por pelo menos 10 minutos
   para cobrir uploads já em andamento (operações de Storage têm timeout de 30 s,
   sem retry implícito). Só depois remove a reserva, mantendo o tombstone.

Downloads exigem proprietário verificado em cada acesso, inclusive miniaturas.
Respostas e objetos usam `no-store`. Regras Firebase negam acesso direto. A retenção
interna de soft delete do bucket é de 7 dias, sem acesso pelo app após exclusão.

## Verificação

Testes em emuladores cobrem arquivos inválidos/grandes, orientação/EXIF, alpha,
animações, colisão de UUID/tipo, interrupção entre objetos, expiração, isolamento,
falha de exclusão e upload concorrente terminando depois da exclusão. O smoke cloud
verifica o contrato com Storage real e duas sessões Firebase. O job foi executado
via Scheduler. Antes do beta: cotas por conta, controles de abuso, observabilidade,
limpeza de conta completa e política final de retenção.

Referências: [Storage e precondições](https://docs.cloud.google.com/python/docs/reference/storage/latest/google.cloud.storage.blob.Blob),
[Pillow](https://pillow.readthedocs.io/en/stable/reference/Image.html),
[job agendado](https://docs.cloud.google.com/run/docs/execute/jobs-on-schedule),
[PhotosPicker](https://developer.apple.com/documentation/photosui/photospicker).

## Editar título e anotações

`PATCH /v1/references/{id}` aceita, desde 12/09/2026, um corpo com `kind` igual ao da referência de imagem, `title`, `text`, `revision` e `mutation_id`. O arquivo e seus metadados não mudam; a revisão sobe, a análise volta para a fila (com reserva de operação e, para mídia, dos segundos de áudio, como em uma nova tentativa) e o mesmo `mutation_id` repetido devolve o mesmo resultado. Tipo diferente ou revisão desatualizada respondem 409. Cobertura em `tests/test_images.py` e `tests/test_media.py`.
