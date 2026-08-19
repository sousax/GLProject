# -*- coding: utf-8 -*-
"""
main.py
--------
CLI da automação de envio de GL.

USO BÁSICO (uma ou várias faturas em PDF, com packing list para cross-check):

    python main.py \
        --invoices fatura1.pdf fatura2.pdf \
        --packing-list BRELE260728SEA.xlsx --pl-sheet PL \
        --importador "ABB ELETRIFICACAO - SOROCABA" \
        --cnpj "33.449.988/0001-20" \
        --modal "MARITIMO" \
        --ncm "8536.20.00" \
        --output GL_gerado.xlsx

Se alguma fatura for uma imagem digitalizada (sem texto selecionável),
o OCR é acionado automaticamente — não é preciso indicar nada.

Todos os parâmetros (--importador, --cnpj, --modal, --ncm) são opcionais;
se omitidos, ficam em branco no relatório para preenchimento manual.
"""

import argparse
import os
import sys
import tempfile

from invoice_extractor import extract_invoice
from packing_list_extractor import extract_packing_list
from cross_check import run_cross_check, summarize
from gl_report import build_gl_workbook
from pdf_splitter import analyze_pdf, is_batch_pdf, split_pdf
from engines import extract_one


def parse_args():
    p = argparse.ArgumentParser(description="Automação de consolidação de faturas para envio de GL")
    p.add_argument("--invoices", nargs="+", required=True, help="Um ou mais PDFs de fatura")
    p.add_argument("--packing-list", help="Excel da packing list (opcional, mas recomendado p/ cross-check)")
    p.add_argument("--pl-sheet", default="PL", help="Nome da aba da packing list (padrão: PL)")
    p.add_argument("--force-ocr", action="store_true", help="Força OCR mesmo em PDFs com texto nativo")
    p.add_argument(
        "--no-split", action="store_true",
        help="Não tenta detectar/separar PDFs em lote (várias faturas concatenadas num único arquivo). "
             "Por padrão a detecção é automática."
    )
    p.add_argument(
        "--engine", choices=["regex", "claude", "gemini", "openai", "ai", "auto"], default="regex",
        help="'regex' (padrão, grátis, só funciona no layout já mapeado em invoice_extractor.py) | "
             "'claude' (API da Anthropic, requer ANTHROPIC_API_KEY) | "
             "'gemini' (API do Google Gemini, requer GEMINI_API_KEY) | "
             "'openai' (qualquer endpoint compatível OpenAI: OpenAI, Azure OpenAI, Ollama local, "
             "OpenRouter, Groq... requer OPENAI_API_KEY e, se não for a OpenAI, OPENAI_BASE_URL) | "
             "'ai' (alias de 'claude', mantido por compatibilidade) | "
             "'auto' (tenta 'regex' primeiro; se a extração falhar/ficar incompleta, refaz aquela "
             "fatura com IA, girando pela lista de --ai-providers)"
    )
    p.add_argument(
        "--ai-providers", default="claude,gemini,openai",
        help="Lista de provedores de IA, em ordem de tentativa, separados por vírgula "
             "(ex.: 'gemini,claude,openai'). Se o provedor escolhido em --engine (ou o primeiro "
             "da lista, no modo 'auto') bater em limite de requisição/erro, passa automaticamente "
             "para o próximo da lista, em vez de travar a fatura toda. "
             "Padrão: claude,gemini,openai"
    )
    p.add_argument("--ai-model", default=None,
                    help="Modelo a usar (padrão: claude-sonnet-5 para Claude, gemini-2.5-flash para Gemini)")
    p.add_argument("--importador", default="")
    p.add_argument("--cnpj", default="")
    p.add_argument("--modal", default="")
    p.add_argument("--ncm", default="")
    p.add_argument("--output", default="GL_gerado.xlsx", help="Caminho do Excel de saída")
    return p.parse_args()


def _needs_ai_fallback(inv) -> bool:
    from engines import needs_ai_fallback
    return needs_ai_fallback(inv)


def _extract_one(path, args):
    providers = [p.strip() for p in args.ai_providers.split(",") if p.strip()]
    return extract_one(path, args.engine, providers, model=args.ai_model, force_ocr=args.force_ocr)


def _expand_batch_pdfs(paths, split_dir, no_split):
    """Detecta PDFs em lote (várias faturas concatenadas num único arquivo)
    e devolve a lista de caminhos expandida, um arquivo por fatura."""
    expanded = []
    for path in paths:
        if no_split:
            expanded.append(path)
            continue
        try:
            infos = analyze_pdf(path)
        except Exception as e:
            print(f"  aviso: não foi possível analisar {os.path.basename(path)} para detecção de lote ({e})")
            expanded.append(path)
            continue
        if is_batch_pdf(infos):
            sub_paths = split_pdf(path, split_dir)
            print(f"  {os.path.basename(path)}: detectado PDF em LOTE com {len(sub_paths)} fatura(s) — separando antes de extrair.")
            expanded.extend(sub_paths)
        else:
            expanded.append(path)
    return expanded


def main():
    args = parse_args()

    with tempfile.TemporaryDirectory() as split_dir:
        print("Analisando arquivos de entrada (detecção de PDFs em lote)...")
        invoice_paths = _expand_batch_pdfs(args.invoices, split_dir, args.no_split)

        print(f"\nLendo {len(invoice_paths)} fatura(s)... (engine: {args.engine})")
        invoices = []
        for path in invoice_paths:
            try:
                inv = _extract_one(path, args)
            except (RuntimeError, ImportError) as e:
                print(f"\nERRO: {e}")
                sys.exit(1)
            invoices.append(inv)
            flag = f" [{inv.extraction_method}]" if inv.extraction_method != "text" else ""
            print(f"  - {inv.source_file}{flag}: fatura {inv.invoice_number}, "
                  f"PO {inv.customer_po}, incoterm {inv.incoterm}, "
                  f"valor {inv.currency} {inv.total_value}")
            for w in inv.warnings:
                print(f"      aviso: {w}")

        pl_summaries = None
        if args.packing_list:
            print(f"\nLendo packing list: {args.packing_list} (aba '{args.pl_sheet}')...")
            try:
                pl_summaries = extract_packing_list(args.packing_list, sheet_name=args.pl_sheet)
                print(f"  {len(pl_summaries)} combinação(ões) PO-Item encontradas na packing list.")
            except Exception as e:
                print(f"  ERRO ao ler packing list: {e}")

        print("\nRodando cross-check...")
        findings = run_cross_check(invoices, pl_summaries)
        for f in findings:
            print(f"  [{f.level}] {f.invoice}: {f.message}")
        print(f"\nResumo do cross-check: {summarize(findings)}")

        valid_total = sum(inv.total_value for inv in invoices if inv.total_value is not None)
        currencies = {inv.currency for inv in invoices if inv.currency}
        currency_label = list(currencies)[0] if len(currencies) == 1 else "/".join(sorted(currencies))
        print(f"\nValor total somado das faturas: {currency_label} {valid_total:,.2f}")

        wb = build_gl_workbook(
            invoices, pl_summaries, findings,
            importador=args.importador, cnpj=args.cnpj,
            modal=args.modal, ncm=args.ncm,
        )
        wb.save(args.output)
        print(f"\nRelatório GL salvo em: {args.output}")

        has_divergence = any(f.level == "DIVERGENCIA" for f in findings)
        if has_divergence:
            print("\n⚠️  Existem DIVERGÊNCIAS pendentes — revise a aba 'Validacao' antes de enviar o GL.")
            sys.exit(2)


if __name__ == "__main__":
    main()
