# 介绍
项目基于cppla版本ServerStatus， 增加如下功能：

- 更方便的节点管理, 支持增删改查
- 上下线通知（telegram）
- Agent机器安装脚本改为systemd， 支持开机自启

>由于未改动cppla版的任何代码，所以，我愿意把这个项目称为ServerStatus的小插件, 理论上它可以为任何版本的ServerStatus服务


# 新用户：全新部署

服务端需要 **docker**（没有就 `curl -sL https://get.docker.com | bash`）和 **jq**（`apt install -y jq`，只有节点管理脚本用得到）。

```bash
git clone https://github.com/3198137738/ServerStatus.git sss && cd sss

# 1. 填配置
cp .env.sample .env
vi .env          # TG_CHAT_ID / TG_BOT_TOKEN, 端口按需改
                 # Bot token 找 @BotFather, chat id 找 @getuserID

# 2. 建空节点表(必须, 否则 docker 会把 config.json 当目录创建, srv 起不来)
echo '{"servers":[]}' > config.json

# 3. 起栈
docker compose up -d --build
```

打开 `http://<你的IP>:8081`（端口即 `.env` 里的 `WEB_PORT`）就能看到面板了，此时还没有节点。

```bash
# 4. 加节点
sudo ./sss.sh
```

选 `2. 添加节点`，填名字/位置/类型，脚本会随机生成用户名密码、写进 `config.json`、重启 `srv`，然后打印出这台机器的 **agent 安装命令**——复制到被监控的机器上执行即可。命令忘了就用菜单 `1. 查看节点` 再翻出来。

# 老用户：升级

先看你的面板目录里有没有 `.git`：

```bash
cd /你的/面板目录 && ls -d .git
```

### 情况 A：有 `.git`（已经是仓库版）

```bash
git pull && docker compose up -d --build
```

完事。`.env`、`config.json`、`json/` 都已 gitignore，`git pull` 不会冲突。

### 情况 B：没有 `.git`（旧版 `wget sss.sh` 装的）

旧版是单文件下载安装的，没法 `git pull`，重新 clone 一次即可（只需这一次）。TG token 在旧目录自己的 `docker-compose.yml` 里，节点数据在 `config.json`。

```bash
# 在老目录(假设叫 sss)的上级执行
git clone https://github.com/3198137738/ServerStatus.git sss-new
cp sss/config.json sss-new/          # 节点数据: 每个节点的用户名密码都在里面, 丢了要挨台重装 agent

cd sss-new
cp .env.sample .env
grep TG_ ../sss/docker-compose.yml   # 打印出旧的 TG_CHAT_ID / TG_BOT_TOKEN
vi .env                              # 填进去; 端口保持默认

(cd ../sss && docker compose down)   # 停老栈, 释放端口
docker compose up -d --build
```

确认面板正常后老目录就能删了。**agent 端一台都不用动**——面板 IP、节点凭据、上报端口都没变。

> 升级后如果页面看着没变化，多半是 CDN（Cloudflare 之类）还攥着旧的 `js/app.js`。新版镜像已经让 nginx 下发 `Cache-Control: no-cache`，但**已经被缓存住的那份得手动 purge 一次**，之后就不用管了。

# 日常更新

```bash
git pull && docker compose up -d --build
```

前端是打进镜像的，所以必须带 `--build`。

# 节点管理

```bash
sudo ./sss.sh
```

`sss.sh` **只管节点**，不装 docker、不建镜像、不起栈——那些都是 `docker compose` 的事。它做的是：增删改查 `config.json`、`docker compose restart srv` 让配置生效、打印 agent 安装命令。

```
1. 查看节点   看某个节点的位置/类型/用户名/密码, 并重新打印 agent 安装命令(回车看全部)
2. 添加节点   随机生成凭据, 加完直接给出 agent 安装命令
3. 删除节点
4. 更新节点   改名字/位置/类型/月流量起始日
5. 远程升级所有 agent   下发当前 commit, 在线 agent 自动升级
0. 退出
```

# Agent 升级到低流量版

agent 默认每 5s 上报一次、三网探测每 30s 一次(原版都是 1s)，单台流量约从 160MB/天 降到 15MB/天。
已装过 agent 的机器不用重新填凭据，**每台执行同一条命令**即可(保留原有 SERVER/USER/PASSWORD，失败自动回滚)：

```bash
curl -fsSL https://raw.githubusercontent.com/3198137738/ServerStatus/master/agent/upgrade.sh | sudo bash
# 自定义间隔(INTERVAL 1~15, 服务端 20s 收不到上报会断开; PING_INTERVAL=0 关闭三网探测):
curl -fsSL https://raw.githubusercontent.com/3198137738/ServerStatus/master/agent/upgrade.sh | sudo INTERVAL=10 PING_INTERVAL=0 bash
```

# 在主控上修改 CU/CT/CM 探测目标

agent 内置的是山西联通/电信/移动(优先 IPv6, 不通自动退回 IPv4)。想换目标不用登录被监控机,
在主控的 `config.json` 里加 `monitors`(name 为 `CU`/`CT`/`CM`), 然后重启 srv, agent 重连时就会拿到新目标:

```bash
cd /opt/serverstatus/sss    # 你的面板目录
jq '.monitors = ((.monitors // []) | map(select((.name | ascii_upcase) as $n | ["CU","CT","CM"] | index($n) | not))) + [
  {"name":"CU","host":"sx-cu-v6.ip.zstaticcdn.com,sx-cu-v4.ip.zstaticcdn.com","interval":30,"type":"tcp"},
  {"name":"CT","host":"sx-ct-v6.ip.zstaticcdn.com,sx-ct-v4.ip.zstaticcdn.com","interval":30,"type":"tcp"},
  {"name":"CM","host":"sx-cm-v6.ip.zstaticcdn.com,sx-cm-v4.ip.zstaticcdn.com","interval":30,"type":"tcp"}
]' config.json > config.json.tmp && mv config.json.tmp config.json
docker compose restart srv
```

- `host`: 逗号分隔的候选, 按顺序用第一个 TCP 能连通的; 可写 `host:port` / `[IPv6]:port`, 缺省 80
- `interval`: 探测间隔(秒), **一定要写**, 不写服务端会默认成 600
- 删掉某项(或整个 `monitors`)再重启 srv, 那一路就回到 agent 内置的默认目标
- 需要 agent 是低流量版(`upgrade.sh` 装的), 且启动参数 `PING_INTERVAL` 不为 0

# 地区 / 类型自动检测

agent 会自动检测出口 IP 所在国家(Cloudflare trace, 如 `JP`)和虚拟化类型(`systemd-detect-virt`, 如 `kvm`),
面板的 REGION / TYPE 列优先显示检测值; 节点离线时显示浏览器缓存的最后一次检测值, 再没有才用 `config.json` 里手填的。

# 远程升级所有 agent

在主控上把仓库更新到最新, 然后 `sudo ./sss.sh` → `5. 远程升级所有 agent`:

```bash
git pull && docker compose up -d --build
sudo ./sss.sh      # 选 5
```

它会把当前 commit SHA 写进 `config.json` 的 `monitors`(`{"name":"UPGRADE","host":"<SHA>"}`)并重启 srv,
每台在线 agent 重连时发现 SHA 变了, 就用 `systemd-run` 拉取**该 commit** 的 `agent/upgrade.sh` 自行升级(失败自动回滚, 保留原有间隔参数)。

- 每个 SHA 只触发一次; 刚装上的 agent 第一次见到 UPGRADE 只记录基线, 不会重复升级
- 只认 40 位 commit SHA, 代码固定来自本仓库(`agent/client-linux.py` 里的 `GITHUB_RAW_BASE`)
- **安全提示**: 这等于所有被监控机以 root 信任本 GitHub 仓库和主控的 `config.json`, 请保护好 GitHub 账号和主控

更多信息请移步 https://lidalao.com/archives/87  +1ip

挺好用的？送作者一杯可乐？->
 [<img src="https://user-images.githubusercontent.com/52455330/139071980-91302a8a-37b1-4196-803e-f91b1de2ee5b.gif" width="60" height="40" />](https://shop.lidalao.com/buy/4)

# 参考
- https://github.com/cppla/ServerStatus
- https://github.com/naiba/nezha
