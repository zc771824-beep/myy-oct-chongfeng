#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
麦悠悠 · 10月冲锋看板 自动更新脚本

功能：
1. 从企微智能表格「冲锋统计 / 10月冲锋-美团」拉取指定日期的实付记录
   （口径：日期=当天、实付金额>=900、按券码去重）
2. 从「基本面信息」拉取 门店名称 -> 所属城市/督导 映射
3. 次日自动结转：把已完成日期的「今日」并入「本月」各维度（门店/城市/督导/周节奏）
4. 重算看板 D 对象，写回 index.html 并 push 到 GitHub Pages

用法：
  python3 update_dashboard.py           # 实跑：更新 + 推送
  python3 update_dashboard.py --dry-run # 试跑：只打印结果，不推送
"""

import base64
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ---------------- 常量 ----------------
DOCID = "s3_AfwAAQZWAKMCNThWRDXuCTJiNgaOh"
SHEET_DAILY = "10月冲锋-美团"
SHEET_BASE = "基本面信息"
REPO = "zc771824-beep/myy-oct-chongfeng"
REPO_DIR = os.path.expanduser("~/.workbuddy/myy-chongfeng-automation/repo")
MIN_AMOUNT = 900  # 剔除实付金额小于 900 的记录
TZ = ZoneInfo("Asia/Shanghai")

# 周节奏：看板 week_series 索引 = [第一二周, 第三周, 第四周, 第五周, 今日]
WEEK_INDEX = {"w1": 0, "w3": 1, "w4": 2, "w5": 3}


# ---------------- 基础工具 ----------------
def run(cmd, check=True, input_data=None):
    r = subprocess.run(cmd, capture_output=True, text=True, input=input_data)
    if check and r.returncode != 0:
        raise RuntimeError(f"命令失败: {' '.join(cmd)}\nSTDERR: {r.stderr}")
    return r.stdout, r.stderr, r.returncode


def _clean_cli_output(out):
    out = re.sub(r"<security_notice>.*?</security_notice>", "", out, flags=re.S)
    out = re.sub(r"<extra_identity_context>.*?</extra_identity_context>", "", out, flags=re.S)
    return out.strip()


def _safe_json_load(out):
    """处理 wecom-cli 可能把大结果写入临时文件返回路径的情况。"""
    out = _clean_cli_output(out)
    if not out:
        return {}
    # 若返回的是文件路径而非 JSON，读取文件
    if not out.lstrip().startswith("{") and "\n" not in out and os.path.exists(out):
        with open(out, encoding="utf-8") as f:
            out = _clean_cli_output(f.read())
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"JSON 解析失败: {e}\n内容: {out[:500]}")


def wecom_query(sqllist):
    """执行 smartsheet records query，返回与 sqllist 一一对应的 rows 列表。"""
    cmd = ["wecom-cli", "smartsheet", "records", "query", "--docid", DOCID]
    for s in sqllist:
        cmd += ["--sql", s]
    out, err, code = run(cmd, check=False)
    if code != 0:
        raise RuntimeError(f"wecom-cli 查询失败: {err or out[:500]}")
    data = _safe_json_load(out)
    if data.get("errcode", 0) != 0:
        raise RuntimeError(f"wecom-cli 返回错误: {data.get('errmsg', data)}")
    result = []
    for v in data.get("values", []):
        if isinstance(v, str):
            v = json.loads(v)
        result.append(v.get("rows", []) if isinstance(v, dict) else [])
    return result


# ---------------- 数据拉取 ----------------
def fetch_mapping():
    """门店名称 -> {city, supervisor}"""
    rows = wecom_query([f"SELECT `门店名称`, `所属城市`, `督导` FROM `{SHEET_BASE}` LIMIT 1000"])[0]
    mapping = {}
    for r in rows:
        name = r.get("门店名称")
        if not name:
            continue
        city = r.get("所属城市") or ""
        sup = r.get("督导") or ""
        if isinstance(sup, list):
            sup = sup[0] if sup else ""
        if isinstance(city, list):
            city = city[0] if city else ""
        mapping[name] = {"city": city, "supervisor": sup}
    return mapping


def fetch_daily(date_str):
    """拉取指定日期的实付记录（过滤 + 按券码去重）。返回 list[dict]。"""
    sql = (
        f"SELECT `门店`, `实付金额`, `平台`, `券码` FROM `{SHEET_DAILY}` "
        f"WHERE DATE_FORMAT(`日期`, \"%Y-%m-%d\") = \"{date_str}\" "
        f"AND `实付金额` >= {MIN_AMOUNT} ORDER BY `填写时间` ASC"
    )
    rows = wecom_query([sql])[0]
    seen = set()
    out = []
    for r in rows:
        code = r.get("券码")
        if code is not None and code in seen:
            continue
        if code is not None:
            seen.add(code)
        out.append(r)
    return out


def map_store(name, mapping):
    if name in mapping:
        return {"city": mapping[name]["city"], "supervisor": mapping[name]["supervisor"], "canonical": name}
    # 兜底：门店名缺「店」后缀等，归一化到基本面标准名
    if name + "店" in mapping:
        return {"city": mapping[name + "店"]["city"], "supervisor": mapping[name + "店"]["supervisor"], "canonical": name + "店"}
    return {"city": "", "supervisor": "", "canonical": name}


# ---------------- 聚合 ----------------
def aggregate_by_store(rows, mapping):
    """把当日记录聚合到门店维度。"""
    agg = {}
    for r in rows:
        raw_name = r.get("门店") or ""
        amt = float(r.get("实付金额") or 0)
        info = map_store(raw_name, mapping)
        name = info["canonical"]
        if name not in agg:
            agg[name] = {
                "amount": 0.0, "cnt": 0,
                "city": info["city"], "supervisor": info["supervisor"],
                "platform": r.get("平台") or "",
            }
        agg[name]["amount"] += amt
        agg[name]["cnt"] += 1
    for v in agg.values():
        v["amount"] = round(v["amount"], 2)
    return agg


def build_today_items(rows, mapping):
    items = []
    for r in rows:
        raw_name = r.get("门店") or ""
        info = map_store(raw_name, mapping)
        items.append({
            "store": info["canonical"],
            "city": info["city"],
            "supervisor": info["supervisor"],
            "amount": float(r.get("实付金额") or 0),
            "platform": r.get("平台") or "",
            "submiter": "",
            "pianqu": "",
        })
    return items


# ---------------- 门店对象维护 ----------------
def find_or_create_store(D, name, city, supervisor):
    for s in D["stores"]:
        if s["store"] == name:
            return s
    s = {
        "store": name, "city": city, "supervisor": supervisor,
        "month": 0.0, "today": 0.0, "total": 0.0,
        "w1": 0.0, "w3": 0.0, "w4": 0.0, "w5": 0.0,
        "today_cnt": 0, "status": "",
    }
    D["stores"].append(s)
    return s


def week_key(date_str):
    d = int(date_str[8:10])
    if d <= 13:
        return "w1"
    if d <= 20:
        return "w3"
    if d <= 25:
        return "w4"
    return "w5"


def next_day(date_str):
    return (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


# ---------------- 重算 ----------------
def recompute(D, today_mmdd):
    stores = D["stores"]
    # 移除无任何活动（month 与 today 均为 0）的门店
    stores = [s for s in stores if s["month"] > 0 or s["today"] > 0]
    D["stores"] = stores
    for s in stores:
        s["total"] = round(s["month"] + s["today"], 2)
        if s["month"] > 0 and s["today"] > 0:
            s["status"] = "本月+今日"
        elif s["month"] > 0:
            s["status"] = "本月已冲"
        else:
            s["status"] = "仅今日"
    stores.sort(key=lambda s: (-s["total"], s["store"]))

    month_total = round(sum(s["month"] for s in stores), 2)
    month_stores = sum(1 for s in stores if s["month"] > 0)
    today_total = round(sum(s["today"] for s in stores), 2)
    today_stores = sum(1 for s in stores if s["today"] > 0)
    today_rows = sum(s["today_cnt"] for s in stores)

    D["kpi"]["month_total"] = month_total
    D["kpi"]["month_stores"] = month_stores
    D["kpi"]["today_total"] = today_total
    D["kpi"]["today_stores"] = today_stores
    D["kpi"]["today_rows"] = today_rows

    w1s = sum(1 for s in stores if s["w1"] > 0)
    w3s = sum(1 for s in stores if s["w3"] > 0)
    w4s = sum(1 for s in stores if s["w4"] > 0)
    w5s = sum(1 for s in stores if s["w5"] > 0)
    D["kpi"]["month_rows"] = w1s + w3s + w4s + w5s

    ws = D["week_series"]
    ws[0]["amount"] = round(sum(s["w1"] for s in stores), 2)
    ws[0]["stores"] = w1s
    ws[1]["amount"] = round(sum(s["w3"] for s in stores), 2)
    ws[1]["stores"] = w3s
    ws[2]["amount"] = round(sum(s["w4"] for s in stores), 2)
    ws[2]["stores"] = w4s
    ws[3]["amount"] = round(sum(s["w5"] for s in stores), 2)
    ws[3]["stores"] = w5s
    ws[4]["amount"] = today_total
    ws[4]["stores"] = today_stores
    ws[4]["sub"] = today_mmdd

    # by_city
    city_map = defaultdict(lambda: {"month": 0.0, "today": 0.0, "month_stores": 0, "today_stores": 0})
    for s in stores:
        c = city_map[s["city"] or "未知"]
        c["month"] += s["month"]
        c["today"] += s["today"]
        if s["month"] > 0:
            c["month_stores"] += 1
        if s["today"] > 0:
            c["today_stores"] += 1
    by_city = []
    for name, c in city_map.items():
        c["month"] = round(c["month"], 2)
        c["today"] = round(c["today"], 2)
        c["total"] = round(c["month"] + c["today"], 2)
        by_city.append({"name": name, "month": c["month"], "today": c["today"],
                        "total": c["total"], "month_stores": c["month_stores"],
                        "today_stores": c["today_stores"]})
    by_city.sort(key=lambda x: -x["total"])
    D["by_city"] = by_city

    # by_sup
    sup_map = defaultdict(lambda: {"month": 0.0, "today": 0.0, "month_stores": 0, "today_stores": 0})
    for s in stores:
        c = sup_map[s["supervisor"] or "未知"]
        c["month"] += s["month"]
        c["today"] += s["today"]
        if s["month"] > 0:
            c["month_stores"] += 1
        if s["today"] > 0:
            c["today_stores"] += 1
    by_sup = []
    for name, c in sup_map.items():
        c["month"] = round(c["month"], 2)
        c["today"] = round(c["today"], 2)
        c["total"] = round(c["month"] + c["today"], 2)
        by_sup.append({"name": name, "month": c["month"], "today": c["today"],
                       "total": c["total"], "month_stores": c["month_stores"],
                       "today_stores": c["today_stores"]})
    by_sup.sort(key=lambda x: -x["total"])
    D["by_sup"] = by_sup


def upsert_trend(D, date_str, amount, cnt):
    for t in D["trend"]:
        if t["date"] == date_str:
            t["amount"] = round(amount, 2)
            t["cnt"] = cnt
            return
    D["trend"].append({"date": date_str, "amount": round(amount, 2), "cnt": cnt})
    D["trend"].sort(key=lambda x: x["date"])


# ---------------- 提取/写回 D ----------------
def extract_D(html):
    m = re.search(r"const D = (\{.*?\});\n", html, re.S)
    if not m:
        raise RuntimeError("未能在 index.html 中找到 const D 对象")
    return json.loads(m.group(1))


def patch_template(html):
    """修补看板模板里硬编码的日期，使其动态跟随 D.meta.today（幂等）。"""
    # KPI「今日已冲锋」日期标签动态化（位于 JS 模板字符串内，可用 ${}）
    html = html.replace(
        '今日已冲锋 <span class="tag">10-27</span>',
        '今日已冲锋 <span class="tag">${D.meta.today.slice(5)}</span>',
    )
    # 页脚口径说明去掉固定日期
    html = html.replace(
        "每日表中 2026-10-27 当日「实付金额」",
        "每日表中当日「实付金额」",
    )
    html = html.replace(
        "今日（2026-10-27）仍算今日，次日再计入本月；今日属第五周第 2 天",
        "今日仍算今日，次日再计入本月",
    )
    return html


def write_D(html, D):
    new = json.dumps(D, ensure_ascii=False, separators=(",", ":"))
    # 压缩无意义的浮点，保持与原文一致（去掉 .0 之类）——这里保持 json 输出即可
    new_html = re.sub(r"const D = \{.*?\};\n", "const D = " + new + ";\n", html, count=1, flags=re.S)
    return new_html


# ---------------- GitHub 读写（走 gh Contents API，规避 git 代理问题） ----------------
def gh_read_index():
    out, err, code = run(["gh", "api", f"repos/{REPO}/contents/index.html"], check=False)
    if code != 0:
        raise RuntimeError(f"gh api 读取 index.html 失败: {err or out[:500]}")
    data = json.loads(out)
    html = base64.b64decode(data.get("content", "")).decode("utf-8")
    return html, data.get("sha", "")


def gh_write_index(html, sha):
    content_b64 = base64.b64encode(html.encode("utf-8")).decode("utf-8")
    payload = json.dumps({
        "message": "chore: 更新冲锋看板数据",
        "content": content_b64,
        "sha": sha,
    })
    out, err, code = run(
        ["gh", "api", "-X", "PUT", f"repos/{REPO}/contents/index.html", "--input", "-"],
        check=False, input_data=payload,
    )
    if code != 0:
        raise RuntimeError(f"gh api 写入 index.html 失败: {err or out[:500]}")
    return out


# ---------------- 主流程 ----------------
def _arg(name):
    """从命令行参数取值，支持 --name value 与 --name=value 两种形式。"""
    for i, a in enumerate(sys.argv):
        if a == name and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def main():
    dry_run = "--dry-run" in sys.argv
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    today_mmdd = now.strftime("%m/%d")
    updated_str = now.strftime("%Y-%m-%d %H:%M")

    print(f"[*] 今日 = {today} | 更新时间 = {updated_str} | 模式 = {'dry-run' if dry_run else '实跑'}")

    print("[*] 拉取门店基本面映射 ...")
    mapping = fetch_mapping()
    print(f"    基本面门店数: {len(mapping)}")

    input_path = _arg("--input")
    if input_path:
        print(f"[*] 读取本地看板 {input_path} ...")
        with open(input_path, encoding="utf-8") as f:
            html = f.read()
        sha = None
    else:
        print("[*] 读取 GitHub 看板 ...")
        html, sha = gh_read_index()
    html = patch_template(html)
    D = extract_D(html)
    old_today = D["meta"]["today"]
    print(f"    看板当前今日 = {old_today}")

    # ---- 结转：把所有已结束日期（old_today .. today-1）并入本月 ----
    rollover_log = []
    d = old_today
    while d < today:
        print(f"[*] 结转 {d} -> 本月 ...")
        rows = fetch_daily(d)
        agg = aggregate_by_store(rows, mapping)
        wk = week_key(d)
        wk_idx = WEEK_INDEX[wk]
        y_total = round(sum(v["amount"] for v in agg.values()), 2)
        y_cnt = len(rows)
        # 先清空当前「今日」字段（今日已变为昨日）
        for s in D["stores"]:
            s["today"] = 0.0
            s["today_cnt"] = 0
        # 把昨日门店金额并入 month 与对应周
        for name, v in agg.items():
            s = find_or_create_store(D, name, v["city"], v["supervisor"])
            s[wk] = round(s[wk] + v["amount"], 2)
            s["month"] = round(s["month"] + v["amount"], 2)
        # 更新该周的 sub 提示（格式 MM/DD，与看板一致）
        D["week_series"][wk_idx]["sub"] = d[5:].replace("-", "/")
        # 更新趋势里昨日为最终值
        upsert_trend(D, d, y_total, y_cnt)
        rollover_log.append({"date": d, "amount": y_total, "cnt": y_cnt, "stores": len(agg)})
        d = next_day(d)

    # ---- 今日 ----
    print(f"[*] 拉取今日 {today} 数据 ...")
    trows = fetch_daily(today)
    tagg = aggregate_by_store(trows, mapping)
    for s in D["stores"]:
        s["today"] = 0.0
        s["today_cnt"] = 0
    for name, v in tagg.items():
        s = find_or_create_store(D, name, v["city"], v["supervisor"])
        s["today"] = v["amount"]
        s["today_cnt"] = v["cnt"]
    t_total = round(sum(v["amount"] for v in tagg.values()), 2)
    t_stores = len(tagg)
    t_rows = len(trows)
    upsert_trend(D, today, t_total, t_rows)
    D["today_items"] = build_today_items(trows, mapping)

    # ---- 重算 + 写 meta ----
    D["meta"]["today"] = today
    D["meta"]["updated"] = updated_str
    recompute(D, today_mmdd)

    # 输出摘要
    print("\n========== 更新摘要 ==========")
    for rl in rollover_log:
        print(f"  结转 {rl['date']}: {rl['amount']} 元 / {rl['cnt']} 笔 / {rl['stores']} 家门店")
    print(f"  今日 {today}: {t_total} 元 / {t_rows} 笔 / {t_stores} 家门店")
    print(f"  本月已冲锋(重算): {D['kpi']['month_total']} 元 / {D['kpi']['month_stores']} 家门店")
    print(f"  门店明细数: {len(D['stores'])} | 城市数: {len(D['by_city'])} | 督导数: {len(D['by_sup'])}")

    # 未匹配门店告警
    unmapped = [n for n in tagg if not tagg[n]["city"] and not tagg[n]["supervisor"]]
    if unmapped:
        print(f"  ⚠️ 未匹配到基本面的门店(今日): {unmapped}")

    if dry_run:
        print("\n[dry-run] 不写文件、不推送。")
        # 打印今日 top5 门店用于核对
        top5 = sorted(tagg.items(), key=lambda x: -x[1]["amount"])[:5]
        print("  今日 Top5 门店:")
        for n, v in top5:
            print(f"    {n}  {v['amount']} 元 / {v['cnt']} 笔 / {v['city']} / {v['supervisor']}")
        return

    new_html = write_D(html, D)
    output_path = _arg("--output")
    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(new_html)
        print(f"\n[OK] 已写出新看板到 {output_path}（未推送 GitHub）。")
    else:
        gh_write_index(new_html, sha)
        print("\n[OK] 已推送更新到 GitHub Pages。")


if __name__ == "__main__":
    main()
