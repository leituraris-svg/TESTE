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
import re
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
CVM_CAD_URL = "https://dados.cvm.gov.br/dados/CIA_ABERTA/CAD/DADOS/cad_cia_aberta.csv"

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
# Mesma empresa, ticker de negociação diferente do que está no FCA — confirmado
# via diagnóstico real (não é chute): a empresa só cadastra a "unit" ou uma
# classe de ação no FCA, mas Lucro Líquido/capital são dados da empresa toda,
# então mapear pro CNPJ do "irmão" é exato, não aproximação.
TICKER_ALIASES = {
    "KLBN3": "KLBN11", "KLBN4": "KLBN11",   # Klabin: só a unit está no FCA
    "ALUP3": "ALUP11", "ALUP4": "ALUP11",   # Alupar: idem
    "MRSA3B": "MRSA3", "MRSA5B": "MRSA5", "MRSA6B": "MRSA6",  # MRS Log.: sufixo "B" a mais
    "INEP4": "INEP3",     # Inepar: só a classe ON está no FCA
    "AXIA6": "AXIA3",     # Axia: sufixo de classe diferente
    "TXRX3": "TXRX4",     # Têxtil Renaux: idem
    "OBTC3": "OBTC",      # Omega/Brasil Telecom antiga: código sem número no FCA
}


def _parse_fca_valor_mobiliario(zf):
    df = _read_csv_from_zip(zf, "valor_mobiliario")
    if df is None:
        return None
    df.columns = [_norm(c) for c in df.columns]
    col_ticker = next((c for c in df.columns if "codigo_negociacao" in c), None)
    col_cnpj = next((c for c in df.columns if c == "cnpj_cia" or "cnpj" in c), None)
    if not col_ticker or not col_cnpj:
        print(f"  aviso: colunas de ticker/CNPJ não reconhecidas no FCA. "
              f"Colunas: {list(df.columns)}", file=sys.stderr)
        return None
    df["_TK"] = df[col_ticker].astype(str).str.strip().str.upper()
    df["_CNPJ"] = df[col_cnpj].astype(str).str.strip()
    return df[["_TK", "_CNPJ"]]


# Abreviações comuns nos nomes que o Fundamentus usa, que o cadastro oficial
# da CVM escreve por extenso. Só as inequívocas (sem risco de trocar o
# sentido), pra não arriscar casar empresa errada.
_ABREV_EMPRESA = {"BCO": "BANCO", "CIA": "COMPANHIA"}


def _norm_company_name(s):
    """Normaliza nome de empresa pra comparação: sem acento, maiúsculas,
    sem pontuação, sem sufixos societários comuns (S.A., S/A, ON, PN...),
    com abreviações comuns expandidas (BCO -> BANCO etc)."""
    s = _norm(s).upper()
    for lixo in (" S.A.", " S/A", " SA", " ON", " PN", " N1", " N2", " NM", "."):
        s = s.replace(lixo.upper(), " ")
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    palavras = [_ABREV_EMPRESA.get(p, p) for p in s.split()]
    return " ".join(palavras)


def fetch_cvm_company_names():
    """Baixa o cadastro geral de companhias abertas (nome <-> CNPJ). Não é
    um zip — é um CSV direto."""
    print("Baixando cadastro geral de companhias (CAD) para tentar por nome...")
    try:
        r = requests.get(CVM_CAD_URL, headers=HEADERS, timeout=90)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        print(f"  aviso: não consegui baixar o CAD ({e}). Pulando fallback por nome.", file=sys.stderr)
        return None
    df = pd.read_csv(io.BytesIO(r.content), sep=";", encoding="latin-1", dtype=str)
    df.columns = [_norm(c) for c in df.columns]
    col_cnpj = next((c for c in df.columns if "cnpj" in c), None)
    col_nome = next((c for c in df.columns if "denom_social" in c), None)
    col_nome2 = next((c for c in df.columns if "denom_comerc" in c), None)
    if not col_cnpj or not col_nome:
        print(f"  aviso: colunas do CAD não reconhecidas. Colunas: {list(df.columns)}", file=sys.stderr)
        return None
    df["_CNPJ"] = df[col_cnpj].astype(str).str.strip()
    df["_NOME_NORM"] = df[col_nome].map(_norm_company_name)
    if col_nome2:
        df["_NOME2_NORM"] = df[col_nome2].map(_norm_company_name)
    return df


def build_ticker_cnpj_map(tickers_alvo=None, nomes_por_ticker=None):
    """A CVM identifica empresas por CNPJ/código CVM, não por ticker. O
    Formulário Cadastral (FCA) tem o arquivo 'valor_mobiliario', que lista
    o código de negociação de cada classe de ação por empresa.

    Funde os FCAs do ano atual E do ano anterior (união, ano atual tem
    prioridade em caso de conflito) — empresas que ainda não reenviaram o
    FCA deste ano continuam aparecendo com o dado do ano passado.

    Depois aplica TICKER_ALIASES (mesma empresa, ticker diferente, já
    confirmado por diagnóstico real) e, pra quem ainda faltar, tenta casar
    pelo NOME da empresa (cadastro CAD) — só aceita automaticamente se o
    nome bater com alta confiança; o resto fica listado pra revisão manual.
    """
    print("Baixando FCA (cadastro) para montar mapa ticker -> CNPJ...")
    frames = []
    for ano in (ANO_ATUAL, ANO_ATUAL - 1):
        zf = _download_zip(f"{CVM_FCA_BASE}/fca_cia_aberta_{ano}.zip")
        if zf is None:
            continue
        parsed = _parse_fca_valor_mobiliario(zf)
        if parsed is not None:
            frames.append((ano, parsed))
    if not frames:
        raise RuntimeError("Não foi possível baixar/ler nenhum FCA para montar o mapa de tickers.")

    # Funde do ano mais antigo pro mais novo, assim o mais novo sobrescreve.
    frames.sort(key=lambda x: x[0])
    mapping = {}
    for _, parsed in frames:
        for tk, cnpj in zip(parsed["_TK"], parsed["_CNPJ"]):
            if tk and tk != "NAN" and cnpj and cnpj != "NAN":
                mapping[tk] = cnpj
    print(f"  {len(mapping)} tickers mapeados para CNPJ (usando {len(frames)} ano(s) de FCA).")

    # Aplica os aliases confirmados (mesma empresa, outro ticker no FCA).
    n_alias = 0
    for tk, tk_origem in TICKER_ALIASES.items():
        if tk not in mapping and tk_origem in mapping:
            mapping[tk] = mapping[tk_origem]
            n_alias += 1
    if n_alias:
        print(f"  +{n_alias} tickers resolvidos via alias (mesma empresa, ticker-irmão no FCA).")

    # Fallback por nome, só pra quem ainda não tem CNPJ.
    if tickers_alvo and nomes_por_ticker:
        ainda_faltando = [tk for tk in tickers_alvo if tk not in mapping and nomes_por_ticker.get(tk)]
        if ainda_faltando:
            cad = fetch_cvm_company_names()
            if cad is not None:
                n_nome = 0
                duvidosos = []
                for tk in ainda_faltando:
                    alvo = _norm_company_name(nomes_por_ticker[tk])
                    if not alvo:
                        continue
                    alvo_tokens = set(alvo.split())
                    # match exato do nome normalizado primeiro
                    exato = cad[(cad["_NOME_NORM"] == alvo) | (cad.get("_NOME2_NORM", "") == alvo)]
                    if len(exato) == 1:
                        mapping[tk] = exato.iloc[0]["_CNPJ"]
                        n_nome += 1
                        continue
                    # senão, empresa cujo nome contém TODAS as palavras do nome
                    # do ticker (ou vice-versa) — só aceita se for candidato único
                    def bate(row_nome):
                        row_tokens = set(row_nome.split())
                        return alvo_tokens and (alvo_tokens <= row_tokens or row_tokens <= alvo_tokens)
                    candidatos = cad[cad["_NOME_NORM"].map(bate)]
                    cnpjs_unicos = candidatos["_CNPJ"].unique()
                    if len(cnpjs_unicos) == 1:
                        mapping[tk] = cnpjs_unicos[0]
                        n_nome += 1
                    elif len(cnpjs_unicos) > 1:
                        duvidosos.append((tk, nomes_por_ticker[tk], list(candidatos["_NOME_NORM"].unique())[:5]))
                if n_nome:
                    print(f"  +{n_nome} tickers resolvidos via nome da empresa (cadastro CAD).")
                if duvidosos:
                    print(f"  {len(duvidosos)} tickers com mais de um nome parecido no CAD (não apliquei, ambíguo):")
                    for tk, nome, opcoes in duvidosos:
                        print(f"    {tk} (\"{nome}\"): candidatos {opcoes}")

    if tickers_alvo:
        faltando = [tk for tk in tickers_alvo if tk not in mapping]
        if faltando:
            todo_df = pd.concat([p for _, p in frames], ignore_index=True)
            print(f"\n  Diagnóstico dos {len(faltando)} tickers ainda sem CNPJ:")
            for tk in faltando:
                raiz = tk[:4]
                parecidos = sorted(set(todo_df.loc[todo_df["_TK"].str.startswith(raiz), "_TK"]))
                nome = (nomes_por_ticker or {}).get(tk, "")
                extra = f' — nome no cotacoes.json: "{nome}"' if nome else ""
                if parecidos:
                    print(f"    {tk}: não achei exato, mas o FCA tem parecidos: {parecidos}{extra}")
                else:
                    print(f"    {tk}: nenhuma linha no FCA começa nem com '{raiz}'{extra}")
    return mapping


# --------------------------------------------------------------------------
# 2) Lucro Líquido Consolidado — últimos N anos
# --------------------------------------------------------------------------
def _escala_para_reais(df):
    """Devolve um multiplicador por linha pra converter VL_CONTA em REAIS
    CHEIOS, baseado na coluna ESCALA_MOEDA da CVM ('MIL' ou 'UNIDADE').

    A maioria das empresas reporta em MIL, mas nem todas — aplicar um x1000
    fixo pra todo mundo inflaria em 1000x quem reporta em UNIDADE, de forma
    silenciosa (sem erro nenhum). Por isso lemos a escala linha a linha."""
    col = next((c for c in df.columns if c.upper() == "ESCALA_MOEDA"), None)
    if col is None:
        # Coluna ausente nessa versão do arquivo: assume MIL (padrão CVM),
        # mas sem confirmação linha a linha.
        return pd.Series(1000.0, index=df.index)
    escala = df[col].astype(str).str.strip().str.upper()
    return escala.map({"MIL": 1000.0, "UNIDADE": 1.0}).fillna(1000.0)


def _extrair_lucro_liquido(df):
    """Recebe o DataFrame de uma DRE (consolidada ou individual) já com
    ORDEM_EXERC=='ÚLTIMO' filtrado e devolve {cnpj: valor} do Lucro Líquido
    do período (em REAIS CHEIOS, já normalizado pela ESCALA_MOEDA), tentando
    primeiro pelo código de conta padrão e, pra quem não aparecer, pela
    descrição da conta (mais no fundo da hierarquia)."""
    df = df.copy()
    df["CD_CONTA"] = df["CD_CONTA"].astype(str).str.strip()
    df["DS_CONTA_NORM"] = df["DS_CONTA"].map(_norm)
    df["VL_CONTA_NUM"] = pd.to_numeric(df["VL_CONTA"], errors="coerce") * _escala_para_reais(df)
    df["CNPJ_NORM"] = df["CNPJ_CIA"].astype(str).str.strip()

    mask_padrao = (
        df["CD_CONTA"].isin(CODIGOS_LUCRO_LIQUIDO)
        & (df["DS_CONTA_NORM"].str.contains("lucro") | df["DS_CONTA_NORM"].str.contains("prejuizo"))
    )
    achados = {}
    for _, row in df[mask_padrao].iterrows():
        valor = row["VL_CONTA_NUM"]
        if pd.notna(valor):
            achados[row["CNPJ_NORM"]] = float(valor)
    n_padrao = len(achados)

    faltando = set(df["CNPJ_NORM"]) - set(achados.keys())
    if faltando:
        candidatos = df[
            df["CNPJ_NORM"].isin(faltando)
            & df["CD_CONTA"].str.startswith("3.")
            & (
                df["DS_CONTA_NORM"].str.contains("lucro liquido")
                | df["DS_CONTA_NORM"].str.contains("prejuizo liquido")
                | df["DS_CONTA_NORM"].str.contains("lucro/prejuizo")
                | df["DS_CONTA_NORM"].str.contains("resultado liquido")
            )
        ].copy()
        if len(candidatos):
            candidatos["_profundidade"] = candidatos["CD_CONTA"].str.count(r"\.")
            candidatos = candidatos.sort_values(["CNPJ_NORM", "_profundidade", "CD_CONTA"])
            for cnpj, grupo in candidatos.groupby("CNPJ_NORM"):
                valor = grupo.iloc[-1]["VL_CONTA_NUM"]
                if pd.notna(valor):
                    achados[cnpj] = float(valor)
    n_fallback = len(achados) - n_padrao
    return achados, n_padrao, n_fallback


def fetch_lucro_liquido_por_cnpj(anos_zip):
    """Retorna {cnpj: {ano: valor}} com o Lucro Líquido do Período de cada
    ano, lendo a DRE de cada DFP.

    Prioriza a DRE CONSOLIDADA (grupo econômico todo). Empresas sem
    subsidiárias (comum em bancos regionais e concessionárias pequenas)
    costumam só entregar a DRE INDIVIDUAL — pra essas, usamos a individual
    como fallback (que, sem subsidiária pra consolidar, é essencialmente
    o mesmo número)."""
    resultado = {}
    for ano_zip in anos_zip:
        print(f"Baixando DFP {ano_zip} (DRE)...")
        zf = _download_zip(f"{CVM_DFP_BASE}/dfp_cia_aberta_{ano_zip}.zip")
        if zf is None:
            continue

        df_con = _read_csv_from_zip(zf, "DRE_con")
        ultimo_con = None
        if df_con is not None:
            df_con["ORDEM_NORM"] = df_con["ORDEM_EXERC"].astype(str).str.strip().str.upper()
            ultimo_con = df_con[df_con["ORDEM_NORM"] == "ÚLTIMO"]

        achados, n_padrao, n_fallback = ({}, 0, 0)
        if ultimo_con is not None and len(ultimo_con):
            achados, n_padrao, n_fallback = _extrair_lucro_liquido(ultimo_con)

        # Fallback: DRE individual, só pra quem não veio na consolidada.
        n_individual = 0
        df_ind = _read_csv_from_zip(zf, "DRE_ind")
        if df_ind is not None:
            df_ind["ORDEM_NORM"] = df_ind["ORDEM_EXERC"].astype(str).str.strip().str.upper()
            ultimo_ind = df_ind[df_ind["ORDEM_NORM"] == "ÚLTIMO"]
            ultimo_ind = ultimo_ind[~ultimo_ind["CNPJ_CIA"].astype(str).str.strip().isin(achados.keys())]
            if len(ultimo_ind):
                achados_ind, _, _ = _extrair_lucro_liquido(ultimo_ind)
                n_individual = len(achados_ind)
                achados.update(achados_ind)

        if not achados:
            print(f"  aviso: nenhuma DRE utilizável no DFP {ano_zip}. "
                  f"Conteúdo: {zf.namelist()}", file=sys.stderr)
            continue

        ano_exercicio = ano_zip  # o nome do zip já É o ano do exercício (ver nota no topo do arquivo)
        for cnpj, valor in achados.items():
            resultado.setdefault(cnpj, {})[str(ano_exercicio)] = valor
        print(f"  {len(achados)} empresas com Lucro Líquido {ano_exercicio} encontradas "
              f"({n_padrao} padrão + {n_fallback} via descrição, consolidado; "
              f"{n_individual} via DRE individual).")
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
    col_escala = next((c for c in df.columns if "escala" in c), None)
    col_total = next((c for c in df.columns if "acao" in c and "total" in c and "tesour" not in c), None)
    col_tesouro = next((c for c in df.columns if "acao" in c and "total" in c and "tesour" in c), None)
    col_ordin = next((c for c in df.columns if "acao" in c and "ordin" in c and "tesour" not in c), None)
    col_pref = next((c for c in df.columns if "acao" in c and "pref" in c and "tesour" not in c), None)

    if not col_cnpj or not (col_total or (col_ordin and col_pref)):
        print(f"  aviso: colunas de composição do capital não reconhecidas. "
              f"Colunas: {list(df.columns)}", file=sys.stderr)
        return {}

    # Mesmo problema do Lucro Líquido: a CVM pode reportar as quantidades de
    # ações em MIL em vez de unidades absolutas, dependendo da empresa (foi
    # o caso da VALE3 — dava 1000x menos ações que o real). Normaliza linha
    # a linha usando a mesma coluna ESCALA_MOEDA, se existir nesse arquivo.
    if col_escala:
        escala = df[col_escala].astype(str).str.strip().str.upper()
        multiplicador = escala.map({"MIL": 1000.0, "UNIDADE": 1.0}).fillna(1.0)
    else:
        multiplicador = pd.Series(1.0, index=df.index)

    for c in (col_total, col_tesouro, col_ordin, col_pref):
        if c:
            df[c] = pd.to_numeric(df[c], errors="coerce") * multiplicador

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

    try:
        with open(COTACOES_PATH, "r", encoding="utf-8") as f:
            acoes_cotacoes = json.load(f).get("acoes", {})
            tickers_ativos = set(acoes_cotacoes.keys())
            nomes_por_ticker = {tk: d.get("nome") for tk, d in acoes_cotacoes.items() if d.get("nome")}
    except Exception:
        tickers_ativos = None  # define depois de ter o mapa, como fallback
        nomes_por_ticker = {}

    ticker_cnpj = build_ticker_cnpj_map(tickers_alvo=tickers_ativos, nomes_por_ticker=nomes_por_ticker)
    lucro_por_cnpj = fetch_lucro_liquido_por_cnpj(anos_zip)
    capital_por_cnpj = fetch_capital_por_cnpj()

    if tickers_ativos is None:
        tickers_ativos = set(ticker_cnpj.keys())  # fallback: todo mundo mapeado

    saida = {}
    sem_cnpj = []          # ticker não achado no mapa FCA (código de negociação não bateu)
    cnpj_sem_lucro = []    # tem CNPJ, mas nenhum ano de Lucro Líquido encontrado no DRE_con
    cnpj_sem_capital = []  # tem CNPJ, mas não achou linha na composição do capital
    for tk in sorted(tickers_ativos):
        cnpj = ticker_cnpj.get(tk)
        if not cnpj:
            sem_cnpj.append(tk)
            continue
        lucro_anos = lucro_por_cnpj.get(cnpj)
        capital = capital_por_cnpj.get(cnpj)
        if not lucro_anos:
            cnpj_sem_lucro.append(tk)
        if not capital:
            cnpj_sem_capital.append(tk)

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

    # Guarda de sanidade: com a cobertura atual do pipeline (alias + nome +
    # DRE individual como fallback), o normal é ficar na faixa de 350-400
    # empresas. Um valor bem abaixo disso (< 200) é sinal de algo quebrado
    # no meio do caminho (FCA, DFP, ou fusão dos dois) — melhor abortar do
    # que sobrescrever o anuais.json bom com um resultado capado.
    MIN_EMPRESAS_ESPERADO = 200
    if len(saida) < MIN_EMPRESAS_ESPERADO:
        raise RuntimeError(
            f"Resultado suspeito (apenas {len(saida)} empresas, esperado >= {MIN_EMPRESAS_ESPERADO}). "
            "Abortando para não sobrescrever o data/anuais.json anterior com algo quebrado."
        )

    data = {
        "atualizado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fonte": "CVM - dados.cvm.gov.br (DFP + FRE)",
        "unidades": {
            "lucroLiquidoAnos": "reais (R$) cheios — já normalizado pela ESCALA_MOEDA de cada empresa, não em milhares",
            "acoesTotal": "número de ações (unidades)",
            "acoesTesouraria": "número de ações (unidades)",
            "acoesExTesouraria": "número de ações (unidades)",
        },
        "dados": saida,
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Salvo em {OUT_PATH} — {len(saida)} empresas.")

    # Diagnóstico: por que cada ticker da carteira que ficou de fora ficou de fora.
    if sem_cnpj:
        print(f"\n{len(sem_cnpj)} tickers sem CNPJ mapeado no FCA (não entraram em nada): {sem_cnpj}")
    if cnpj_sem_lucro:
        print(f"{len(cnpj_sem_lucro)} tickers com CNPJ mas sem Lucro Líquido encontrado no DRE_con: {cnpj_sem_lucro}")
    if cnpj_sem_capital:
        print(f"{len(cnpj_sem_capital)} tickers com CNPJ mas sem composição do capital: {cnpj_sem_capital}")


if __name__ == "__main__":
    main()
