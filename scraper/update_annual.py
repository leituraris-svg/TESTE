#!/usr/bin/env python3
"""
Atualiza data/anuais.json com dados que só fazem sentido buscar 1x/ano:

  - Lucro Líquido Consolidado dos últimos N anos (auditado)
  - Número total de ações e ações em tesouraria (-> ex-tesouraria)

Diferente de update_data.py (que roda 1x/dia e usa o Fundamentus), este
script usa os DADOS ABERTOS OFICIAIS da CVM (dados.cvm.gov.br):

  - Formulário DFP -> DRE Consolidada:  Lucro Líquido Consolidado por ano
  - Formulário FRE -> Ações em Tesouraria (Item 19.3 do Anexo 24)
  - Formulário DFP -> Composição do Capital: nº total de ações
  - Formulário FCA -> Valores Mobiliários: mapa ticker (B3) -> CNPJ

Uso:
    pip install requests pandas
    python scraper/update_annual.py

Recomendado rodar 1x/ano (ex.: GitHub Actions com cron anual, tipo
"0 6 1 5 *" = todo 1º de maio, depois que a maioria das empresas já
entregou o DFP do ano anterior).

IMPORTANTE — antes do primeiro uso real:
Os nomes de arquivo/coluna dos datasets da CVM foram confirmados via
documentação oficial e exemplos públicos, EXCETO o arquivo de
"Composição do Capital" dentro do DFP, cujo nome exato eu não consegui
confirmar sem baixar o zip de verdade. Se `fetch_total_acoes_por_cnpj()`
não achar o arquivo, ele imprime a lista de arquivos do zip no console —
copie essa lista e me mande que eu ajusto o padrão de busca.
"""

import io
import json
import sys
import time
import unicodedata
import zipfile
from datetime import datetime, timezone

import pandas as pd
import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

CVM_DFP_BASE = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/DFP/DADOS"
CVM_FRE_BASE = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FRE/DADOS"
CVM_FCA_BASE = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FCA/DADOS"

OUT_PATH = "data/anuais.json"
COTACOES_PATH = "data/cotacoes.json"  # usado só pra saber quais tickers existem hoje

ANOS_LUCRO = 5  # quantos anos de Lucro Líquido Consolidado buscar
ANO_ATUAL = datetime.now().year

# Código da conta "Lucro/Prejuízo Consolidado do Período" no plano de
# contas padrão da CVM. Mudou de 3.09 (até ~2019) para 3.11 (2020 em
# diante) — checamos os dois e confirmamos pela descrição da conta
# (DS_CONTA), pra não depender só do código caso mude de novo.
CODIGOS_LUCRO_LIQUIDO = {"3.11", "3.09"}


def _norm(s):
    """minúsculas, sem acento — pra achar coluna/arquivo mesmo com
    variação de nome entre anos."""
    if s is None:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = s.encode("ascii", "ignore").decode("ascii")
    return s.strip().lower()


def _download_zip(url, retries=3, timeout=60):
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            return zipfile.ZipFile(io.BytesIO(r.content))
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"  tentativa {attempt}/{retries} falhou ({url}): {e}", file=sys.stderr)
            time.sleep(3 * attempt)
    print(f"  aviso: não foi possível baixar {url} ({last_err}). Pulando.", file=sys.stderr)
    return None


def _read_csv_from_zip(zf, name_contains):
    """Acha, dentro do zip, o primeiro arquivo cujo nome contém
    `name_contains` (case-insensitive) e lê como DataFrame. Os CSVs da
    CVM usam ';' como separador e latin-1 como encoding."""
    candidates = [n for n in zf.namelist() if name_contains.lower() in n.lower()]
    if not candidates:
        return None
    with zf.open(candidates[0]) as f:
        return pd.read_csv(f, sep=";", encoding="latin-1", dtype=str)


# --------------------------------------------------------------------------
# 1) Mapeamento ticker (B3) -> CNPJ da companhia
# --------------------------------------------------------------------------
def build_ticker_cnpj_map():
    """A CVM identifica empresas por CNPJ/código CVM, não por ticker. O
    Formulário Cadastral (FCA) tem o arquivo 'valor_mobiliario', que lista
    o código de negociação de cada classe de ação por empresa."""
    print("Baixando FCA (cadastro) para montar mapa ticker -> CNPJ...")
    zf = _download_zip(f"{CVM_FCA_BASE}/fca_cia_aberta_{ANO_ATUAL}.zip")
    if zf is None:
        # No início do ano, o FCA do ano corrente pode ainda não ter sido
        # entregue por ninguém; tenta o ano anterior.
        zf = _download_zip(f"{CVM_FCA_BASE}/fca_cia_aberta_{ANO_ATUAL - 1}.zip")
    if zf is None:
        raise RuntimeError("Não foi possível baixar o FCA para montar o mapa de tickers.")

    df = _read_csv_from_zip(zf, "valor_mobiliario")
    if df is None:
        raise RuntimeError(
            "Arquivo 'valor_mobiliario' não encontrado dentro do FCA. "
            f"Conteúdo do zip: {zf.namelist()}"
        )

    df.columns = [_norm(c) for c in df.columns]
    col_ticker = next((c for c in df.columns if "codigo_negociacao" in c), None)
    col_cnpj = next((c for c in df.columns if c == "cnpj_cia" or "cnpj" in c), None)
    if not col_ticker or not col_cnpj:
        raise RuntimeError(
            "Não achei as colunas de ticker/CNPJ no FCA. "
            f"Colunas disponíveis: {list(df.columns)}"
        )

    mapping = {}
    for _, row in df.iterrows():
        tk = str(row.get(col_ticker) or "").strip().upper()
        cnpj = str(row.get(col_cnpj) or "").strip()
        if tk and tk != "NAN" and cnpj and cnpj != "NAN":
            mapping[tk] = cnpj
    print(f"  {len(mapping)} tickers mapeados para CNPJ.")
    return mapping


# --------------------------------------------------------------------------
# 2) Lucro Líquido Consolidado — últimos N anos
# --------------------------------------------------------------------------
def fetch_lucro_liquido_por_cnpj(anos_zip):
    """Retorna {cnpj: {ano: valor}} com o Lucro/Prejuízo Consolidado do
    Período de cada ano, lendo direto da DRE consolidada de cada DFP."""
    resultado = {}
    for ano_zip in anos_zip:
        print(f"Baixando DFP {ano_zip} (DRE consolidada)...")
        zf = _download_zip(f"{CVM_DFP_BASE}/dfp_cia_aberta_{ano_zip}.zip")
        if zf is None:
            continue
        df = _read_csv_from_zip(zf, "DRE_con")
        if df is None:
            print(f"  aviso: DRE_con não encontrada no DFP {ano_zip}. "
                  f"Conteúdo: {zf.namelist()}", file=sys.stderr)
            continue

        df["CD_CONTA"] = df["CD_CONTA"].astype(str).str.strip()
        df["DS_CONTA_NORM"] = df["DS_CONTA"].map(_norm)
        df["VL_CONTA_NUM"] = pd.to_numeric(df["VL_CONTA"], errors="coerce")
        df["ORDEM_NORM"] = df["ORDEM_EXERC"].astype(str).str.strip().str.upper()

        mask = (
            (df["ORDEM_NORM"] == "ÚLTIMO")
            & df["CD_CONTA"].isin(CODIGOS_LUCRO_LIQUIDO)
            & (df["DS_CONTA_NORM"].str.contains("lucro") | df["DS_CONTA_NORM"].str.contains("prejuizo"))
        )
        sub = df[mask]

        # o exercício reportado como "ÚLTIMO" no zip do ano X é o ano X-1
        ano_exercicio = ano_zip - 1
        contados = 0
        for _, row in sub.iterrows():
            valor = row["VL_CONTA_NUM"]
            if pd.isna(valor):
                continue
            cnpj = str(row["CNPJ_CIA"]).strip()
            resultado.setdefault(cnpj, {})[str(ano_exercicio)] = float(valor)
            contados += 1
        print(f"  {contados} empresas com Lucro Líquido {ano_exercicio} encontradas.")
    return resultado


# --------------------------------------------------------------------------
# 3) Ações em tesouraria
# --------------------------------------------------------------------------
def fetch_tesouraria_por_cnpj():
    print(f"Baixando FRE {ANO_ATUAL} (ações em tesouraria)...")
    zf = _download_zip(f"{CVM_FRE_BASE}/fre_cia_aberta_{ANO_ATUAL}.zip")
    if zf is None:
        zf = _download_zip(f"{CVM_FRE_BASE}/fre_cia_aberta_{ANO_ATUAL - 1}.zip")
    if zf is None:
        print("  aviso: não consegui baixar o FRE. Tesouraria ficará vazia.", file=sys.stderr)
        return {}

    df = _read_csv_from_zip(zf, "valor_mobiliario_tesouraria_ultimo_exercicio")
    if df is None:
        print(f"  aviso: arquivo de tesouraria não encontrado no FRE. "
              f"Conteúdo: {zf.namelist()}", file=sys.stderr)
        return {}

    df.columns = [_norm(c) for c in df.columns]
    col_cnpj = next((c for c in df.columns if "cnpj" in c), None)
    col_qtd = next((c for c in df.columns if "quantidade" in c), None)
    if not col_cnpj or not col_qtd:
        print(f"  aviso: colunas de tesouraria não reconhecidas. "
              f"Colunas: {list(df.columns)}", file=sys.stderr)
        return {}

    resultado = {}
    for _, row in df.iterrows():
        cnpj = str(row.get(col_cnpj) or "").strip()
        qtd = pd.to_numeric(row.get(col_qtd), errors="coerce")
        if not cnpj or pd.isna(qtd):
            continue
        # soma ON + PN + outras classes, se houver mais de uma linha por empresa
        resultado[cnpj] = resultado.get(cnpj, 0.0) + float(qtd)
    print(f"  {len(resultado)} empresas com dado de tesouraria.")
    return resultado


# --------------------------------------------------------------------------
# 4) Número total de ações (Composição do Capital)
# --------------------------------------------------------------------------
def fetch_total_acoes_por_cnpj():
    print(f"Baixando DFP {ANO_ATUAL} (composição do capital)...")
    zf = _download_zip(f"{CVM_DFP_BASE}/dfp_cia_aberta_{ANO_ATUAL}.zip")
    if zf is None:
        zf = _download_zip(f"{CVM_DFP_BASE}/dfp_cia_aberta_{ANO_ATUAL - 1}.zip")
    if zf is None:
        print("  aviso: não consegui baixar o DFP para composição do capital.", file=sys.stderr)
        return {}

    # Nome exato não confirmado — tenta os padrões mais prováveis.
    df = None
    for pattern in ("composicao_capital", "capital_social"):
        df = _read_csv_from_zip(zf, pattern)
        if df is not None:
            break
    if df is None:
        print(
            "  AVISO: não achei o arquivo de composição do capital dentro do zip. "
            "Me manda essa lista que eu ajusto o padrão de busca:\n  "
            f"{zf.namelist()}",
            file=sys.stderr,
        )
        return {}

    df.columns = [_norm(c) for c in df.columns]
    col_cnpj = next((c for c in df.columns if "cnpj" in c), None)
    col_on = next((c for c in df.columns if "ordinaria" in c and ("qtd" in c or "quant" in c)), None)
    col_pn = next((c for c in df.columns if "preferencial" in c and ("qtd" in c or "quant" in c)), None)
    col_total = next(
        (c for c in df.columns if "total" in c and ("qtd" in c or "quant" in c or "acoes" in c)), None
    )
    if not col_cnpj or not (col_total or col_on or col_pn):
        print(f"  aviso: colunas de composição do capital não reconhecidas. "
              f"Colunas: {list(df.columns)}", file=sys.stderr)
        return {}

    resultado = {}
    for _, row in df.iterrows():
        cnpj = str(row.get(col_cnpj) or "").strip()
        if not cnpj:
            continue
        if col_total:
            total = pd.to_numeric(row.get(col_total), errors="coerce")
        else:
            on = pd.to_numeric(row.get(col_on), errors="coerce") if col_on else 0
            pn = pd.to_numeric(row.get(col_pn), errors="coerce") if col_pn else 0
            total = (on or 0) + (pn or 0)
        if pd.isna(total) or total <= 0:
            continue
        resultado[cnpj] = float(total)
    print(f"  {len(resultado)} empresas com nº total de ações.")
    return resultado


# --------------------------------------------------------------------------
def main():
    anos_exercicio = list(range(ANO_ATUAL - ANOS_LUCRO, ANO_ATUAL))  # ex: 2021..2025
    anos_zip = [a + 1 for a in anos_exercicio]  # DFP entregue no ano seguinte ao exercício

    ticker_cnpj = build_ticker_cnpj_map()
    lucro_por_cnpj = fetch_lucro_liquido_por_cnpj(anos_zip)
    tesouraria_por_cnpj = fetch_tesouraria_por_cnpj()
    total_por_cnpj = fetch_total_acoes_por_cnpj()

    try:
        with open(COTACOES_PATH, "r", encoding="utf-8") as f:
            tickers_ativos = set(json.load(f).get("acoes", {}).keys())
    except Exception:
        tickers_ativos = set(ticker_cnpj.keys())  # fallback: todo mundo mapeado

    saida = {}
    for tk in tickers_ativos:
        cnpj = ticker_cnpj.get(tk)
        if not cnpj:
            continue
        lucro_anos = lucro_por_cnpj.get(cnpj)
        total = total_por_cnpj.get(cnpj)
        tesouraria = tesouraria_por_cnpj.get(cnpj, 0.0)

        if not lucro_anos and not total:
            continue  # nada de novo pra essa empresa, não polui o JSON

        entry = {}
        if lucro_anos:
            entry["lucroLiquidoAnos"] = lucro_anos
        if total:
            entry["acoesTotal"] = total
            entry["acoesTesouraria"] = tesouraria
            entry["acoesExTesouraria"] = max(total - tesouraria, 0)
        saida[tk] = entry

    if len(saida) < 50:
        raise RuntimeError(
            f"Resultado suspeito (apenas {len(saida)} empresas). "
            "Abortando para não sobrescrever o data/anuais.json anterior com algo quebrado."
        )

    data = {
        "atualizado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fonte": "CVM - dados.cvm.gov.br (DFP + FRE)",
        "dados": saida,
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Salvo em {OUT_PATH} — {len(saida)} empresas.")


if __name__ == "__main__":
    main()
