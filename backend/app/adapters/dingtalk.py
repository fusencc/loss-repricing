"""Fixed official DingTalk endpoints. Never persist/log raw responses, auth codes or exceptions."""
import time
import json
from urllib.parse import urlsplit
import httpx

TOKEN = 'https://api.dingtalk.com/v1.0/oauth2/accessToken'
OAPI = 'https://oapi.dingtalk.com/'
SEND = 'https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend'
RESULT = 'https://api.dingtalk.com/v1.0/robot/oToMessages/readStatus'
USER = 'topapi/v2/user/getuserinfo'


class DingTalkClient:
    def __init__(self, config, transport=None):
        self.config = config
        self.http = httpx.Client(timeout=15, transport=transport, follow_redirects=False, trust_env=False)
        self._cached_token = None
        self._token_until = 0

    def close(self):
        self.http.close()

    def _token(self):
        if self._cached_token and time.monotonic() < self._token_until:
            return self._cached_token
        r = self.http.post(TOKEN, json={'appKey':self.config.ding_client_id,
                                       'appSecret':self.config.ding_client_secret})
        r.raise_for_status()
        value = r.json()
        token = value.get('accessToken')
        if not isinstance(token,str) or not token:
            raise ValueError('应用凭证获取失败')
        self._cached_token = token
        self._token_until = time.monotonic() + max(0, min(int(value.get('expireIn',0)),7200)-120)
        return token

    def _post(self, endpoint, payload):
        r = self.http.post(OAPI+endpoint, params={'access_token':self._token()},json=payload)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data,dict):
            raise ValueError('无效钉钉响应')
        return data

    def user_from_code(self, code):
        try:
            data = self._post(USER,{'code':code})
            result=data.get('result',{})
            if data.get('errcode') != 0 or not isinstance(result.get('userid'),str) or not result['userid']:
                raise ValueError('钉钉免登失败，请重新获取授权码')
            return {'userid':result['userid'], 'name':str(result.get('name',''))}
        except Exception:
            raise ValueError('钉钉免登失败，请重新获取授权码') from None

    def send_notice(self, userid, title, markdown, url):
        # 官方人与机器人会话接口；每次只传一个经业务路由确认的销售员 userId。
        # 网页未部署时直接发送摘要，不伪造外网详情地址。
        if url:
            parsed=urlsplit(url)
            if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password or parsed.query:
                return {'state':'failed','error':'通知链接必须为不含凭证或查询参数的HTTPS地址'}
        try:
            token=self._token()
        except Exception:
            return {'state':'failed','error':'钉钉应用凭证获取失败，未提交消息'}
        text = markdown + (f"\n\n[查看详情]({url})" if url else '')
        message = {'title': title[:100], 'text': text}
        # 沿用本机预算项目机器人单聊的请求和回执处理，使用本项目的应用凭据。
        try:
            response = self.http.post(
                SEND,
                headers={'x-acs-dingtalk-access-token': token},
                json={
                    'robotCode': self.config.ding_client_id,
                    'userIds': [userid],
                    'msgKey': 'sampleMarkdown',
                    'msgParam': json.dumps(message, ensure_ascii=False),
                },
            )
        except (httpx.ConnectError, httpx.ConnectTimeout):
            return {'state': 'failed', 'error': '连接发送接口失败，未发送消息'}
        except Exception:
            return {'state': 'unknown', 'error': '发送响应未知，禁止自动重发'}

        if response.status_code >= 500:
            return {'state': 'unknown', 'error': '发送服务返回错误，结果不明'}
        try:
            result = response.json()
        except ValueError:
            return {'state': 'unknown', 'error': '发送返回无法识别，需核查回执'}
        if not isinstance(result, dict):
            return {'state': 'unknown', 'error': '发送返回无法识别，需核查回执'}
        if result.get('invalidStaffIdList') or result.get('filteredStaffIdList'):
            return {'state': 'failed', 'error': '钉钉拒绝接收人，请核对绑定与应用范围'}
        if response.status_code == 429 or result.get('flowControlledStaffIdList'):
            return {'state': 'failed', 'error': '钉钉限流，消息未受理'}
        if response.is_error:
            return {'state': 'failed', 'error': f'机器人接口明确拒绝消息，HTTP {response.status_code}'}
        key = result.get('processQueryKey')
        if not isinstance(key, str) or not key:
            return {'state': 'unknown', 'error': '机器人未返回消息标识，禁止自动重发'}
        return {'state': 'accepted', 'task_id': key}

    def query_notice(self, task_id, userid):
        try:
            r=self.http.get(RESULT,headers={'x-acs-dingtalk-access-token':self._token()},
                params={'robotCode':self.config.ding_client_id,'processQueryKey':task_id})
            r.raise_for_status()
            data=r.json()
            # READ/UNREAD 必须属于目标收件人；其他人的已读不作为该人的送达证据。
            for row in data.get('messageReadInfoList',[]):
                if row.get('userId') == userid:
                    if row.get('readStatus') == 'READ':
                        return {'state':'read'}
                    if row.get('readStatus') == 'UNREAD' and data.get('sendStatus') == 'SUCCESS':
                        return {'state':'delivered'}
            return {'state':'accepted','error':'机器人已受理，尚无该收件人送达证据'}
        except Exception:
            return {'state':'accepted','error':'发送结果查询失败，保留原任务，禁止重发'}
