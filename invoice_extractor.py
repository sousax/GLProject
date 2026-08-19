# -*- coding: utf-8 -*-
"""
invoice_extractor.py
---------------------
Extrai dados-chave de faturas (invoices) em PDF, tanto faturas com texto
"nativo" (texto selecionável) quanto faturas digitalizadas/escaneadas
(imagem), usando OCR como fallback automático.

Campos extraídos por fatura:
    - invoice_number
    - invoice_date
    - customer_po        (PO do cliente)
    - incoterm            (ex.: CPT, FOB, EXW...)
    - incoterm_place       (texto após o incoterm)
    - manner_of_transport  (ex.: Sea, Air)
    - currency
    - total_value          (valor total da fatura)
    - total_value_estimated (True se o total não foi encontrado por rótulo e
                              foi calculado somando os itens, como fallback)
    - ncm                   (NCM/HS code mais frequente encontrado na fatura)
    - line_items            (lista de LineItem — ver abaixo)
    - source_file
    - extraction_method     ("text" ou "ocr")
    - raw_text (texto completo extraído, útil para auditoria/depuração)

Cada LineItem inclui, além dos campos de sempre:
    - po_line   : o número da linha/item DENTRO DA PO DO CLIENTE (o que
                  costuma vir rotulado como "PO Line", "Item", "Pos" etc. —
                  é esse o número que deve ir no GL, não o "Ref line" interno
                  do documento do fornecedor, que pode ser outro número).
    - hs_code   : código NCM/HS daquela linha, se encontrado perto dela.

NÚMEROS COM VÍRGULA DECIMAL (formato europeu, ex.: "551,37" = 551.37,
"2.205,48" = 2205.48): o parser detecta automaticamente o formato (US ou
europeu) em vez de assumir sempre vírgula = separador de milhar. Isso
importa bastante — faturas de fornecedores europeus estavam sendo lidas com
valores 100x maiores por causa disso antes desse ajuste.

MÚLTIPLOS LAYOUTS: cada campo tenta uma LISTA de padrões conhecidos, em
ordem, até um bater. Isso cobre mais de um layout de fornecedor sem precisar
de um módulo separado por fornecedor. Se o seu fornecedor usa um layout que
nenhum desses padrões reconhece, ainda assim os motores de IA
(ai_extractor.py / gemini_extractor.py / openai_extractor.py) funcionam,
porque não dependem de layout nenhum.
"""

import re
import os
from collections import Counter
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import pdfplumber

VALID_INCOTERMS = {
    "EXW", "FCA", "FAS", "FOB", "CFR", "CIF",
    "CPT", "CIP", "DAP", "DPU", "DDP", "DAT",
}

# ---------------------------------------------------------------------------
# Padrões de extração (regex), em ordem de prioridade por campo.
# ---------------------------------------------------------------------------
INVOICE_NUMBER_PATTERNS = [
    re.compile(r"Invoice\s*number\s+(\S+)", re.IGNORECASE),
    re.compile(r"\bINVOICE\s+(\d{5,})\b"),                     # layout ABB S.p.A.: "INVOICE 2026427634"
    re.compile(r"Invoice\s*No\.?\s*[:\s]+(\S+)", re.IGNORECASE),
]

INVOICE_DATE_PATTERNS = [
    re.compile(r"Invoice\s*date\s+([\d.\-/]+)", re.IGNORECASE),
    re.compile(r"\bINVOICE\s+\d{5,}\s+([\d.\-/]+)"),           # "INVOICE 2026427634 14.07.2026"
    re.compile(r"^Date[:\s]+([\d.\-/,]+)$", re.IGNORECASE | re.MULTILINE),
]

CUSTOMER_PO_PATTERNS = [
    re.compile(r"Customer'?s\s*PO\s+(\S+)", re.IGNORECASE),
    re.compile(r"YOUR\s*ORDER\s*N[o°.]?\s*(\d{5,})", re.IGNORECASE),   # layout ABB S.p.A.
    re.compile(r"\bP\.?O\.?\s*N[o°.]?\s*[:\s]+(\d{5,})", re.IGNORECASE),
    re.compile(r"Purchase\s*Order\s*N[o°.]?\s*[:\s]+(\d{5,})", re.IGNORECASE),
]

# rótulos usados como "âncora" para achar o incoterm por perto (o código de
# 3 letras raramente vem sozinho sem contexto, então procuramos perto de
# uma dessas frases primeiro; se não achar nada, caímos para uma busca
# genérica no documento inteiro)
INCOTERM_ANCHORS = [
    "Terms of payment", "PORTO-DELIVERY", "PORTO - DELIVERY", "Delivery terms",
    "Incoterm", "Condizioni di resa", "as per Incoterms",
]

# total: lista de (regex, grupo_da_moeda_ou_None, grupo_do_valor)
# quando grupo_da_moeda é None, tentamos achar a moeda em outro lugar do texto.
TOTAL_VALUE_PATTERNS: List[Tuple[re.Pattern, Optional[int], int]] = [
    (re.compile(r"Final\s*amount\s*incl\.?\s*VAT\s+([A-Z]{3})\s+([\d.,]+)", re.IGNORECASE), 1, 2),
    (re.compile(r"TOTALE\s*-\s*TOTAL\s+([\d.,]+)\s+([A-Z]{3})", re.IGNORECASE), 2, 1),  # layout ABB S.p.A.
    (re.compile(r"\bGRAND\s+TOTAL\b\s*[:\s]*([A-Z]{3})?\s*([\d.,]+)", re.IGNORECASE), 1, 2),
    (re.compile(r"\bTOTAL\s+DUE\b\s*[:\s]*([A-Z]{3})?\s*([\d.,]+)", re.IGNORECASE), 1, 2),
    (re.compile(r"\bINVOICE\s+TOTAL\b\s*[:\s]*([A-Z]{3})?\s*([\d.,]+)", re.IGNORECASE), 1, 2),
    (re.compile(r"\bAMOUNT\s+DUE\b\s*[:\s]*([A-Z]{3})?\s*([\d.,]+)", re.IGNORECASE), 1, 2),
]

HS_CODE_PATTERNS = [
    re.compile(r"CUSTOMS\s*TARIFF\s*NO\.?\s*[:\s]*(\d{6,12})", re.IGNORECASE),
    re.compile(r"\bNCM\b\s*[:\s\-]*([\d]{4}\.?[\d]{2}\.?[\d]{2})", re.IGNORECASE),
    re.compile(r"\bHS\s*[-\s]?CODE\b\s*[:\s]*(\d{6,12})", re.IGNORECASE),
    re.compile(r"\bHS\s*NO\.?\s*[:\s]*(\d{6,12})", re.IGNORECASE),
]

# "PO Line 180 ..." — o número da linha do item na PO do cliente
PO_LINE_PATTERNS = [
    re.compile(r"\bPO\s*Line\s+(\d+)", re.IGNORECASE),
    re.compile(r"\bPO\s*Item\s*[:\s]+(\d+)", re.IGNORECASE),
    re.compile(r"\bItem\s*Line\s*[:\s]+(\d+)", re.IGNORECASE),
]

MANNER_OF_TRANSPORT_PATTERN = re.compile(r"Manner\s*of\s*transport\s+(\w+)", re.IGNORECASE)

# --- padrões de linha de item (mais de um layout, tentados em ordem) -------
LINE_ITEM_PATTERNS = [
    # Layout ABB Xiamen: "000100 RB9481823 10.000 PC 3,972.7300 0.00 39,727.30 39,727.30"
    re.compile(
        r"^(?P<ref_line>\d{4,8})\s+(?P<part_number>\S+)\s+"
        r"(?P<qty>[\d.,]+)\s+(?P<unit>[A-Za-z]{1,4})\s+"
        r"(?P<unit_price>[\d.,]+)\s+(?P<vat_amt>[\d.,]+)\s+"
        r"(?P<amount_excl_vat>[\d.,]+)\s+(?P<amount_incl_vat>[\d.,]+)\s*$",
        re.MULTILINE,
    ),
    # Layout ABB S.p.A.: "2400 1SDA073913R1 8015644778460 NR 4 551,37 2.205,48"
    #  <pos> <part_number> <ean 10-14 dig> <unit> <qty> <unit_price> <amount>
    re.compile(
        r"^(?P<ref_line>\d{3,6})\s+(?P<part_number>\S+)\s+\d{10,14}\s+"
        r"(?P<unit>[A-Za-z]{1,4})\s+(?P<qty>[\d.,]+)\s+"
        r"(?P<unit_price>[\d.,]+)\s+(?P<amount_incl_vat>[\d.,]+)\s*$",
        re.MULTILINE,
    ),
    # Variante do layout ABB S.p.A. sem código EAN:
    # "300 UXAB100400445 NR 1.000 1,10 1.100,00"
    re.compile(
        r"^(?P<ref_line>\d{3,6})\s+(?P<part_number>\S+)\s+"
        r"(?P<unit>[A-Za-z]{1,4})\s+(?P<qty>[\d.,]+)\s+"
        r"(?P<unit_price>[\d.,]+)\s+(?P<amount_incl_vat>[\d.,]+)\s*$",
        re.MULTILINE,
    ),
]


@dataclass
class LineItem:
    ref_line: str
    part_number: str
    qty: str
    unit: str
    unit_price: str
    amount_excl_vat: str
    amount_incl_vat: str
    ref_po: Optional[str] = None
    po_line: Optional[str] = None   # número da linha/item na PO do cliente
    hs_code: Optional[str] = None   # NCM/HS code dessa linha, se encontrado


@dataclass
class InvoiceData:
    source_file: str
    extraction_method: str = "text"
    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = None
    customer_po: Optional[str] = None
    incoterm: Optional[str] = None
    incoterm_place: Optional[str] = None
    manner_of_transport: Optional[str] = None
    currency: Optional[str] = None
    total_value: Optional[float] = None
    total_value_estimated: bool = False
    ncm: Optional[str] = None
    line_items: List[LineItem] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    raw_text: str = ""

    def to_dict(self):
        return {
            "source_file": self.source_file,
            "extraction_method": self.extraction_method,
            "invoice_number": self.invoice_number,
            "invoice_date": self.invoice_date,
            "customer_po": self.customer_po,
            "incoterm": self.incoterm,
            "incoterm_place": self.incoterm_place,
            "manner_of_transport": self.manner_of_transport,
            "currency": self.currency,
            "total_value": self.total_value,
            "total_value_estimated": self.total_value_estimated,
            "ncm": self.ncm,
            "n_line_items": len(self.line_items),
            "warnings": "; ".join(self.warnings),
        }


def _parse_number(s: str) -> Optional[float]:
    """
    Converte um número em string para float, DETECTANDO automaticamente se
    o formato é dos EUA ('1,234.56') ou europeu ('1.234,56' ou '551,37').

    Regra:
      - se tiver vírgula E ponto: o separador que aparece por ÚLTIMO é o
        decimal (o outro é separador de milhar).
      - se só tiver vírgula: é decimal se tiver exatamente 2 dígitos depois
        dela (ex.: '551,37'); senão é separador de milhar (ex.: '2,205').
      - se só tiver ponto, ou nenhum separador: mantém como está.
    """
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    s = re.sub(r"[^\d.,\-]", "", s)
    if not s:
        return None

    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        tail = s.split(",")[-1]
        if len(tail) == 2:
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")

    try:
        return float(s)
    except ValueError:
        return None


def _extract_incoterm(text: str) -> Tuple[Optional[str], Optional[str]]:
    """Busca o incoterm perto de rótulos conhecidos primeiro; se não achar,
    faz uma busca genérica por qualquer token de 3 letras maiúsculas que
    seja um incoterm válido em qualquer lugar do documento."""
    for anchor in INCOTERM_ANCHORS:
        for m in re.finditer(re.escape(anchor), text, re.IGNORECASE):
            window = text[m.end(): m.end() + 200]
            for tok_m in re.finditer(r"\b([A-Za-z]{3})\b", window):
                token = tok_m.group(1).upper()
                if token in VALID_INCOTERMS:
                    place = window[tok_m.end():tok_m.end() + 60].strip().splitlines()[0] if tok_m.end() < len(window) else None
                    return token, (place or None)

    # fallback genérico: token exato em maiúsculas, em qualquer lugar
    counts = Counter(
        m.group(1) for m in re.finditer(r"\b([A-Z]{3})\b", text) if m.group(1) in VALID_INCOTERMS
    )
    if counts:
        return counts.most_common(1)[0][0], None
    return None, None


def _extract_hs_code_near(text: str, pos: int, window: int = 400) -> Optional[str]:
    snippet = text[pos: pos + window]
    for pat in HS_CODE_PATTERNS:
        m = pat.search(snippet)
        if m:
            return m.group(1).replace(".", "")
    return None


def _extract_po_line_near(text: str, pos: int, window: int = 400) -> Optional[str]:
    snippet = text[pos: pos + window]
    for pat in PO_LINE_PATTERNS:
        m = pat.search(snippet)
        if m:
            return m.group(1)
    return None


def _extract_line_items(text: str) -> List[LineItem]:
    items = []
    lines = text.splitlines()
    # mapeia offset de char -> índice de linha, pra localizar contexto
    # (HS code / PO Line) logo depois de cada item encontrado.
    line_starts = []
    offset = 0
    for line in lines:
        line_starts.append(offset)
        offset += len(line) + 1

    for pattern in LINE_ITEM_PATTERNS:
        found_this_pattern = []
        for i, line in enumerate(lines):
            m = pattern.match(line.strip())
            if not m:
                continue
            d = m.groupdict()
            ref_po = None
            for j in range(i + 1, min(i + 4, len(lines))):
                m2 = re.match(r"^\S+\s+(\d{6,12})\s*$", lines[j].strip())
                if m2:
                    ref_po = m2.group(1)
                    break

            char_pos = line_starts[i]
            hs_code = _extract_hs_code_near(text, char_pos)
            po_line = _extract_po_line_near(text, char_pos)

            found_this_pattern.append(
                LineItem(
                    ref_line=d.get("ref_line", ""),
                    part_number=d.get("part_number", ""),
                    qty=d.get("qty", ""),
                    unit=d.get("unit", ""),
                    unit_price=d.get("unit_price", ""),
                    amount_excl_vat=d.get("amount_excl_vat", d.get("amount_incl_vat", "")),
                    amount_incl_vat=d.get("amount_incl_vat", ""),
                    ref_po=ref_po,
                    po_line=po_line,
                    hs_code=hs_code,
                )
            )
        if found_this_pattern:
            items.extend(found_this_pattern)
            break  # usa o primeiro padrão que bateu; não mistura padrões diferentes no mesmo doc

    return items


def _extract_total_value(text: str) -> Tuple[Optional[str], Optional[float]]:
    for pattern, currency_group, value_group in TOTAL_VALUE_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        currency = m.group(currency_group).strip().upper() if currency_group and m.group(currency_group) else None
        value = _parse_number(m.group(value_group))
        if value is not None:
            return currency, value
    return None, None


def _extract_ncm(text: str, line_items: List[LineItem]) -> Optional[str]:
    codes = [li.hs_code for li in line_items if li.hs_code]
    if not codes:
        codes = [m.group(1).replace(".", "") for pat in HS_CODE_PATTERNS for m in pat.finditer(text)]
    if not codes:
        return None
    return Counter(codes).most_common(1)[0][0]


def _extract_fields_from_text(text: str, source_file: str, method: str) -> InvoiceData:
    inv = InvoiceData(source_file=source_file, extraction_method=method, raw_text=text)

    for pat in INVOICE_NUMBER_PATTERNS:
        m = pat.search(text)
        if m:
            inv.invoice_number = m.group(1).strip()
            break
    if not inv.invoice_number:
        inv.warnings.append("invoice_number não encontrado")

    for pat in INVOICE_DATE_PATTERNS:
        m = pat.search(text)
        if m:
            inv.invoice_date = m.group(1).strip()
            break

    for pat in CUSTOMER_PO_PATTERNS:
        m = pat.search(text)
        if m:
            inv.customer_po = m.group(1).strip()
            break
    if not inv.customer_po:
        inv.warnings.append("customer_po não encontrado")

    incoterm, place = _extract_incoterm(text)
    inv.incoterm = incoterm
    inv.incoterm_place = place
    if not incoterm:
        inv.warnings.append("incoterm não encontrado")

    m = MANNER_OF_TRANSPORT_PATTERN.search(text)
    if m:
        inv.manner_of_transport = m.group(1).strip()

    inv.line_items = _extract_line_items(text)
    if not inv.line_items:
        inv.warnings.append("nenhuma linha de item (line item) reconhecida")

    currency, total = _extract_total_value(text)
    if total is not None:
        inv.currency = currency
        inv.total_value = total
    elif inv.line_items:
        # fallback: soma os itens encontrados em vez de deixar vazio
        summed = sum(_parse_number(li.amount_incl_vat) or 0 for li in inv.line_items)
        if summed > 0:
            inv.total_value = summed
            inv.total_value_estimated = True
            inv.warnings.append(
                "rótulo de valor total não encontrado; total ESTIMADO somando os itens "
                "reconhecidos — confira manualmente."
            )
    if inv.total_value is None:
        inv.warnings.append("valor total não encontrado")
    if not inv.currency and inv.line_items:
        # tenta achar a moeda em qualquer lugar do texto como último recurso
        m = re.search(r"\b(USD|EUR|CNY|BRL|GBP|JPY)\b", text)
        if m:
            inv.currency = m.group(1)

    inv.ncm = _extract_ncm(text, inv.line_items)

    return inv


def _ocr_pdf_text(pdf_path: str, dpi: int = 300) -> str:
    """Renderiza cada página do PDF como imagem e roda OCR (pytesseract)."""
    from pdf2image import convert_from_path
    import pytesseract

    pages = convert_from_path(pdf_path, dpi=dpi)
    text_parts = []
    for page_img in pages:
        text_parts.append(pytesseract.image_to_string(page_img, lang="eng+por"))
    return "\n".join(text_parts)


def extract_invoice(pdf_path: str, force_ocr: bool = False) -> InvoiceData:
    """
    Extrai os dados de uma fatura em PDF.

    1. Tenta extrair texto nativo do PDF (rápido e preciso).
    2. Se o PDF não tiver texto extraível (fatura digitalizada/escaneada)
       ou `force_ocr=True`, cai para OCR automaticamente.
    """
    text = ""
    method = "text"

    if not force_ocr:
        with pdfplumber.open(pdf_path) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)

    # Heurística: se o texto extraído for muito curto, provavelmente é
    # uma fatura escaneada (imagem) -> usar OCR.
    if force_ocr or len(text.strip()) < 50:
        text = _ocr_pdf_text(pdf_path)
        method = "ocr"

    inv = _extract_fields_from_text(text, os.path.basename(pdf_path), method)
    if method == "ocr":
        inv.warnings.append(
            "Extraído via OCR — confira manualmente os valores, "
            "OCR é sujeito a erros de leitura."
        )
    return inv
