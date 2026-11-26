"""把同一类别的多批订单明细合并成一个表，便于直接查看/导入。

用法：python merge_batches.py <下载目录>
只合并 <key>-订单明细-NNN.xlsx，输出 <key>-合并.xlsx，并打印每类的行数/订单号数/状态分布。
"""
from __future__ import annotations

import sys
import warnings
from collections import Counter
from pathlib import Path

from openpyxl import Workbook, load_workbook


def read_sheet(path):
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        book = load_workbook(path, read_only=True, data_only=True)
        try:
            sheet = book.active
            sheet.reset_dimensions()
            rows = list(sheet.iter_rows(values_only=True))
        finally:
            book.close()
    return rows[0] if rows else (), rows[1:]


def main(folder_arg):
    folder = Path(folder_arg)
    groups = {}
    for path in sorted(folder.glob('*-订单明细-*.xlsx')):
        key = path.name.split('-订单明细-')[0]
        groups.setdefault(key, []).append(path)
    if not groups:
        print('没有找到订单明细文件')
        return 1

    summary = []
    for key, paths in sorted(groups.items()):
        headers = None
        merged = []
        seen = set()
        duplicates = 0
        for path in paths:
            head, rows = read_sheet(path)
            if headers is None:
                headers = head
            elif list(head) != list(headers):
                print(f'  警告：{path.name} 表头与首文件不一致，仍按首文件表头写入')
            for row in rows:
                if all(v is None for v in row):
                    continue
                order = row[list(headers).index('订单编号')] if '订单编号' in headers else None
                if order in seen:
                    duplicates += 1
                    continue
                seen.add(order)
                merged.append(row)
        dest = folder / f'{key}-合并.xlsx'
        book = Workbook(write_only=True)
        sheet = book.create_sheet('订单明细')
        sheet.append(list(headers))
        for row in merged:
            sheet.append(list(row))
        book.save(dest)
        book.close()
        status_col = list(headers).index('订单状态') if '订单状态' in headers else None
        dist = Counter(row[status_col] for row in merged) if status_col is not None else {}
        summary.append({'key': key, 'files': len(paths), 'rows': len(merged),
                        'orders': len(seen), 'duplicates_removed': duplicates,
                        'status': dict(dist), 'output': str(dest)})
        print(f'{key}: {len(paths)} 批 → {len(merged)} 行 / {len(seen)} 单'
              f'（去重 {duplicates}），状态 {dict(dist)} → {dest.name}', flush=True)

    import json
    (folder / 'merge-summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'合并结果：{folder / "merge-summary.json"}', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else '.'))
