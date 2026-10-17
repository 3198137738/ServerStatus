#!/usr/bin/env python3
# coding: utf-8
# Create by : https://github.com/lidalao/ServerStatus
# 版本：0.0.1, 支持Python版本：2.7 to 3.9
# 支持操作系统： Linux, OSX, FreeBSD, OpenBSD and NetBSD, both 32-bit and 64-bit architectures

import os
import sys
import json
import requests
import time
import traceback

NODE_STATUS_URL = 'http://srv/json/stats.json'

offs = []
counterOff = {}
counterOn = {}

def _send(text):
    chat_id = os.getenv('TG_CHAT_ID')
    bot_token = os.environ.get('TG_BOT_TOKEN')
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage?parse_mode=HTML&disable_web_page_preview=true&chat_id=" + chat_id + "&text=" + text
    try:
        requests.get(url)
    except Exception as e:
        print("catch exception: ", traceback.format_exc())

def send2tg(srv, flag):
    if srv not in counterOff:
        counterOff[srv] = 0
    if srv not in counterOn:
        counterOn[srv] = 0

    if flag == 1 : # online
        if srv in offs:
            if counterOn[srv] < 10:
                counterOn[srv] += 1
                return
            #1. Remove srv from offs; 2. Send to tg: I am online
            offs.remove(srv)
            counterOn[srv] = 0
            text = '<b>Server Status</b>' + '\n主机上线: ' + srv 
            _send(text)
    else: #offline
        if srv not in offs:
            if counterOff[srv] < 10:
                counterOff[srv] += 1
                return
            #1. Append srv to offs; 2. Send to tg: I am offline
            offs.append(srv)
            counterOff[srv] = 0
            text = '<b>Server Status</b>' + '\n主机下线: ' + srv 
            _send(text)

# 节点静态信息缓存: agent 只在连接后/每 60s 上报地区、类型、系统、CPU(省流量), 中间的 stats.json
# 里这些字段为空。这里记下每个节点最后一次的非空值写成 meta/nodes.json, 由 web 提供,
# 新浏览器打开面板第一屏就有完整信息, 不用等下一次静态上报
META_FILE = os.getenv('META_FILE', '/meta/nodes.json')
META_FIELDS = ('loc', 'virt', 'arch', 'os', 'cpu', 'cores')


def load_meta():
    try:
        with open(META_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def update_meta(meta, servers):
    """把 stats.json 里的非空静态字段合并进 meta; 有变化返回 True"""
    changed = False
    for s in servers:
        name = s.get("name")
        if not name:
            continue
        got = {}
        for kv in str(s.get("custom") or "").split(";"):
            k, _, v = kv.partition("=")
            if k and v:
                got[k] = v
        if s.get("os"):
            got["os"] = s["os"]
        if s.get("cpu_model"):
            got["cpu"] = s["cpu_model"]
        if (s.get("cpu_cores") or 0) > 0:
            got["cores"] = s["cpu_cores"]
        node = meta.setdefault(name, {})
        for k in META_FIELDS:
            if got.get(k) and node.get(k) != got[k]:
                node[k] = got[k]
                changed = True
    return changed


def save_meta(meta):
    try:
        os.makedirs(os.path.dirname(META_FILE), exist_ok=True)
        tmp = META_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(meta, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, META_FILE)      # 原子替换, web 不会读到半个文件
    except Exception:
        print("写入 %s 失败: %s" % (META_FILE, traceback.format_exc()))


def sscmd(address):
    meta = load_meta()
    while True:
        try:
            r = requests.get(url=address, headers={"User-Agent": "ServerStatus/20211116"})
            jsonR = r.json()
        except Exception as e:
            # srv 尚未就绪 / 暂时无数据, 稍后重试, 不让进程崩溃
            print('未发现任何节点')
            time.sleep(3)
            continue
        if update_meta(meta, jsonR.get("servers", [])):
            save_meta(meta)
        for i in jsonR["servers"]:
            if i["online4"] is False and i["online6"] is False:
                send2tg(i["name"], 0)
            else:
                send2tg(i["name"], 1)


        time.sleep(3)

if __name__ == '__main__':
    sscmd(NODE_STATUS_URL)
