"""对已成功的真实执行查询官方日志并补档；本入口始终禁止上传。"""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'backend'))

from app.adapters.manual_live import ManualLiveAdapter
from app.config import get_settings
from app.db import create_database
from app.services.price_log_archive import archive_verified_price_record


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--authorization',type=Path,required=True,help='原执行authorization.json路径；同目录须有持久intent文件')
    parser.add_argument('--execution-id',required=True)
    parser.add_argument('--output',type=Path,help='新增补档版本目录的父目录')
    args=parser.parse_args()
    settings=get_settings()
    database=settings.live_history_database_url.get_secret_value()
    if not database: raise ValueError('缺少独立真实档案数据库绑定')
    config=settings.model_copy(update={'adapter_mode':'live','database_url':database,'live_submit_enabled':False})
    path=args.authorization.resolve()
    authorization=json.loads(path.read_text(encoding='utf-8-sig'))
    adapter=ManualLiveAdapter(config,authorization,path.parent)
    engine,factory=create_database(database)
    try:
        result=archive_verified_price_record(factory,adapter,args.execution_id,
            args.output or settings.runtime_dir/'price-log-archives')
        print(json.dumps(result,ensure_ascii=False,indent=2))
    finally:
        adapter.client.close()
        engine.dispose()


if __name__=='__main__':
    main()
