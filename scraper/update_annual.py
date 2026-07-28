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
documentação oficial, exemplos públicos e por uma execução real do
script. O nº total de ações e as ações em tesouraria vêm juntos do
arquivo 'composicao_capital' dentro do próprio DFP (a antiga fonte prevista
no FRE foi descontinuada pela CVM em 2016).

CONVENÇÃO DE ANO DO ZIP (confirmado observando o histórico de tamanhos
dos arquivos no índice público da CVM): o número no nome
'dfp_cia_aberta_AAAA.zip' é o ANO DO PRÓPRIO EXERCÍCIO/BALANÇO, não o
ano de entrega. Esse arquivo fica quase vazio logo no início do ano
seguinte e só se completa perto do prazo de entrega (~31/mar do ano
seguinte ao exercício). Por isso: (1) nunca usamos o zip do ANO_ATUAL
pra nada que precise estar completo — ele corresponde ao exercício
ainda em andamento; (2) pegamos sempre os últimos N anos JÁ FECHADOS
(ANO_ATUAL-N até ANO_ATUAL-1).
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

# dados.cvm.gov.br responde com endereço IPv6, e alguns runners do GitHub
# Actions têm rota IPv6 quebrada pra esse destino (dá "Network is
# unreachable" mesmo com o site no ar). Força tudo a resolver só em IPv4.
import socket
import urllib3.util.connection as _urllib3_cn

def _allowed_gai_family():
    return socket.AF_INET

_urllib3_cn.allowed_gai_family = _allowed_gai_family

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

CVM_DFP_BASE = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/DFP/DADOS"
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


def _download_zip(url, retries=5, timeout=90):
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            return zipfile.ZipFile(io.BytesIO(r.content))
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"  tentativa {attempt}/{retries} falhou ({url}): {e}", file=sys.stderr)
            time.sleep(10 * attempt)  # espera crescente: 10s, 20s, 30s...
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
        ano_exercicio = ano_zip  # o nome do zip já É o ano do exercício (ver nota no topo do arquivo)
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
# 3) Número total de ações e ações em tesouraria — mesmo arquivo
# --------------------------------------------------------------------------
def fetch_capital_por_cnpj():
    """O arquivo 'composicao_capital' do DFP já traz total de ações E ações em
    tesouraria juntos (colunas qt_acao_total_cap_integr / qt_acao_total_tesouro).
    Não existe mais um arquivo de tesouraria separado no FRE — esse item foi
    descontinuado pela CVM em 2016."""
    # ANO_ATUAL ainda está em andamento (o exercício só fecha em 31/dez), então o
    # zip desse ano está quase vazio a maior parte do tempo. Usa o ano anterior,
    # que já deve estar com o prazo de entrega vencido (~31/mar).
    ano_capital = ANO_ATUAL - 1
    print(f"Baixando DFP {ano_capital} (composição do capital)...")
    zf = _download_zip(f"{CVM_DFP_BASE}/dfp_cia_aberta_{ano_capital}.zip")
    if zf is None:
        zf = _download_zip(f"{CVM_DFP_BASE}/dfp_cia_aberta_{ano_capital - 1}.zip")
    if zf is None:
        print("  aviso: não consegui baixar o DFP para composição do capital.", file=sys.stderr)
        return {}

    df = _read_csv_from_zip(zf, "composicao_capital")
    if df is None:
        print(
            "  AVISO: não achei o arquivo de composição do capital dentro do zip. "
            f"Conteúdo do zip: {zf.namelist()}",
            file=sys.stderr,
        )
        return {}

    df.columns = [_norm(c) for c in df.columns]
    col_cnpj = next((c for c in df.columns if "cnpj" in c), None)
    col_versao = next((c for c in df.columns if c == "versao"), None)
    col_total = next((c for c in df.columns if "acao" in c and "total" in c and "tesour" not in c), None)
    col_tesouro = next((c for c in df.columns if "acao" in c and "total" in c and "tesour" in c), None)
    col_ordin = next((c for c in df.columns if "acao" in c and "ordin" in c and "tesour" not in c), None)
    col_pref = next((c for c in df.columns if "acao" in c and "pref" in c and "tesour" not in c), None)

    if not col_cnpj or not (col_total or (col_ordin and col_pref)):
        print(f"  aviso: colunas de composição do capital não reconhecidas. "
              f"Colunas: {list(df.columns)}", file=sys.stderr)
        return {}

    for c in (col_total, col_tesouro, col_ordin, col_pref):
        if c:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    # Uma empresa pode ter mais de uma versão do documento entregue
    # (reapresentações); fica só com a versão mais recente de cada CNPJ.
    if col_versao:
        df["_versao_num"] = pd.to_numeric(df[col_versao], errors="coerce").fillna(0)
        df = df.sort_values("_versao_num").drop_duplicates(subset=[col_cnpj], keep="last")

    resultado = {}
    for _, row in df.iterrows():
        cnpj = str(row.get(col_cnpj) or "").strip()
        if not cnpj:
            continue
        total = row.get(col_total) if col_total else (
            (row.get(col_ordin) or 0) + (row.get(col_pref) or 0)
        )
        if pd.isna(total) or not total or total <= 0:
            continue
        tesouraria = row.get(col_tesouro) if col_tesouro else 0
        tesouraria = 0.0 if pd.isna(tesouraria) else float(tesouraria)
        resultado[cnpj] = {"total": float(total), "tesouraria": tesouraria}
    print(f"  {len(resultado)} empresas com composição do capital.")
    return resultado


# --------------------------------------------------------------------------
def main():
    # Últimos N exercícios já FECHADOS (o ano corrente ainda não fechou, então
    # não teria DFP nenhum na prática — ver nota no topo do arquivo).
    anos_zip = list(range(ANO_ATUAL - ANOS_LUCRO, ANO_ATUAL))  # ex: 2021..2025 se ANO_ATUAL=2026

    ticker_cnpj = build_ticker_cnpj_map()
    lucro_por_cnpj = fetch_lucro_liquido_por_cnpj(anos_zip)
    capital_por_cnpj = fetch_capital_por_cnpj()

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
        capital = capital_por_cnpj.get(cnpj)

        if not lucro_anos and not capital:
            continue  # nada de novo pra essa empresa, não polui o JSON

        entry = {}
        if lucro_anos:
            entry["lucroLiquidoAnos"] = lucro_anos
        if capital:
            total = capital["total"]
            tesouraria = capital["tesouraria"]
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
