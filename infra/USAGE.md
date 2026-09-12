# Uso diário — desenvolvimento

Limites operacionais iniciais por conta, independentes de assinatura. Cotas e preços
comerciais continuam para a etapa de RevenueCat/IAP.

| Medida | Padrão | Configuração |
| --- | --- | --- |
| Salvamentos, edições e novas análises | 100/dia | `QUEEP_OPERATION_DAILY_LIMIT` |
| Áudio/vídeo para analisar | 30 min/dia | `QUEEP_MEDIA_DAILY_MINUTES` |
| Arquivos preparados, incluindo miniaturas | 100 MiB/dia | `QUEEP_UPLOAD_DAILY_MIB` |
| Buscas que iniciam IA | 100/dia | `QUEEP_SEARCH_DAILY_LIMIT` |

O dia vira à meia-noite UTC. `GET /v1/usage` retorna contadores e `resets_at`;
os apps mostram a data/hora local em **Conta → Uso e privacidade**. A consulta
exige conta ativa, mas não assinatura. Respostas são privadas, sem cache.

## Comportamento

- A reserva integra a transação do conteúdo/outbox ou do upload. Concorrência não
  permite ultrapassar cotas. Contenção esgotada retorna 503, com reenvio seguro.
- Repetir o mesmo envio/edição não cobra outra reserva, inclusive depois de falha
  no Storage ou ao atingir o limite. Upload abandonado mantém a reserva do dia.
- Edição e nova análise manual contam; tentativas automáticas do mesmo trabalho
  ficam incluídas. Nova análise de mídia volta a consumir sua duração, arredondada
  para o segundo seguinte. Excluir não devolve orçamento consumido.
- 429 informa a restrição e `Retry-After`. Leitura e exclusão continuam disponíveis.
- Contadores ficam em `users/{uid}/usage/{dia}` e são removidos na exclusão da conta;
  o TTL os expira 30 dias após o fechamento do dia. Nenhum conteúdo vai nesse ledger.
- O orçamento global de IA de US$ 1/dia e as proteções por arquivo continuam ativos.
  Estas cotas não definem armazenamento total contratado nem prometem disponibilidade
  de IA quando o orçamento global acabar.

## Evidência

169 testes backend passaram, incluindo concorrência, repetição, edição, falha de
upload, bloqueio antes do Storage, duração, busca, virada de dia e isolamento.
Lint aprovado. O buffering de imagens passou a respeitar o mesmo limite de
concorrência usado por áudio/vídeo.

Nuvem: `scripts/smoke-usage-cloud.py` testa esgotamento somente em contas novas,
sem gerar 100 trabalhos de IA. Consulta, reenvio, 429 e leitura/exclusão passaram;
[relatório](../evaluation/results/cloud-usage-smoke.json). Regressão de CRUD,
imagens e isolamento também aprovada. TTL `usage.expires_at` ativo.

Publicação: API `queep-api-00024-57f`, worker `queep-worker-00010-rfs`; manutenção
atualizada para a mesma imagem, execução `queep-image-cleanup-4s5kd` aprovada.
