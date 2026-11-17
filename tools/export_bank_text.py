# -*- coding: utf-8 -*-
"""把本地题库导出成「人能直接读」的文本。

为什么要有这个脚本：题库的价值不是 ABCD，而是
    「这道题 + 每个选项的原文 + 哪几项原文是对的」
只有把答案还原成文字，才能确认库里存的是「知识」，而不是换个选项
顺序就作废的字母索引。

用法：
    runtime\\python.exe tools\\export_bank_text.py
输出：
    D:\\OCS-AI\\题库_可读版.txt
"""
import json
import os
import sqlite3
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "data", "bank.db")
OUT = os.path.join(BASE, "题库_可读版.txt")

TYPE_NAME = {"single": "单选", "multiple": "多选", "judgement": "判断",
             "completion": "填空", "short": "简答"}


def load():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT title,qtype,answer,options,source,verified,hits FROM bank "
        "ORDER BY qtype, title"
    ).fetchall()
    conn.close()
    return rows


def render(rows):
    lines = []
    lines.append("本地题库可读版  共 %d 题  导出时间 %s" % (
        len(rows), time.strftime("%Y-%m-%d %H:%M")))
    lines.append("=" * 72)
    cur = None
    for i, r in enumerate(rows, 1):
        qt = r["qtype"]
        if qt != cur:
            cur = qt
            lines.append("")
            lines.append("【%s】" % TYPE_NAME.get(qt, qt))
            lines.append("-" * 72)
        ans = str(r["answer"] or "").strip()
        letters = [x for x in ans.split("#") if x]
        try:
            opts = json.loads(r["options"] or "[]")
        except Exception:
            opts = []

        lines.append("%d. %s" % (i, r["title"]))
        if opts:
            for o in opts:
                mark = "*" if o[0] in letters else " "
                lines.append("   %s %s. %s" % (mark, o[0], o[1]))
        # 把答案还原成文字 —— 这一行才是真正被记住的东西
        if opts and all(l in [o[0] for o in opts] for l in letters):
            texts = [o[1] for o in opts if o[0] in letters]
            lines.append("   >> 正确答案：%s" % "；".join(texts))
        else:
            lines.append("   >> 正确答案：%s" % ans)
        lines.append("      [来源 %s / 命中 %d 次]" % (r["source"], r["hits"]))
    return "\n".join(lines)


def main():
    rows = load()
    text = render(rows)
    with open(OUT, "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write(text)
    print("已导出 %d 题 -> %s" % (len(rows), OUT))


if __name__ == "__main__":
    main()
