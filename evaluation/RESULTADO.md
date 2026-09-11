# IA do Queep — decisão inicial

11/09/2026 · Teste real via OpenRouter, com dados sintéticos. **Base escolhida para
desenvolver a engine:** Gemini 2.5 Flash-Lite interpreta ao salvar; Gemini Embedding 2
gera os vetores; Gemini 3.1 Flash-Lite confere os 10 candidatos encontrados na busca.
Ainda não integrado aos apps nem aprovado para lançamento.

## Evidência

34 referências (28 notas, 2 links parciais, 4 imagens gráficas). 12 consultas calibraram
o limiar; 20 testaram a busca inicial. Outras 10 consultas foram acrescentadas para
conferir a seleção de resultados. O mesmo acervo e prompt foram usados entre modelos.

| Método | Itens conhecidos no top 5 exibido | Precisão de conjuntos | Recall de conjuntos | Consultas vazias com falso positivo |
| --- | ---: | ---: | ---: | ---: |
| Embedding do texto, sem interpretação — 20 consultas | 64,3% | 50,0% | 66,7% | 0/3 |
| Interpretação 2.5 + embedding + limiar — 20 consultas | 85,7% | 72,2% | 83,3% | 0/3 |
| Conferência 2.5 — reteste das 20 | 100% | 77,8% | 100% | 2/3 |
| Conferência 3.1 — reteste das 20 | 100% | 100% | 100% | 0/3 |
| Conferência 3.1 — 10 consultas adicionais | 100% | 100% | 100% | 0/2 |

A seleção com 3.1 separou jogo solo de cooperativo, roupa branca de fundo branco e
software de clínicas de software para lojas. A consulta de uma tela ainda trouxe
uma nota de CRM além da imagem esperada; as métricas acima não significam ausência
de qualquer resultado secundário irrelevante.

Qwen 3.5 e 3.8 via Alibaba não tinham rota que atendesse à política ZDR. A tentativa
de 3.8 via Makora recebeu HTTP 429. **Qualidade desses modelos não foi avaliada**;
não há evidência para dizer que são melhores ou piores. Nenhuma restrição de
privacidade foi relaxada para completar a comparação.

## Custo observado

- 34 interpretações com 2.5: **US$ 0,005071**.
- 30 conferências de busca com 3.1: **US$ 0,007397**, aproximadamente **US$ 0,000247 por busca**.
- Rodadas principais completas, incluindo comparações/embeddings: **US$ 0,01669**
  reportados nas respostas. Diagnósticos de conexão adicionais não entram nesse total.

São custos desta amostra curta, sem extração, armazenamento, tráfego ou taxas de
compra de créditos. Acervos maiores, entradas longas, vídeo e novas tentativas mudam
o custo. A escolha evita usar o modelo mais caro para interpretar toda referência.

## Limites e próxima implementação

- A amostra é pequena e autorada. O reteste já havia sido visto no diagnóstico;
  nenhuma dessas porcentagens comprova desempenho geral. A próxima bateria deve
  trazer novos conteúdos e consultas, sem ajustar o sistema com suas respostas.
- A revisão das 34 interpretações encontrou inferências sem suporte: o 2.5 inventou
  documentação em um repositório inacessível e crescimento na história da startup.
  Também há ruído em entidades/aparência. A engine deve preservar fonte, reduzir
  inferências e tratar links sem conteúdo apenas pelo material disponível.
- A conferência usa o texto original para notas/links e descrição para imagens.
  Isso reduz dependência de resumos especulativos; ainda falta ampliar os testes
  de aparência, fotos reais, áudio/vídeo e extração de fontes externas.
- Implementar worker por revisão, segredos no backend, custo/latência observáveis,
  testes de exclusão/reprocessamento e busca com resultados rastreáveis à fonte.

Evidência compacta versionada em [results/2026-09-11.json](results/2026-09-11.json).
Detalhes locais em `runs/20260911T030025379248Z/`, incluindo análises, rankings,
respostas selecionadas, custos e configurações das duas rodadas de conferência.

Validação do código: lint e 50 testes aprovados na execução final. Uma execução
anterior teve timeout de lock no teste de concorrência do emulador Firestore; o
reteste isolado e a suíte completa seguinte passaram, sem alteração no código da API.
