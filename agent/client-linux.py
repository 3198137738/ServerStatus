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
#   8. 探测目标可由主控下发: config.json 的 monitors 里 name 为 CU/CT/CM 的项,
#      host 写逗号分隔的候选(可带 :port), interval 为探测间隔; 改完 restart srv 即生效
#   9. 自动检测地区(Cloudflare trace 的 loc, 如 JP)和虚拟化类型(systemd-detect-virt),
#      经 custom 字段上报为 "loc=JP;virt=kvm", 面板优先显示它们
#  10. 远程升级: monitors 里 name 为 UPGRADE、host 为 40 位 commit SHA 的项变化时,
#      用 systemd-run 在独立单元里执行该 commit 的 agent/upgrade.sh
#  11. 恢复 tcp/udp/进程/线程数(上面 20211117 那次被置 0), 改为读 /proc/net/sockstat 和 /proc/loadavg
#  12. 上报磁盘读写速度 io_read/io_write: /proc/diskstats 中物理磁盘扇区数的差值
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
LOCATION_REFRESH = 21600      # 地区每 6 小时重新检测一次, 失败 10 分钟后重试
GITHUB_RAW_BASE = "https://raw.githubusercontent.com/3198137738/ServerStatus"

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
try:
    from urllib.request import urlopen      # python3
except ImportError:
    from urllib2 import urlopen             # python2

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


def _sockstat_inuse(proto):
    """/proc/net/sockstat(6) 里某协议的 inuse 之和, 如 'TCP' -> TCP + TCP6"""
    total = 0
    for path, key in (('/proc/net/sockstat', proto + ':'), ('/proc/net/sockstat6', proto + '6:')):
        try:
            with open(path) as f:
                for line in f:
                    parts = line.split()
                    if parts and parts[0] == key and 'inuse' in parts:
                        total += int(parts[parts.index('inuse') + 1])
        except (IOError, OSError, ValueError, IndexError):
            pass
    return total


def get_tupd():
    """tcp/udp 连接数、进程数、线程数; 全部读 /proc, 不起子进程(原版每次跑 4 条 ss/ps)"""
    try:
        process = sum(1 for d in os.listdir('/proc') if d.isdigit())
    except OSError:
        process = 0
    try:
        with open('/proc/loadavg') as f:
            thread = int(f.read().split()[3].split('/')[1])    # "0.00 0.01 0.05 1/123 4567" 里的 123
    except (IOError, OSError, ValueError, IndexError):
        thread = 0
    return _sockstat_inuse('TCP'), _sockstat_inuse('UDP'), process, thread


# 只统计整块磁盘: 分区会和所在磁盘重复计数, dm-*/md* 是叠在物理盘上的 LVM/RAID, loop/ram/zram/光驱不是真实磁盘
DISK_SKIP = ('loop', 'ram', 'zram', 'sr', 'fd', 'dm-', 'md')


def get_disk_bytes():
    """所有物理磁盘的累计 (读取字节, 写入字节); /proc/diskstats 的扇区数恒按 512 字节计"""
    try:
        disks = set(d for d in os.listdir('/sys/block') if not d.startswith(DISK_SKIP))
    except OSError:
        disks = None
    read = write = 0
    try:
        with open('/proc/diskstats') as f:
            for line in f:
                parts = line.split()
                if len(parts) < 10:
                    continue
                name = parts[2]
                if disks is None:
                    # 没有 /sys/block 时退而按名字过滤, 分区可能会重复计数
                    if name.startswith(DISK_SKIP):
                        continue
                elif name not in disks:
                    continue
                read += int(parts[5]) * 512
                write += int(parts[9]) * 512
    except (IOError, OSError, ValueError):
        pass
    return read, write


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

# 主控 config.json 的 monitors 里 name 为 CU/CT/CM 的项会覆盖内置探测目标, 见 apply_monitors()
MONITOR_MARKS = {'CU': '10010', 'CT': '189', 'CM': '10086'}
probeTargets = {}
targetsVersion = 0


def _parse_hosts(text):
    """把 a.com,b.com:8080,[2001:db8::1]:80,2001:db8::2 解析成 [(host, port), ...], 缺省端口 PROBEPORT"""
    result = []
    for item in str(text).split(','):
        item = item.strip()
        if not item:
            continue
        host, port = item, PROBEPORT
        if item.startswith('['):                     # [IPv6]:port
            host, _, rest = item[1:].partition(']')
            if rest.startswith(':') and rest[1:].isdigit():
                port = int(rest[1:])
        elif item.count(':') == 1:                   # host:port
            h, p = item.split(':')
            if p.isdigit():
                host, port = h, int(p)
        result.append((host, port))
    return result


def _default_targets():
    return dict((mark, {'hosts': _parse_hosts(','.join(hosts)), 'interval': PING_INTERVAL})
                for hosts, mark in ((CU, '10010'), (CT, '189'), (CM, '10086')))


def apply_monitors(lines):
    """解析主控下发的 monitors 行; 没下发 CU/CT/CM 的那一路用内置默认"""
    global probeTargets, targetsVersion
    targets = _default_targets()
    for line in lines:
        line = line.strip()
        if not line.startswith('{'):
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        mark = MONITOR_MARKS.get(str(item.get('name', '')).strip().upper())
        hosts = _parse_hosts(item.get('host', ''))
        if mark is None or not hosts:
            continue
        try:
            interval = int(item.get('interval') or 0)
        except (TypeError, ValueError):
            interval = 0
        targets[mark] = {'hosts': hosts, 'interval': interval if interval > 0 else PING_INTERVAL}
    if targets != probeTargets:
        probeTargets = targets
        targetsVersion += 1
        for name, mark in sorted(MONITOR_MARKS.items()):
            t = targets[mark]
            print("probe %s: %s every %ss" % (name, ','.join('%s:%d' % hp for hp in t['hosts']), t['interval']))


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


def _select_target(hosts):
    """返回第一个能连通的 (ip, port); 都不通时返回最后一个能解析的, 让丢包率如实反映"""
    fallback = None
    for host, port in hosts:
        ip = _resolve(host)
        if ip is None:
            continue
        if _tcp_reachable(ip, port):
            return ip, port
        fallback = (ip, port)
    return fallback


def _ping_thread(mark):
    lostPacket = 0
    packet_queue = Queue(maxsize=PING_PACKET_HISTORY_LEN)
    target = None
    last_select = 0
    seen_version = targetsVersion

    while True:
        cfg = probeTargets[mark]
        if seen_version != targetsVersion:
            # 主控换了探测目标: 旧目标的样本作废
            seen_version = targetsVersion
            lostPacket = 0
            packet_queue = Queue(maxsize=PING_PACKET_HISTORY_LEN)
            lostRate[mark], pingTime[mark] = 0.0, 0
            target, last_select = None, 0
        # 定期重选: IPv6 恢复/CDN 换 IP 都能跟上; 解析失败时也不会每次探测都查 DNS
        if time.time() - last_select >= DNS_RETRY_INTERVAL:
            target = _select_target(cfg['hosts']) or target
            last_select = time.time()

        if target is not None:
            if packet_queue.full():
                if packet_queue.get() == 0:
                    lostPacket -= 1
            b = timeit.default_timer()
            try:
                socket.create_connection(target, timeout=1).close()
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

        # 分段睡眠, 目标变更时不必等满一个(可能很长的)间隔
        deadline = time.time() + cfg['interval']
        while time.time() < deadline and seen_version == targetsVersion:
            time.sleep(min(1, max(0, deadline - time.time())))


def start_ping_threads():
    if PING_INTERVAL <= 0:
        return
    apply_monitors([])
    for mark in MONITOR_MARKS.values():
        t = threading.Thread(target=_ping_thread, kwargs={'mark': mark})
        t.daemon = True
        t.start()


agentInfo = {'loc': '', 'virt': ''}
# 直接显示 systemd-detect-virt 的原始值 (kvm/amazon/microsoft/xen/openvz/lxc...), 只把物理机的 none 改成 dedicated
VIRT_ALIASES = {'none': 'dedicated'}


def detect_location():
    """出口 IP 所在国家, 两位大写代码; 失败返回 None"""
    for url in ("https://www.cloudflare.com/cdn-cgi/trace", "https://1.1.1.1/cdn-cgi/trace"):
        try:
            body = urlopen(url, timeout=5).read().decode('utf-8', 'ignore')
        except Exception:
            continue
        for line in body.splitlines():
            if line.startswith('loc='):
                loc = line[4:].strip().upper()
                if re.match(r'^[A-Z]{2}$', loc) and loc != 'XX':
                    return loc
    return None


def detect_virt():
    try:
        out = subprocess.check_output(['systemd-detect-virt'], stderr=open(os.devnull, 'w'))
    except subprocess.CalledProcessError as e:
        out = e.output      # 物理机输出 none 且退出码为 1
    except Exception:
        out = b''
    virt = out.decode('utf-8', 'ignore').strip().lower() if isinstance(out, bytes) else str(out).strip().lower()
    if not virt:
        if os.path.exists('/proc/user_beancounters') or os.path.exists('/proc/vz'):
            virt = 'openvz'
        else:
            return ''
    return VIRT_ALIASES.get(virt, virt)


def _location_thread():
    while True:
        loc = detect_location()
        if loc:
            agentInfo['loc'] = loc
        time.sleep(LOCATION_REFRESH if loc else 600)


def start_info_detection():
    agentInfo['virt'] = detect_virt()
    t = threading.Thread(target=_location_thread)
    t.daemon = True
    t.start()


def agent_custom():
    return ';'.join('%s=%s' % (k, agentInfo[k]) for k in ('loc', 'virt') if agentInfo[k])


UPGRADE_TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'upgrade.token')


def handle_upgrade(lines):
    """monitors 里 UPGRADE 的 host(commit SHA) 与上次见过的不同就升级; 每个 SHA 只触发一次"""
    token = None
    for line in lines:
        line = line.strip()
        if not line.startswith('{'):
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if str(item.get('name', '')).strip().upper() == 'UPGRADE':
            token = str(item.get('host', '')).strip().lower()
    if not token:
        return
    if not re.match(r'^[0-9a-f]{40}$', token):
        print("upgrade: ignore %r, host must be a full 40-char commit SHA" % token)
        return
    try:
        with open(UPGRADE_TOKEN_FILE) as f:
            seen = f.read().strip()
    except IOError:
        seen = None
    if seen == token:
        return
    with open(UPGRADE_TOKEN_FILE, 'w') as f:
        f.write(token + '\n')
    if seen is None:
        # 首次运行(刚手动装/升级完)只记下基线, 不重复升级
        print("upgrade: baseline %s" % token)
        return
    # upgrade.sh 会重启 sss-agent; 必须放到独立的 transient unit 里跑,
    # 否则它作为本服务的子进程会被一起杀掉, 来不及做存活检查和回滚
    url = "%s/%s/agent/upgrade.sh" % (GITHUB_RAW_BASE, token)
    cmd = ['systemd-run', '--unit=sss-agent-upgrade-' + token[:12], '--setenv=GITHUB_REF=' + token,
           '/bin/bash', '-c', 'curl -fsSL --max-time 30 "$0" | bash', url]
    try:
        subprocess.check_call(cmd)
        print("upgrade: started %s" % token)
    except Exception as e:
        print("upgrade: failed to start %s: %s" % (token, e))


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
    start_info_detection()
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
            # cppla 2.0 服务端紧跟着下发 monitors (每行一个 JSON), 可能被拆成多个包
            while data and not data.endswith('\n'):
                more = byte_str(s.recv(1024))
                if not more:
                    break
                data += more
            if PING_INTERVAL > 0:
                apply_monitors(data.splitlines())
            handle_upgrade(data.splitlines())

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
            prev_dr, prev_dw = get_disk_bytes()
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

                # 磁盘读写速度, 同样取间隔内平均值; 计数器回绕/换盘时按 0 处理
                disk_r, disk_w = get_disk_bytes()
                io_read = max(0, int((disk_r - prev_dr) / diff))
                io_write = max(0, int((disk_w - prev_dw) / diff))
                prev_dr, prev_dw = disk_r, disk_w

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
                array['io_read'] = io_read
                array['io_write'] = io_write
                array['ip_status'] = True
                array['ping_10010'] = lostRate['10010'] * 100
                array['ping_189'] = lostRate['189'] * 100
                array['ping_10086'] = lostRate['10086'] * 100
                array['time_10010'] = pingTime['10010']
                array['time_189'] = pingTime['189']
                array['time_10086'] = pingTime['10086']
                array['tcp'], array['udp'], array['process'], array['thread'] = get_tupd()
                # 服务端每次用整条上报替换旧值, 所以 custom 每次都要带
                array['custom'] = agent_custom()

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
