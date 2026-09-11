# Queep — ambiente de desenvolvimento

| Recurso | Configuração |
| --- | --- |
| Projeto | `queep-dev-hstlx` · número `721281571441` |
| Conta de operação | `gazzoni@hstlx.com` |
| Organização herdada | `rudimentar.com` (`99549870261`) |
| Faturamento autorizado | Conta de Faturamento \| Google Tags |
| Firestore | Standard, `(default)`, `southamerica-east1`, proteção contra exclusão |
| Storage | `queep-dev-hstlx.firebasestorage.app`, São Paulo, acesso público bloqueado |
| Auth | Email/senha; proteção contra enumeração de emails; Identity Platform |
| API | `https://queep-api-721281571441.southamerica-east1.run.app` |
| Cloud Run | 0–2 instâncias, 1 CPU, 512 MiB, concorrência 20, timeout 30 s |

API executa com `queep-api@queep-dev-hstlx.iam.gserviceaccount.com`: somente
`roles/datastore.user` e `roles/firebaseauth.viewer`. Build usa identidade separada
`queep-build`, com `roles/run.builder`. Nenhuma chave de service account foi criada.

Regras publicadas negam acesso direto dos clientes a Firestore/Storage. Referências
passam pela API, que verifica o token Firebase, revogação e proprietário.
Storage está provisionado, mas upload de mídia ainda não foi implementado.
Escala mínima zero reduz ociosidade; máximo de instâncias não é teto de gastos.

## Acesso dos apps

Exceção `run.managed.requireInvokerIam=false` aplicada **somente em
`projects/721281571441`**, com autorização explícita do usuário. A organização
continua com `enforce: true`; seus demais projetos não receberam exceção.
Cloud Run está com `invoker-iam-disabled: true`, permitindo acesso à borda pelos
apps; a API continua exigindo Firebase para cada endpoint de referências.
Esse é o [modelo de acesso documentado pelo Cloud Run](https://cloud.google.com/run/docs/authenticating/public).

Para aplicar a exceção, foi usada uma permissão temporária de administração de
políticas, condicionada a uma tag exclusiva do Queep e expiração em 30 minutos.
A permissão, a associação da tag, o valor e a chave da tag foram removidos após
a aplicação. A conta mantém apenas a função administrativa anterior na organização.
O arquivo `allow-firebase-invocation.dev.yaml` registra a exceção vigente.

## Publicar e verificar

`bash infra/deploy-dev.sh --public` atualiza a API acessível aos apps;
`--private` restaura a exigência IAM na borda. O script fixa projeto,
conta, região e identidades; não depende dos defaults da máquina. `.gcloudignore`
envia apenas Dockerfile, dependências e código da API.

```sh
uv run python scripts/smoke-cloud.py \
  --api-url https://queep-api-721281571441.southamerica-east1.run.app \
  --firebase-plist ../queep-apple/Config/Firebase/Development/iOS/GoogleService-Info.plist
```

O teste padrão usa apenas Firebase, assim como os apps. `--via-iam` é opcional
para diagnosticar uma implantação privada como desenvolvedor autorizado.
O teste cria duas contas descartáveis, valida operações e isolamento e
remove contas/conteúdo no final; fica apenas o tombstone contra reenvio tardio.
Usar `/health` no Cloud Run; `/healthz` é reservado pela plataforma.

**Validado em 10/09/2026 (11/09 UTC):** revisão `queep-api-00002-d5g`; smoke aprovado
sem IAM (duas contas descartáveis, duas sessões, reenvio, isolamento, bloqueio de
leitura direta, edição/conflitos e exclusão). Também validado pela interface nativa:
nota criada no Mac → aberta e editada no iPhone 17 simulado → alteração recuperada
no Mac; revisão 2 conferida pela API. Sessões encerradas; contas temporárias e conteúdo
removidos. Base local: 19 testes da API, 2 Swift e 1 teste de interface iOS aprovados.
