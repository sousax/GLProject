# -*- coding: utf-8 -*-
"""
openai_extractor.py
----------------------
Extrator genérico para QUALQUER API compatível com o padrão OpenAI (chat
completions + visão via image_url). Isso cobre, com o MESMO código, vários
provedores diferentes — só muda a URL base e o modelo:

  - OpenAI (api.openai.com) — pago, mas tem modelos baratos (gpt-4o-mini)
  - Azure OpenAI — é o que roda por trás do GitHub/Microsoft Copilot; não
    existe uma "API do Copilot" pública de propósito geral pra extração de
    documento, mas se você (ou sua empresa) tem acesso a um deployment
    Azure OpenAI, dá pra apontar pra cá.
  - Ollama, rodando LOCAL na sua máquina — GRÁTIS de verdade e sem limite de
    requisição (o único limite é o hardware do seu computador). Veja como
    configurar no README.
  - OpenRouter — agrega dezenas de modelos, alguns com sufixo ":free"
    (zero custo). Bom pra espalhar a carga e não bater no limite de um
    provedor só.
  - Groq — inferência muito rápida, free tier generoso (mais focado em
    modelos de texto; para visão, confira quais modelos eles hospedam no
    momento).

Como o PDF nesse padrão não é aceito diretamente (diferente da API da
Anthropic/Gemini), as páginas do PDF são renderizadas como imagem e
mandadas como `image_url` em base64 — por isso esse módulo depende de
pdf2image (que já é usado pelo caminho de OCR do projeto).

Requer:
    pip install openai pdf2image
    export OPENAI_API_KEY="..."
    (opcional) export OPENAI_BASE_URL="https://outro-endpoint/v1"
    (opcional) export OPENAI_MODEL="gpt-4o-mini"
"""

import base64
import json
import os

from invoice_extractor import InvoiceData, LineItem
from ai_extractor import EXTRACTION_PROMPT, _strip_json_fences

DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_BASE_URL = None  # None = usa o padrão da OpenAI (api.openai.com)

MAX_PAGES = 6  # limite de páginas enviadas como imagem (custo/tamanho de payload)


def _get_client(base_url: str = None):
    try:
        from openai import OpenAI
    except ImportError as e:
        raise ImportError(
            "O pacote 'openai' não está instalado. Rode: pip install openai"
        ) from e

    api_key = os.environ.get("OPENAI_API_KEY", "ollama-nao-precisa-de-chave-real")
    base_url = base_url or os.environ.get("OPENAI_BASE_URL") or DEFAULT_BASE_URL

    if not os.environ.get("OPENAI_API_KEY") and not base_url:
        raise RuntimeError(
            "Variável de ambiente OPENAI_API_KEY não definida. Se estiver usando um "
            "servidor local (Ollama) ou outro endpoint compatível, defina também "
            "OPENAI_BASE_URL (ex.: http://localhost:11434/v1)."
        )

    kwargs = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    return OpenAI(**kwargs)


def _pdf_to_image_data_urls(pdf_path: str, max_pages: int = MAX_PAGES):
    from pdf2image import convert_from_path

    pages = convert_from_path(pdf_path, dpi=200)[:max_pages]
    data_urls = []
    for page_img in pages:
        from io import BytesIO
        buf = BytesIO()
        page_img.save(buf, format="PNG")
        b64 = base64.standard_b64encode(buf.getvalue()).decode("utf-8")
        data_urls.append(f"data:image/png;base64,{b64}")
    return data_urls


def extract_invoice_openai(
    pdf_path: str,
    model: str = None,
    base_url: str = None,
) -> InvoiceData:
    """
    Extrai os dados de uma fatura em PDF usando qualquer API compatível com
    o padrão OpenAI (veja o módulo acima para as opções de provedor).
    """
    model = model or os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL
    client = _get_client(base_url)

    image_urls = _pdf_to_image_data_urls(pdf_path)

    content = [{"type": "text", "text": EXTRACTION_PROMPT}]
    for url in image_urls:
        content.append({"type": "image_url", "image_url": {"url": url}})

    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": content}],
        temperature=0,
        response_format={"type": "json_object"},
    )

    text = _strip_json_fences(response.choices[0].message.content or "")

    inv = InvoiceData(source_file=os.path.basename(pdf_path), extraction_method="openai", raw_text=text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        inv.warnings.append(f"Falha ao interpretar JSON retornado pelo modelo: {e}")
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

    if len(pos_found) == 1:
        inv.customer_po = next(iter(pos_found))
    elif len(pos_found) > 1:
        inv.customer_po = sorted(pos_found)[0]
        inv.warnings.append(
            f"Fatura tem múltiplas POs distintas nas linhas ({sorted(pos_found)}); "
            f"usando '{inv.customer_po}' como principal."
        )

    if ncm_found:
        from collections import Counter
        inv.ncm = Counter(ncm_found).most_common(1)[0][0]

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
    if len(image_urls) >= MAX_PAGES:
        inv.warnings.append(
            f"Apenas as primeiras {MAX_PAGES} páginas foram enviadas ao modelo "
            f"(limite do motor 'openai'); se a fatura tiver mais páginas com itens, "
            f"eles podem estar faltando."
        )

    return inv
