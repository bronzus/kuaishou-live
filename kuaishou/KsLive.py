import _thread
import binascii
import copy
import json
import logging
import os
import random
import re
import time
from typing import Optional

import websocket
import requests

from protobuf_inspector.types import StandardParser
from google.protobuf import json_format
from selenium.webdriver.chrome.options import Options
from selenium import webdriver

from .ks_pb2 import CSWebEnterRoom
from .ks_pb2 import CSWebHeartbeat
from .ks_pb2 import SocketMessage
from .ks_pb2 import SCHeartbeatAck
from .ks_pb2 import PayloadType
from .ks_pb2 import SCWebFeedPush
from .ks_pb2 import SCWebLiveWatchingUsers
from .ks_pb2 import SCWebEnterRoomAck


class NoLivingException(Exception):
    """主播还没有开始直播"""


class CookieNotUseful(Exception):
    pass


user_agent = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.36'


class Tool:
    apiHost = 'https://live.kuaishou.com/live_graphql'
    # 进入房间时需要的token
    token = ''
    # 网页token
    cookie = ''
    # websocket地址
    webSocketUrl = ''
    # 房间号
    liveRoomId = None
    # 直播网页地址
    liveUrl = ''
    # 公共请求头
    headers = {
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9',
        'User-Agent': user_agent,
    }
    # 存储用户直播信息 比如直播地址
    userLiveInfo = ''

    # 初始化
    def __init__(self, liveUrl: str, chrome_bin_path: str, chrome_driver_path: str, runtime_dir: str,
                 proxy_host: Optional[str] = None, proxy_port: Optional[str] = None, cookie: Optional[str] = None):
        self.feed_push_callback = None

        self.liveUrl = liveUrl
        self.chrome_bin_path = chrome_bin_path
        self.chrome_driver_path = chrome_driver_path
        self.runtime_dir = runtime_dir
        self.proxy_host = proxy_host
        self.proxy_port = proxy_port
        if self.proxy_host is not None and self.proxy_port is not None:
            _proxy = f"{self.proxy_host}:{self.proxy_port}"
            self._request_proxies = {
                'http': 'http://' + _proxy,
                'https': 'https://' + _proxy
            }
        else:
            self._request_proxies = None
        self.headers['Referer'] = self.liveUrl
        if cookie:
            self.headers['cookie'] = cookie
        else:
            self.headers['cookie'] = self._get_cookie()

    @staticmethod
    def get_browser(chrome_bin_path, chrome_driver_path, user_data_dir: str, proxy_host: Optional[str] = None,
                    proxy_port: Optional[str] = None):
        options = Options()
        options.binary_location = chrome_bin_path
        options.add_argument('--headless')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument("--start-maximized")
        options.add_argument("--disable-gpu")
        # options.add_argument('--incognito')  # 无痕
        if proxy_host is not None and proxy_port is not None:
            options.add_argument(f'--proxy-server={proxy_host}:{proxy_port}')
        options.add_argument(f'--user-data-dir={user_data_dir}')
        options.add_argument('--profile-directory=Default')
        chromedriver = chrome_driver_path
        browser = webdriver.Chrome(chrome_options=options, executable_path=chromedriver)
        return browser

    def _get_cookie(self) -> str:
        user_data_dir = os.path.join(self.runtime_dir, "chrome_user_data_dir")
        if not os.path.exists(user_data_dir):
            os.makedirs(user_data_dir, mode=0o755, exist_ok=True)
        browser = self.get_browser(self.chrome_bin_path, self.chrome_driver_path, user_data_dir, self.proxy_host,
                                   self.proxy_port)
        browser.get(self.liveUrl)
        browser.implicitly_wait(50)
        # time.sleep(5)
        cookie_list = browser.get_cookies()
        browser.quit()
        ret_str_list = []
        for cookie_dict in cookie_list:
            ret_str_list.append(f"{cookie_dict['name']}={cookie_dict['value']}")
        return "; ".join(ret_str_list)

    # 获取房间号
    def getLiveRoomId(self):
        liveUrl = self.liveUrl.strip('/')
        logging.info(f"requests 代理信息. [proxies = {self._request_proxies}]")
        res = requests.get(url=liveUrl, headers=self.headers, proxies=self._request_proxies)
        ss = re.search(
            r'__INITIAL_STATE__=(.*?);\(function\(\)\{var s;\(s=document\.currentScript\|\|document\.scripts\[document\.scripts\.length-1]\)\.parentNode\.r',
            res.text)
        if ss is None:
            raise CookieNotUseful("cookie 不能用了，请手动配置cookie 或者切换ip")
        text = ss.group(1)
        text = json.loads(text)

        # print(text['liveroom']['liveStream'])
        live_room_info = text['liveroom']['liveStream']
        if not live_room_info:
            raise CookieNotUseful("cookie 不能用了，请手动配置cookie 或者切换ip")
        if 'id' not in text['liveroom']['liveStream']:
            raise NoLivingException("还未开播")
        self.liveRoomId = live_room_info['id']
        if self.liveRoomId == '':
            raise RuntimeError('liveRoomId获取失败')
        return self.liveRoomId

    # 获取主播直播信息 （主播个人信息，直播地址，房间号等等）
    def getAnchorInfo(self):
        self.getLiveRoomId()
        return self.userLiveInfo

    # 获取直播websocket信息
    def getWebSocketInfo(self, liveRoomId):
        _url = 'https://live.kuaishou.com/live_api/liveroom/websocketinfo'

        _headers = copy.deepcopy(self.headers)
        _headers['Accept'] = 'application/json, text/plain, */*'
        resp = requests.get(_url, params={'liveStreamId': liveRoomId}, headers=_headers,
                            proxies=self._request_proxies)
        resp_data = resp.json()
        if resp_data['data']['result'] != 1:
            raise CookieNotUseful(f"cookie 不能用了，请手动配置cookie 或者切换ip. [func = getWebSocketInfo, data = {resp_data}]")
        return resp.json()

    # 启动websocket服务
    def wssServerStart(self, feed_push_callback=None):
        self.feed_push_callback = feed_push_callback

        rid = self.getLiveRoomId()
        wssInfo = self.getWebSocketInfo(rid)
        self.token = wssInfo['data']['token']
        self.webSocketUrl = wssInfo['data']['websocketUrls'][0]
        websocket.enableTrace(False)
        # 创建一个长连接
        ws = websocket.WebSocketApp(
            self.webSocketUrl, on_message=self.onMessage, on_error=self.onError, on_close=self.onClose,
            on_open=self.onOpen
        )
        ws.run_forever(http_proxy_host=self.proxy_host, http_proxy_port=self.proxy_port)

    def onMessage(self, ws: websocket.WebSocketApp, message: bytes):
        wssPackage = SocketMessage()
        wssPackage.ParseFromString(message)

        if wssPackage.payloadType == PayloadType.SC_ENTER_ROOM_ACK:
            self.parseEnterRoomAckPack(wssPackage.payload)
            return

        if wssPackage.payloadType == PayloadType.SC_HEARTBEAT_ACK:
            self.parseHeartBeatPack(wssPackage.payload)
            return

        if wssPackage.payloadType == PayloadType.SC_FEED_PUSH:
            self.parseFeedPushPack(wssPackage.payload)
            return

        if wssPackage.payloadType == PayloadType.SC_LIVE_WATCHING_LIST:
            self.parseSCWebLiveWatchingUsers(wssPackage.payload)
            return

        data = json_format.MessageToDict(wssPackage, preserving_proto_field_name=True)
        log = json.dumps(data, ensure_ascii=False)
        logging.debug('[onMessage] [无法解析的数据包⚠️]' + log)

    def parseEnterRoomAckPack(self, message: bytes):
        scWebEnterRoomAck = SCWebEnterRoomAck()
        scWebEnterRoomAck.ParseFromString(message)
        data = json_format.MessageToDict(scWebEnterRoomAck, preserving_proto_field_name=True)
        log = json.dumps(data, ensure_ascii=False)
        logging.info('[parseEnterRoomAckPack] [进入房间成功ACK应答👌] [RoomId:' + self.liveRoomId + '] ｜ ' + log)
        return data

    # 进入直播间的用户
    def parseSCWebLiveWatchingUsers(self, message: bytes):
        scWebLiveWatchingUsers = SCWebLiveWatchingUsers()
        scWebLiveWatchingUsers.ParseFromString(message)
        data = json_format.MessageToDict(scWebLiveWatchingUsers, preserving_proto_field_name=True)
        log = json.dumps(data, ensure_ascii=False)
        logging.debug(
            '[parseSCWebLiveWatchingUsers] [不知道是啥的数据包🤷] [RoomId:' + self.liveRoomId + '] ｜ ' + log)
        return data

    # 直播间弹幕信息
    def parseFeedPushPack(self, message: bytes):
        scWebFeedPush = SCWebFeedPush()
        scWebFeedPush.ParseFromString(message)
        data = json_format.MessageToDict(scWebFeedPush, preserving_proto_field_name=True)
        if self.feed_push_callback is not None:
            self.feed_push_callback(data)
        log = json.dumps(data, ensure_ascii=False)
        logging.debug('[parseFeedPushPack] [直播间弹幕🐎消息] [RoomId:' + self.liveRoomId + '] ｜ ' + log)
        return data

    def parseHeartBeatPack(self, message: bytes):
        heartAckMsg = SCHeartbeatAck()
        heartAckMsg.ParseFromString(message)
        data = json_format.MessageToDict(heartAckMsg, preserving_proto_field_name=True)
        log = json.dumps(data, ensure_ascii=False)
        logging.debug('[parseHeartBeatPack] [心跳❤️响应] [RoomId:' + self.liveRoomId + '] ｜ ' + log)
        return data

    def onError(self, ws, error):
        logging.error(f'[Error] [websocket异常, err = {error}]')

    def onClose(self, ws):
        logging.info('[Close] [websocket已关闭]')

    def onOpen(self, ws):
        data = self.connectData()
        logging.info('[onOpen] [建立wss连接]')
        ws.send(data, websocket.ABNF.OPCODE_BINARY)
        _thread.start_new_thread(self.keepHeartBeat, (ws,))

    def connectData(self):
        obj = CSWebEnterRoom()
        obj.payloadType = 200
        obj.payload.token = self.token
        obj.payload.liveStreamId = self.liveRoomId
        obj.payload.pageId = self.getPageId()  # pageId
        data = obj.SerializeToString()  # 序列化成二进制字符串
        return data

    # 封装心跳包
    def heartbeatData(self):
        obj = CSWebHeartbeat()
        obj.payloadType = 1
        obj.payload.timestamp = int(time.time() * 1000)
        return obj.SerializeToString()

    # 发送心跳包
    def keepHeartBeat(self, ws: websocket.WebSocketApp):
        while True:
            # 20秒发一次心跳包
            time.sleep(20)
            payload = self.heartbeatData()
            logging.debug("[keepHeartBeat] [发送心跳]")
            ws.send(payload, websocket.ABNF.OPCODE_BINARY)

    def getPageId(self):
        # js 中获取到该值的组成字符串
        charset = "-_zyxwvutsrqponmlkjihgfedcba9876543210ZYXWVUTSRQPONMLKJIHGFEDCBA"
        pageId = ""
        for _ in range(0, 16):
            pageId += random.choice(charset)
        pageId += "_"
        pageId += str(int(time.time() * 1000))
        return pageId

    # content内容 liveStreamId房间号 color字幕颜色
    def sendMsg(self, content: str, liveStreamId=None, color=None):
        variables = {
            'color': color,
            'content': content,
            'liveStreamId': liveStreamId
        }
        query = 'mutation SendLiveComment($liveStreamId: String, $content: String, $color: String) {\n  sendLiveComment(liveStreamId: $liveStreamId, content: $content, color: $color) {\n    result\n    __typename\n  }\n}\n'
        return self.liveGraphql('SendLiveComment', variables, query)

    # 关注用户 principalId用户ID type 1关注 2取消关注
    def follow(self, principalId=None, type=1):
        variables = {
            'principalId': principalId,
            'type': type,
        }
        query = 'mutation UserFollow($principalId: String, $type: Int) {\n  webFollow(principalId: $principalId, type: $type) {\n    followStatus\n    __typename\n  }\n}\n'
        return self.liveGraphql('UserFollow', variables, query)

    # 获取用户基本信息 principalId用户ID
    def getUserCardInfoById(self, principalId):
        variables = {
            'principalId': principalId,
            'count': 3,
        }
        query = 'query UserCardInfoById($principalId: String, $count: Int) {\n  userCardInfo(principalId: $principalId, count: $count) {\n    id\n    originUserId\n    avatar\n    name\n    description\n    sex\n    constellation\n    cityName\n    followStatus\n    privacy\n    feeds {\n      eid\n      photoId\n      thumbnailUrl\n      timestamp\n      __typename\n    }\n    counts {\n      fan\n      follow\n      photo\n      __typename\n    }\n    __typename\n  }\n}\n'
        return self.liveGraphql('UserCardInfoById', variables, query)

    # 获取所有礼物信息
    def getAllGifts(self):
        # variables = {}
        # query = 'query AllGifts {\n  allGifts\n}\n'
        # data = self.liveGraphql('AllGifts', variables, query)
        all_gift_url = 'https://live.kuaishou.com/live_api/emoji/allgifts'
        resp = requests.get(all_gift_url, headers=self.headers, proxies=self._request_proxies)
        return resp.json()

    # 底层统一请求方法
    def liveGraphql(self, operationName: str, variables, query, headers=None):
        if headers is None:
            head = self.headers
            head['content-type'] = 'application/json'
        else:
            head = headers

        data = {
            'operationName': operationName,
            'variables': variables,
            'query': query
        }
        res = requests.post(url=self.apiHost, data=json.dumps(data), headers=head, proxies=self._request_proxies).json()
        logging.debug('[liveGraphql] [操作返回数据] ｜ ' + json.dumps(res, ensure_ascii=False))
        return res

    # 十六进制字符串转protobuf格式 （用于快手网页websocket调试分析包体结构）
    def hexStrToProtobuf(self, hexStr):
        # 示例数据
        # hexStr = '08d40210011aeb250a8e010a81010a0b66666731343535323939391206e68595e799bd1a6a68747470733a2f2f70312e612e7978696d67732e636f6d2f75686561642f41422f323032322f31312f31322f32312f424d6a41794d6a45784d5449794d5441304d6a68664d6a67784d4445354e7a67344d5638795832686b4d6a6335587a49324d513d3d5f732e6a7067180120012a04323132300aa8010a9e010a0b79697869616f77753636361209e79fa5e5b08fe6ada61a830168747470733a2f2f616c69696d672e612e7978696d67732e636f6d2f75686561642f41422f323032322f31302f30322f31392f424d6a41794d6a45774d4449784f544d304e4442664e6a497a4d4455324d445977587a4a66614751304e444a664d546b335f732e6a70674030655f306f5f306c5f3530685f3530775f3835712e73726318012a033430330ab6010aac010a0c4c31333130373432373635301212e9bb8ee699a8f09f8c8af09f8c8af09f8c8a1a870168747470733a2f2f616c69696d672e612e7978696d67732e636f6d2f75686561642f41422f323032322f31312f31392f31332f424d6a41794d6a45784d546b784d7a4d784d5452664d5441344e5467794d5449334d5638795832686b4e444935587a45304d513d3d5f732e6a70674030655f306f5f306c5f3530685f3530775f3835712e73726318012a033335390ac2010ab8010a104757515053414148445244444145775a121ee69492e4b880e58fa3e8a28be6989fe6989fe98081e7bb99e58c97e699a81a830168747470733a2f2f616c69696d672e612e7978696d67732e636f6d2f75686561642f41422f323032322f31312f31322f32312f424d6a41794d6a45784d5449794d5445774d6a4e664d5445314d7a59354d4451324e6c38795832686b4e7a46664f5449355f732e6a70674030655f306f5f306c5f3530685f3530775f3835712e73726318012a033130330a91010a88010a0f337870323538746a6d6376337a62751209e58699e7949ce8af971a6a68747470733a2f2f70332e612e7978696d67732e636f6d2f75686561642f41422f323032322f31312f32302f31372f424d6a41794d6a45784d6a41784e7a4d784e5442664d7a45784d7a4d784f5445784e6c38795832686b4d545530587a49344d513d3d5f732e6a706718012a0233340aac010aa3010a0979756875616e306b64120ce88a8be594a4e1b587e1b69c1a870168747470733a2f2f616c69696d672e612e7978696d67732e636f6d2f75686561642f41422f323032322f30362f31312f30312f424d6a41794d6a41324d5445774d5451334e5452664d5441774d6a51324f4445344f4638795832686b4e444535587a457a4e773d3d5f732e6a70674030655f306f5f306c5f3530685f3530775f3835712e73726318012a0233330a8a010a81010a0c6868686832303033313032391205e888aac2b71a6a68747470733a2f2f70332e612e7978696d67732e636f6d2f75686561642f41422f323032322f31312f30332f32322f424d6a41794d6a45784d444d794d6a41784d546c664d6a4d7a4d7a45794d4455304d3138795832686b4e544133587a4d314d513d3d5f732e6a706718012a0233310aab010aa2010a0f3378686d3568677a6e657963666b6b1209e890a8e5bf853536391a830168747470733a2f2f616c69696d672e612e7978696d67732e636f6d2f75686561642f41422f323032302f30372f32342f30382f424d6a41794d4441334d6a51774f4449774d4442664d5467344e5455314d5449784e6c38795832686b4d6a4d79587a673d5f732e6a70674030655f306f5f306c5f3530685f3530775f3835712e73726318012a0232330a93010a8a010a0f33787569663363746b6e6d38773271120be790aae790aa37313432321a6a68747470733a2f2f70312e612e7978696d67732e636f6d2f75686561642f41422f323032322f31312f31382f31322f424d6a41794d6a45784d5467784d6a45784e4446664d7a45794f5441354f446b304e3138795832686b4e7a6730587a49344e773d3d5f732e6a706718012a0232320a94010a8b010a0f337862677a6a627034777570763567120cefbc87e7bbade99b86efbc821a6a68747470733a2f2f70352e612e7978696d67732e636f6d2f75686561642f41422f323032322f30382f33302f31352f424d6a41794d6a41344d7a41784e5451334e4452664d5441324d6a51334d6a41324e3138795832686b4f545533587a63314e413d3d5f732e6a706718012a0232320aa5010a9c010a0979796473696f73313212054c696b652e1a870168747470733a2f2f616c69696d672e612e7978696d67732e636f6d2f75686561642f41422f323032322f31312f31382f31322f424d6a41794d6a45784d5467784d6a51324e4456664d6a4d354d4459774d5455344e5638785832686b4d546731587a51354d773d3d5f732e6a70674030655f306f5f306c5f3530685f3530775f3835712e73726318012a0232310a90010a85010a0a48657969676530353230120be4bba5e6ad8c20f09f8e801a6a68747470733a2f2f70312e612e7978696d67732e636f6d2f75686561642f41422f323032322f31312f31332f31302f424d6a41794d6a45784d544d784d4455354d6a4e664d544d324e6a41304e7a41774d5638785832686b4d6a6779587a51794f413d3d5f732e6a7067180120012a0231320aa8010a9f010a0f33786b623464793435706a797570711206e684a6e6829f1a830168747470733a2f2f616c69696d672e612e7978696d67732e636f6d2f75686561642f41422f323032322f31312f30342f32322f424d6a41794d6a45784d4451794d6a41774d445a664e4445794f446b324d545931587a4a66614751324e4456664f446b355f732e6a70674030655f306f5f306c5f3530685f3530775f3835712e73726318012a0231310aa5010a9c010a08776a353431383830120ae88b8fe791bee699a82f1a830168747470733a2f2f616c69696d672e612e7978696d67732e636f6d2f75686561642f41422f323032322f31312f30332f30302f424d6a41794d6a45784d444d774d4449344e546c664d6a41354e6a45794e544d31587a4666614751304d7a6c664e444d785f732e6a70674030655f306f5f306c5f3530685f3530775f3835712e73726318012a0231310abc010ab3010a0f33786334746a7a376e7875636561791216e7a9bfe5b1b1e794b2efbc88696b756ee59ba2efbc891a870168747470733a2f2f616c69696d672e612e7978696d67732e636f6d2f75686561642f41422f323032322f30382f32362f31312f424d6a41794d6a41344d6a59784d5451334d4442664d6a4d314d7a41314d4449774d3138795832686b4e444d79587a4d7a4e673d3d5f732e6a70674030655f306f5f306c5f3530685f3530775f3835712e73726318012a0231300a8c010a84010a0957535162616f353230120fe5b08f20e5a88120e5b09120e380821a6668747470733a2f2f70312e612e7978696d67732e636f6d2f75686561642f41422f323032322f31312f31332f31392f424d6a41794d6a45784d544d784f5445354d5446664f5455314e4449304f445578587a4a666147517a4e5452664e4449785f732e6a706718012a01330a8f010a87010a0f337834646178626768746a78716979120ce7a78be8be9ee1b587e1b69c1a6668747470733a2f2f70332e612e7978696d67732e636f6d2f75686561642f41422f323032322f31312f31342f31312f424d6a41794d6a45784d5451784d544d7a4d544a664e7a55354d7a63774d446b31587a4a66614751784f544e664e5459315f732e6a706718012a01330a8d010a85010a0f3378723937706975747768326d7a321206e791bee4b8811a6a68747470733a2f2f70312e612e7978696d67732e636f6d2f75686561642f41422f323032312f30342f32332f32312f424d6a41794d5441304d6a4d794d5445304e544a664d5463794d7a55304f4463304d4638795832686b4f546378587a59354e513d3d5f732e6a706718012a01330ab0010aa8010a0d6c713431383835343138386868120de585b3e4ba8ee58a8920e5bcb71a870168747470733a2f2f616c69696d672e612e7978696d67732e636f6d2f75686561642f41422f323032322f31312f30352f32332f424d6a41794d6a45784d4455794d7a4d334e544a664d54517a4f44497a4e6a67784d6c38795832686b4d544578587a67324f413d3d5f732e6a70674030655f306f5f306c5f3530685f3530775f3835712e73726318012a01330a8b010a83010a0e4c544431353933313731353337371209e4bba5e6a4bfe383bb1a6668747470733a2f2f70332e612e7978696d67732e636f6d2f75686561642f41422f323032322f31312f30372f31392f424d6a41794d6a45784d4463784f544d314d545a664d5441314d5463784e4459334e6c38795832686b4f444579587a55315f732e6a706718012a01330a89010a83010a0b64796c3230303830363130120ce5b08fe5b08fe5a79ce99c961a6668747470733a2f2f70342e612e7978696d67732e636f6d2f75686561642f41422f323032322f31302f31362f31312f424d6a41794d6a45774d5459784d5455354e5442664f4449304e7a517a4e545535587a4a66614751334d7a42664e5445335f732e6a70672a01320ab7010ab1010a0f3378727a33667a69737438717334611218e5bf98e5b79de38088e5b7b2e69c89e58584e5bc9fe380891a830168747470733a2f2f616c69696d672e612e7978696d67732e636f6d2f75686561642f41422f323032322f31312f31342f31312f424d6a41794d6a45784d5451784d54557a4d6a42664f4441304e444d794f545579587a4666614751304d7a52664d7a4d785f732e6a70674030655f306f5f306c5f3530685f3530775f3835712e7372632a01320a90010a8a010a0e796f6e6773686974756f7a68616e1210e5b08fe58b87e5a3ab2de99988e8b68a1a6668747470733a2f2f70312e612e7978696d67732e636f6d2f75686561642f41422f323032322f30352f32382f31332f424d6a41794d6a41314d6a67784d7a51354d445a664e7a59304f44517a4d7a6b35587a4a66614751794d6a56664e544d7a5f732e6a70672a01320a8d010a87010a0f3378746d706b6167627536766e7139120ce5ad90e792a9e1b587e1b69c1a6668747470733a2f2f70312e612e7978696d67732e636f6d2f75686561642f41422f323032322f31302f33312f31312f424d6a41794d6a45774d7a45784d544d784e4468664e6a45324d4445304d444d30587a4a66614751314e446c664d54497a5f732e6a70672a01320a89010a83010a0f4c4a483532304c4a31333134656d6f1204f09f88b71a6a68747470733a2f2f70352e612e7978696d67732e636f6d2f75686561642f41422f323032322f31312f31342f31382f424d6a41794d6a45784d5451784f4441314d4442664d6a67354d4441774f5455314e6c38795832686b4d6a6730587a63794e513d3d5f732e6a70672a01320aa5010a9d010a0f33786967326675743533373872626b1204456e6d681a830168747470733a2f2f616c69696d672e612e7978696d67732e636f6d2f75686561642f41422f323032322f31312f32332f31312f424d6a41794d6a45784d6a4d784d544d784e446c664d6a59324e7a517a4d4455334f5638785832686b4d7a4132587a6b315f732e6a70674030655f306f5f306c5f3530685f3530775f3835712e73726318012a01320ab1010aab010a0f33786d39353268396a393473787a731212e5ad90e792a9efbc88e5b08fe58fb7efbc891a830168747470733a2f2f616c69696d672e612e7978696d67732e636f6d2f75686561642f41422f323032322f31312f30372f30302f424d6a41794d6a45784d4463774d4451794e4452664d6a517a4d5463314d6a49354e5638795832686b4e5464664e7a49785f732e6a70674030655f306f5f306c5f3530685f3530775f3835712e7372632a01320aa9010aa3010a0f3378397370326436703272727866391206e585b1e5928c1a870168747470733a2f2f616c69696d672e612e7978696d67732e636f6d2f75686561642f41422f323032322f30372f32382f31322f424d6a41794d6a41334d6a67784d6a55314d7a42664d6a4d354e6a4d774e4455304d3138795832686b4e6a6b35587a45774e413d3d5f732e6a70674030655f306f5f306c5f3530685f3530775f3835712e7372632a01320aa5010a9f010a0b4c5a5032303133313479611206e6b3bde4b8801a870168747470733a2f2f616c69696d672e612e7978696d67732e636f6d2f75686561642f41422f323032322f30372f31332f31362f424d6a41794d6a41334d544d784e6a49304e5456664d546b324e4445794f4463334f5638795832686b4f545179587a597a4d673d3d5f732e6a70674030655f306f5f306c5f3530685f3530775f3835712e7372632a01320a9f010a99010a0f3378326e37793865656573766b6879121ee58c97e699a8e79a84e4bfa1e699baefbc88e5b7b2e7b4abe7a082efbc891a6668747470733a2f2f70312e612e7978696d67732e636f6d2f75686561642f41422f323032322f31312f31332f31312f424d6a41794d6a45784d544d784d5441344d6a68664d5467344d44497a4f5441354e5638785832686b4f446b79587a45795f732e6a70672a013220a699a0ebca30'
        with open('t-proto', 'wb') as w:
            w.write(binascii.unhexlify(hexStr))

        parser = StandardParser()
        with open('t-proto', 'rb') as fh:
            output = parser.parse_message(fh, "message")
        logging.debug(output)
        return output

    # 把十六进制字符串转成ascii编码格式 （用于快手网页websocket调试分析包体结构）
    def unHexLify(self, data: str):
        # 示例数据
        # data = 'E5 8C 97 E6 99 A8 E7 9A 84 E4 BF A1 E6 99 BA EF BC 88 E5 B7 B2 E7 B4 AB'
        data.replace(' ', '')
        data = binascii.unhexlify(data).decode()
        logging.debug(data)
        return data
