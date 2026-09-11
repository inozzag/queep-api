# Leitura de links

Salvar continua imediato. O worker lê páginas públicas e READMEs do GitHub, interpreta
o texto e publica fonte/análise/vetor na mesma revisão. O campo opcional `source`
contém título, trecho, endereço final, data, estado e limitações. Título/contexto do
usuário são preservados. Editar descarta a extração anterior; excluir remove seu conteúdo.

## Cobertura

- HTML, texto e Markdown públicos. GitHub: README renderizado, sem baixar o repositório.
- Até 12.000 bytes de texto armazenados; até 8.000 chegam à interpretação. Texto e
  resumo entram no vetor; a conferência usa resumo e trecho relacionado à consulta.
- Cobertura sempre parcial: só texto acessível, sem imagens da página, vídeo ou áudio.
- Login, bloqueios, JavaScript obrigatório, formatos não suportados e falhas de acesso
  preservam o link/contexto; ficam pesquisáveis por esses dados, sem inventar o conteúdo.
- Referências antigas são relidas quando editadas e salvas. Não há recaptura automática,
  monitoramento de alterações, execução de JavaScript ou acesso a sessões do navegador.

## Limites operacionais

HTTP(S), portas padrão, até 4 redirecionamentos por tentativa e 20 s de tempo total.
Falhas transitórias de rede/servidor têm uma nova tentativa dentro desse mesmo prazo.
Todos os saltos
passam por validação DNS/IP; a conexão usa o IP público validado, com Host e verificação
TLS do domínio original. Endereços internos/metadata, credenciais na URL e downgrade
HTTPS são bloqueados. Sem cookies, tokens de usuário, proxies do ambiente ou recursos
secundários. Gzip é limitado a 2 MiB tanto na transferência quanto descomprimido.

O processamento mantém o limite de 3 tentativas; uma falha posterior de IA pode reler
a página. Falha apenas de leitura produz análise parcial, sem repetir chamadas de IA
para tentar adivinhar a fonte. Mantidos modelos, orçamento e reservas em [AI.md](AI.md).

Conteúdo externo é dado não confiável nos prompts e exibido como texto no app.
A extração depende do HTML recebido; páginas dinâmicas e mudanças de layout podem
exigir ajustes. O limite de 10 candidatos da busca permanece.

## Verificação

Testes cobrem endereços internos, DNS misto/rebinding, redirecionamentos, Host/SNI,
timeout, tamanho/descompressão, extração de README, fonte indisponível, orçamento de
entrada, persistência e edição/exclusão durante a leitura. Testes com páginas públicas
usam uma conta descartável, sem fotos pessoais.

- Nuvem: `uv run python scripts/smoke-sources-cloud.py`.
- Local: API com `QUEEP_AI_ENABLED=true`, `.env.example` e `.env.evaluation`; executar
  o mesmo script com `--local` e os mesmos arquivos de ambiente. Usa IA real com
  orçamento; só dirige os jobs criados pelo próprio teste.

Fundamentos: [Host/SNI do HTTP Core](https://www.encode.io/httpcore/extensions/#sni_hostname)
e [Beautiful Soup](https://www.crummy.com/software/BeautifulSoup/bs4/doc/).

## Estado em 11/09/2026

- Implementação local validada com IA real: 3 links públicos/indisponíveis e 5 consultas
  PT/EN; regressão com 6 referências sintéticas e 7 consultas, isolamento e exclusão.
  Resultados em [fontes](../evaluation/results/local-sources-smoke.json) e
  [regressão](../evaluation/results/local-ai-smoke.json).
- Uma consulta inicialmente confundiu assunto principal e linguagem usada por uma
  biblioteca; a regra de conferência foi ajustada e ambas as rodadas finais passaram.
  Houve falha transitória de acesso a Python.org; o fallback preservou o link.
- Publicado em desenvolvimento: API `queep-api-00009-kl4`, worker privado
  `queep-worker-00003-b47` e job de limpeza com o mesmo digest da API.
- Na nuvem, as 5 buscas de [fontes](../evaluation/results/cloud-sources-smoke.json)
  e as 7 de [regressão](../evaluation/results/cloud-ai-smoke.json) passaram.
  A regressão também verificou worker privado, isolamento entre contas e exclusão.
- O teste iOS de busca/abertura/expansão passou em Debug local e CloudDevelopment após
  a correção do identificador. Conferidos visualmente o iPhone e o detalhe no Mac;
  a mesma referência pública foi encontrada pelo assunto e teve seu texto expandido.
  O contexto opcional de links ocupa menos espaço, mantendo a área maior das notas.
- Referências e contas descartáveis dos smokes removidas. Fixture nativa removida
  por identificador, preservando as quatro referências existentes e a sessão do usuário.
