# GAZ-115 — preparação para lojas Apple

## Estado desta execução

Atualizado em **22/09/2026**. Site e páginas legais publicados em `https://queep.app`.
API, worker, fila e scheduler TestFlight implantados; 24 contas existentes
reconciliadas antes de ativar billing required. Os oito índices Firestore estão
READY e os TTLs `usage`, `ai_calls`, `billing_events` e `fair_use` estão ACTIVE.
O código da API implantado está no commit `4fa2732`.

O archive **Mac 0.1.0 (1)** foi enviado, processado e está **Pronta para envio**
no TestFlight, com a declaração de criptografia concluída. Ainda não há grupo
de testers configurado. O archive iOS está bloqueado neste Mac por suporte de
plataforma ausente no Xcode; o autor proibiu downloads de simuladores.
Nenhuma compra sandbox foi executada e nenhum convite foi enviado.

Validação: 329 testes da API, 18 testes QueepCore, guard de billing e build do
site passaram. Smoke cloud confirmou Free, cotas 12/8, bloqueios de Spaces e
A/V, isolamento entre contas, captura/retry, worker na fila exclusiva, queries
de arquivamento e cobrança única de uso. As contas sintéticas foram removidas.

Preparação adotada: **Caminho A**, Firebase `queep-dev-hstlx`, sandbox Apple e API
isolada com `QUEEP_BILLING_MODE=required`. O serviço compartilhado `queep-api`
permanece em development. A escolha foi registrada na GAZ-115 antes dos produtos.

| Plataforma | App Store Connect | Bundle | Grupo | RevenueCat app |
|---|---|---|---|---|
| iOS | [Queep · 6814305316](https://appstoreconnect.apple.com/apps/6814305316/distribution) | `app.queep.ios.dev` | `22400342` — Queep Plans | `app58a82f4b36` |
| macOS | [Queep for Mac · 6814306133](https://appstoreconnect.apple.com/apps/6814306133/distribution) | `app.queep.mac.dev` | `22400489` — Queep Mac Plans | `appfd4e84991e` |

Ambos os apps RevenueCat usam a credencial de compras Apple já existente no
mesmo workspace, validada pelo console. Nenhuma chave privada nova foi criada,
baixada ou copiada. A API administrativa do App Store Connect não foi conectada;
os produtos RevenueCat são cadastrados manualmente.

| Produto | Apple iOS | Produto equivalente Mac | Apple macOS |
|---|---|---|---|
| `app.queep.pro.monthly` | `6814305964` | `app.queep.mac.pro.monthly` | `6814307138` |
| `app.queep.pro.yearly` | `6814305946` | `app.queep.mac.pro.yearly` | `6814307574` |
| `app.queep.max.monthly` | `6814305673` | `app.queep.mac.max.monthly` | `6814306447` |
| `app.queep.max.yearly` | `6814305933` | `app.queep.mac.max.yearly` | `6814306576` |

A Apple recusou reutilizar IDs de produto entre os apps. Por isso, os quatro
equivalentes macOS usam `app.queep.mac.*`; QueepCore reconhece explicitamente
os oito IDs. Os quatro testes `SubscriptionContractTests` passaram após o ajuste.

Preços salvos em ambos: Pro US$6,99/mês e US$59,99/ano; Max US$19,90/mês.
Conversão brasileira automática: R$49,90/mês, R$399,90/ano e R$129,90/mês.
Max anual **US$159,99 / R$999,90** confirmado nos dois apps, conforme escolha
explícita do autor; a Apple não oferece US$159,90.
Trial grátis de uma semana salvo somente no Pro anual de cada app, de
20/09/2026 sem data final. Disponibilidade: 175 regiões atuais, sem adesão
automática a regiões futuras; anuais apenas com pagamento antecipado.
Family Sharing permanece desligado. Localizações dos produtos em PT-BR.

Níveis e localização dos grupos confirmados nos dois apps: Max 1, Pro 2,
localização PT-BR "Planos Queep".

RevenueCat: os oito produtos foram cadastrados e vinculados à oferta `default`
(`ofrng5f2f128c8a`), com Pro anual primeiro. Cada package reúne os equivalentes
iOS/macOS. `queep_pro` inclui todos os oito; `queep_max` inclui apenas os quatro
Max. Os produtos históricos do Test Store foram preservados.
**Keep with original App User ID** foi salvo no projeto, sem comportamento
diferente para sandbox. As duas chaves públicas `appl_` foram preenchidas no
arquivo local ignorado `queep-apple/Config/BillingTestFlight.xcconfig`.

Notificações Apple → RevenueCat de produção e sandbox confirmadas nos dois
apps. Contrato Paid Applications, banco e formulários fiscais estão Active.
URL de privacidade salva em ambos: `https://queep.app/privacy`.
Permanecem pendentes: archive/upload iOS em ambiente apto, grupos/testers,
metadados/revisão beta externa e testes de compra/restore em sandbox. A
verificação de comerciante DSA ainda aparece pendente para distribuição pública.
A API administrativa Apple ausente impede consultar o status da loja
automaticamente no RevenueCat; a credencial de compras aparece validada.

## Contrato do catálogo

Um grupo de assinatura por app (ou um registro universal iOS/macOS, se os bundles
forem unificados antes da publicação). Os bundles atuais são diferentes; não
presumir que seus grupos Apple sejam compartilhados. O UID Firebase no RevenueCat
compartilha o acesso entre aparelhos. Assinantes ativos são encaminhados para
Gerenciar na App Store para trocar plano, evitando iniciar uma segunda assinatura
no outro app. Conferir esse fluxo em sandbox nos dois sentidos.

| Produto | Nível no grupo | Preço base USD | Entitlements | Trial |
|---|---:|---:|---|---|
| `app.queep.pro.monthly` | 2 | 6,99/mês | `queep_pro` | nenhum |
| `app.queep.pro.yearly` | 2 | 59,99/ano | `queep_pro` | 7 dias, elegíveis |
| `app.queep.max.monthly` | 1 | 19,90/mês | `queep_pro` + `queep_max` | nenhum |
| `app.queep.max.yearly` | 1 | 159,99/ano | `queep_pro` + `queep_max` | nenhum |

Não habilitar Family Sharing. Se algum preço exato não estiver disponível no
catálogo Apple, confirmar com o autor antes de substituir por outro price point.
RevenueCat `default`: packages `$rc_annual` (Pro anual), `$rc_monthly` (Pro mensal),
`max_yearly` e `max_monthly`; equivalentes por plataforma no mesmo package.
Restore behavior configurado: **Keep with original App User ID**, sem transferir
assinatura entre contas Queep, tanto em produção quanto no sandbox.

A tela só usa `localizedPriceString`; desconto anual é calculado comparando os
preços dos produtos da mesma moeda retornados pela loja. Trial só aparece no Pro
anual se a oferta cadastrada for grátis por 7 dias e a Apple/RevenueCat informar
eligibilidade. A UI não promete trial a quem já usou.

## API e worker isolados

Implantação concluída em `queep-dev-hstlx`, região `southamerica-east1`:

- API: `https://queep-api-testflight-721281571441.southamerica-east1.run.app`
  (revisão `queep-api-testflight-00002-9gl`).
- Worker privado: `queep-worker-testflight`; fila `queep-references-testflight`;
  scheduler `queep-testflight-dispatch`; outbox `jobs_testflight`.
- Token exclusivo: Secret Manager `queep-testflight-revenuecat-webhook:1`.
  O valor não deve ser registrado em arquivos ou logs.
- RevenueCat iOS: `whintgr6c260adbaa`, sandbox, app `app58a82f4b36`, todos os
  eventos, endpoint canônico acima + `/v1/billing/webhook`.
- RevenueCat Mac: `whintgr2aaafd88a0`, sandbox, app `appfd4e84991e`, todos os
  eventos, endpoint alternativo do mesmo serviço
  `https://queep-api-testflight-eafe6imkgq-rj.a.run.app/v1/billing/webhook`.
- Ambos os webhooks responderam HTTP 200 ao evento TEST do RevenueCat.
  A integração development/Test Store original foi preservada.

Requisitos para futuras implantações:

- Criar serviço API e worker/queue próprios para o sandbox TestFlight no mesmo
  projeto dev; não reutilizar fila/worker antigos em modo development, pois o
  worker também aplica os limites de storage ao baixar previews.
- API e worker: `QUEEP_ENV=production`, `QUEEP_BILLING_MODE=required`,
  `QUEEP_BILLING_ACCEPT_SANDBOX=true`, projeto/bucket dev. Manter autenticação
  Firebase nas operações; worker privado com Cloud Tasks/OIDC.
- `REVENUECAT_ENTITLEMENT=queep_pro`, `REVENUECAT_MAX_ENTITLEMENT=queep_max`;
  chave de consulta e token exclusivo do webhook no Secret Manager. Nunca no app.
- Webhook sandbox → API isolada `/v1/billing/webhook`, Bearer exclusivo, ambos
  entitlements/produtos. Eventos invalidam o acesso inteiro.
- Reconciliar contas antigas conforme [PLANS.md](PLANS.md) antes de liberar
  required. Firestore é compartilhado pelo Caminho A; dados dev existentes não
  são apagados nem convertidos em assinaturas.
- Validar configuração de TTL em `fair_use`, `usage` e `ai_calls`/`expires_at`.
- A URL isolada não é inventada no código. Preencher após criar o serviço.

## Build

O repo Apple inclui `QueepiOSTestFlight` e `QueepMacTestFlight`, configuração
**TestFlight** de release, sem `DEBUG`, com Firebase/bundles `.dev`.
O arquivo ignorado `Config/BillingTestFlight.xcconfig` contém as chaves públicas
`appl_…` por app, a URL isolada e `https://queep.app/privacy` / `/terms`.
Para outro checkout, partir do exemplo e copiar as chaves públicas dos
respectivos apps no RevenueCat. URLs usam `https:/$()/` para preservar `//`
na sintaxe xcconfig.

O guard do build bloqueia `test_…`, chave ausente, URLs legais ausentes e a API
compartilhada de desenvolvimento. Não incluir chave secreta no bundle.
O archive Mac foi assinado e enviado automaticamente com `xcodebuild
-exportArchive`, método `app-store-connect`, preservando versão/build.
O bundle final foi inspecionado: 0.1.0 (1), API isolada e URLs legais corretas.
Os Info.plist gerados agora referenciam MARKETING_VERSION/CURRENT_PROJECT_VERSION;
o projeto declara apenas criptografia isenta para evitar repetir o questionário.

## Aceite externo pendente

- Free: 13ª captura e 9ª busca → Pro; arquivo A/V → Max antes de preparar.
- Pro: 101ª captura → Max; restore com mesmo UID em Mac/iPhone.
- Max: vídeo curto contabiliza duração; 40 min/mês.
- Cancelamento, expiração, reembolso e Max→Pro: ler vídeo antigo, bloquear novo.
- Acervo acima do teto: leitura/exclusão liberadas e upload bloqueado.
- Verificar upgrade sem segunda assinatura, restore entre aparelhos e entre UIDs.
- Confirmar preços, trial elegível/não elegível, latência compra/restore (<15 s
  após interação com a loja em condições normais), falha sem conceder acesso.
- VoiceOver e screenshots reais nos dois apps; teste de abertura da extensão
  para o app em iPhone físico (o host pode recusar `NSExtensionContext.open`).
- Completar GAZ-116 antes de anunciar Spaces como disponível no beta pago.
- Capturar evidências Apple/RevenueCat e vinculá-las à issue antes de marcá-la Done.

## Referências

- [Produtos e grupos Apple via RevenueCat](https://www.revenuecat.com/docs/getting-started/entitlements/ios-products)
- [Packages e offerings](https://www.revenuecat.com/docs/offerings/overview)
- [Elegibilidade das ofertas introdutórias](https://developer.apple.com/help/app-store-connect/manage-subscriptions/set-up-introductory-offers-for-auto-renewable-subscriptions)
- [Compra universal e bundle ID](https://developer.apple.com/help/glossary/universal-purchase)
- [Restore e identidade](https://www.revenuecat.com/docs/projects/restore-behavior)

## Inconsistência de margem no anual — decisão do autor antes da venda

A margem ≥40% descrita na issue é calculada com **receita mensal**. Com os preços
anuais e as mesmas cotas renovadas mensalmente, o pior mix informado pela própria
issue produz estes valores (Apple 30%, sem CAC):

| Plano anual | Receita líquida por mês | COGS/mês no pior mix da issue | Margem |
|---|---:|---:|---:|
| Pro US$59,99/ano | US$3,4994 | US$2,63 | 24,84% |
| Max US$159,99/ano | US$9,3328 | US$8,08 | 13,42% |

Fórmula: `1 - COGS / (preço_anual × 0,70 / 12)`. Para manter 40%, COGS teria
que ser ≤US$2,0997/mês (Pro) e ≤US$5,5997/mês (Max), ou o preço/cota precisaria
ser revisto pelo autor. A implementação não altera os números solicitados. Essa
conta precisa de decisão antes de vender os anuais; os cadastros são rascunhos.
Não assumir que a
margem do mensal se aplica a eles.
