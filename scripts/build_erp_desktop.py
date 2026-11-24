"""从项目源文件确定性构建独立可读脚本；不读取.env或运行目录。"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def definitions(relative, names):
    source = (ROOT / relative).read_text(encoding='utf-8')
    tree = ast.parse(source)
    selected = {}
    for node in tree.body:
        name = getattr(node, 'name', None)
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
        if name in names:
            selected[name] = ast.get_source_segment(source, node)
    if selected.keys() != set(names):
        raise ValueError('独立脚本构建缺少指定源码')
    return '\n\n'.join(selected.values())


def build(output):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    core=(ROOT/'backend/app/adapters/erp_bulk_capture.py').read_text(encoding='utf-8')
    lines=core.splitlines(keepends=True)
    for node in reversed(ast.parse(core).body):
        if isinstance(node,ast.ImportFrom) and node.module.startswith('app.'):
            del lines[node.lineno-1:node.end_lineno]
    privacy=(ROOT/'backend/app/adapters/transport_privacy.py').read_text(encoding='utf-8')
    source = '# -*- coding: utf-8 -*-\n# 由项目 scripts/build_erp_desktop.py 生成；不依赖项目或WSL。\n'
    source += 'import json\n' + privacy + '\n'
    source += definitions('backend/app/adapters/base.py',['SourceUnavailable'])+'\n'
    source += definitions('backend/app/adapters/erp_web.py',
        ['ErpBusinessFailure', 'ErpAccountRateLimited', 'error_evidence', '_failure', 'retry_erp_read', 'safe_page_excerpt', 'WEB_BASE']) + '\n'
    source += definitions('backend/app/services/erp_capture_store.py',['write_verified'])+'\n'
    source += ''.join(lines)+'\n'
    # 批量核心通过event_callback注入日志，独立文件采用入口内的标准库白名单记录器，
    # 不嵌入项目配置、凭据、环境文件或项目日志单例。
    source += (ROOT/'scripts/erp_desktop_entry.py').read_text(encoding='utf-8')
    compile(source,'erp_capture.py','exec')
    target=output/'erp_capture.py';target.write_text(source,encoding='utf-8')
    return target


if __name__ == '__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('output',type=Path)
    print(build(p.parse_args().output))
