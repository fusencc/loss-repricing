"""Fixed official DingTalk endpoints. Never persist/log raw responses, auth codes or exceptions."""
import time
from urllib.parse import urlsplit
import httpx

TOKEN = 'https://api.dingtalk.com/v1.0/oauth2/accessToken'
OAPI = 'https://oapi.dingtalk.com/'
SEND = 'topapi/message/corpconversation/asyncsend_v2'
RESULT = 'topapi/message/corpconversation/getsendresult'
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
        parsed=urlsplit(url)
        if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password or parsed.query:
            return {'state':'failed','error':'通知链接必须为不含凭证或查询参数的HTTPS地址'}
        try:
            # Token failure precedes send and is therefore a known failure.
            self._token()
        except Exception:
            return {'state':'failed','error':'钉钉应用凭证获取失败，未提交消息'}
        try:
            data=self._post(SEND, {'agent_id':int(self.config.ding_agent_id),'userid_list':userid,
                'to_all_user':False,'msg':{'msgtype':'action_card','action_card':{
                    'title':title[:100], 'markdown':markdown[:500], 'single_title':'查看详情','single_url':url}}})
        except Exception:
            return {'state':'unknown','error':'发送响应未知，禁止自动重发'}
        result={key:str(data[key])[:255] for key in ('task_id','request_id') if data.get(key) is not None}
        if data.get('errcode') == 0 and result.get('task_id'):
            return dict(result,state='accepted')
        if isinstance(data.get('errcode'),int) and data['errcode'] != 0:
            return dict(result,state='failed',error='钉钉明确拒绝消息，错误码 '+str(data['errcode']))
        return dict(result,state='unknown',error='钉钉未返回有效任务ID，禁止重发')

    def query_notice(self, task_id, userid):
        try:
            data=self._post(RESULT,{'agent_id':int(self.config.ding_agent_id),'task_id':int(task_id)})
            result=data.get('send_result',{})
            ids={key:str(data[key])[:255] for key in ('request_id',) if data.get(key) is not None}
            if data.get('errcode') != 0:
                return dict(ids,state='accepted',error='发送结果暂不可查询')
            # Only evidence for the requested recipient counts.
            for key in ('invalid_user_id_list','forbidden_user_id_list','failed_user_id_list'):
                if userid in result.get(key,[]):
                    return dict(ids,state='failed',error='钉钉结果明确未发送：'+key)
            if any(row.get('userid')==userid for row in result.get('forbidden_list',[])):
                return dict(ids,state='failed',error='钉钉结果明确限制发送')
            if userid in result.get('read_user_id_list',[]):
                return dict(ids,state='read')
            if userid in result.get('unread_user_id_list',[]):
                return dict(ids,state='delivered')
            return dict(ids,state='accepted',error='任务已受理，尚无该收件人送达证据')
        except Exception:
            return {'state':'accepted','error':'发送结果查询失败，保留原任务，禁止重发'}
