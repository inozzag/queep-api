# Exclusão de conta

Mac/iPhone: **Conta → Excluir conta**, confirmação de senha e aviso de ação
irreversível. Cancelar ou errar a senha preserva a conta. Excluir não exige
assinatura ativa e não cancela a assinatura Apple; a tela oferece o link de gestão.

## Comportamento

- `POST /v1/account/deletion` exige token Firebase válido e autenticação nos últimos
  cinco minutos. Usa somente o UID verificado; o cliente não escolhe outra conta.
- `202 / {"status":"deleting"}` confirma o pedido persistido. O acesso pela API é
  bloqueado imediatamente, inclusive para outras sessões, uploads e trabalhos de IA.
- A limpeza remove usuário Auth, referências/análises/vetores, jobs, uso por conta,
  imagens/miniaturas e cache de assinatura. Um job privado retoma falhas a cada cinco
  minutos, em lotes limitados, mesmo se o app fechar ou a API reiniciar.
- Reservas de imagens permanecem dez minutos para recolher gravações já iniciadas.
  Depois, a API RevenueCat V2 exclui o customer. DELETE 200 é apenas aceitação;
  somente GET 404 confirma conclusão. Falhas mantêm o trabalho pendente.
- Reenvios são idempotentes; eventos atrasados não recriam acesso ou conteúdo.
  O registro final retém só hash do UID, estado e data, para impedir recriação por
  trabalhos antigos. O UID legível sai desse registro ao concluir.
- Nos apps ativos, respostas 401 encerram a sessão e limpam biblioteca/detalhes,
  inclusive com uma tela aberta. Aparelhos sem conexão verificam ao voltar a usar.

## Retenção a declarar antes do beta

Em `queep-dev-hstlx`, o Storage tem soft delete de **sete dias**, conferido em
11/09/2026. Arquivos deixam de ser servidos, mas permanecem recuperáveis pela
infraestrutura durante esse prazo. A política do bucket não foi alterada.
Recibos técnicos de cobrança/IA expiram por TTL de 30 dias, sem conteúdo pessoal;
o hash de bloqueio da conta permanece. Privacidade final também precisa descrever
retenção dos provedores de IA, logs e obrigações da loja. Não prometer eliminação
física instantânea de todas as cópias nem cancelamento de cobrança pela exclusão.

## Ativação cloud

Código implantado na API `queep-api-00014-q42` e worker `queep-worker-00005-dn8`;
job `queep-image-cleanup` atualizado e agendado a cada cinco minutos.
Execução manual `queep-image-cleanup-9bhmx` concluída com sucesso.
**Novos pedidos cloud ainda desativados**, aguardando a credencial de exclusão do
RevenueCat e o teste real do ciclo completo. As operações existentes continuam liberadas.

1. Projeto RevenueCat Queep: ID de painel `f6e36522`; ID REST V2 **`projf6e36522`**.
   Criar secret V2 somente com Customers Read & Write e executar
   `python3 scripts/configure-revenuecat-deletion.py`. Chave em `.env.accounts`, 0600,
   ignorada por Git/Docker/build; nunca vai para o Swift.
2. Após atualizar API/worker, executar
   `uv run python infra/enable-account-deletion-dev.py`. O script valida leitura,
   criação e exclusão de um customer sintético, exige GET 404, configura Secret
   Manager, atualiza/testa manutenção e só então habilita o endpoint.
3. Validar na nuvem com conta descartável: nota/imagem, duas sessões, pedido 202,
   bloqueio 401, outra conta intacta e limpeza final de Auth/Firestore/Storage/RevenueCat.
   Aguardar o processamento; não reduzir a janela de dez minutos em produção.

Permissão Google adicional: papel `queepAccountDeletion`, somente
`firebaseauth.users.delete`, vinculado à identidade `queep-api`. Essa identidade
também executa manutenção. O segredo de exclusão não é concedido ao worker de IA.
`QUEEP_ACCOUNT_DELETION_ENABLED=true` em cloud exige projeto/segredo RevenueCat;
sem essa configuração a aplicação recusa habilitar o fluxo.

## Verificação desta entrega

137 testes de backend aprovados: autorização recente, outra conta, barreira de
escrita, worker/reenvio atrasado, falha de Storage/provedor, retomada e apagamento
assíncrono. Três testes do núcleo Swift aprovados. Testes de interface com contas
descartáveis emuladas: iPhone cancelar/senha errada/excluir, Mac encerrar uma sessão
com a tela de exclusão aberta após pedido por outra sessão.

Builds finais CloudDevelopment do Mac/iPhone aprovados, incluindo Share Extension.
Regressão cloud de login, notas, imagens, sessões e isolamento aprovada; a conta
existente e suas quatro referências foram conferidas e preservadas. O teste
com a API real RevenueCat e a exclusão cloud completa ainda estão pendentes.
Evidências locais: `/tmp/queep-account-all-tests.log`,
`/tmp/queep-account-ios-final.log`, `/tmp/queep-account-mac-test.log` e
`/tmp/queep-account-cloud-regression.log`.

Referências: [Apple](https://developer.apple.com/support/offering-account-deletion-in-your-app/),
[Firebase Auth](https://firebase.google.com/docs/auth/admin/manage-users),
[RevenueCat V2](https://www.revenuecat.com/docs/api-v2/customer).
