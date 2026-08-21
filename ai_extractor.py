# -*- coding: utf-8 -*-
"""
ai_extractor.py
-----------------
Extração de dados de fatura usando a API da Anthropic (Claude) com visão,
em vez de regex fixo. Diferente de `invoice_extractor.py` (que só funciona
bem no layout ABB para o qual os regex foram escritos), este módulo lê a
fatura como um humano leria: manda o PDF para o modelo e pede os campos de
volta em JSON. Funciona com qualquer layout de fornecedor, sem precisar
escrever um novo conjunto de regex a cada fornecedor novo.

Requer:
    pip install anthropic
    export ANTHROPIC_API_KEY="sk-ant-..."

Retorna o MESMO tipo `InvoiceData` / `LineItem` de `invoice_extractor.py`,
então `cross_check.py` e `gl_report.py` funcionam sem alteração nenhuma,
não importa qual dos dois extratores você usou.
"""

import base64
import json
import os
import re
from typing import Optional

from invoice_extractor import InvoiceData, LineItem

DEFAULT_MODEL = "claude-sonnet-5"

EXTRACTION_PROMPT = """\
Você é um extrator de dados de faturas comerciais (invoices) de importação.
Analise o PDF anexado e devolva APENAS um objeto JSON (sem markdown, sem
```json, sem texto antes ou depois), no seguinte formato exato:

{
  "invoice_number": "string ou null",
  "invoice_date": "string como aparece no documento, ou null",
  "incoterm": "código de 3 letras do Incoterm (ex: CPT, FOB, FCA, EXW...) ou null",
  "incoterm_place": "local/complemento do incoterm, se houver, ou null",
  "manner_of_transport": "ex: Sea, Air, Maritime, Road, ou null",
  "currency": "código de 3 letras da moeda (ex: USD, EUR, CNY), ou null",
  "total_value": número (o valor TOTAL final da fatura, sem separador de milhar, ponto como decimal) ou null,
  "line_items": [
    {
      "ref_line": "número/posição da linha no documento do FORNECEDOR (ex: Pos, Line), como string — NÃO é necessariamente o número do item da PO do cliente",
      "po_line": "número da linha/item DENTRO DA PO DO CLIENTE, se aparecer no documento (rótulos comuns: 'PO Line', 'PO Item', 'Item'). Esse é o número que deve ser usado no GL. Se não aparecer explicitamente, use null (não invente igualando ao ref_line).",
      "part_number": "código do material/peça",
      "po": "número da PO (purchase order) do cliente associada a essa linha, ou null",
      "hs_code": "código HS/NCM da linha (ex: rótulo 'Customs Tariff No.', 'NCM', 'HS Code'), se houver, ou null",
      "qty": "quantidade, como string, ex: '10.000'",
      "unit": "unidade (PC, KG, etc.) ou null se não indicado",
      "unit_price": "preço unitário, como string",
      "amount_excl_vat": "valor total da linha, como string",
      "amount_incl_vat": "valor total da linha com impostos, como string (repita amount_excl_vat se não houver VAT separado)"
    }
  ]
}

Regras importantes:
- Se o documento tiver UMA ÚNICA PO listada no cabeçalho (fora da tabela de
  itens) e ela se aplicar a todas as linhas, repita-a em "po" de cada linha.
- Se cada linha tiver uma PO diferente (ou a mesma repetida por linha),
  use o valor daquela linha específica.
- Números devem usar PONTO como separador decimal (converta vírgula
  decimal, ex.: "8,06" -> "8.06"), e NÃO usar separador de milhar.
- Se algum campo não existir no documento, use null. Não invente valores.
- Retorne JSON válido e nada mais.
"""


def _get_client():
    try:
        import anthropic
    except ImportError as e:
        raise ImportError(
            "O pacote 'anthropic' não está instalado. Rode: pip install anthropic"
        ) from e
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Variável de ambiente ANTHROPIC_API_KEY não definida. "
            "Defina sua chave da API da Anthropic antes de usar --engine ai."
        )
    return anthropic.Anthropic(api_key=api_key)


def _strip_json_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    return text


def extract_invoice_ai(pdf_path: str, model: str = DEFAULT_MODEL) -> InvoiceData:
    """
    Extrai os dados de uma fatura em PDF usando o Claude (visão), robusto a
    qualquer layout de fornecedor. Funciona tanto para PDFs de texto quanto
    para PDFs escaneados/fotografados (o modelo lê a página como imagem).
    """
    client = _get_client()

    with open(pdf_path, "rb") as f:
        pdf_b64 = base64.standard_b64encode(f.read()).decode("utf-8")

    response = client.messages.create(
        model=model,
        max_tokens=16000,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_b64,
                        },
                    },
                    {"type": "text", "text": EXTRACTION_PROMPT},
                ],
            }
        ],
    )

    text = "".join(block.text for block in response.content if block.type == "text")
    text = _strip_json_fences(text)

    inv = InvoiceData(source_file=os.path.basename(pdf_path), extraction_method="ai", raw_text=text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        hint = ""
        if "Unterminated" in str(e) or e.pos > len(text) - 20:
            hint = (" A resposta do modelo parece ter sido CORTADA no meio (provável "
                    "limite de tokens de saída atingido — fatura com muitos itens). "
                    "Se persistir, considere separar essa fatura em partes menores.")
        inv.warnings.append(f"Falha ao interpretar JSON retornado pelo modelo: {e}.{hint}")
        return inv

    inv.invoice_number = data.get("invoice_number")
    inv.invoice_date = data.get("invoice_date")
    inv.incoterm = data.get("incoterm")
    inv.incoterm_place = data.get("incoterm_place")
    inv.manner_of_transport = data.get("manner_of_transport")
    inv.currency = data.get("currency")
    tv = data.get("total_value")
    inv.total_value = float(tv) if tv is not None else None

    pos_found = set()
    ncm_found = []
    for li in data.get("line_items", []) or []:
        hs = li.get("hs_code")
        item = LineItem(
            ref_line=str(li.get("ref_line") or ""),
            part_number=str(li.get("part_number") or ""),
            qty=str(li.get("qty") or ""),
            unit=str(li.get("unit") or ""),
            unit_price=str(li.get("unit_price") or ""),
            amount_excl_vat=str(li.get("amount_excl_vat") or ""),
            amount_incl_vat=str(li.get("amount_incl_vat") or li.get("amount_excl_vat") or ""),
            ref_po=str(li.get("po")) if li.get("po") else None,
            po_line=str(li.get("po_line")) if li.get("po_line") else None,
            hs_code=str(hs) if hs else None,
        )
        inv.line_items.append(item)
        if item.ref_po:
            pos_found.add(item.ref_po)
        if hs:
            ncm_found.append(str(hs))

    if ncm_found:
        from collections import Counter
        inv.ncm = Counter(ncm_found).most_common(1)[0][0]

    # customer_po: se todas as linhas apontam para a mesma PO, usa ela.
    # Se houver mais de uma PO distinta na mesma fatura, avisa (o cross-check
    # e o relatório GL, hoje, assumem uma PO "principal" por fatura).
    if len(pos_found) == 1:
        inv.customer_po = next(iter(pos_found))
    elif len(pos_found) > 1:
        inv.customer_po = sorted(pos_found)[0]
        inv.warnings.append(
            f"Fatura tem múltiplas POs distintas nas linhas ({sorted(pos_found)}); "
            f"usando '{inv.customer_po}' como principal. Confira a aba Itens_Fatura."
        )

    if not inv.invoice_number:
        inv.warnings.append("invoice_number não encontrado pelo modelo")
    if not inv.customer_po:
        inv.warnings.append("customer_po não encontrado pelo modelo")
    if not inv.incoterm:
        inv.warnings.append("incoterm não encontrado pelo modelo")
    if inv.total_value is None:
        inv.warnings.append("valor total não encontrado pelo modelo")
    if not inv.line_items:
        inv.warnings.append("nenhuma linha de item retornada pelo modelo")

    return inv
