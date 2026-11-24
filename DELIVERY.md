# 交付说明（本次实际完成与验证）

## 本次修复：持续补报时拆分不再“追读新明细”

**问题复现**：`_copy_records` 用开放游标 `id > last_id ORDER BY id LIMIT n` 追读，持续补报恰好
填满每一批时循环看不到尾。确定性脚本（每批钩子里再插满一批）20 秒内复制 1653 批、8265 行仍不结束
（初始仅 40 行）。

**改动**（仅 `inspection/db.py`、`inspection/service.py`，无新依赖、无接口破坏）：

1. `split_jobs` 增加 `copy_upto_id`（含旧库 `ALTER TABLE ... ADD COLUMN` 幂等迁移）。
   `begin_split` 在 `BEGIN IMMEDIATE` 事务内取原分区 `MAX(records.id)` 作为拆分开始快照高水位。
2. `_copy_records` 增加 `id <= copy_upto_id` 上界，只复制拆分开始时已有明细，批数在开始时固定；
   `on_batch(job_id, copied)` 触发期间 `copied` 不会超过开始时条数。
3. 新增 `_stage_late_reports()`：进入切换事务前，用独立写事务把复制期间的补报全部幂等补拷备齐。
4. `_switch_split` 保留最后一次幂等扫尾（补齐“备齐提交后、切换事务前”极小窗口的提交）、
   条数校验与原子搬移；切点归右、每条一份的逻辑不变。
5. `split_day` 新增可选观测钩子 `on_before_switch(job_id, staged)`；不传时行为向后兼容。

**新增测试** `tests/test_split_snapshot.py`（5 项）：每批都插满一批的对抗场景必须有界结束且
`copied≤初始条数`；切换前全部已确认明细可从唯一原 active 分区下载；后台线程持续补报下拆分不丢不重、
按里程左右下载互斥且并集为全集；备齐后/切换前注入中断，原分区完整、可重试且切点归右；旧库缺列自动迁移。

**实测（真实 gunicorn + HTTP/CSRF 网页流程，命令同 Dockerfile CMD，仅绑定 127.0.0.1）**：

- G101：网页上传 6000 行；网页发起 `cut=9000` 拆分的同时连续提交 121 次补报（242 行，0 失败）。
  拆分 3.28s 完成、搬移 6240；最终 6242 条全部唯一，左 `[0,8999]` 3120、右 `[9000,+∞)` 3122，
  切点 9000 的 122 行（1 旧 + 121 补报）全在右侧，左右不相交且并集为全集。
- G202：网页上传 40000 行，拆分途中（job 已复制 500 条）`kill -9` gunicorn worker；master 拉起新
  worker，启动恢复把任务记为 `failed / interrupted before switch`、清空影子与暂存，原分区 40000 条
  完整，网页下载与杀进程前逐字节一致；再补报 2 行后第二次在复制 500 条时强杀，仍完整（40002 行）；
  随后网页重试拆分成功，搬移 40002 条，左 22500 / 右 17502，切点 45000 两行均归右。
- 另跑 4 线程零间隔补报极限：拆分有界完成，0 错误、0 重复、0 越界，下载条数与库一致。
- `pytest`：原 47 项 + 新增 5 项全部通过；并发/模糊用例与新用例重复运行 10 轮无抖动。

**环境限制（如实）**：本沙箱仍无 Docker CLI/守护进程且无 root/capabilities，无法真正
`docker compose up --build`；已用 Dockerfile 中等价的 gunicorn 命令实跑网页验证，并静态校验
`docker-compose.yml` 结构。首次镜像构建仍建议在有 Docker 的环境确认。

---

# 以下为基线交付说明


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
