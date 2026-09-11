# Escolha de IA do Queep

**Resultado inicial:** [decisão e evidências](RESULTADO.md). Usar Gemini 2.5 Flash-Lite
na interpretação, Gemini Embedding 2 e Gemini 3.1 Flash-Lite na conferência dos
candidatos da busca. Configuração de desenvolvimento; a engine ainda será integrada.

Direção aprovada: OpenRouter, modelo de interpretação configurável e embedding estável.
Comparar o custo para atingir a qualidade exigida. Nenhuma troca automática pelo modelo
mais barato do catálogo; mudanças passam por esta avaliação.

| Papel | Candidato | Entrada / saída por milhão de tokens (USD) |
| --- | --- | --- |
| Interpretação inicial | Gemini 2.5 Flash-Lite | 0,10 / 0,40 |
| Comparação | Qwen 3.5 Flash (02-23) | 0,065 / 0,26 |
| Comparação | Qwen 3.8 Flash | 0,15 / 0,47 |
| Embedding compartilhado | Gemini Embedding 2, 768 dimensões | 0,20 / — (texto) |
| Conferência da busca | Gemini 3.1 Flash-Lite | 0,25 / 1,50 |

Preços consultados em 11/09/2026; não incluem taxa de compra de créditos, mídia,
Firebase, extração ou tráfego. Cada chamada impõe o teto de preço configurado e
registra o custo retornado, tokens, latência, modelo, provedor e falhas.

## Comparação

- 34 referências sintéticas: notas variadas, dois links com conteúdo indisponível e
  quatro imagens gráficas. 32 consultas PT/EN: 12 de calibração, 20 de teste.
- Mesmo prompt/contrato e mesmo embedding para os três intérpretes. Consultas e
  respostas esperadas nunca são enviadas ao intérprete. Texto original acompanha a análise.
- Baseline `raw_text`: embedding do texto recebido, sem interpretação. Imagens sem
  legenda não têm informação visual nesse baseline; comparar resultados por formato.
- Ajustar o limiar só na calibração; medir no teste: top 5 de item conhecido (meta ≥90%),
  precisão de conjuntos (≥80%), recall e falsos positivos nas consultas sem resposta.
- Falhas de interpretação não somem do denominador. O relatório inclui resultados por
  consulta/formato, análises para revisão e custo desta bateria por resgate conhecido.
  Esse custo depende desta distribuição de consultas; não é projeção de custo por usuário.

Este acervo é pequeno e autorado: sucesso aqui não comprova desempenho geral. Após
escolher o candidato inicial, testar novas referências/consultas sem ajustar por elas,
incluir fontes acessíveis e revisar fatos inventados, limitações e aparência. Não há
avaliação de áudio/vídeo, fotos reais ou captura de páginas nesta rodada.

## Rodar

```sh
uv run python -m evaluation.run             # prévia, sem rede ou cobrança
python3 scripts/configure-openrouter.py    # chave em entrada oculta; fora do Git
uv run --env-file .env.evaluation python -m evaluation.run --live
```

Para conferir os 10 candidatos mais próximos de cada consulta usando a rodada salva:

```sh
uv run --env-file .env.evaluation python -m evaluation.verify \
  --run evaluation/runs/ID_DA_RODADA --verifier google/gemini-3.1-flash-lite --live
```

Essa etapa reaproveita as análises/vetores e usa uma reserva local de até US$ 0,25
por execução, com as mesmas limitações do controle descrito abaixo. Compara 20 consultas já vistas (reteste diagnóstico) e 10 consultas de
confirmação adicionais. O modelo escolhe IDs do acervo a partir da consulta e fonte;
não recebe respostas esperadas. Resultados sem suporte devem ficar vazios.

Use chave exclusiva com limite no OpenRouter. `--budget-usd` reduz o limite local
(máximo US$ 1 por execução); `--model ID` avalia só um candidato aprovado.
O controle local reserva US$ 0,003 antes de cada chamada, inclusive falhas, sem retry
automático. É uma margem conservadora, não garantia da fatura; o limite da chave é
a proteção externa. Custo superior à reserva interrompe novas chamadas. Custo não
reportado é marcado como desconhecido, nunca considerado gratuito.

Resultados e checkpoints ficam em `runs/`, ignorados pelo Git. O manifesto registra
hashes do acervo, prompt e imagens, além de modelos/provedores/configuração. Não há
acesso ao Firebase nem à biblioteca pessoal. Não execute chamadas live na CI.

## Privacidade e integração

Provedores explícitos, sem fallback; `data_collection=deny`, `zdr=true` e suporte
obrigatório aos parâmetros. Se não houver rota compatível, registrar indisponibilidade
sem relaxar as restrições. Configurar também a conta OpenRouter sem armazenamento
optativo de prompts/respostas ou compartilhamento para melhoria de modelos.

Na verificação inicial, Gemini Embedding 2 via Vertex aceitou entradas individuais;
lotes direcionaram a AI Studio, sem rota ZDR disponível. Por isso esta avaliação
envia um documento por chamada. O nome nativo `gemini-embedding-2` retornado por Vertex
é um alias explícito aceito; outro modelo/dimensão é rejeitado.

A engine de produção ainda será integrada ao worker. Reprocessar apenas conteúdo
alterado; limitar tamanho, saída e tentativas; guardar versão/modelo da análise;
preservar autorização, revisão e exclusões. Troca de espaço vetorial exige reindexar.

Fontes: [Gemini Flash-Lite](https://openrouter.ai/google/gemini-2.5-flash-lite),
[Qwen 3.5 Flash](https://openrouter.ai/qwen/qwen3.5-flash-02-23),
[Qwen 3.8 Flash](https://openrouter.ai/qwen/qwen3.8-flash),
[Embedding 2](https://openrouter.ai/google/gemini-embedding-2),
[roteamento/privacidade](https://openrouter.ai/docs/guides/routing/provider-selection),
[preços da plataforma](https://openrouter.ai/pricing),
[espaços vetoriais Gemini](https://ai.google.dev/gemini-api/docs/embeddings).

[Gemini 3.1 Flash-Lite](https://openrouter.ai/google/gemini-3.1-flash-lite) foi acrescentado
como verificador após falhas do 2.5 em condições/negações e consultas sem resposta.
