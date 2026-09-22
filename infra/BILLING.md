# Assinaturas — Queep

> GAZ-115: o contrato atual Free/Pro/Max está em [PLANS.md](PLANS.md).
> Preparação e pendências de publicação: [APPLE-LAUNCH.md](APPLE-LAUNCH.md).
> As seções abaixo registram a integração original de setembro, anterior aos planos.

## Implementado

RevenueCat SDK 5.89.0 nos apps Mac/iPhone, com Firebase UID como `appUserID`.
Uma tela nativa em **Conta → Assinatura** carrega os produtos/preços da loja,
compra, restaura e consulta o acesso no backend. Não há preço fixo no código.
A extensão de compartilhamento não carrega o SDK de compras.

| API | Comportamento |
| --- | --- |
| `GET /v1/billing/access` | Acesso da conta Firebase autenticada. |
| `POST /v1/billing/sync` | Reconsulta após compra/restauração; no máximo uma consulta por conta a cada 10 s. |
| `POST /v1/billing/webhook` | Exige `Authorization: Bearer <token exclusivo>`; recebe até 64 KiB, deduplica por ID e invalida acesso. |

A autorização vem da API RevenueCat V1, nunca do corpo enviado pelo app.
O cache dura até 5 min para assinantes e 1 min para inativos, sem ultrapassar o
vencimento. Eventos invalidam todas as contas/aliases envolvidos, inclusive ambos
os lados de transferências. A próxima consulta lê o estado atual no RevenueCat,
assim eventos atrasados não sobrescrevem renovação, expiração ou reembolso.
Um evento durante uma consulta impede a publicação da resposta anterior.
Eventos guardam somente recibo de processamento com TTL de 30 dias.

Cancelamento mantém acesso até expirar; período de graça é respeitado. Reembolso
e expiração bloqueiam novas capturas, edições que acionam IA, reanálise e busca.
Listar, abrir e excluir referências continuam liberados à conta autenticada.
Falha de confirmação retorna erro recuperável, sem conceder acesso.
Esta entrega suporta assinaturas recorrentes; vitalício/promocionais ficam fora.

## Test Store configurado

Projeto Queep `f6e36522`, app Test Store `appf755aea124`, entitlement `queep_pro`,
offering atual `default` (`ofrng5f2f128c8a`), produtos recorrentes `monthly` e `yearly`.
Preço final e teste gratuito seguem indefinidos; os valores são apenas de teste.
Nenhum projeto RevenueCat anterior foi alterado.

1. No terminal executar `python3 scripts/configure-revenuecat.py` com a chave pública
   `test_…`. A consulta de assinante da API V1 aceita essa chave; não é necessária uma
   chave secreta administrativa. Configurações ficam em `.env.billing` e no xcconfig
   de desenvolvimento, ignorados pelo Git e 0600. O script não altera a nuvem nem compra.
2. Para teste local, iniciar a API com `.env.example`, `.env.evaluation` e
   **`.env.billing` por último**. Recompilar apps. A API passará a exigir assinatura
   nesse processo; não interromper o ambiente comum sem combinar o teste.
3. Para teste de acesso pago na nuvem: configurar a chave de consulta V1 e o token de webhook,
   conceder acesso somente à identidade `queep-api`, vincular os segredos à API e
   definir `QUEEP_BILLING_MODE=required`, `QUEEP_BILLING_ACCEPT_SANDBOX=true`.
   Configurar no RevenueCat a URL da API acima + `/v1/billing/webhook`, com o
   header Bearer exclusivo. Não usar OpenRouter nem chave pública como token.
4. Testes externos restantes: eventos de ciclo de vida da assinatura, expiração/reembolso
   e política de restauração/troca de conta. Estados simulados já têm testes de contrato.

Webhook cloud validado em `queep-api-00012-dx2`: segredo `queep-revenuecat-webhook:1`
vinculado exclusivamente à identidade da API. URL:
`https://queep-api-721281571441.southamerica-east1.run.app/v1/billing/webhook`.
Integração `whintgrdb1ada9154` salva para **Sandbox / Test Store / All events**.
Em 11/09/2026, às 19:50 UTC, o evento TEST
`7A2ADFB1-43C0-429F-9752-9A690E4D8D94` enviado diretamente pelo painel RevenueCat
recebeu **200 / {"received":true}**. Cloud Run e Firestore confirmaram a entrega,
o recibo com TTL de 30 dias e a invalidação dos dois aliases sintéticos.

Repetição autenticada do mesmo ID pela API retornou 200 sem modificar recibo,
geração ou data de invalidação; sem autenticação retornou 401. O botão “Resend Test
Event” do painel gera outro ID: essa segunda entrega também recebeu 200, mas não é
a evidência de deduplicação. Relatório local: `/tmp/queep-rc-webhook-verification.json`.
O teste confirma conexão, autenticação e processamento de TEST; eventos reais de
renovação, expiração e reembolso ainda precisam de validação externa.

Para configuração futura, `python3 scripts/copy-revenuecat-webhook.py` copia o header
sem imprimi-lo. Nenhuma alteração de código ou nova implantação foi necessária.

O ambiente `queep-dev-hstlx` permanece com `QUEEP_BILLING_MODE=development`:
sem cobrança e sem exigência de assinatura. Scripts `deploy-*-dev.sh` explicitam
esse modo; após uma implantação de desenvolvimento, reativar o teste de assinatura
se necessário. Bypass e aceitação de sandbox são proibidos em outros projetos cloud.
Produção usa `required` por padrão e, sem chave, bloqueia operações pagas com 503.

## Antes de cobrar / publicar

Configurar apps/produtos Apple, contratos fiscais/bancários e assinatura de distribuição;
chaves Apple `appl_…` próprias para cada app, mesma conta Queep nos dois dispositivos,
URLs publicadas de privacidade/termos e webhook de produção. Sugerido manter compras
com o App User ID original no RevenueCat; a restauração não deve transferir assinatura
silenciosamente entre contas Queep. Confirmar a política antes do beta.

`Config/BillingProduction.xcconfig` é separado e ignorado. Build Release rejeita
chave Test Store, chave ausente e URLs legais ausentes. A API de produção não aceita
sandbox. O Firebase/API de produção também continua pendente; isto não é distribuição.
Não enviar segredos para os apps; apenas a chave pública de cada plataforma.

O limite de buscas existente é configurável (`QUEEP_SEARCH_DAILY_LIMIT`, padrão 100/dia,
UTC), além do intervalo mínimo de 1 s e orçamento diário global de IA. Isso é controle
operacional do desenvolvimento, não definição comercial de planos. Cotas de captura,
armazenamento e teste gratuito precisam ser decididas antes de abrir o beta.

## Evidências e limites

Testes de contrato com respostas simuladas RevenueCat e Firestore real em emulador
cobrem expiração, cancelamento, graça, reembolso, sandbox, isolamento de identidade,
eventos duplicados/atrasados, transferências, disputa durante consulta e indisponibilidade.
Esses testes **não** substituem as validações externas abaixo nem o sandbox Apple.

Test Store real validado em 11/09/2026, sem cobrança: cancelamento do diálogo,
falha simulada da loja, compra mensal, confirmação da API, restauração e reabertura
no iPhone. A mesma assinatura foi reconhecida e restaurada no Mac. Reinstalação iOS
com o novo bundle local também manteve o acesso. Assinante conseguiu salvar pela API;
outra conta recebeu 402, não conseguiu forjar UID/acesso e não leu a referência privada.
Nota e conta descartáveis removidas. Biblioteca cloud reaberta com as quatro referências.

Testes externos são opt-in: criar `.local/revenuecat-test-account.json` no repo Apple
somente durante a rodada, usando a conta emulada `preview@queep.test`. API deve estar
em modo `required` com Test Store configurado. Executar individualmente, nesta ordem:
`RevenueCatTests/testRealTestStoreCancellationFailurePurchaseAndRestore` (exige conta
sem assinatura ativa), `RevenueCatTests/testExistingTestStoreAccessAfterLocalAppReinstall`
e `RevenueCatCrossDeviceTests/testPurchaseFromIPhoneIsActiveOnMac` (exigem assinatura ativa).
Remover o marcador ao terminar e voltar a API local ao modo `development`.
Os diálogos de compra só são confirmados quando identificados como Test Store.
Ao encerrar esta rodada, marcador removido e API local restaurada. A tela de acesso
de desenvolvimento passou novamente; testes externos foram ignorados como esperado.

XCResults no repo Apple: `build-local/Logs/Test/Test-QueepiOS-2026.09.11_16-18-01--0300.xcresult`,
`build-local/Logs/Test/Test-QueepiOS-2026.09.11_16-30-18--0300.xcresult` e
`build-mac-local/Logs/Test/Test-QueepMacCloud-2026.09.11_16-29-16--0300.xcresult`.
Logs `/tmp/queep-rc-ios-real-test.log`, `/tmp/queep-rc-ios-isolated-test.log`,
`/tmp/queep-rc-mac-real-test.log`. Apple IAP real e sandbox Apple permanecem pendentes.

Validado em 11/09/2026: 124 testes de backend (17 de assinaturas), lint, 3 testes Swift,
builds Mac/iPhone e teste iOS da tela com emuladores e com API cloud. Conferência visual
no Mac aprovada. API `queep-api-00011-44q` e worker `queep-worker-00004-5hz` publicados,
job de limpeza atualizado; regressão cloud de contas/CRUD/imagens/isolamento aprovada.
Todos os testes cloud descartáveis foram limpos; quatro referências do usuário mantidas.

Evidências locais: `/tmp/queep-billing-all-tests.log`, `/tmp/queep-billing-ios-test.log`,
`/tmp/queep-billing-ios-cloud.log`, `/tmp/queep-billing-cloud-smoke.log`.
XCResults: `build-local/Logs/Test/Test-QueepiOS-2026.09.11_14-59-17--0300.xcresult`
e `build/Logs/Test/Test-QueepiOS-2026.09.11_15-03-40--0300.xcresult` no repo Apple.

Referências: [SDK Apple](https://www.revenuecat.com/docs/getting-started/installation/ios),
[Test Store](https://www.revenuecat.com/docs/test-and-launch/sandbox/test-store),
[identidade](https://www.revenuecat.com/docs/customers/identifying-customers),
[webhooks](https://www.revenuecat.com/docs/integrations/webhooks),
[API V1](https://www.revenuecat.com/docs/api-v1/customers).

## Decisão atual sobre exclusão

A limpeza de customers RevenueCat foi adiada pelo usuário. Exclusão/recuperação do
Queep funciona de forma independente; pedidos do fornecedor ficam em fila separada.
Não configurar nova chave ou App Store Connect nesta etapa. [Política](ACCOUNTS.md).
