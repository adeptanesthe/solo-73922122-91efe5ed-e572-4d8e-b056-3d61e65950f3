# 口述史馆检索 API

FastAPI + SQLite 实现的检索服务：编辑按稿号刊发/撤稿分词后的转写稿，读者用查询词做
BM25 检索并凭游标翻页；撤稿即时生效，续页自动跳过被撤版本；同号重刊只进入重刊后
新开的检索。

续页不再每次读遍全库词频：服务维护一份**语料级倒排索引**（词 → 版本），索引补到
某游标首查时的版本高水位后，`/search/next` 只按查询词定位候选版本，再只读取这些
版本的词频与稿长。升级前签发的游标照常可用；旧库在后台分批补齐，期间照常刊发、
撤稿、续页，补齐出错或重启后从已完成处继续。

## 目录结构

```
app/main.py            # 服务全部实现（FastAPI + sqlite3 标准库）
app/requirements.txt   # 运行依赖
tests/test_api.py      # 关键操作与索引/回退/续补测试（pytest + TestClient）
Dockerfile             # 服务镜像
compose.yaml           # Docker Compose 启动
demo.sh                # 端到端演示脚本（含升级前游标、撤稿重刊、索引读取范围）
```

## 快速开始

### Docker Compose（推荐）

```bash
docker compose up --build
# 服务监听 http://127.0.0.1:8000 ，SQLite 数据在命名卷 search_data 中
# 重新演示前如需清空数据：docker compose down -v
# 另有 /healthz 健康检查；旧库升级后启动即在后台补齐索引
```

### 本地开发

```bash
pip install -r app/requirements.txt
cd app && uvicorn main:app --host 127.0.0.1 --port 8000
```

### 运行演示与测试

```bash
pip install -r app/requirements-dev.txt   # pytest + httpx
./demo.sh                          # 对 127.0.0.1:8000 跑完整场景（需 jq）
python3 -m pytest tests/ -v        # 自动化测试
```

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/manuscripts` | 刊发分词后的转写稿；在刊稿号重复刊发返回 409；同时写倒排索引 |
| POST | `/manuscripts/{doc_id}/withdraw` | 撤稿（立即生效）；无在刊版本返回 404 |
| GET  | `/manuscripts` | 列出全部版本（含已撤稿），便于观察版本演变 |
| POST | `/search` | 首查：BM25 计分，返回第一页与 `next_cursor` |
| POST | `/search/next` | 凭游标续页，可临时改页长；索引就绪时走索引路径 |
| GET  | `/index/status` | 索引补齐进度（`indexed_seq` / `latest_version` / `ready`） |
| POST | `/index/backfill?batches=N` | 同步推进补齐（测试/运维）；后台线程也会继续补 |
| POST | `/index/pause`、`/index/resume` | 暂停/恢复后台补齐（演示未补齐状态） |
| POST | `/index/rebuild` | 清空并重建索引（`?pause=true` 复现"旧库未补齐"） |
| GET  | `/healthz` | 健康检查 |

### 请求示例

```bash
# 刊发
curl -X POST localhost:8000/manuscripts -H 'Content-Type: application/json' \
  -d '{"doc_id":"MS-001","tokens":["抗战","北平","胡同","童年","粮票"]}'

# 首查：查询词 + 页长（terms 数组，或用 "query":"抗战 北平"）
curl -X POST localhost:8000/search -H 'Content-Type: application/json' \
  -d '{"terms":["抗战","北平"],"size":2}'

# 续页
curl -X POST localhost:8000/search/next -H 'Content-Type: application/json' \
  -d '{"cursor":"<上页返回的 next_cursor>"}'

# 续页并返回读取范围诊断（索引路径 vs 原路径）
curl -X POST localhost:8000/search/next -H 'Content-Type: application/json' \
  -d '{"cursor":"<cursor>","debug":true}'
# => {"read_scope":{"path":"index","index_candidates":7,"docs_scored":5,
#                   "snapshot_published":18,"snapshot_seq":20}, ...}

# 撤稿 / 同号重刊
curl -X POST localhost:8000/manuscripts/MS-001/withdraw
curl -X POST localhost:8000/manuscripts -H 'Content-Type: application/json' \
  -d '{"doc_id":"MS-001","tokens":["北平","抗战","重逢","口述","整理"]}'
```

`demo.sh` 串起完整场景：刊发 7 篇 → 重复刊发被拒(409) → 首查(页长 2) →
撤掉第 3 名 → 续页跳过被撤版本并补足当页 → 同号重刊 → 旧游标看不到重刊版、
新检索能看到 → 撤稿不等待慢速计分请求 → **索引升级**：还原"升级前旧库"（索引未
补齐）→ 升级前游标走原路径 → 补齐期间照常刊发/撤稿/续页 → 补齐后同一游标自动切到
索引路径，并打印两者读取范围。

## 关键设计

### 版本与刊发语义

- `manuscripts.version` 为自增版本序号，单调递增，作为全局"时间戳"。
- 部分唯一索引 `UNIQUE(doc_id) WHERE status='published'` 在数据库层保证同一稿号
  任一时刻至多一个在刊版本：在刊时重复刊发被 409 拒绝；撤稿后同号重刊插入新行、
  获得新版本号。

### 首查与游标

- 首查在**单个读事务**内读取一致快照：当前全部在刊稿 + 版本高水位
  `snapshot_seq`，据此计算 BM25（Okapi，k1=1.2，b=0.75，idf 取
  `ln(1+(N-df+0.5)/(df+0.5))` 保证为正）。命中至少一个查询词的稿件按
  **分数降序、同分按稿号升序**返回。
- 服务端为每个检索只登记**查询词、页长、语料统计（N、avgdl、各词 df）**
  （`searches` 表，O(查询词数)），**不保存整张榜单**；翻页位置
  `(上一页末条分数, 稿号)` 放在客户端持有的不透明游标里（base64 JSON）。
- 续页时取出冻结的语料统计，对 `version <= snapshot_seq` 且**当前仍在刊**的版本
  重算分数——相同输入下浮点结果逐位一致，因此分数与顺序与首查相同；再用键集
  分页取游标之后的 `size` 条。被撤版本在过滤时被跳过，后续命中自然上移补足当页；
  重刊版本 `version > snapshot_seq`，不会进入旧检索，只出现在重刊后新开的检索中。
- 游标可重复使用（幂等），续页可临时指定不同页长。
- 游标里没有任何榜单/文档内容，也没有索引指针；因此**升级前签发**的游标无需迁移，
  升级后直接可用。

### 倒排索引与续页读取范围

- `term_index(term, version)`：每个（词，版本）一行，`WITHOUT ROWID`，主键即覆盖
  索引。它只描述"哪个版本含有哪个词"，**与游标/榜单无关**，因此同一份索引可服务
  所有检索、所有读者。
- `index_meta.indexed_seq`：已补齐到的高水位。**某个游标的索引路径可用条件**是
  `indexed_seq >= 该游标的 snapshot_seq`——即首查那一刻的在刊版本都已进索引。
- 续页时在**同一个读事务**里先查 `index_meta`、再用 `term_index` 按查询词取
  `version <= snapshot_seq` 的候选版本，最后 `WHERE version IN (...) AND
  status = 'published'` 读取这些版本的 `tf/length`。索引查询、在刊状态、文档内容
  出自同一快照，所以"跳过已撤版本"与"同号重刊不进入旧榜"两条规则原样成立。
- 计分仍用首查冻结的 `N/avgdl/df`（不是当前语料），因此分数、同分按稿号排序、
  `remaining_hits` 与原规则**逐位一致**；只是候选集从"全部在刊稿"缩小为"含任一
  查询词的版本"。
- 索引未就绪（`indexed_seq < snapshot_seq`）、被禁用或读取失败时，续页自动回退到
  原来的全量路径；两条路径返回结构相同，`debug:true` 时额外给出 `read_scope`：
  - `path`: `index` / `scan`
  - `index_candidates`: 索引命中的候选版本数
  - `docs_scored`: 实际读取 `tf/length` 并计分的版本数
  - `snapshot_published`: 首查时在刊稿总数（对照全量路径的读取量）
  - `snapshot_seq`: 首查版本高水位

### 补齐：旧库升级、增量、出错与重启

- 服务启动即建 `term_index` / `index_meta`（幂等）。旧库升级后 `indexed_seq=0`，
  后台线程按版本升序**分批**扫描 `manuscripts`，每批一个独立写事务，提交后推进
  `indexed_seq`。批次只推进到本批最后一个成功写入的版本，不会跳洞。
- 每批提交只持写锁毫秒级（WAL），因此**补齐期间照常刊发、撤稿、续页**；续页在补齐
  完成前走原路径，不会等待。
- 出错时当前批次回滚、已提交的进度保留，错误写入 `index_meta.last_error` 并输出
  日志，后台退避重试；这段时间续页仍走原路径。
- 重启后从 `index_meta.indexed_seq` 继续，不重扫已完成部分。
- 新刊发在同一写事务内把词条写入 `term_index`；若补齐恰好停在上一个版本（稳态），
  同时原子推进 `indexed_seq`，新检索的续页立即走索引。若补齐仍在追赶，高水位保持
  不动，由后台按序补齐（避免把任何未索引版本"跳"过去）。
- 补齐完成度按版本号而非"已索引条目数"判定，所以撤稿/重刊不会卡住进度；撤稿不改
  索引（版本仍保留在语料里，由续页的 `status` 过滤排除），重刊产生新版本、正常入索引。
- 环境变量：`INDEX_ENABLED=0` 关闭索引（续页恒走原路径）、`INDEX_BACKFILL_ENABLED=0`
  只关后台补齐、`INDEX_BATCH_SIZE` 每批版本数（默认 200）、`INDEX_IDLE_SECONDS`
  空闲轮询间隔（默认 0.05）。

### 撤稿不阻塞计分

- SQLite 开 **WAL**：读（计分）与写（撤稿）互不等待；每请求独立连接，写事务
  只做一条 UPDATE/INSERT，毫秒级释放写锁；应用层没有任何全局锁。
- 同步视图函数由 Starlette 线程池并发执行，慢速计分请求不占着事件循环；
  后台补齐线程也只做短写事务，不与撤稿争抢。
- `/search` 的 `debug_delay_seconds` 是**测试钩子**（默认 0），人为拉长计分耗时，
  用于演示/验证撤稿不必等计分结束（见 `demo.sh` 第 9 步与
  `tests/test_api.py::test_withdraw_does_not_wait_for_scoring`）。

## 实测结果（交付前验证）

环境：Debian 12 / Python 3.11，无 Docker 守护进程（见"剩余事项"）。

- `python3 -m pytest tests/ -v`：**11 passed**
  - 刊发/重复刊发 409/撤稿/重刊版本递增；空 token 不入索引；
  - 完整走查：榜序与分数符合手算 BM25（MS-001 与 MS-005 同分按稿号升序），
    续页跳过被撤的 MS-003 并由 MS-006/MS-007 补足，续页分数与首查逐位一致，
    重刊版(version=8)不进旧游标、进新检索；
  - 续页改页长、游标幂等复用、坏游标 400、未知游标 404；
  - 并发：3 秒慢速计分期间撤稿 **60ms 内返回**，慢检索仍按其快照返回结果；
  - 索引等价：随机语料 40 篇、5 轮随机查询，逐页对比索引路径与强制原路径的
    分数、同分顺序、剩余命中数、有无下一页，并在翻页间随机撤稿/重刊后仍一致；
  - 升级前游标：索引未就绪时签发的游标，先走原路径；补齐后同一游标自动走索引，
    且 `docs_scored` 从"快照内全部在刊版本"降到"含查询词的候选版本"；
  - 索引未就绪 / `INDEX_ENABLED=0` 时回退原路径；就绪边界恰在
    `indexed_seq == snapshot_seq`：差一版仍走原路径，补到该版即切索引且结果一致；
  - 补齐幂等与续补：分批提交后人为抛错，进度停在上一批已提交处、`last_error`
    记录原因、续页仍走原路径；恢复后从该进度补到最新；补齐期间的撤稿/重刊在切到
    索引后仍按旧游标规则生效。
- `./demo.sh` 对本地 uvicorn 实例全流程跑通（退出码 0），关键输出：
  - 首查页 1：`MS-001, MS-005`（同分）；撤 MS-003 后续页：`MS-006, MS-007`；
  - 重刊 MS-003 → `version=8`；旧游标第 3 页仅 `MS-002`；新检索含 `MS-003 v8`；
  - 4 秒慢计分期间撤稿约 60ms；
  - 索引升级段：索引未补齐时升级前游标 `path=scan, docs_scored=18`；补齐后同一
    游标 `path=index, docs_scored=5`，分数/顺序/`remaining_hits` 与对照逐位一致。
- 独立探测：新进程接手同一旧库时，首个响应 `indexed_seq` 等于已提交进度（未从头
  重扫），随后自行补到 `latest_version`；后台线程在无人工干预下追平。
- 真升级演练：用**升级前代码**（基线 commit 的 `app/main.py`）建库并签发游标，
  再让新代码打开同一 SQLite 文件。旧游标在 `INDEX_ENABLED=0`（原路径）与索引就绪
  （索引路径）两种配置下，`hits` 的稿号/版本/分数、`remaining_hits` 与旧代码当年
  输出逐位一致；索引路径读取 5 个版本，原路径 6 个。
- 扩展随机对拍（未入库的临时脚本）：60 篇 12 词随机语料、20 轮随机查询共 183 页，
  翻页间随机撤稿/重刊并分批补齐；索引路径与原路径每页分数、顺序、剩余数、
  下一页有无全部一致。

## 剩余事项

- 本环境没有 Docker，`Dockerfile`/`compose.yaml` 未实际构建过；镜像 CMD 与本地
  验证过的启动命令一致（`uvicorn main:app`），建议在有 Docker 的环境跑一次
  `docker compose up --build` 复核。（本轮已升级 compose 为带健康检查的版本。）
- `term_index` 每次续页按查询词取候选后仍在应用内计分；若要进一步减少候选读取，
  可把 `tf` 与 `length` 也放进索引表（当前是两步读取：索引定位 + 主表取字段）。
- `searches` 表随首查数量单调增长，需要加过期清理（如按 `created_at` 定期删除，
  过期游标返回 404 的逻辑已就位）。
- 补齐线程为单实例假设：同一 SQLite 库由多个服务进程共享时，`_BACKFILL_LOCK`
  只在本进程内生效，跨进程靠 SQLite 写锁与 `indexed_seq` 条件推进（安全但可能重复
  写入，`INSERT OR IGNORE` 保证幂等）。多实例部署建议只让一个实例跑补齐，其余设
  `INDEX_BACKFILL_ENABLED=0`。
- 稿号按字典序排序，建议馆方使用等宽编号（如 `MS-001`）；如需数值序可改存整数列。
- 未做认证鉴权与限流；`debug_delay_seconds` / `debug` / 索引运维接口上生产前建议
  移除或加开关。
- 分数一致性依赖同一服务版本内浮点重算的确定性；滚动升级计分公式期间，旧游标
  的分数可能与新公式不一致（可通过在游标中记录公式版本号来防御）。
