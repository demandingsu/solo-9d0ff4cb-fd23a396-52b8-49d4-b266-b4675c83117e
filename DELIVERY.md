# 交付说明（本次实际完成与验证）

## 实际完成

| 需求 | 实现 |
| --- | --- |
| Flask 网页，选日期、上传单条公路 CSV | `inspection/web.py`、`templates/index.html`；上传需 `road` + `date` + 文件 |
| 每行记录号 / 整米里程 / 病害等级 | `inspection/csvio.py`：中英文表头、UTF-8/GB18030、严格整数校验、物理行号报错 |
| 存入 SQLite | `inspection/db.py`：WAL、`partitions` / `records` / `split_jobs` / `split_staging` |
| 同日同号原样补报只收一次 | `upload_records`：内容一致计为 unchanged，不重复写；文件内重复同理 |
| 内容不同报冲突 | 整份文件回滚并逐条列出冲突（库内冲突或文件内自相矛盾） |
| 未拆分日分区按里程点拆两份，切点归右 | `split_day` / `begin_split` / `_switch_split`；半开区间 `[lo,cut)` 与 `[cut,hi)` |
| 搬旧记录时补报仍能完成 | 复制期原分区保持 active 可写；切换事务内做增量补拷，验证条数一致后整体搬移 |
| 切换后两份新分区不重不漏 | 切换是单个 `BEGIN IMMEDIATE` 事务；`records` 唯一键与范围触发器兜底；测试逐条核对 |
| 后续补报按里程归档 | 上传按当前 active 分区半开区间路由 |
| 按日期/里程范围下载，只读相交分区 | `download_csv` 只选与 `[start,end]` 相交的 active 分区 |
| 途中切换也按下载开始布局读完 | 下载包在显式读事务里，WAL 快照保证布局与记录一致 |
| 未切换就中断/报错，原分区仍可查全、可重试 | 复制期只写影子/暂存；失败或 `kill -9` 后丢弃影子，原分区完整；启动或下次拆分自动回收陈旧任务 |
| Docker Compose 启动网页 | `Dockerfile`（gunicorn 单 worker 多线程、非 root、healthcheck）+ `docker-compose.yml`（命名卷、必填 `SECRET_KEY`） |

## 实际验证

1. **自动化测试 47 项全部通过**（`pytest`，Python 3.11 + SQLite 3.40）：
   - 上传幂等、文件内重复、冲突整份拒绝、跨日期隔离；
   - 切点归右、二级拆分、非法切点/边界拒绝、布局不变；
   - 复制中补报（切换前可见性、切换后归档、后续补报）；
   - 下载只读相交分区、范围端点、下行中切换仍按开始快照读完；
   - 复制失败/异常、进程重启恢复、不重启直接重试、陈旧任务回收；
   - 复制中幂等/冲突补报；
   - 多进程存活性锁（`flock`）不误杀在途任务；
   - 随机上传+拆分模糊测试（分区无缝无叠、无重无漏、范围合法）；
   - 上传/下载/拆分并发压力测试（3 轮随机种子）；
   - 网页层：CSRF 缺失拒绝、上传/冲突/拆分/下载/413/路径名清洗。

2. **真实进程（gunicorn，127.0.0.1:8765）试跑**：
   - 2 万～3 万行 CSV 经真实 HTTP 上传，下载条数一致；
   - 复制中途 `kill -9` 工作进程：重启后任务标记 `failed / interrupted before switch`，
     原分区 2 万条完整可下载；再重试拆分成功，切换后 2 万条不重不漏、无越界；
   - 复制中途补报（切点两侧各一条）：切换后分别落入左右分区，最终 30002 条无重复；
   - 并发下载循环 + 连续两次拆分：40 次下载均返回完整 20001 行。

3. **其他检查**：依赖固定版本并查询 OSV（Flask 3.1.3、gunicorn 27.0.1、pytest 9.2.0 均无已知漏洞）；
   从非项目工作目录导入运行正常；`python -m inspection` 与 gunicorn 入口均启动、`/healthz` 返回 200；
   Compose YAML 结构可解析。

## 未完成 / 未验证（如实说明）

* **Docker 未实际执行**：本机没有 Docker CLI/守护进程，`docker compose up --build` 没有跑过；
  Dockerfile/Compose 只做了静态检查与结构校验。
* 未做登录、权限、路段授权；“养护员只取自己路段”目前靠部署方在网关层限制，页面本身不限公路。
* 未提供记录删除/修改/纠错、分区合并、流式下载、拆分进度条。
* 单 SQLite 写者、上传全量内存解析，适合中低并发与单路段规模，不是高并发方案。

## 运行

```bash
export SECRET_KEY="$(openssl rand -hex 32)"
docker compose up --build -d      # http://localhost:8000
```

本地无 Docker：`pip install -r requirements.txt && export SECRET_KEY=... && python -m inspection`。
