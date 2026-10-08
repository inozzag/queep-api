# Áudio, vídeo e fontes sociais

Arquivos de até **cinco minutos e 25 MiB preparados**. Os apps aceitam originais de
até 150 MiB, exportam vídeo em 720p/H.264 ou áudio M4A e conferem o tamanho antes de
enviar. A API aceita MP4/M4A, MP3 e WAV compatíveis; vídeo precisa ser H.264.
Não há gravação de microfone nem importação de playlists. O mp4 original de um
post não fica para reprodução. Quando a fonte publica um clipe, o worker guarda um
loop mudo de até 8 s ao lado do pôster (`ImageInfo.motion`) e o app pede
`GET /v1/references/{id}/image?motion=true`. Uploads usam `MediaInfo.loop` e
`GET /media?loop=true`.

## Captura e processamento

1. App prepara o arquivo e envia com UUID estável. A confirmação ocorre após Storage
   e registro da referência; falhas podem ser reenviadas sem duplicação.
2. Worker privado recebe somente identificadores. FFmpeg extrai a faixa de áudio
   e até oito cenas distribuídas pelo vídeo, com limite total de leitura de 40 s.
3. Gemini 2.5 Flash-Lite transcreve a fala em uma chamada dedicada; outra interpreta
   esse texto. Uma chamada independente interpreta as cenas **sem receber título ou transcrição**. Isso
   impede que uma descrição falada seja fornecida como contexto visual ao modelo.
4. Resumo, transcrição e aparência alimentam a busca. A verificação distingue fala
   de evidência visual e a API confere o formato solicitado. O vídeo sempre informa
   cobertura parcial por amostragem.

Transcrição limitada a 18 mil caracteres; entrada do embedding limitada a 7 mil
bytes, incluindo resumo e trecho da transcrição. Não equivale a busca exaustiva em
cada palavra ou frame. A avaliação inicial usa mídia sintética em português;
sotaques, ruído, música, cenas rápidas e outros idiomas precisam de acervo maior.

## Armazenamento e operação

- Reprodução exige token Firebase e baixa o arquivo para um temporário do app,
  removido ao fechar o detalhe. Controles nativos, sem reprodução automática.
- Reutiliza o registro histórico `image_uploads` e prefixo `images/v1` como ciclo de
  vida comum dos arquivos. Isso preserva limpeza, idempotência, isolamento e proteção
  contra gravações tardias; não é uma coleção pública.
- FFmpeg/FFprobe incluídos no Docker. Sem URLs remotas no decodificador, sem logs de
  conteúdo; processos com limites de duração/alocação. API serializa transformações.
- Worker: uma requisição concorrente, timeout e Cloud Tasks de 300 s, lease de 360 s.
  Tentativas e orçamento diário permanecem limitados. Não há fallback caro automático.
- Suspensão de conta mantém os arquivos para recuperação por 30 dias. Exclusão
  definitiva usa a mesma manutenção de imagens; [retenção](ACCOUNTS.md).

## Fontes sociais

| Fonte | O que pode ser lido nesta entrega |
| --- | --- |
| YouTube | Título, autor, pôster e ícone públicos via oEmbed, incluindo links curtos/Shorts. Sem arquivo de vídeo. |
| Vimeo | Título, autor, pôster e ícone públicos via oEmbed para links de vídeo reconhecidos. Sem arquivo de vídeo. |
| X / Twitter | Texto público do post, nome do autor, foto de perfil, fotos, GIFs e pôsteres de vídeo. Do primeiro vídeo, transcreve-se só a fala dos 15 s iniciais (música de fundo é descartada). O mp4 público vira o loop mudo de `motion`, não um arquivo solto. Áudio solto segue a mesma janela de 15 s e aparece no detalhe com player. Respostas não entram. |
| Instagram | Descrição, pôster e avatar públicos quando a página os oferece. Um `video_url` escapado em `gql_data` vira o clipe do loop. Login e bloqueio preservam o link. |
| Dribbble | Sem proxy. Um shot que a origem recusa continua indisponível. |

Esses links têm cobertura parcial. O app mostra o material efetivamente lido e
orienta enviar o arquivo para analisar fala/cenas. Não usa cookies da conta do
usuário, não promete transcrição de qualquer link e não contorna restrições da fonte.

## Validação

- Emuladores: upload/reenvio, limites, reprodução privada, isolamento, retomada após
  falha, recuperação/exclusão, análise única e separação entre áudio e visão.
- Swift: exportação de áudio/vídeo, limpeza dos temporários, cópia do arquivo antes
  de o provedor de compartilhamento removê-lo; teste de interface toca/pausa os dois tipos.
- `uv run scripts/smoke-media-cloud.py` testa IA e busca reais com contas novas e
  mídias sintéticas de `queep-apple/Tests/Fixtures`. Remove referências e identidades
  ao terminar. O relatório fica em `evaluation/results/cloud-media-smoke.json`.

Publicado em desenvolvimento: API `queep-api-00023-8c6`, worker
`queep-worker-00009-k9q` e manutenção com a mesma imagem. Os cinco casos de busca
passaram na nuvem: assunto, formato de vídeo/áudio, aparência não observada e
resultado vazio. YouTube retornou metadados; os casos de Vimeo/Instagram preservaram
o link com leitura indisponível. [Relatório](../evaluation/results/cloud-media-smoke.json).
As sete consultas anteriores de notas/imagens/links também passaram;
[regressão](../evaluation/results/cloud-ai-smoke.json). São amostras pequenas,
não uma garantia de recuperação para qualquer conteúdo.

Referências: [áudio OpenRouter](https://openrouter.ai/docs/guides/overview/multimodal/audio),
[vídeo OpenRouter](https://openrouter.ai/docs/guides/overview/multimodal/videos),
[oEmbed Vimeo](https://help.vimeo.com/hc/en-us/articles/12427952430481-Overview-oEmbed).

## Editar título e anotações

`PATCH /v1/references/{id}` aceita, desde 12/09/2026, um corpo com `kind` igual ao da
referência (`image`, `audio`, `video` ou `link`), `title`, `text`, `revision` e
`mutation_id`. Em upload, o arquivo não muda; a revisão sobe e a análise volta para a
fila. Em post (`kind=link`), só o título muda e a análise não é reenfileirada.
O mesmo `mutation_id` repetido devolve o mesmo resultado. Tipo diferente ou revisão
desatualizada respondem 409. Cobertura em `tests/test_images.py` e `tests/test_media.py`.

## Analisar de novo

`POST /v1/references/{id}/retry` aceita `revision` e `scope`. O padrão é `fetch`.
`analysis` usa o mesmo conjunto de aceitação e também reenfileira o worker, que
hoje relê o link. Entra na fila quando o item está em `error`, com
`media_incomplete`, com `source.state=unavailable`, ou com um slide `video`/`gif`
sem `motion`. Revisão diferente ou item já `queued`/`processing` respondem 409.
Cota diária ou uma nova tentativa do mesmo item em menos de 10 minutos respondem
429 com `Retry-After`. Sucesso devolve a referência com `status=queued`.

O lote correspondente é `python -m queep.reprocess` (plano seco por padrão).
`--enqueue` só grava jobs se `--require-version` for a `ANALYSIS_VERSION` que o
worker publicado serve. Não chama Cloud Tasks; o scheduler `queep-ai-dispatch`
entrega a fila.
