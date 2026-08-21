# -*- coding: utf-8 -*-
"""
app.py — Interface web da automação de GL (Streamlit)
--------------------------------------------------------
Roda localmente no seu navegador, sem precisar usar o terminal depois de
iniciado: arraste as faturas em PDF (e, opcionalmente, a packing list em
Excel), escolha o motor de extração, clique em "Processar" e baixe o Excel
consolidado com o resumo pronto para o GL + a aba de validação (cross-check).

COMO RODAR:

    pip install streamlit
    streamlit run app.py

Isso abre automaticamente uma aba no seu navegador (normalmente em
http://localhost:8501). Para parar, feche o terminal ou aperte Ctrl+C.
"""

import os
import tempfile
from io import BytesIO

import pandas as pd
import streamlit as st

from invoice_extractor import extract_invoice
from packing_list_extractor import extract_packing_list
from cross_check import run_cross_check, summarize
from gl_report import build_gl_workbook
from pdf_splitter import analyze_pdf, is_batch_pdf, split_pdf
from engines import extract_one, PROVIDERS

st.set_page_config(page_title="Automação de GL", page_icon="📦", layout="wide")

LEVEL_ICON = {"OK": "✅", "DIVERGENCIA": "❌", "AVISO": "⚠️"}


# ---------------------------------------------------------------- Sidebar
st.sidebar.header("Configuração")

engine = st.sidebar.selectbox(
    "Motor de extração",
    options=["regex", "auto", "claude", "gemini", "openai"],
    index=0,
    help=(
        "regex: grátis, só funciona em layouts já mapeados (ex.: ABB).\n\n"
        "claude / gemini / openai: usa IA, funciona com qualquer layout de fornecedor, requer chave de API "
        "('openai' também serve pra Azure OpenAI, Ollama local e outros compatíveis — veja o README).\n\n"
        "auto: tenta regex primeiro; se faltar campo essencial, refaz só aquela fatura com IA."
    ),
)

api_keys_needed = []
if engine in ("claude", "gemini", "openai", "auto"):
    st.sidebar.caption(
        "Ordem de tentativa dos provedores de IA — se um bater no limite de requisição "
        "(ex.: Gemini), passa automaticamente para o próximo da lista."
    )
    default_order = [engine] + [p for p in PROVIDERS if p != engine] if engine != "auto" else PROVIDERS
    provider_order = st.sidebar.multiselect(
        "Ordem dos provedores (arraste para reordenar)",
        options=PROVIDERS,
        default=default_order,
    )
    if not provider_order:
        provider_order = default_order

    for provider in provider_order:
        label = {"claude": "Chave da API Anthropic (Claude)",
                  "gemini": "Chave da API Google (Gemini)",
                  "openai": "Chave da API OpenAI-compatível (OpenAI/Azure/Ollama/OpenRouter...)"}[provider]
        env_var = {"claude": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY"}[provider]
        default_key = os.environ.get(env_var, "")
        api_key = st.sidebar.text_input(label, value=default_key, type="password", key=f"key_{provider}")
        if api_key:
            os.environ[env_var] = api_key
        if provider == "openai":
            base_url = st.sidebar.text_input(
                "OPENAI_BASE_URL (deixe em branco para usar a OpenAI oficial; "
                "use http://localhost:11434/v1 para Ollama local, etc.)",
                value=os.environ.get("OPENAI_BASE_URL", ""),
            )
            if base_url:
                os.environ["OPENAI_BASE_URL"] = base_url
    st.sidebar.caption(
        "As chaves ficam só na memória desta sessão do navegador/processo — não são salvas em disco."
    )
else:
    provider_order = PROVIDERS

st.sidebar.divider()
st.sidebar.subheader("Dados do GL")
importador = st.sidebar.text_input("Importador", "ABB ELETRIFICAÇÃO LTDA")
cnpj = st.sidebar.text_input("CNPJ", "33.449.988/0001-20")
modal = st.sidebar.text_input("Modal", "MARITIMO")
centro = st.sidebar.text_input("Centro", "")
incoterm_manual = st.sidebar.text_input(
    "Incoterm (deixe em branco para usar o detectado automaticamente na fatura)", ""
)
ncm = st.sidebar.text_input("NCM (deixe em branco para usar o detectado automaticamente)", "")

# ------------------------------------------------------------------ Corpo
st.title("📦 Automação de envio de GL")
st.write(
    "Envie uma ou mais faturas (PDF) e, se quiser cross-check automático, "
    "a packing list em Excel. O relatório final soma o valor de todas as "
    "faturas do lote e sinaliza divergências entre os documentos."
)

col1, col2 = st.columns(2)
with col1:
    invoice_files = st.file_uploader(
        "Faturas (PDF) — pode selecionar várias", type=["pdf"], accept_multiple_files=True
    )
with col2:
    pl_file = st.file_uploader("Packing list (Excel) — opcional", type=["xlsx", "xlsm"])
    pl_sheet = st.text_input("Nome da aba da packing list", value="PL")

process = st.button("🚀 Processar", type="primary", disabled=not invoice_files)

if process:
    with tempfile.TemporaryDirectory() as tmpdir:
        # --- salva os PDFs enviados em disco (os extratores esperam caminho de arquivo) ---
        invoice_paths = []
        for uf in invoice_files:
            path = os.path.join(tmpdir, uf.name)
            with open(path, "wb") as f:
                f.write(uf.getbuffer())
            invoice_paths.append(path)

        pl_path = None
        if pl_file is not None:
            pl_path = os.path.join(tmpdir, pl_file.name)
            with open(pl_path, "wb") as f:
                f.write(pl_file.getbuffer())

        # ------------------------------------- detecta PDFs em lote e separa
        # (alguns exports de ERP juntam várias faturas num único PDF; se
        # mandarmos isso pro motor de IA como "uma fatura só", o JSON de
        # saída fica gigante e a resposta vem cortada no meio)
        expanded_paths = []
        for path in invoice_paths:
            fname = os.path.basename(path)
            try:
                infos = analyze_pdf(path)
            except Exception:
                expanded_paths.append(path)
                continue
            if is_batch_pdf(infos):
                sub_paths = split_pdf(path, tmpdir)
                st.info(f"📄 **{fname}** contém {len(sub_paths)} faturas diferentes num único PDF — separando antes de extrair.")
                expanded_paths.extend(sub_paths)
            else:
                expanded_paths.append(path)
        invoice_paths = expanded_paths

        # --------------------------------------------------------- extração
        invoices = []
        progress = st.progress(0.0, text="Lendo faturas...")
        for i, path in enumerate(invoice_paths):
            fname = os.path.basename(path)
            progress.progress(i / len(invoice_paths), text=f"Lendo {fname}...")
            try:
                inv = extract_one(path, engine, provider_order, log=lambda m: None)
            except Exception as e:
                st.error(f"Falha ao processar **{fname}**: {e}")
                continue
            invoices.append(inv)
        progress.progress(1.0, text="Concluído.")

        if not invoices:
            st.stop()

        # ---------------------------------------------------- packing list
        pl_summaries = None
        if pl_path:
            try:
                pl_summaries = extract_packing_list(pl_path, sheet_name=pl_sheet)
                st.success(f"Packing list lida: {len(pl_summaries)} combinação(ões) PO-Item encontradas.")
            except Exception as e:
                st.warning(f"Não foi possível ler a packing list: {e}")

        # ------------------------------------------------------ cross-check
        findings = run_cross_check(invoices, pl_summaries)

        # ------------------------------------------------------------ UI: resumo
        st.subheader("Resumo das faturas")
        df = pd.DataFrame([inv.to_dict() for inv in invoices])
        st.dataframe(df, use_container_width=True)

        currencies = {inv.currency for inv in invoices if inv.currency}
        total = sum(inv.total_value for inv in invoices if inv.total_value is not None)
        currency_label = list(currencies)[0] if len(currencies) == 1 else "/".join(sorted(currencies))
        st.metric(f"Valor total somado ({len(invoices)} fatura(s))", f"{currency_label} {total:,.2f}")

        st.subheader(f"Cross-check — {summarize(findings)}")
        for f in findings:
            icon = LEVEL_ICON.get(f.level, "•")
            line = f"{icon} **{f.invoice}** — {f.message}"
            if f.level == "DIVERGENCIA":
                st.error(line)
            elif f.level == "AVISO":
                st.warning(line)
            else:
                st.success(line)

        # -------------------------------------------------------- gera Excel
        wb = build_gl_workbook(
            invoices, pl_summaries, findings,
            importador=importador, cnpj=cnpj, modal=modal,
            centro=centro, incoterm=incoterm_manual, ncm=ncm,
        )
        buf = BytesIO()
        wb.save(buf)
        buf.seek(0)

        st.subheader("Baixar relatório")
        st.download_button(
            "⬇️ Baixar Excel do GL (GL_gerado.xlsx)",
            data=buf,
            file_name="GL_gerado.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
