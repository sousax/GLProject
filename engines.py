# -*- coding: utf-8 -*-
"""
engines.py
------------
Lógica compartilhada de escolha/rotação entre motores de extração
(regex + os três provedores de IA). Usado tanto pelo `main.py` (CLI) quanto
pelo `app.py` (interface web), pra não duplicar a mesma lógica nos dois.
"""

from invoice_extractor import extract_invoice

PROVIDERS = ["claude", "gemini", "openai"]


def needs_ai_fallback(inv) -> bool:
    """Heurística simples: se os campos essenciais não saíram, ou nenhum
    item foi reconhecido, a extração por regex provavelmente falhou
    (layout diferente do mapeado)."""
    critical_missing = sum([
        inv.invoice_number is None,
        inv.customer_po is None,
        inv.total_value is None,
        len(inv.line_items) == 0,
    ])
    return critical_missing >= 2


def extract_with_provider(path, provider, model=None):
    if provider == "gemini":
        from gemini_extractor import extract_invoice_gemini, DEFAULT_MODEL
        return extract_invoice_gemini(path, model=model or DEFAULT_MODEL)
    elif provider == "openai":
        from openai_extractor import extract_invoice_openai, DEFAULT_MODEL
        return extract_invoice_openai(path, model=model or DEFAULT_MODEL)
    else:
        from ai_extractor import extract_invoice_ai, DEFAULT_MODEL
        return extract_invoice_ai(path, model=model or DEFAULT_MODEL)


def extract_with_rotation(path, providers, model=None, log=print):
    """Tenta os provedores em `providers`, em ordem; se um falhar (limite
    de requisição, chave não configurada, erro transitório...), passa pro
    próximo em vez de desistir da fatura inteira."""
    last_err = None
    for i, provider in enumerate(providers):
        try:
            inv = extract_with_provider(path, provider, model)
            if i > 0:
                inv.warnings.insert(
                    0, f"Provedor '{providers[0]}' indisponível/em limite; usado fallback '{provider}'."
                )
            return inv
        except Exception as e:
            log(f"      aviso: provedor '{provider}' falhou ({e}); tentando próximo da lista...")
            last_err = e
            continue
    raise RuntimeError(f"Todos os provedores de IA falharam ({', '.join(providers)}). Último erro: {last_err}")


def extract_one(path, engine, providers, model=None, force_ocr=False, log=print):
    """
    Extrai uma fatura de acordo com o `engine` escolhido:
      - 'regex': só regex.
      - 'claude' / 'gemini' / 'openai' / 'ai' (alias de claude): usa esse
        provedor primeiro, com fallback pros demais de `providers` se falhar.
      - 'auto': tenta regex; se ficar incompleto, cai pra IA (ordem de `providers`).
    """
    ai_engines = ("ai", "claude", "gemini", "openai")
    if engine in ai_engines:
        provider = "claude" if engine == "ai" else engine
        ordered = [provider] + [p for p in providers if p != provider]
        return extract_with_rotation(path, ordered, model, log=log)

    inv = extract_invoice(path, force_ocr=force_ocr)

    if engine == "auto" and needs_ai_fallback(inv):
        log(f"      -> extração por regex incompleta, tentando IA (ordem: {', '.join(providers)})...")
        try:
            inv_ai = extract_with_rotation(path, providers, model, log=log)
            inv_ai.warnings.insert(0, "Extraído via fallback automático de IA (regex falhou nesse layout).")
            return inv_ai
        except Exception as e:
            inv.warnings.append(f"Fallback de IA também falhou (todos os provedores): {e}")
            return inv

    return inv
