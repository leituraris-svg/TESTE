#!/usr/bin/env python3
"""
Atualiza data/cotacoes.json com cotação + indicadores fundamentalistas de
TODAS as ações e TODOS os FIIs negociados na B3, usando as tabelas públicas
do Fundamentus (resultado.php e fii_resultado.php).

Só faz 2 requisições no total (uma para ações, outra para FIIs) — não é
scraping ticker a ticker. Feito para rodar 1x/dia via GitHub Actions.
"""

import json
import sys
import time
import unicodedata
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

HEADERS = {
    # Fundamentus bloqueia o User-Agent padrão do requests/python.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9",
}

URL_ACOES = "https://www.fundamentus.com.br/resultado.php"
URL_FIIS = "https://www.fundamentus.com.br/fii_resultado.php"
NAMES_URL = "https://brapi.dev/api/quote/list"  # nome das empresas (best-effort)
OUT_PATH = "data/cotacoes.json"

# Guarda de sanidade: a B3 tem hoje algo entre ~400-480 ações e ~380-450 FIIs
# ativos, então 300 é um piso alto o bastante pra pegar um scraping quebrado
# (ex: paginação cortada, tabela carregada pela metade), mas baixo o
# suficiente pra não disparar falso positivo em oscilação normal.
MIN_ACOES_ESPERADO = 300
MIN_FIIS_ESPERADO = 300


def fetch_names():
    """Busca o nome de cada empresa/fundo via brapi.dev. Isso é 'melhor esforço':
    se o endpoint mudar, exigir chave, ou cair, simplesmente seguimos sem nomes
    (o app usa só o ticker nesse caso) — não deve travar o robô.

    Observação: pra FIIs, esse endpoint costuma devolver o próprio ticker
    como "nome" (não tem o nome comercial do fundo) — é uma limitação da
    fonte, não um bug daqui. Pra ações costuma vir o nome de verdade."""
    try:
        r = requests.get(NAMES_URL, headers=HEADERS, timeout=20)
        r.raise_for_status()
        data = r.json()
        items = data.get("stocks") or data.get("results") or []
        names = {}
        for item in items:
            tk = (item.get("stock") or item.get("symbol") or "").strip().upper()
            nome = item.get("name")
            if tk and nome:
                names[tk] = nome
        if not names:
            print("  aviso: brapi retornou 0 nomes (formato inesperado). Seguindo sem nomes.", file=sys.stderr)
        return names
    except Exception as e:  # noqa: BLE001
        print(f"  aviso: não foi possível obter nomes via brapi ({e}). Seguindo sem nomes.", file=sys.stderr)
        return {}


def parse_br_number(raw):
    """Converte string no formato brasileiro ('1.234,56', '8,50%', '-') em float ou None."""
    if raw is None:
        return None
    s = raw.strip()
    if s in ("", "-", "--", "N/A"):
        return None
    is_pct = s.endswith("%")
    s = s.replace("%", "").strip()
    s = s.replace(".", "").replace(",", ".")
    try:
        val = float(s)
    except ValueError:
        return None
    return val / 100 if is_pct else val


def fetch_html(url, retries=3, timeout=20):
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            return r.text
        except Exception as e:  # noqa: BLE001 - queremos capturar qualquer falha de rede
            last_err = e
            print(f"  tentativa {attempt}/{retries} falhou: {e}", file=sys.stderr)
            time.sleep(2 * attempt)
    raise RuntimeError(f"Não foi possível baixar {url}: {last_err}")


def _norm(s):
    """minúsculas, sem acento — pra achar a coluna mesmo se a página vier com
    encoding diferente (ex.: 'Cotação' vs 'Cota\\xe7\\xe3o' vs 'Cotacao')."""
    if s is None:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = s.encode("ascii", "ignore").decode("ascii")
    return s.strip().lower()


def parse_table(html):
    """Extrai a tabela principal da página como lista de dicts {cabeçalho_normalizado: valor_bruto}."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", {"id": "resultado"}) or soup.find("table")
    if table is None:
        raise RuntimeError("Tabela não encontrada na página — o layout do site pode ter mudado.")

    header_row = table.find("thead")
    header_cells = header_row.find_all("th") if header_row else table.find("tr").find_all("th")
    headers = [_norm(h.get_text(strip=True)) for h in header_cells]
    if not headers:
        raise RuntimeError("Cabeçalho da tabela não encontrado.")

    rows = []
    body = table.find("tbody") or table
    for tr in body.find_all("tr"):
        cells = tr.find_all("td")
        if not cells or len(cells) != len(headers):
            continue
        row = {headers[i]: cells[i].get_text(strip=True) for i in range(len(headers))}
        rows.append(row)
    return rows


def build_acoes(rows, names=None):
    names = names or {}
    out = {}
    skipped = 0
    for row in rows:
        tk = row.get("papel", "").strip().upper()
        if not tk:
            continue

        preco = parse_br_number(row.get("cotacao"))
        pl = parse_br_number(row.get("p/l"))
        pvp = parse_br_number(row.get("p/vp"))
        dy = parse_br_number(row.get("div.yield"))
        cresc5a = parse_br_number(row.get("cresc. rec.5a"))
        roe = parse_br_number(row.get("roe"))
        liq2m = parse_br_number(row.get("liq.2meses"))

        # Sem cotação, ou sem nenhuma negociação nos últimos 2 meses: provavelmente
        # deslistado, suspenso, ou irrelevante pra qualquer análise. Descarta.
        if not preco or preco <= 0:
            skipped += 1
            continue
        if liq2m is not None and liq2m <= 0:
            skipped += 1
            continue

        # Fundamentus não publica LPA/VPA direto nessa tabela, mas dá pra
        # derivar a partir de Cotação/P-L e Cotação/P-VP.
        #
        # IMPORTANTE: usamos "pl not in (None, 0)" e não "pl > 0" de propósito.
        # P/L negativo é o que a empresa tem QUANDO está com prejuízo — nesse
        # caso, Preço/P_L ainda é uma conta válida e dá um LPA negativo (o
        # sinal certo, que é justamente o alerta de risco que importa mostrar).
        # Se a gente exigisse pl > 0, LPA de empresa com prejuízo vira None,
        # ficando indistinguível de "essa métrica não está disponível".
        lpa = (preco / pl) if (preco and pl not in (None, 0)) else None
        vpa = (preco / pvp) if (preco and pvp not in (None, 0)) else None

        out[tk] = {
            "preco": preco,
            "pl": pl,
            "pvp": pvp,
            "dy": dy,
            "lpa": lpa,
            "vpa": vpa,
            # aproximação de crescimento: Fundamentus não traz crescimento de
            # lucro, então usa-se o crescimento de receita em 5 anos como proxy.
            # Ajuste/troque de fonte se quiser algo mais preciso pro Lynch.
            "g": cresc5a,
            "roe": roe,
            "liq_2m": liq2m,
            "nome": names.get(tk),
        }

    print(f"  {skipped} ações descartadas (sem cotação/liquidez recente).")
    return out


def build_fiis(rows, names=None):
    names = names or {}
    out = {}
    skipped = 0
    for row in rows:
        tk = row.get("papel", "").strip().upper()
        if not tk:
            continue

        preco = parse_br_number(row.get("cotacao"))
        pvp = parse_br_number(row.get("p/vp"))
        dy = parse_br_number(row.get("dividend yield"))
        liquidez = parse_br_number(row.get("liquidez"))

        if not preco or preco <= 0:
            skipped += 1
            continue
        if liquidez is not None and liquidez <= 0:
            skipped += 1
            continue

        # Mesmo raciocínio do build_acoes: P/VP negativo (patrimônio líquido
        # negativo) ainda dá um VPA válido (negativo), que é o sinal de risco
        # que interessa preservar — em vez de virar None e se confundir com
        # "métrica indisponível".
        vpa = (preco / pvp) if (preco and pvp not in (None, 0)) else None

        out[tk] = {
            "preco": preco,
            "pvp": pvp,
            "dy": dy,
            "lpa": None,  # não se aplica a FIIs
            "vpa": vpa,
            "g": None,
            "segmento": row.get("segmento"),
            "liq_2m": liquidez,
            "nome": names.get(tk),
        }

    print(f"  {skipped} FIIs descartados (sem cotação/liquidez recente).")
    return out


def main():
    print("Buscando nomes das empresas/fundos (best-effort)...")
    names = fetch_names()
    print(f"  {len(names)} nomes obtidos.")

    print("Baixando tabela de ações...")
    rows_acoes = parse_table(fetch_html(URL_ACOES))
    acoes = build_acoes(rows_acoes, names)
    print(f"  {len(acoes)} ações processadas.")

    print("Baixando tabela de FIIs...")
    rows_fiis = parse_table(fetch_html(URL_FIIS))
    fiis = build_fiis(rows_fiis, names)
    print(f"  {len(fiis)} FIIs processados.")

    if len(acoes) < MIN_ACOES_ESPERADO or len(fiis) < MIN_FIIS_ESPERADO:
        # Guarda de sanidade: se vier muito pouca coisa, o site provavelmente
        # mudou o layout ou bloqueou a requisição — melhor não sobrescrever
        # o JSON bom que já está no repositório com um resultado quebrado.
        raise RuntimeError(
            f"Resultado suspeito (ações={len(acoes)}, esperado >= {MIN_ACOES_ESPERADO}; "
            f"fiis={len(fiis)}, esperado >= {MIN_FIIS_ESPERADO}). "
            "Abortando para não sobrescrever dados válidos."
        )

    data = {
        "atualizado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "acoes": acoes,
        "fiis": fiis,
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Salvo em {OUT_PATH} — {len(acoes)} ações + {len(fiis)} FIIs.")


if __name__ == "__main__":
    main()
