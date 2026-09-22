# GAZ-115 — Free / Pro / Max

## Contrato implementado

O backend resolve o entitlement válido mais alto (`queep_max` > `queep_pro` > Free).
O Free permanece utilizável após expiração/reembolso; cancelar mantém o plano até
expirar. Falha na consulta RevenueCat continua 503 e não concede acesso. O cache
antigo sem `plan` é invalidado automaticamente. Webhooks de qualquer produto
invalidam o snapshot inteiro, com as mesmas proteções de identidade e concorrência.

`GET /v1/billing/access` inclui `plan`, `features: {spaces, media_av}` e `limits`.
`GET /v1/usage` retorna o mesmo plano e os contadores abaixo. `active` continua
significando assinatura paga ativa; não significa que Free perdeu a biblioteca.
Modo `development` identifica explicitamente o bypass e usa limites Max.

| Recurso | Free | Pro | Max |
|---|---:|---:|---:|
| Operações de IA/mês | 12 | 100 | 200 |
| Buscas/mês | 8 | 50 | 80 |
| Segundos A/V de arquivo/mês | 0 | 0 | 2400 |
| Upload preparado/mês | 80 MiB | 400 MiB | 6 GiB |
| Itens | 100 | 800 | 2500 |
| Storage | 250 MiB | 2 GiB | 10 GiB |
| Spaces | 0 | 15 | 40 |

O ledger mensal é `users/{uid}/usage/{YYYY-MM}`, renovado no primeiro dia do mês,
00:00 UTC (inclusive dezembro/janeiro e anos bissextos). Upgrades/downgrades não
zeram consumo. `fair_use/{YYYY-MM-DD}` mantém tetos diários de abuso; excedê-los
continua 429 com Retry-After. Não há créditos, câmbio nem compra de uso extra.

402 retorna `detail: {code, message, plan, resource, required_plan, resets_at}`.
`media_av_required` sempre indica Max; `storage_limit` identifica disco;
`plan_limit` identifica operações, busca, itens ou Spaces. Free → Pro, Pro → Max;
Max retorna `required_plan: null`, sem oferecer um plano inexistente. Inventário
não renova por mês; `resets_at` é a renovação das cotas mensais, não a do disco.

As reservas mensais, itens e storage são transacionais junto à criação do trabalho.
Reenvio exato não cobra de novo; envio interrompido mantém a reserva e pode ser
retomado. Storage inclui originais, thumbnails, previews sociais e avatares, além
de uploads pendentes até a limpeza. Excluir libera itens imediatamente e bytes
após a exclusão confirmada e a janela de proteção de uploads (10 min).
Listar/abrir/reproduzir/excluir continuam sem consulta de assinatura.

A/V enviado como arquivo é verificado antes de decodificar/preparar/gravar.
Posts sociais continuam com o contrato de 15 segundos do primeiro clipe público;
YouTube/Vimeo continuam metadados. Vídeo já salvo continua reproduzível no Pro/Free.

## Integração com GAZ-116

Spaces ainda não tem modelo/API/UI funcional neste checkout: é a GAZ-116.
Esta entrega define `features.spaces`, limites e `require_space_change` com testes:
Free não cria/atribui; Pro no 16º pede Max; atribuição em Spaces existentes é
permitida mesmo após downgrade acima da quantidade permitida. A GAZ-116 deve
chamar essa política dentro de suas transações e atualizar `inventory/current.spaces`.
Leitura/filtro/exclusão não devem chamar o gate. Isso é pendência de integração,
não uma afirmação de que os fluxos de Spaces foram entregues nesta issue.

## Migração antes do rollout

Contas novas inicializam o inventário automaticamente. Manifests antigos não
registram tamanho de thumbnails: não estimamos nem contamos zero silenciosamente.
Antes de ativar `required`, reconciliar cada conta preexistente, em janela sem
uploads em andamento:

```
uv run --env-file <ambiente> python -m scripts.reconcile-storage --uid <firebase-uid>
```

O script lê os tamanhos reais de objetos, não apaga arquivos e trava mutações da
conta durante a medição. Recusa uploads ainda na janela de assentamento. Em falha
parcial, deixa `reconciling=true`; corrigir a causa e revisar o lock antes de
reexecutar. Leitura da biblioteca permanece disponível. Não foi executado em
contas cloud nesta implementação.

Os contadores diários históricos não são tratados como mensais. O primeiro mês
comercial começa no rollout; definir a data de ativação antes de liberar o beta.
O TTL das novas reservas é 90 dias depois do reset; `fair_use` mantém 30 dias.

## Observabilidade de custo

`ai_calls` agora persiste `created_at`, `plan`, `kind`, `operation_id`,
`media_seconds`, `cost_usd` e se o custo é conhecido. Atribuição usa ContextVar,
sem alterar o cliente de IA compartilhado. Outbox carrega o plano autorizado;
retries somam ao mesmo trabalho. Jobs antigos aparecem como não atribuídos.

```
uv run --env-file <ambiente> python -m scripts.export-plan-costs
```

Exporta média móvel de 30 dias por plano/tipo, somando interpretar + embedding +
transcrição por operação. Custos desconhecidos não viram zero. Sinaliza revisão
acima de US$ 0,015/captura, US$ 0,022/social ou US$ 0,06/minuto de mídia.
**É custo do provedor**, não COGS completo: acrescentar extração, Cloud Run,
Firestore, storage e egress antes de recalcular margem e confirmar os tetos.
Preços e alíquotas comerciais da issue são premissas, não novas medições.
