# Áudio, vídeo e fontes sociais

Arquivos de até **cinco minutos e 25 MiB preparados**. Os apps aceitam originais de
até 150 MiB, exportam vídeo em 720p/H.264 ou áudio M4A e conferem o tamanho antes de
enviar. A API aceita MP4/M4A, MP3 e WAV compatíveis; vídeo precisa ser H.264.
Não há gravação de microfone, download de vídeos sociais ou importação de playlists.

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
| YouTube | Título e autor públicos via oEmbed, incluindo links curtos/Shorts reconhecidos. |
| Vimeo | Título e autor públicos via oEmbed para links de vídeo reconhecidos. |
| Instagram | Descrição pública quando acessível; login, bloqueios e ausência de texto preservam só o link. |

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

`PATCH /v1/references/{id}` aceita, desde 12/09/2026, um corpo com `kind` igual ao da referência de áudio/vídeo, `title`, `text`, `revision` e `mutation_id`. O arquivo e seus metadados não mudam; a revisão sobe, a análise volta para a fila (com reserva de operação e, para mídia, dos segundos de áudio, como em uma nova tentativa) e o mesmo `mutation_id` repetido devolve o mesmo resultado. Tipo diferente ou revisão desatualizada respondem 409. Cobertura em `tests/test_images.py` e `tests/test_media.py`.
