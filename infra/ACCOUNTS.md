# Exclusão e recuperação de conta

Política aprovada e ativa no ambiente de desenvolvimento: **30 dias para recuperar
uma conta**, com opção de iniciar a exclusão definitiva imediatamente.

## Experiência

- **Conta → Excluir conta:** confirma a senha. Por padrão, agenda a exclusão para
  30 dias depois e bloqueia biblioteca, capturas, busca e novos processamentos.
- Login e arquivos permanecem durante esse prazo. Ao entrar novamente, a pessoa
  vê a data exata e **Cancelar exclusão e recuperar conta**. Entrar não cancela nada.
- Recuperar exige senha; notas, imagens, links e análises voltam à mesma conta.
  Trabalho de IA pendente retoma pela fila. Pedidos antigos não reagendam a exclusão.
- **Excluir definitivamente agora** dispensa a recuperação. A limpeza começa em
  segundo plano. A conta e seus conteúdos não podem ser recuperados pelo app.
- Vencidos os 30 dias, a limpeza automática começa pelo job privado, a cada cinco
  minutos. Cancelamento e início da limpeza disputam a mesma transação; uma conta
  recuperada não pode ser apagada por um job atrasado.
- A assinatura Apple é gerenciada separadamente, com aviso e link nas duas telas.

## Contrato e proteção

| API | Comportamento |
| --- | --- |
| `GET /v1/account` | Estado, versão, data de exclusão e possibilidade de recuperar; acessível à sessão autenticada suspensa. |
| `POST /v1/account/deletion` | `{operation_id: UUID, version: N, immediate: false}`; agendamento por padrão. `immediate: true` inicia exclusão definitiva. |
| `POST /v1/account/deletion/cancel` | `{operation_id: UUID, version: N}`; recuperação explícita antes do prazo. |

Mutações exigem autenticação nos últimos cinco minutos. UID vem exclusivamente do
Firebase. Versão e identificador tornam reenvios idempotentes e rejeitam intenções
antigas. Estados: `active → scheduled → deleting → deleted`; recuperação só de
`scheduled` antes do prazo. Pedidos antigos `pending` continuam irreversíveis.

API e worker verificam a barreira em transações. Workers também conferem a versão
da conta, impedindo publicação de uma execução anterior à suspensão/recuperação.
Os apps limpam a biblioteca suspensa e consultam o estado mesmo com um detalhe aberto.

A limpeza remove Auth, referências/análises/vetores, jobs, uso, imagens/miniaturas e
cache de assinatura. Lotes limitados e pedidos persistidos permitem retomar falhas.
Reservas ficam pelo menos dez minutos após o pedido para recolher uploads em curso;
esse prazo já passou quando uma exclusão agendada vence normalmente.

## RevenueCat e retenção

**RevenueCat adiado por decisão do usuário.** A exclusão do Queep funciona sem a
chave administrativa. Ao concluir, o trabalho do fornecedor fica separado em
`provider_deletions`, com UID e projeto para futura limpeza; nenhuma chamada de
exclusão RevenueCat ocorre nesta versão. O helper de configuração permanece para
uso futuro. Não apresentar isso como eliminação já concluída em todos os fornecedores.

O registro final do Queep retém hash do UID, versão/ID da operação, estado e data,
sem conteúdos, para impedir recriação por trabalho antigo. O registro de recuperação
retém versão/ID/data. Recibos técnicos sem conteúdo expiram por TTL de 30 dias.

O Storage de desenvolvimento mantém **soft delete por sete dias após a remoção**.
Esse prazo de infraestrutura é separado dos 30 dias de recuperação no app; a
política do bucket não foi alterada. Privacidade final, confirmação da conclusão,
retenção de fornecedores/logs e obrigações da loja seguem na preparação para beta.

## Operação e evidências — 11/09/2026

API `queep-api-00017-k8m`, worker `queep-worker-00006-7l9` e manutenção atualizados.
`QUEEP_ACCOUNT_DELETION_ENABLED=true`; faturamento continua em `development`.
Sequência: `deploy-dev.sh --public`, `deploy-ai-dev.sh`,
`enable-account-deletion-dev.sh`. Este último confere a imagem API/worker, prepara
permissão mínima de Auth, atualiza/testa a manutenção e então ativa a API.

146 testes backend; três testes Swift; iPhone: cancelar diálogo, senha errada,
exclusão imediata, agendamento/reabertura/recuperação. Mac: bloqueio de outra sessão
com tela aberta e recuperação. Todos aprovados; builds cloud Mac/iPhone aprovados.

`scripts/smoke-account-recovery-cloud.py` passou com contas novas descartáveis:
30 dias, bloqueio, Auth/dados preservados, restauração da mesma imagem, prazo vencido,
exclusão imediata, isolamento e ausência final em Auth/Firestore/Storage. O relógio
foi avançado **somente nos registros de teste**, sem mudar a política de produção.
A primeira rodada evidenciou uma disputa na preparação do relógio do teste com a
retentativa inicial; o teste agora espera essa passagem terminar. O backend estava
retomando corretamente. Execução final: `queep-image-cleanup-mmxd5`.

Logs: `/tmp/queep-recovery-api-tests.log`, `/tmp/queep-recovery-race-tests.log`,
`/tmp/queep-recovery-ios-tests.log`, `/tmp/queep-recovery-mac-tests.log`,
`/tmp/queep-recovery-cloud-smoke-final.log`. Relatório de fixtures em
`.local/account-recovery-smoke.json`, ignorado por Git/builds.

Referências: [Apple](https://developer.apple.com/support/offering-account-deletion-in-your-app/),
[Firebase Auth](https://firebase.google.com/docs/auth/admin/manage-users).
