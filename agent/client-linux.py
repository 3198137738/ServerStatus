#!/usr/bin/env python3
# coding: utf-8
# Create by : https://github.com/cppla/ServerStatus
# 低流量版 (Update by: https://github.com/3198137738/ServerStatus), 支持Python版本：2.7 to 3.x
# 20211117: 移除tupd信息 (Update by: https://github.com/lidalao/ServerStatus)
#
# 与原版的区别:
#   1. 上报间隔 INTERVAL 默认 5s (原版 1s), CPU 取整个间隔内的平均值
#   2. 三网探测间隔 PING_INTERVAL 默认 30s (原版 1s), PING_INTERVAL=0 关闭探测
#   3. 双栈检测 (连 google) 间隔 ONLINE_CHECK_INTERVAL 默认 300s (原版约 10s)
#   4. 探测目标 DNS 解析失败时不再每次连接都重新解析, 而是每 10 分钟重试一次
#   5. 网速改为在主循环里按 /proc/net/dev 差值计算, 去掉单独的线程
#   6. 参数按 KEY=VALUE 精确解析 (原版用子串匹配, PING_INTERVAL 会被当成 INTERVAL)
#   7. 三网探测改为山西联通/电信/移动, 优先 IPv6, 不通自动退回 IPv4
#
# 用法 (参数均可选, 除 SERVER/USER/PASSWORD 外都有默认值):
#   python3 client-linux.py SERVER=1.2.3.4 USER=s01 PASSWORD=xxx INTERVAL=5 PING_INTERVAL=30

SERVER = "127.0.0.1"
USER = "s01"
PORT = 35601
PASSWORD = "USER_DEFAULT_PASSWORD"
INTERVAL = 5                  # 上报间隔(秒)
PING_INTERVAL = 30            # 三网 TCP 探测间隔(秒), 0 = 关闭
ONLINE_CHECK_INTERVAL = 300   # IPv4/IPv6 连通性检测间隔(秒)
PROBEPORT = 80
PING_PACKET_HISTORY_LEN = 100
PING_MIN_SAMPLES = 10         # 至少这么多样本才计算丢包率 (原版 30, 间隔拉长后太慢)
DNS_RETRY_INTERVAL = 600      # 每隔这么久重新解析/选择探测目标
# 三网探测目标: 山西联通/电信/移动 (zstaticcdn)。按顺序选第一个能连通的,
# v6 域名只有 AAAA、v4 域名只有 A; 本机没有 IPv6 时自动退回 v4
CU = ("sx-cu-v6.ip.zstaticcdn.com", "sx-cu-v4.ip.zstaticcdn.com")
CT = ("sx-ct-v6.ip.zstaticcdn.com", "sx-ct-v4.ip.zstaticcdn.com")
CM = ("sx-cm-v6.ip.zstaticcdn.com", "sx-cm-v4.ip.zstaticcdn.com")

import socket
import time
import timeit
import re
import os
import sys
import json
import errno
import subprocess
import threading
try:
    from queue import Queue     # python3
except ImportError:
    from Queue import Queue     # python2

SKIP_IFACES = ('lo', 'tun', 'docker', 'veth', 'br-', 'vmbr', 'vnet', 'kube')


def get_uptime():
    with open('/proc/uptime', 'r') as f:
        return int(f.readline().split('.', 2)[0])


def get_memory():
    re_parser = re.compile(r'^(?P<key>\S*):\s*(?P<value>\d*)\s*kB')
    result = dict()
    with open('/proc/meminfo') as f:
        for line in f:
            match = re_parser.match(line)
            if match:
                key, value = match.groups()
                result[key] = int(value)
    MemTotal = float(result['MemTotal'])
    MemUsed = MemTotal - float(result['MemFree']) - float(result['Buffers']) - float(result['Cached']) - float(result['SReclaimable'])
    SwapTotal = float(result['SwapTotal'])
    SwapFree = float(result['SwapFree'])
    return int(MemTotal), int(MemUsed), int(SwapTotal), int(SwapFree)


def get_hdd():
    p = subprocess.check_output(['df', '-Tlm', '--total', '-t', 'ext4', '-t', 'ext3', '-t', 'ext2', '-t', 'reiserfs', '-t', 'jfs', '-t', 'ntfs', '-t', 'fat32', '-t', 'btrfs', '-t', 'fuseblk', '-t', 'zfs', '-t', 'simfs', '-t', 'xfs']).decode("utf-8")
    total = p.splitlines()[-1].split()
    return int(total[2]), int(total[3])


def get_cpu_times():
    with open("/proc/stat", "r") as f:
        return [int(x) for x in f.readline().split()[1:5]]


def get_net_bytes():
    """返回所有物理网卡的 (累计接收, 累计发送) 字节数"""
    rx = tx = 0
    with open('/proc/net/dev') as f:
        for line in f.readlines()[2:]:
            name, data = line.split(':', 1)
            name = name.strip()
            if any(s in name for s in SKIP_IFACES):
                continue
            fields = data.split()
            rx += int(fields[0])
            tx += int(fields[8])
    return rx, tx


def get_network(ip_version):
    host = "ipv4.google.com" if ip_version == 4 else "ipv6.google.com"
    try:
        socket.create_connection((host, 80), 2).close()
        return True
    except Exception:
        return False


lostRate = {'10010': 0.0, '189': 0.0, '10086': 0.0}
pingTime = {'10010': 0, '189': 0, '10086': 0}


def _resolve(host):
    if host.count(':') >= 1:    # 纯 IPv6 地址
        return host
    try:
        return socket.getaddrinfo(host, None, 0, socket.SOCK_STREAM)[0][4][0]
    except Exception:
        return None


def _tcp_reachable(ip, port):
    try:
        socket.create_connection((ip, port), timeout=2).close()
        return True
    except socket.error as error:
        return error.errno == errno.ECONNREFUSED


def _select_target(hosts, port):
    """返回第一个能连通的候选 IP; 都不通时返回最后一个能解析的, 让丢包率如实反映"""
    fallback = None
    for host in hosts:
        ip = _resolve(host)
        if ip is None:
            continue
        if _tcp_reachable(ip, port):
            return ip
        fallback = ip
    return fallback


def _ping_thread(hosts, mark, port):
    lostPacket = 0
    packet_queue = Queue(maxsize=PING_PACKET_HISTORY_LEN)
    IP = None
    last_resolve = 0

    while True:
        # 定期重选: IPv6 恢复/CDN 换 IP 都能跟上; 解析失败时也不会每次探测都查 DNS
        if time.time() - last_resolve >= DNS_RETRY_INTERVAL:
            IP = _select_target(hosts, port) or IP
            last_resolve = time.time()
        if IP is None:
            time.sleep(PING_INTERVAL)
            continue

        if packet_queue.full():
            if packet_queue.get() == 0:
                lostPacket -= 1
        b = timeit.default_timer()
        try:
            socket.create_connection((IP, port), timeout=1).close()
            pingTime[mark] = int((timeit.default_timer() - b) * 1000)
            packet_queue.put(1)
        except socket.error as error:
            if error.errno == errno.ECONNREFUSED:
                pingTime[mark] = int((timeit.default_timer() - b) * 1000)
                packet_queue.put(1)
            else:
                lostPacket += 1
                packet_queue.put(0)

        if packet_queue.qsize() >= PING_MIN_SAMPLES:
            lostRate[mark] = float(lostPacket) / packet_queue.qsize()

        time.sleep(PING_INTERVAL)


def start_ping_threads():
    if PING_INTERVAL <= 0:
        return
    for hosts, mark in ((CU, '10010'), (CT, '189'), (CM, '10086')):
        t = threading.Thread(target=_ping_thread, kwargs={'hosts': hosts, 'mark': mark, 'port': PROBEPORT})
        t.daemon = True
        t.start()


def byte_str(obj):
    if isinstance(obj, str):
        return obj.encode(encoding="utf-8")
    elif isinstance(obj, bytes):
        return bytes.decode(obj)
    print(type(obj))


def parse_args():
    global SERVER, PORT, USER, PASSWORD, INTERVAL, PING_INTERVAL, ONLINE_CHECK_INTERVAL
    for arg in sys.argv[1:]:
        if '=' not in arg:
            continue
        key, value = arg.split('=', 1)
        key = key.upper()
        if key == 'SERVER':
            SERVER = value
        elif key == 'PORT':
            PORT = int(value)
        elif key == 'USER':
            USER = value
        elif key == 'PASSWORD':
            PASSWORD = value
        elif key == 'INTERVAL':
            INTERVAL = max(1, int(value))
        elif key == 'PING_INTERVAL':
            PING_INTERVAL = int(value)
        elif key == 'ONLINE_CHECK_INTERVAL':
            ONLINE_CHECK_INTERVAL = max(10, int(value))


if __name__ == '__main__':
    parse_args()
    socket.setdefaulttimeout(30)
    start_ping_threads()
    while True:
        try:
            print("Connecting...")
            s = socket.create_connection((SERVER, PORT))
            data = byte_str(s.recv(1024))
            if data.find("Authentication required") > -1:
                s.send(byte_str(USER + ':' + PASSWORD + '\n'))
                data = byte_str(s.recv(1024))
                if data.find("Authentication successful") < 0:
                    print(data)
                    raise socket.error
            else:
                print(data)
                raise socket.error

            print(data)
            if data.find("You are connecting via") < 0:
                data = byte_str(s.recv(1024))
                print(data)

            if data.find("IPv4") > -1:
                check_ip = 6
            elif data.find("IPv6") > -1:
                check_ip = 4
            else:
                print(data)
                raise socket.error

            last_online_check = 0
            prev_cpu = get_cpu_times()
            prev_rx, prev_tx = get_net_bytes()
            prev_clock = time.time()

            while True:
                time.sleep(INTERVAL)

                # CPU / 网速都取本次上报间隔内的平均值
                cur_cpu = get_cpu_times()
                delta = [c - p for c, p in zip(cur_cpu, prev_cpu)]
                prev_cpu = cur_cpu
                st = sum(delta) or 1
                CPU = round(100 - (delta[3] * 100.0 / st), 1)

                NET_IN, NET_OUT = get_net_bytes()
                now = time.time()
                diff = (now - prev_clock) or 1
                netrx = int((NET_IN - prev_rx) / diff)
                nettx = int((NET_OUT - prev_tx) / diff)
                prev_rx, prev_tx, prev_clock = NET_IN, NET_OUT, now

                Load_1, Load_5, Load_15 = os.getloadavg()
                MemoryTotal, MemoryUsed, SwapTotal, SwapFree = get_memory()
                HDDTotal, HDDUsed = get_hdd()

                array = {}
                if now - last_online_check >= ONLINE_CHECK_INTERVAL:
                    array['online' + str(check_ip)] = get_network(check_ip)
                    last_online_check = now

                array['uptime'] = get_uptime()
                array['load_1'] = Load_1
                array['load_5'] = Load_5
                array['load_15'] = Load_15
                array['memory_total'] = MemoryTotal
                array['memory_used'] = MemoryUsed
                array['swap_total'] = SwapTotal
                array['swap_used'] = SwapTotal - SwapFree
                array['hdd_total'] = HDDTotal
                array['hdd_used'] = HDDUsed
                array['cpu'] = CPU
                array['network_rx'] = netrx
                array['network_tx'] = nettx
                array['network_in'] = NET_IN
                array['network_out'] = NET_OUT
                array['ip_status'] = True
                array['ping_10010'] = lostRate['10010'] * 100
                array['ping_189'] = lostRate['189'] * 100
                array['ping_10086'] = lostRate['10086'] * 100
                array['time_10010'] = pingTime['10010']
                array['time_189'] = pingTime['189']
                array['time_10086'] = pingTime['10086']
                array['tcp'] = array['udp'] = array['process'] = array['thread'] = 0

                # separators 去掉多余空格, 每条约省 50 字节
                s.send(byte_str("update " + json.dumps(array, separators=(',', ':')) + "\n"))
        except KeyboardInterrupt:
            raise
        except socket.error:
            print("Disconnected...")
            if 's' in locals().keys():
                del s
            time.sleep(3)
        except Exception as e:
            print("Caught Exception:", e)
            if 's' in locals().keys():
                del s
            time.sleep(3)
