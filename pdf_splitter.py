# -*- coding: utf-8 -*-
"""
pdf_splitter.py
-----------------
Alguns exports de ERP (ex.: ABB) juntam VÁRIAS faturas em um único PDF
(uma atrás da outra, cada uma com 1-N páginas). Se você mandar esse PDF
inteiro pra extração como se fosse "uma fatura", dois problemas acontecem:

  1. Semanticamente errado: só sairia UM registro (uma PO, um valor, um
     invoice_number), ignorando as outras faturas do arquivo.
  2. Na prática, quando o motor é IA (Claude/Gemini), pedir pro modelo
     devolver um JSON gigante com centenas de itens de dezenas de faturas
     de uma vez estoura o limite de tokens de saída e a resposta vem
     cortada no meio (erro de "Unterminated string" / JSON inválido).

Este módulo detecta os limites entre faturas dentro de um PDF e separa em
um arquivo por fatura, para cada um ser extraído individualmente e depois
consolidado normalmente pelo resto do pipeline (que já foi feito pra somar
várias faturas).

Estratégia de detecção (heurística, não depende de um layout específico):
  a) Procura, em cada página, um número de fatura usando um conjunto de
     padrões comuns (label em PT/EN + dígitos).
  b) Procura também um contador de página tipo "Page 1 of 4",
     "PAG. 1/ 4", "Página 1 de 4" — quando esse contador reinicia em 1,
     é um forte sinal de início de nova fatura (mesmo que o número da
     fatura não tenha sido reconhecido naquela página).
  c) Agrupa páginas consecutivas: inicia um novo grupo quando o número de
     fatura muda OU quando o contador de página reinicia em 1.
  d) Se nada disso for detectado em nenhuma página, o PDF é tratado como
     UMA fatura só (comportamento normal, sem split).
"""

import os
import re
from dataclasses import dataclass
from typing import List, Optional

import pdfplumber
from pypdf import PdfReader, PdfWriter

INVOICE_NUMBER_PATTERNS = [
    re.compile(r"Invoice\s*[Nn]umber[:\s]+(\S+)"),
    re.compile(r"Invoice\s*[Nn]o\.?[:\s]+(\S+)"),
    re.compile(r"\bINVOICE\s+(\d{5,})\b"),          # layout ABB SpA: "INVOICE 2026427634 14.07.2026"
    re.compile(r"Fatura\s*[Nn][ºo°]?[:\s]+(\S+)"),
    re.compile(r"N[uú]mero da [Ff]atura[:\s]+(\S+)"),
]

PAGE_COUNTER_PATTERNS = [
    re.compile(r"PAG\.?\s*(\d+)\s*/\s*(\d+)"),        # "PAG. 1/ 4"
    re.compile(r"Page\s+(\d+)\s+of\s+(\d+)", re.IGNORECASE),
    re.compile(r"P[aá]gina\s+(\d+)\s+de\s+(\d+)", re.IGNORECASE),
]


@dataclass
class PageInfo:
    index: int
    invoice_number: Optional[str] = None
    page_counter: Optional[int] = None  # posição "X" dentro de "X/Y"


def _detect_invoice_number(text: str) -> Optional[str]:
    for pat in INVOICE_NUMBER_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1).strip()
    return None


def _detect_page_counter(text: str) -> Optional[int]:
    for pat in PAGE_COUNTER_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
    return None


def analyze_pdf(pdf_path: str) -> List[PageInfo]:
    infos = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            infos.append(PageInfo(
                index=i,
                invoice_number=_detect_invoice_number(text),
                page_counter=_detect_page_counter(text),
            ))
    return infos


def group_pages_by_invoice(infos: List[PageInfo]) -> List[List[int]]:
    """Agrupa índices de página em faturas distintas. Retorna uma lista de
    grupos (cada grupo é uma lista de índices de página, em ordem)."""
    groups: List[List[int]] = []
    current: List[int] = []
    last_invoice_number = None

    for info in infos:
        starts_new = False
        if current:
            if info.invoice_number and info.invoice_number != last_invoice_number:
                starts_new = True
            elif info.invoice_number is None and info.page_counter == 1:
                starts_new = True

        if starts_new:
            groups.append(current)
            current = []

        current.append(info.index)
        if info.invoice_number:
            last_invoice_number = info.invoice_number

    if current:
        groups.append(current)

    return groups


def is_batch_pdf(infos: List[PageInfo]) -> bool:
    """True se detectamos sinais de mais de uma fatura no arquivo."""
    numbers = {i.invoice_number for i in infos if i.invoice_number}
    return len(numbers) > 1


def split_pdf(pdf_path: str, output_dir: str) -> List[str]:
    """
    Se o PDF tiver mais de uma fatura, separa em um arquivo por fatura
    dentro de `output_dir` e retorna a lista de caminhos gerados (em
    ordem). Se for detectada apenas uma fatura, retorna uma lista com o
    próprio `pdf_path` original (nenhum split necessário).
    """
    infos = analyze_pdf(pdf_path)

    if not is_batch_pdf(infos):
        return [pdf_path]

    groups = group_pages_by_invoice(infos)
    os.makedirs(output_dir, exist_ok=True)
    reader = PdfReader(pdf_path)
    base_name = os.path.splitext(os.path.basename(pdf_path))[0]

    out_paths = []
    for gi, page_indices in enumerate(groups):
        writer = PdfWriter()
        for pi in page_indices:
            writer.add_page(reader.pages[pi])

        # nomeia o arquivo com o nº da fatura detectado, se houver, senão
        # com um índice sequencial
        inv_number = next(
            (infos[pi].invoice_number for pi in page_indices if infos[pi].invoice_number),
            None,
        )
        suffix = inv_number if inv_number else f"parte{gi+1:03d}"
        out_path = os.path.join(output_dir, f"{base_name}__{suffix}.pdf")
        with open(out_path, "wb") as f:
            writer.write(f)
        out_paths.append(out_path)

    return out_paths
