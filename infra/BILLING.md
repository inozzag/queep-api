# Assinaturas — Queep

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

## Configuração de teste pendente

1. Criar um projeto **Queep** no RevenueCat e usar o mesmo projeto para Mac/iPhone.
2. Criar o entitlement `queep_pro`; no Test Store, associar produtos recorrentes
   a ele e a uma offering marcada como atual. Preço final e teste gratuito seguem
   indefinidos; os valores do Test Store são apenas de teste.
3. Criar uma chave secreta **API V1** exclusiva e copiar a chave pública `test_…`.
   No terminal executar `python3 scripts/configure-revenuecat.py`. As chaves ficam
   em `.env.billing` e no xcconfig de desenvolvimento, ignorados pelo Git e 0600.
   O script não ativa a nuvem nem realiza compras.
4. Para teste local, iniciar a API com `.env.example`, `.env.evaluation` e
   **`.env.billing` por último**. Recompilar apps. A API passará a exigir assinatura
   nesse processo; não interromper o ambiente comum sem combinar o teste.
5. Para teste na nuvem: armazenar chave V1 e token de webhook no Secret Manager,
   conceder acesso somente à identidade `queep-api`, vincular os segredos à API e
   definir `QUEEP_BILLING_MODE=required`, `QUEEP_BILLING_ACCEPT_SANDBOX=true`.
   Configurar no RevenueCat a URL da API acima + `/v1/billing/webhook`, com o
   header Bearer exclusivo. Não usar OpenRouter nem chave pública como token.
6. Validar compra/cancelamento/restauração, renovação/expiração/reembolso,
   assinatura iniciada em um dispositivo e reconhecida no outro, e troca de conta.

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
Esses testes **não** substituem uma compra no Test Store e no sandbox Apple.
Até configurar as contas externas, não há compra/restauração real validada.

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
