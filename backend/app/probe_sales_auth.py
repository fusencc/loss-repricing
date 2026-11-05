"""仅测试销售系统认证，不导入/调用改价适配器或业务接口。

根目录运行：.venv/Scripts/python -m app.probe_sales_auth（需PYTHONPATH=backend）。
--renew-login 会再调用一次同一登录接口，验证可重新取证，不调用未确认的刷新接口。
"""
import argparse
import json
import httpx
from app.adapters.base import SourceUnavailable
from app.adapters.sales_auth import SalesAuth
from app.config import Settings


def main():
    parser=argparse.ArgumentParser(description='只读认证测试，不查询或修改商品')
    parser.add_argument('--renew-login',action='store_true')
    args=parser.parse_args()
    settings=Settings()
    try:
        with httpx.Client(timeout=30,follow_redirects=False,trust_env=False) as client:
            auth=SalesAuth(settings,client)
            auth.get_token()
            result={'initial':auth.summary()}
            if args.renew_login:
                auth.get_token(force_login=True)
                result['renewed']=auth.summary()
            result['live_submit_enabled']=settings.live_submit_enabled
            print(json.dumps(result,ensure_ascii=False))
    except SourceUnavailable as error:
        print(json.dumps({'authenticated':False,'error':str(error)},ensure_ascii=False))
        raise SystemExit(1) from None


if __name__=='__main__':main()
