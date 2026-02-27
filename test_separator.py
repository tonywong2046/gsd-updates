#!/usr/bin/env python3
"""
本地测试：验证 insert_rows 空行位置是否正确。
不需要 Google API，只用 gspread 的 mock 来模拟行为。

运行：python3 test_separator.py
"""

existing_sheet = [
    ["日期", "领域", "期刊名称", "作者", "题目", "中文简介", "链接"],
    ["2026-02-25", "医学社会学", "Old Journal A", "Old Author", "Old Title A", "旧摘要A", "https://old-a"],
    ["2026-02-25", "老年学",     "Old Journal B", "Old Author", "Old Title B", "旧摘要B", "https://old-b"],
]

new_rows = [
    ["'2026-02-26", "医学社会学",   "Journal of Health", "Lei Jin",  "New Title 1", "新摘要1", "https://new-1"],
    ["'2026-02-26", "老年学",       "Journal of Aging",  "Sophie",   "New Title 2", "新摘要2", "https://new-2"],
    ["'2026-02-26", "计算社会科学", "Nature HB",         "N/A",      "New Title 3", "新摘要3", "https://new-3"],
    ["'2026-02-26", "计算社会科学", "Social Sci CR",     "Manuel",   "New Title 4", "新摘要4", "https://new-4"],
]

separator = [["" ] * len(new_rows[0])]

def simulate_insert_rows(sheet, values_to_insert, row=2):
    result = sheet[:row-1] + values_to_insert + sheet[row-1:]
    return result

def print_sheet(sheet, title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")
    for i, r in enumerate(sheet, start=1):
        if i == 1:
            print(f"  Row {i:2d} [HEADER]: {r[1]}")
        elif all(c == "" for c in r):
            print(f"  Row {i:2d} [  空行 ]: ─── ← 分隔线")
        else:
            tag = "NEW" if "2026-02-26" in str(r[0]) else "OLD"
            print(f"  Row {i:2d} [{tag}    ]: {r[1]:10s} | {r[2][:25]}")

sheet_A = simulate_insert_rows(existing_sheet, separator + new_rows, row=2)
print_sheet(sheet_A, "❌ 旧代码: insert_rows(separator + rows)  ← 空行在数据前，没有分隔效果")

sheet_B = simulate_insert_rows(existing_sheet, new_rows + separator, row=2)
print_sheet(sheet_B, "✅ 新代码: insert_rows(rows + separator)  ← 空行在新旧数据之间")

print("\n结论：新代码中空行会出现在新数据和旧数据之间，效果正确。")
