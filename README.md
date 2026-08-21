# Automação de envio de GL

Ferramenta em Python para consolidar faturas (invoices) de importação e gerar
o resumo pronto para envio de GL: PO, item, incoterm e soma do valor total,
com cross-check automático contra a packing list.

Tem **duas formas de usar**: por linha de comando (`main.py`) ou por uma
**interface web local** (`app.py`, feita com Streamlit) — arrasta os
arquivos, clica em processar, baixa o Excel, sem precisar mexer no terminal
depois de iniciada.

**Testada e validada** com os arquivos de exemplo (`CNILX_911308975_BRELE.PDF`,
`CNILX_911310893_BRELE.PDF` + `BRELE260728SEA.xlsx`): o valor total somado
(USD 65.950,15) e o peso bruto (3.053,51 KGS) bateram exatamente com o
`GL_TESTE.xlsx` de referência.

## Interface web (recomendado para uso do dia a dia)

```bash
pip install streamlit
streamlit run app.py
```

Isso abre automaticamente uma aba no navegador (normalmente
`http://localhost:8501`). Lá dá para:

- arrastar uma ou várias faturas em PDF, e opcionalmente a packing list em Excel;
- escolher o motor de extração (`regex` / `claude` / `gemini` / `auto`) e,
  se precisar de IA, colar a chave de API num campo de senha (fica só na
  memória da sessão, não é salva em disco);
- preencher Importador/CNPJ/Modal/NCM (opcional);
- clicar em **Processar** e ver na tela: tabela com os dados extraídos de
  cada fatura, valor total somado, resultado do cross-check (✅/⚠️/❌ por
  item) e um botão para **baixar o Excel** consolidado.

Para parar o servidor, feche o terminal ou aperte `Ctrl+C`.

## O que ela faz

1. **Lê uma ou mais faturas em PDF** — funciona tanto com PDFs "de texto"
   (o mais comum, como nos exemplos ABB) quanto com faturas digitalizadas
   (escaneadas/foto). Se o PDF não tiver texto selecionável, o OCR
   (Tesseract) entra automaticamente, sem precisar avisar nada.
   Extrai: nº da fatura, data, **PO do cliente**, **incoterm**, moeda e
   **valor total**.

2. **Lê a packing list (Excel)** e localiza, para cada PO, o(s) **número(s)
   de item**, peso líquido, peso bruto e quantidade de volumes. É a packing
   list — não a fatura — que traz de forma confiável o número do item da PO
   (na fatura o "Ref line" interno do fornecedor costuma ser diferente do
   item real da PO).

3. **Soma o valor total** de quantas faturas você quiser incluir no mesmo
   lote (útil quando um GL cobre várias faturas de um mesmo embarque).

4. **Faz o cross-check** entre os documentos e avisa:
   - se a PO da fatura não aparece na packing list (DIVERGÊNCIA);
   - se as faturas do lote têm incoterms diferentes (DIVERGÊNCIA);
   - se as faturas do lote estão em moedas diferentes (DIVERGÊNCIA);
   - se algum campo não foi extraído / heurística falhou (AVISO), para você
     revisar manualmente aquele documento específico.

5. **Gera um Excel** (`GL_gerado.xlsx` por padrão) com 3 abas:
   - `GL`: o resumo consolidado, pronto para copiar/preencher o formulário
     de GL (segue o mesmo espírito do `GL_TESTE.xlsx` enviado como modelo).
   - `Validacao`: a lista de achados do cross-check (OK / DIVERGÊNCIA / AVISO),
     coloridos, para auditoria antes do envio.
   - `Itens_Fatura`: o detalhe de cada linha de item extraída de cada fatura,
     para conferência.

## Instalação

```bash
pip install pdfplumber openpyxl pdf2image pytesseract
```

Para o OCR funcionar (faturas digitalizadas) você também precisa do
Tesseract instalado no sistema:

```bash
# Ubuntu/Debian
sudo apt-get install tesseract-ocr tesseract-ocr-por poppler-utils

# Windows: instale o Tesseract (https://github.com/UB-Mannheim/tesseract/wiki)
# e o Poppler (https://github.com/oschwartz10612/poppler-windows), e garanta
# que ambos estejam no PATH.
```

Se você só trabalha com faturas em PDF "de texto" (não escaneadas), pode
pular a parte do Tesseract/Poppler — o script funciona igual, só o caminho
de OCR não vai estar disponível.

## Uso por linha de comando (`main.py`)

### Uma fatura, sem packing list

```bash
python main.py --invoices fatura1.pdf --output GL_fatura1.xlsx
```

### Várias faturas do mesmo embarque, somando o valor total, com cross-check

```bash
python main.py \
    --invoices fatura1.pdf fatura2.pdf fatura3.pdf \
    --packing-list packing_list.xlsx --pl-sheet PL \
    --importador "ABB ELETRIFICACAO - SOROCABA" \
    --cnpj "33.449.988/0001-20" \
    --modal "MARITIMO" \
    --ncm "8536.20.00" \
    --output GL_lote.xlsx
```

O script imprime no console um resumo de cada fatura lida, o resultado do
cross-check e o valor total somado, além de salvar o Excel completo.

Se houver **divergência** o script encerra com código de saída 2 (útil para
travar um pipeline/CI automático até a divergência ser resolvida), mas o
Excel é gerado normalmente para você revisar a aba `Validacao`.

### Forçar OCR

Se uma fatura tiver texto selecionável ruim/incompleto (ex.: PDF gerado a
partir de scan com uma camada de texto de baixa qualidade), force o OCR:

```bash
python main.py --invoices fatura_ruim.pdf --force-ocr
```

## Estrutura do código

```
invoice_extractor.py      # lê o(s) PDF(s) de fatura -> InvoiceData (regex multi-layout + fallback de OCR)
ai_extractor.py             # idem, via API da Anthropic (Claude) — robusto a qualquer layout
gemini_extractor.py         # idem, via API do Google Gemini — robusto a qualquer layout
openai_extractor.py         # idem, via qualquer API compatível com OpenAI (OpenAI/Azure/Ollama/OpenRouter/Groq)
engines.py                   # escolhe/roda entre os motores acima, com fallback automático entre provedores
pdf_splitter.py              # detecta PDFs em lote (várias faturas concatenadas) e separa
packing_list_extractor.py # lê a aba de packing list -> resumo por PO+Item
cross_check.py             # compara fatura x packing list e gera os "findings"
gl_report.py                # monta o Excel final (abas GL / Validacao / Itens_Fatura)
main.py                     # CLI que amarra tudo, com --engine/--ai-providers para escolher o(s) motor(es)
app.py                       # interface web (Streamlit) que usa os mesmos módulos acima
```

## Sobre o motor de extração: é IA ou é regex? E qual provedor de IA?

**Por padrão (`--engine regex`), NÃO é IA.** O `invoice_extractor.py` é
baseado em expressões regulares. Ele funciona muito bem — e de graça, sem
depender de API — **enquanto o layout for um dos já mapeados** (hoje: ABB
Xiamen e ABB S.p.A. Itália, incluindo variações de formato numérico
americano `1,234.56` e europeu `1.234,56`). Fora esses, ele falha.

Regex é rápido de mapear no início, mas fica cada vez mais trabalhoso de
manter conforme aparecem fornecedores novos — cada layout diferente exige
escrever (e depois manter) um novo conjunto de padrões. Por isso existem
**três motores baseados em IA**, que leem a fatura como um humano leria (a
imagem/PDF é enviada direto pro modelo) e devolvem os campos em JSON — sem
depender de layout nenhum, sem precisar mapear nada nem para fornecedor
novo:

- **`ai_extractor.py`** → API da **Anthropic (Claude)**
- **`gemini_extractor.py`** → API do **Google Gemini**
- **`openai_extractor.py`** → qualquer API **compatível com o padrão OpenAI**:
  OpenAI oficial, **Azure OpenAI** (o que roda por trás do Copilot — não
  existe uma "API do Copilot" pública para extração de documentos, mas se
  você tiver acesso a um deployment Azure OpenAI dá pra apontar pra lá),
  **Ollama rodando local na sua máquina** (grátis e sem limite de
  requisição — só o hardware do seu PC limita), e agregadores como
  **OpenRouter** (tem modelos com sufixo `:free`, zero custo) ou **Groq**.

Os três devolvem o mesmo formato de dados (`InvoiceData` / `LineItem`),
então `cross_check.py` e `gl_report.py` funcionam igual não importa qual
você usar.

### Não existe API de IA "grátis e sem limite nenhum"

Isso é estrutural — rodar um modelo grande consome processamento real, então
todo provedor limita de algum jeito (por requisição, por token, ou cobrando
por uso). A opção mais próxima de "grátis e sem limite" de verdade é rodar
um **modelo local via Ollama** (não depende de servidor externo, então não
tem limite de chamada — só o hardware). É mais lento e menos preciso que
Claude/GPT/Gemini em documentos complicados, mas é uma alternativa real.
Ferramentas como **n8n não substituem isso** — n8n é um orquenstrador de
fluxo (tipo Zapier), ainda precisaria chamar uma dessas APIs (ou o Ollama
local) por trás; ele ajuda a *disparar* o pipeline automaticamente (ex.: PDF
novo numa pasta → roda o script → manda o Excel por e-mail), não a remover
limite de IA nenhum.

### Rotação automática entre provedores (mitiga limite de requisição)

Em vez de travar quando um provedor bate no limite, o `--ai-providers`
define uma ordem de tentativa — se o primeiro falhar (limite de requisição,
erro transitório, chave não configurada), o próximo da lista é tentado
automaticamente, **por fatura**, sem precisar reiniciar nada:

```bash
python main.py --invoices fatura1.pdf fatura2.pdf \
    --engine auto --ai-providers "gemini,claude,openai" \
    --output GL_lote.xlsx
```

No `app.py` (interface web) a mesma ordem é configurável na barra lateral
(campo "Ordem dos provedores").

### Tabela de motores

| `--engine` | Como funciona | Quando usar |
|---|---|---|
| `regex` (padrão) | Só regex, sem custo de API | Fornecedores com layout já mapeado |
| `claude` (ou `ai`) | Anthropic, com fallback pros demais de `--ai-providers` se falhar | Fornecedor novo/desconhecido |
| `gemini` | Google Gemini, idem fallback | Idem |
| `openai` | Qualquer endpoint OpenAI-compatível (OpenAI/Azure/Ollama/OpenRouter/Groq), idem fallback | Idem — inclusive opção local/grátis via Ollama |
| `auto` | Tenta `regex`; se os campos essenciais não saírem, refaz **só aquela fatura** girando por `--ai-providers` | Lote misto de fornecedores conhecidos e novos — recomendado no dia a dia |

```bash
# Uma fatura de layout desconhecido, usando o Gemini
python main.py --invoices fatura_nova.pdf --engine gemini --output GL.xlsx

# Usando um modelo local via Ollama (grátis, sem limite de requisição)
export OPENAI_BASE_URL="http://localhost:11434/v1"
export OPENAI_API_KEY="ollama"     # qualquer valor não-vazio serve
python main.py --invoices fatura_nova.pdf --engine openai --ai-model llama3.2-vision --output GL.xlsx

# Lote misto: ABB (regex resolve) + fornecedor novo (gira pela lista de IA sozinho)
python main.py --invoices fatura_abb.pdf fatura_nova.pdf --engine auto --ai-providers "gemini,claude,openai" --output GL_lote.xlsx
```

Instalação e chave de API, dependendo do provedor escolhido:

```bash
# Claude (Anthropic)
pip install anthropic
export ANTHROPIC_API_KEY="sk-ant-..."

# Gemini (Google)
pip install google-genai
export GEMINI_API_KEY="..."     # gere em https://aistudio.google.com/apikey

# OpenAI / Azure OpenAI / OpenRouter / Groq (qualquer endpoint compatível)
pip install openai pdf2image
export OPENAI_API_KEY="sk-..."
export OPENAI_BASE_URL="..."    # deixe em branco para a OpenAI oficial

# Ollama local (grátis, sem limite de requisição — só o hardware limita)
#   1. instale o Ollama: https://ollama.com
#   2. baixe um modelo com visão: ollama pull llama3.2-vision  (ou qwen2.5vl, etc.)
#   3. rode o servidor: ollama serve
#   4. aponte o motor 'openai' pra ele:
export OPENAI_BASE_URL="http://localhost:11434/v1"
export OPENAI_API_KEY="ollama"
python main.py --invoices fatura.pdf --engine openai --ai-model llama3.2-vision --output GL.xlsx
```

Modelos padrão: `claude-sonnet-5` (Claude), `gemini-2.5-flash` (Gemini),
`gpt-4o-mini` (OpenAI-compatível). Dá pra trocar com `--ai-model
nome-do-modelo`.

Validei a lógica de parsing dos três módulos simulando a resposta do modelo
(mockada, sem gastar chamada de API real) com valores reais das faturas de
teste — o pipeline completo (cross-check + geração do Excel) rodou sem erro
nos três casos, incluindo os campos novos `po_line` e `ncm`. Não tenho como
testar as chamadas reais às APIs a partir daqui (não tenho nenhuma das três
chaves neste ambiente), então na primeira vez que rodar de verdade, vale
conferir a aba `Validacao`/`Itens_Fatura` do Excel gerado com atenção.

## PO Line e NCM (captura automática)

Duas melhorias na extração, tanto no motor `regex` quanto nos motores de IA:

- **`po_line`**: quando a fatura traz explicitamente o número da linha/item
  DENTRO DA PO do cliente (rótulos comuns: "PO Line", "PO Item"), esse
  número é capturado e usado como o item do GL — em vez do "Ref line"
  interno do fornecedor (que pode ser um número totalmente diferente do
  item real da PO). Se a fatura não tiver esse rótulo, o relatório cai de
  volta pro item da packing list, como antes.
- **`ncm`**: procura automaticamente por rótulos como "Customs Tariff No.",
  "NCM" ou "HS Code" perto de cada item, e preenche o campo NCM do
  relatório sozinho (o `--ncm` continua existindo, como valor manual que
  tem prioridade / complementa o que foi detectado).

Testado com o export de 34 faturas da ABB S.p.A. (que usa exatamente esses
rótulos): 34/34 faturas com PO Line e NCM capturados corretamente, contra
0/34 antes dessa mudança.

## Revisão da captura de valores (bug de formato numérico corrigido)

Foi identificado e corrigido um bug real no parser de números: faturas com
formato **europeu** (vírgula como separador decimal, ex.: `551,37` = 551.37)
estavam sendo lidas errado — o código antigo tratava a vírgula sempre como
separador de milhar, então `"551,37"` virava `55137` (100x maior). O parser
novo detecta automaticamente o formato (US `1,234.56` vs europeu
`1.234,56`) olhando qual separador aparece por último e quantos dígitos vêm
depois da vírgula, em vez de assumir sempre um formato só.

Também foram adicionados: múltiplos padrões de "valor total" tentados em
ordem (cobrindo tanto `Final amount incl. VAT USD 123.45` quanto `TOTALE -
TOTAL 123,45 EUR`, entre outros rótulos comuns), e um **fallback**: se
nenhum rótulo de total for reconhecido mas os itens foram, o total é
estimado somando os itens (marcado como `total_value_estimated=True` e
sinalizado como aviso na aba Validação, pra você conferir).

Com essas correções, o teste no lote de 34 faturas da ABB S.p.A. foi de
**0/34** para **34/34** faturas com valor total capturado corretamente.

## PDFs em lote (várias faturas num único arquivo)

Alguns exports de ERP (foi o caso testado com um export da ABB S.p.A. — 34
faturas diferentes concatenadas num PDF de 102 páginas) juntam várias
faturas em um único arquivo. Se isso for mandado como "uma fatura só" pro
motor de IA, o JSON de resposta fica gigante e a chamada à API trunca no
meio (erro de "Unterminated string" / JSON inválido) — e mesmo sem truncar,
o resultado estaria errado, porque só sairia uma fatura das várias que tem
no arquivo.

Por isso, tanto o `main.py` quanto o `app.py` agora **detectam e separam
automaticamente** PDFs em lote antes de extrair (usando `pdf_splitter.py`):
cada fatura detectada é tratada como um item separado do lote, e a soma do
valor total já considera todas elas. Não precisa fazer nada — a detecção é
automática (dá pra desligar com `--no-split` no `main.py`, se quiser tratar
o PDF inteiro como uma única fatura por algum motivo).

A detecção funciona procurando o número da fatura em cada página (com
alguns padrões comuns em PT/EN, incluindo o rótulo `INVOICE <número>` do
layout ABB S.p.A.) e também contadores de página tipo "Page 1 of N" /
"PAG. 1/ N" — quando um desses reinicia em 1, é sinal de início de nova
fatura. Testado com o export real de 34 faturas: os 34 grupos foram
detectados corretamente, cobrindo as 102 páginas sem sobra nem furo.

Se o seu fornecedor não usa nenhum desses padrões, o PDF é tratado como uma
fatura só (comportamento normal) — ajuste `INVOICE_NUMBER_PATTERNS` /
`PAGE_COUNTER_PATTERNS` no topo de `pdf_splitter.py` se precisar reconhecer
outro padrão.

### JSON truncado mesmo numa fatura só (CI&PL grande)

Mesmo sem ser um PDF em lote, uma fatura combinada com packing list (CI&PL)
com bastante item pode gerar uma resposta de IA grande o suficiente pra
estourar um limite de tokens de saída baixo demais. Isso já aconteceu com
`max_tokens=4000` no motor Claude; o limite dos três motores (Claude,
Gemini, OpenAI-compatível) foi aumentado para `16000`. Se ainda assim
truncar em faturas muito grandes, o aviso agora deixa isso explícito
("resposta do modelo parece ter sido CORTADA") em vez de só mostrar o erro
cru de JSON inválido — nesse caso, considere separar a fatura em partes
menores antes de extrair.

## Dados fixos do GL (Importador / CNPJ / Modal / Centro / Incoterm / NCM)

O bloco de cabeçalho do relatório (`GL`, linhas 2-7) tem seis campos, todos
editáveis mas com valor padrão pra agilizar o dia a dia:

| Campo | Padrão | Origem |
|---|---|---|
| Importador | `ABB ELETRIFICAÇÃO LTDA` | fixo |
| CNPJ | `33.449.988/0001-20` | fixo |
| Modal | `MARITIMO` | fixo |
| Centro | (vazio) | manual — sem padrão definido |
| Incoterm | detectado automaticamente nas faturas | manual sobrescreve, se preenchido |
| NCM | detectado automaticamente (HS code das faturas) | manual sobrescreve/complementa, se preenchido |

No `app.py` esses campos ficam na barra lateral, em "Dados do GL". No
`main.py`, use `--importador`, `--cnpj`, `--modal`, `--centro`,
`--incoterm` e `--ncm` (todos opcionais — os três primeiros já vêm com o
padrão acima se você não passar nada).

## Adaptando o motor `regex` para o layout de um fornecedor específico

As faturas de exemplo seguem o layout padrão ABB (rótulos em inglês tipo
`Invoice number`, `Customer's PO`, `Terms of payment`, `Final amount incl. VAT`).
Se seus fornecedores usam outro layout, os pontos a ajustar são:

- `INVOICE_NUMBER_PATTERNS`, `CUSTOMER_PO_PATTERNS`, `TOTAL_VALUE_PATTERNS`,
  `LINE_ITEM_PATTERNS`, `HS_CODE_PATTERNS`, `PO_LINE_PATTERNS` no topo de
  `invoice_extractor.py` — cada um é uma LISTA de padrões tentados em ordem;
  basta adicionar mais um item na lista para cobrir mais um layout.
- `COLUMN_ALIASES` no topo de `packing_list_extractor.py` — os nomes de
  coluna aceitos na packing list (o script já procura o cabeçalho
  automaticamente, então a aba não precisa começar sempre na mesma linha).

Um jeito rápido de testar novos regex: rode
`python -c "import pdfplumber; print(pdfplumber.open('sua_fatura.pdf').pages[0].extract_text())"`
e ajuste os padrões olhando o texto real extraído.

## Limitações conhecidas

- OCR é heurístico: para faturas realmente escaneadas, sempre confira os
  valores extraídos (o relatório já sinaliza com aviso "[OCR]" quais faturas
  passaram por esse caminho).
- O cross-check de PO/Item depende dos nomes de coluna da packing list
  estarem entre os aliases conhecidos (`ILX PO`, `PO Item`, etc.) — ajuste
  `COLUMN_ALIASES` se sua planilha usar nomes diferentes.
- O booking form (Kuehne+Nagel) não é lido automaticamente na versão atual —
  os dados de referência hoje vêm da fatura + packing list, que já
  cobriram 100% do que foi validado nos exemplos. Se quiser que o booking
  também entre no cross-check (ex.: validar o incoterm do booking contra o
  da fatura), me avise que eu adiciono um `booking_extractor.py` seguindo o
  mesmo padrão dos outros dois módulos.
