# -*- coding: utf-8 -*-
"""
cross_check.py
----------------
Compara os dados extraídos das faturas com os dados da packing list
(e, opcionalmente, do booking) e gera uma lista de achados (findings):
OK, DIVERGÊNCIA ou AVISO — para o usuário revisar antes de enviar o GL.

Regras de validação aplicadas:
  1. O "Customer's PO" de cada fatura deve existir na packing list.
  2. O incoterm de cada fatura deve ser o mesmo em todas as faturas do
     mesmo embarque (evita misturar remessas com incoterms diferentes
     no mesmo GL).
  3. Todas as faturas do lote devem estar na mesma moeda (para a soma
     do valor total fazer sentido).
  4. Alerta se a fatura não teve nenhuma linha de item reconhecida
     (provável falha de extração / layout diferente).
"""

from dataclasses import dataclass
from typing import List, Dict

from invoice_extractor import InvoiceData
from packing_list_extractor import POItemSummary


@dataclass
class Finding:
    level: str      # "OK", "DIVERGENCIA", "AVISO"
    invoice: str     # invoice_number ou source_file
    message: str


def run_cross_check(
    invoices: List[InvoiceData],
    pl_summaries: Dict[str, POItemSummary] = None,
) -> List[Finding]:
    findings: List[Finding] = []

    # --- 1) PO de cada fatura existe na packing list? ---------------------
    if pl_summaries:
        pl_pos = {s.po for s in pl_summaries.values()}
        for inv in invoices:
            label = inv.invoice_number or inv.source_file
            if inv.customer_po is None:
                findings.append(Finding("AVISO", label, "PO não extraído da fatura; não foi possível cruzar com a packing list."))
                continue
            if inv.customer_po in pl_pos:
                matches = [s for s in pl_summaries.values() if s.po == inv.customer_po]
                total_pkgs = sum(s.n_packages for s in matches)
                total_gw = sum(s.total_gross_weight for s in matches)
                items = ", ".join(sorted({s.po_item for s in matches}))
                findings.append(Finding(
                    "OK", label,
                    f"PO {inv.customer_po} encontrada na packing list "
                    f"(item(s): {items}; {total_pkgs} volume(s); peso bruto {total_gw:.2f} kg)."
                ))
            else:
                findings.append(Finding(
                    "DIVERGENCIA", label,
                    f"PO {inv.customer_po} da fatura NÃO foi encontrada na packing list."
                ))

    # --- 2) Todas as faturas com o mesmo incoterm? -------------------------
    incoterms = {inv.incoterm for inv in invoices if inv.incoterm}
    if len(incoterms) > 1:
        findings.append(Finding(
            "DIVERGENCIA", "(lote)",
            f"As faturas do lote têm incoterms diferentes: {sorted(incoterms)}. "
            f"Confirme se pertencem ao mesmo embarque/GL."
        ))
    elif len(incoterms) == 1:
        findings.append(Finding("OK", "(lote)", f"Incoterm consistente entre as faturas: {list(incoterms)[0]}."))

    # --- 3) Mesma moeda em todas as faturas? -------------------------------
    currencies = {inv.currency for inv in invoices if inv.currency}
    if len(currencies) > 1:
        findings.append(Finding(
            "DIVERGENCIA", "(lote)",
            f"As faturas do lote estão em moedas diferentes: {sorted(currencies)}. "
            f"A soma do valor total pode não fazer sentido — verifique antes de consolidar."
        ))

    # --- 4) Fatura sem linha de item reconhecida ----------------------------
    for inv in invoices:
        label = inv.invoice_number or inv.source_file
        if not inv.line_items:
            findings.append(Finding(
                "AVISO", label,
                "Nenhuma linha de item foi reconhecida no PDF. "
                "Se o layout for diferente do padrão, revise manualmente."
            ))
        for w in inv.warnings:
            if "OCR" not in w:
                findings.append(Finding("AVISO", label, w))

    return findings


def summarize(findings: List[Finding]) -> str:
    n_ok = sum(1 for f in findings if f.level == "OK")
    n_div = sum(1 for f in findings if f.level == "DIVERGENCIA")
    n_warn = sum(1 for f in findings if f.level == "AVISO")
    return f"{n_ok} OK | {n_div} divergência(s) | {n_warn} aviso(s)"
