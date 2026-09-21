#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""旅行日程表シートの読み書き。

  itinerary.py backup  --spec spec.json --out DIR
  itinerary.py apply   --spec spec.json [--dry-run]
  itinerary.py verify  --spec spec.json
  itinerary.py restore --spec spec.json --from DIR/xxx_FORMULA.json

1日 = 6列（調整時間・所要時間・開始・－・終了・予定）+ 空列1。
時刻は値ではなく数式で連鎖させる。各日の先頭行の開始だけがリテラル。
"""
import argparse
import datetime
import json
import os
import sys

from google.oauth2 import service_account
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SA_FILE = os.environ.get("GOOGLE_SA_FILE", "/tmp/sa.json")
DASH = "－"          # 全角ハイフンマイナス。ASCII の '-' ではない
WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}
CYAN = {"red": 0.0, "green": 1.0, "blue": 1.0}    # フライト行の時刻セル
GREEN = {"red": 0.0, "green": 1.0, "blue": 0.0}   # 食事行の予定セル
COLS_PER_DAY = 6
STRIDE = COLS_PER_DAY + 1  # 6列 + 空列1


# ---------- 時刻ユーティリティ（すべて分単位、0..1439） ----------

def hhmm_to_min(s):
    s = str(s).strip().replace(":", "").zfill(4)
    return (int(s[:2]) * 60 + int(s[2:])) % 1440


def min_to_hhmm(m):
    m %= 1440
    return f"{m // 60:02d}{m % 60:02d}"


def col_letter(idx):
    """0 -> A, 25 -> Z, 26 -> AA"""
    out = ""
    idx += 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        out = chr(65 + r) + out
    return out


# ---------- 数式 ----------

def f_start(prev_end_col, prev_row, adj_col, row):
    return (f'=TEXT(MOD(TIME(VALUE(LEFT({prev_end_col}{prev_row},2)),'
            f'VALUE(RIGHT({prev_end_col}{prev_row},2)),0)+{adj_col}{row}/1440,1),"hhmm")')


def f_end(start_col, dur_col, row):
    return (f'=TEXT(MOD(TIME(VALUE(LEFT({start_col}{row},2)),'
            f'VALUE(RIGHT({start_col}{row},2)),0)+{dur_col}{row}/1440,1),"hhmm")')


# ---------- 1日目の出発ブロック ----------

def build_airport_block(dep):
    """空港での「チェックイン/ラウンジ行」と「フライト行」を作る。

    往路の出発ブロックと復路の搭乗ブロックで共通。フライト行は
    調整30分（搭乗）＋所要=飛行時間 なので、終了が現地到着時刻に一致する。
    """
    kind = "国際線" if dep.get("international") else "国内線"
    lead = int(dep.get("lead_minutes", 180 if dep.get("international") else 120))

    dep_m = hhmm_to_min(dep["flight_dep"])
    arr_m = hhmm_to_min(dep["flight_arr"])
    flight_min = (arr_m - dep_m) % 1440
    if flight_min == 0:
        raise ValueError("flight_dep と flight_arr が同一です")

    scheduled = (dep_m - lead) % 1440                    # ルール上の目標到着
    # 始発が間に合わない等でルール通りに着けない場合は実到着時刻を明示する
    terminal_arrival = hhmm_to_min(dep["terminal_arrival"]) if dep.get("terminal_arrival") else scheduled
    lounge = (dep_m - 30 - terminal_arrival) % 1440      # 出発30分前まで滞在
    if not 0 < lounge <= lead:
        raise ValueError(f"ターミナル到着 {min_to_hhmm(terminal_arrival)} が "
                         f"出発 {dep['flight_dep']} の30分前を過ぎています")
    shortfall = (terminal_arrival - scheduled) % 1440

    d = dep["flight_dep"]
    rows = [
        [0, lounge, dep.get("lounge_label",
                            f"チェックイン・保安検査・ラウンジ ※{d[:2]}:{d[2:]}発・{kind}{lead // 60}時間前着")],
        [30, flight_min, dep["flight_label"], "flight"],  # 調整30分 = 搭乗
    ]
    info = {
        "terminal_arrival": terminal_arrival,
        "detail": {
            "ターミナル到着": min_to_hhmm(terminal_arrival),
            f"{kind}リード": (f"{lead}分前" if not shortfall
                           else f"{lead}分前に対し{shortfall}分遅い(実{(dep_m - terminal_arrival) % 1440}分前)"),
            "ラウンジ滞在": f"{lounge}分",
            "搭乗(調整)": "30分",
            "出発": dep["flight_dep"],
            "飛行時間": f"{flight_min}分",
            "到着": dep["flight_arr"],
        },
    }
    return rows, info


def build_departure_rows(dep):
    """1日目の先頭。起床と自宅→ターミナルの移動を空港ブロックの前に足す。

    返り値: (rows, literal_start_hhmm, detail)
    """
    rows, info = build_airport_block(dep)
    wake = int(dep.get("wake_minutes", 60))
    transit = int(dep["transit_minutes"])
    home_dep = (info["terminal_arrival"] - transit) % 1440
    wake_start = (home_dep - wake) % 1440

    rows = [
        [0, wake, dep.get("wake_label", "起床・準備・自宅出発")],
        [0, transit, dep["transit_label"]],
    ] + rows
    detail = {"起床": min_to_hhmm(wake_start), "自宅出発": min_to_hhmm(home_dep), **info["detail"]}
    return rows, min_to_hhmm(wake_start), detail


# ---------- グリッド生成 ----------

def build_grid(spec):
    """spec から A2 起点の2次元配列を作る。返り値: (grid, meta)"""
    days = spec["days"]
    n_cols = len(days) * STRIDE - 1
    details = []

    prepared = []
    for i, day in enumerate(days):
        rows = [list(r) for r in day["rows"]]
        start = day.get("start")
        if day.get("departure"):
            lead_rows, start, detail = build_departure_rows(day["departure"])
            rows = lead_rows + rows
            details.append((i, detail))
        if day.get("boarding"):
            tail_rows, info = build_airport_block(day["boarding"])
            rows = rows + tail_rows
            details.append((i, info["detail"]))
        if not start:
            raise ValueError(f"{i + 1}日目に start も departure もありません")
        prepared.append((day, rows, start))

    n_rows = max(len(r) for _, r, _ in prepared)
    grid = [["" for _ in range(n_cols)] for _ in range(n_rows)]
    marks = []

    for i, (day, rows, start) in enumerate(prepared):
        base = i * STRIDE
        adj, dur, st, dash, en = (col_letter(base + k) for k in range(5))
        plan_i = base + 5
        for n, row in enumerate(rows):
            a, p, text = row[0], row[1], row[2]
            tag = row[3] if len(row) > 3 else None
            r = 2 + n                       # 実際のシート行番号
            if tag:
                marks.append({"tag": tag, "row": r, "base": base})
            g = grid[n]
            g[base + 0] = a
            g[base + 1] = p
            g[base + 2] = f'="{start}"' if n == 0 else f_start(en, r - 1, adj, r)
            g[base + 3] = DASH
            g[base + 4] = f_end(st, dur, r)
            g[plan_i] = text

    meta = {
        "n_rows": n_rows,
        "n_cols": n_cols,
        "last_col": col_letter(n_cols - 1),
        "day_rows": [len(r) for _, r, _ in prepared],
        "day_starts": [s for _, _, s in prepared],
        "details": details,
        "marks": marks,
    }
    return grid, meta


def simulate(spec):
    """各日の時刻をPython側で再現し、最終行の終了時刻を返す。"""
    out = []
    for i, day in enumerate(spec["days"]):
        rows = [list(r) for r in day["rows"]]
        start = day.get("start")
        if day.get("departure"):
            lead_rows, start, _ = build_departure_rows(day["departure"])
            rows = lead_rows + rows
        if day.get("boarding"):
            rows = rows + build_airport_block(day["boarding"])[0]
        t = hhmm_to_min(start)
        end = (t + int(rows[0][1])) % 1440
        for row in rows[1:]:
            t = (end + int(row[0])) % 1440
            end = (t + int(row[1])) % 1440
        out.append({"day": i + 1, "rows": len(rows), "start": start,
                    "last_end": min_to_hhmm(end),
                    "last_end_cell": f"{col_letter(i * STRIDE + 4)}{1 + len(rows)}"})
    return out


# ---------- Sheets API ----------

def service():
    creds = service_account.Credentials.from_service_account_file(SA_FILE, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()


def sheet_props(svc, sid, tab):
    meta = svc.get(spreadsheetId=sid, fields="sheets.properties").execute()
    for s in meta["sheets"]:
        if s["properties"]["title"] == tab:
            return s["properties"]
    raise SystemExit(f"タブ '{tab}' が見つかりません")


def cmd_backup(spec, args):
    svc = service()
    sid, tab = spec["spreadsheet_id"], spec["tab"]
    props = sheet_props(svc, sid, tab)
    last = col_letter(props["gridProperties"]["columnCount"] - 1)
    rows = props["gridProperties"]["rowCount"]
    os.makedirs(args.out, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    for render in ("FORMULA", "FORMATTED_VALUE"):
        vals = svc.values().get(spreadsheetId=sid, range=f"'{tab}'!A1:{last}{rows}",
                                valueRenderOption=render).execute().get("values", [])
        path = os.path.join(args.out, f"{tab}_{stamp}_{render}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(vals, fh, ensure_ascii=False, indent=1)
        print(f"  backup {render}: {path} ({len(vals)} rows)")


def cmd_apply(spec, args):
    grid, meta = build_grid(spec)
    sim = simulate(spec)

    print(f"  範囲 A2:{meta['last_col']}{meta['n_rows'] + 1}  ({meta['n_rows']}行 x {meta['n_cols']}列)")
    for i, detail in meta["details"]:
        print(f"  {i + 1}日目 空港ブロック: " + " / ".join(f"{k}={v}" for k, v in detail.items()))
    for s in sim:
        print(f"  {s['day']}日目: {s['rows']}行 開始{s['start']} → {s['last_end_cell']}={s['last_end']}")
    if args.dry_run:
        print("  (dry-run: 書き込みませんでした)")
        return

    svc = service()
    sid, tab = spec["spreadsheet_id"], spec["tab"]
    props = sheet_props(svc, sid, tab)
    sheet_id = props["sheetId"]
    total_rows = props["gridProperties"]["rowCount"]
    total_cols = props["gridProperties"]["columnCount"]
    last_col = meta["last_col"]
    end_row = meta["n_rows"] + 1

    # 1) 本体を書き込む
    res = svc.values().batchUpdate(spreadsheetId=sid, body={
        "valueInputOption": "USER_ENTERED",
        "data": [{"range": f"'{tab}'!A2:{last_col}{end_row}", "values": grid}]}).execute()
    print(f"  書き込み: {res['totalUpdatedCells']} セル")

    # 2) その下をクリア（行削除はしない）
    clear_to = int(spec.get("clear_below", 200))
    if clear_to > end_row:
        rng = f"'{tab}'!A{end_row + 1}:{last_col}{clear_to}"
        svc.values().clear(spreadsheetId=sid, range=rng, body={}).execute()
        print(f"  クリア: {rng}")

    # 3) ヘッダー。C1 に日付、各日のタイトル。J1/Q1 の =C1+1 等は触らない
    data = [{"range": f"'{tab}'!C1", "values": [[spec["date"]]]}]
    for i, day in enumerate(spec["days"]):
        if day.get("title"):
            data.append({"range": f"'{tab}'!{col_letter(i * STRIDE + 5)}1",
                         "values": [[day["title"]]]})
    res = svc.values().batchUpdate(spreadsheetId=sid, body={
        "valueInputOption": "USER_ENTERED", "data": data}).execute()
    print(f"  ヘッダー: {res['totalUpdatedCells']} セル（J1/Q1 は未変更）")

    # 4) 書式。2行目以降の網掛けを白に戻してからフライトと食事だけ塗り直す。
    #    1行目のヘッダー色は残す。下線はシート全体から削除する。
    def cell_range(row, c0, c1):
        return {"sheetId": sheet_id, "startRowIndex": row - 1, "endRowIndex": row,
                "startColumnIndex": c0, "endColumnIndex": c1}

    reqs = []
    if spec.get("clear_shading", True):
        reqs.append({"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": total_rows,
                      "startColumnIndex": 0, "endColumnIndex": total_cols},
            "cell": {"userEnteredFormat": {"backgroundColor": WHITE}},
            "fields": "userEnteredFormat.backgroundColor,userEnteredFormat.backgroundColorStyle"}})
    reqs.append({"repeatCell": {      # 下線を削除。太字・フォント等はそのまま
        "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": total_rows,
                  "startColumnIndex": 0, "endColumnIndex": total_cols},
        "cell": {"userEnteredFormat": {"textFormat": {"underline": False}}},
        "fields": "userEnteredFormat.textFormat.underline"}})

    n_flight = n_meal = 0
    for m in meta["marks"]:
        if m["tag"] == "flight":      # 開始・－・終了 の3セルだけ水色
            rng, color = cell_range(m["row"], m["base"] + 2, m["base"] + 5), CYAN
            n_flight += 1
        elif m["tag"] == "meal":      # 予定セルだけ黄緑
            rng, color = cell_range(m["row"], m["base"] + 5, m["base"] + 6), GREEN
            n_meal += 1
        else:
            raise ValueError(f"未知のタグ: {m['tag']}")
        reqs.append({"repeatCell": {
            "range": rng, "cell": {"userEnteredFormat": {"backgroundColor": color}},
            "fields": "userEnteredFormat.backgroundColor,userEnteredFormat.backgroundColorStyle"}})

    svc.batchUpdate(spreadsheetId=sid, body={"requests": reqs}).execute()
    print(f"  書式: 網掛けリセット+下線削除、フライト{n_flight}行を水色、食事{n_meal}行を黄緑")


def cmd_verify(spec, args):
    svc = service()
    sid, tab = spec["spreadsheet_id"], spec["tab"]
    _, meta = build_grid(spec)
    last_col, end_row = meta["last_col"], meta["n_rows"] + 1
    vals = svc.values().get(spreadsheetId=sid, range=f"'{tab}'!A1:{last_col}{end_row}").execute().get("values", [])

    def cell(a1):
        i = 0
        while a1[i].isalpha():
            i += 1
        col, row = a1[:i], int(a1[i:]) - 1
        ci = 0
        for ch in col:
            ci = ci * 26 + (ord(ch) - 64)
        ci -= 1
        if row >= len(vals) or ci >= len(vals[row]):
            return ""
        return str(vals[row][ci])

    print("  ヘッダー: " + " ".join(
        f"{c}1={cell(c + '1')!r}" for c in ["C", "J", "Q"] if cell(c + "1")))
    ok = True
    for s in simulate(spec):
        got = cell(s["last_end_cell"])
        want = s["last_end"]
        exp = (spec["days"][s["day"] - 1].get("expect_last_end") or want)
        mark = "OK " if got == want == exp else "NG "
        ok &= (got == want == exp)
        print(f"  {mark}{s['day']}日目 {s['last_end_cell']}: 期待={exp} 計算={want} 実際={got}")
    print("  => すべて一致" if ok else "  => 不一致あり。数式の参照行がずれています")
    return 0 if ok else 1


def cmd_restore(spec, args):
    with open(args.source, encoding="utf-8") as fh:
        vals = json.load(fh)
    svc = service()
    sid, tab = spec["spreadsheet_id"], spec["tab"]
    props = sheet_props(svc, sid, tab)
    last = col_letter(props["gridProperties"]["columnCount"] - 1)
    rows = props["gridProperties"]["rowCount"]
    svc.values().clear(spreadsheetId=sid, range=f"'{tab}'!A1:{last}{rows}", body={}).execute()
    width = max((len(r) for r in vals), default=0)
    padded = [list(r) + [""] * (width - len(r)) for r in vals]
    svc.values().update(spreadsheetId=sid, range=f"'{tab}'!A1",
                        valueInputOption="USER_ENTERED", body={"values": padded}).execute()
    print(f"  復元: {args.source} -> '{tab}' ({len(padded)} 行)")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["backup", "apply", "verify", "restore"])
    ap.add_argument("--spec", required=True)
    ap.add_argument("--out", default="./backup")
    ap.add_argument("--from", dest="source")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(args.spec, encoding="utf-8") as fh:
        spec = json.load(fh)
    print(f"[{args.command}] {spec['tab']} ({spec['spreadsheet_id']})")

    if args.command == "restore" and not args.source:
        ap.error("restore には --from が必要です")
    return {"backup": cmd_backup, "apply": cmd_apply,
            "verify": cmd_verify, "restore": cmd_restore}[args.command](spec, args) or 0


if __name__ == "__main__":
    sys.exit(main())
