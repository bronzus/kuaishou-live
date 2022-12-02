import logging

from kuaishou import KsLive

if __name__ == '__main__':
    LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"
    logging.basicConfig(level=logging.DEBUG, format=LOG_FORMAT)
    ks = KsLive.Tool()
    ks.init('快手直播地址', '你的快手网页cookie')
    # 启动快手ws客户端
    ks.wssServerStart()