# -*- coding: utf-8 -*-
"""
gemini_extractor.py
---------------------
Igual a `ai_extractor.py`, mas usando a API do Google Gemini em vez da
Anthropic. Mesma ideia: manda o PDF direto pro modelo (funciona com PDF de
texto ou escaneado) e pede os campos de volta em JSON, robusto a qualquer
layout de fornecedor. Devolve o MESMO tipo `InvoiceData` / `LineItem` de
`invoice_extractor.py`, então `cross_check.py` e `gl_report.py` funcionam
sem alteração nenhuma, independente de qual motor (Claude ou Gemini) você
escolher.

Requer:
    pip install google-genai
    export GEMINI_API_KEY="..."     (ou GOOGLE_API_KEY)

A chave é obtida em https://aistudio.google.com/apikey
"""

import base64
import json
import os
import re

from invoice_extractor import InvoiceData, LineItem
from ai_extractor import EXTRACTION_PROMPT, _strip_json_fences  # reaproveita o mesmo prompt

DEFAULT_MODEL = "gemini-2.5-flash"


def _get_client():
    try:
        from google import genai
    except ImportError as e:
        raise ImportError(
            "O pacote 'google-genai' não está instalado. Rode: pip install google-genai"
        ) from e
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Variável de ambiente GEMINI_API_KEY (ou GOOGLE_API_KEY) não definida. "
            "Gere uma chave em https://aistudio.google.com/apikey e defina-a antes "
            "de usar --engine gemini."
        )
    return genai.Client(api_key=api_key)


def extract_invoice_gemini(pdf_path: str, model: str = DEFAULT_MODEL) -> InvoiceData:
    """
    Extrai os dados de uma fatura em PDF usando o Gemini (visão), robusto a
    qualquer layout de fornecedor.
    """
    from google.genai import types

    client = _get_client()

    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()

    response = client.models.generate_content(
        model=model,
        contents=[
            types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
            EXTRACTION_PROMPT,
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0,
            max_output_tokens=16000,
        ),
    )

    text = _strip_json_fences(response.text or "")

    inv = InvoiceData(source_file=os.path.basename(pdf_path), extraction_method="gemini", raw_text=text)

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
