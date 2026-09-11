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

## Acesso pendente

A organização exige `run.managed.requireInvokerIam` e restringe bindings `allUsers`.
A API foi implantada **privada**. Apps não podem usá-la até autorizar e aplicar
a exceção somente neste projeto. `allow-firebase-invocation.dev.yaml` foi preparado,
mas não aplicado. Não alterar a política da organização.

Após autorização explícita, aplicar o arquivo pelo `gcloud org-policies set-policy`,
com `--project=queep-dev-hstlx --account=gazzoni@hstlx.com`, e publicar com
`bash infra/deploy-dev.sh --public`. A borda recebe requisições dos apps e a API
continua exigindo Firebase para cada endpoint de referências.
Esse é o [modelo de acesso documentado pelo Cloud Run](https://cloud.google.com/run/docs/authenticating/public).

## Publicar e verificar

`bash infra/deploy-dev.sh --private` publica mantendo IAM. O script fixa projeto,
conta, região e identidades; não depende dos defaults da máquina. `.gcloudignore`
envia apenas Dockerfile, dependências e código da API.

```sh
uv run python scripts/smoke-cloud.py \
  --api-url https://queep-api-721281571441.southamerica-east1.run.app \
  --firebase-plist ../queep-apple/Config/Firebase/Development/iOS/GoogleService-Info.plist \
  --via-iam
```

`--via-iam` permite testar a implantação privada como desenvolvedor autorizado;
isso não habilita acesso dos apps. Depois da liberação pública, repetir sem essa
opção. O teste cria duas contas descartáveis, valida operações e isolamento e
remove contas/conteúdo no final; fica apenas o tombstone contra reenvio tardio.
Usar `/health` no Cloud Run; `/healthz` é reservado pela plataforma.

**Validado em 10/09/2026:** build/deploy da revisão `queep-api-00002-d5g`, smoke
com `--via-iam` aprovado (duas contas reais descartáveis, duas sessões, reenvio,
isolamento, bloqueio de leitura direta, edição/conflitos e exclusão), 19 testes
locais da API aprovados. As contas temporárias e o conteúdo foram removidos.
Esse resultado não substitui a futura validação de acesso diretamente pelos apps.
