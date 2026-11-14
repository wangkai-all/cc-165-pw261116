# warcview — 本地 WARC 档案检索与数据抽取

纯 Python 标准库实现(无第三方依赖)的本地 Web 工具:上传 WARC/WARC.gz 网页档案,
流式扫描建立索引,按 URL 与捕获时间检索,对选定时间范围按规则抽取标题与表格数据。

## 快速开始

```bash
python3 run.py --port 8000 --data ./data
# 打开 http://127.0.0.1:8000
```

在「档案」页点 **生成示例档案** 可得到一份合成档案(含重复正文、revisit、
缺失引用、损坏记录),点 **扫描** 建索引后即可在「页面」页检索、预览、比较、抽取。

运行测试:

```bash
python3 -m unittest discover -s tests -v   # 36 个测试
```

生成合成档案到文件:

```bash
python3 -m warcview.fixtures /path/to/sample.warc.gz
```

## 功能与实现要点

| 需求 | 实现 |
| --- | --- |
| 上传压缩档案,流式扫描 | 逐 gzip member 读取(`warc.py`),每 member 一条记录,支持 `.warc` 明文 |
| 按地址与捕获时间索引 | SQLite `records(archive_id, url, timestamp)` 索引(`indexer.py`) |
| response / revisit 区分 | `type` 字段;revisit 记录解析 `WARC-Refers-To`,预览/抽取时解析到原始响应 |
| 解压/字符集失败保留位置 | 损坏记录入库 `type='error'` 带 `file_offset` 与错误;字符集失败回退解码并标记 `charset_error`,原始字节始终可按偏移重读 |
| 时间范围 + 规则抽取 | 抽取任务(runs):URL + 起止时间 + 规则(标题/表格/自定义正则) |
| 结果带捕获编号/指纹/偏移 | `results` 表含 `capture_id`、`digest`(sha256)、`file_offset` |
| 相同正文保留各自时间 | 每次捕获独立成行,指纹相同仅表示正文一致 |
| 预览历史版本 | 沙箱 iframe + 严格 CSP(`default-src 'none'`),不加载任何外部资源 |
| 比较字段变化 | 两版本抽取结果逐字段 diff(标题、表格行增删、自定义字段) |
| 重跑规则 / 下载 | `POST /api/runs/{id}/rerun`;CSV(带 BOM)/JSON 下载 |
| 扫描中断恢复 | `archives.scan_offset` 逐条提交;停止/崩溃后从断点继续;重启服务自动归为 paused |
| 档案不可重写 | 注册后 `chmod 0444`,所有访问只读;测试校验扫描前后 SHA-256 不变 |
| 不访问外部网址 | 服务端无任何外联;预览 CSP 禁止一切外部加载 |

## 目录结构

```
warcview/
  warc.py       # WARC/gzip 流式解析,字节级偏移,损坏容错与重同步
  indexer.py    # SQLite 索引 + 可恢复扫描器
  extract.py    # HTTP/传输编码/内容编码/字符集解码,标题与表格抽取,字段 diff
  engine.py     # 按偏移取正文、revisit 引用解析、任务执行、版本比较
  fixtures.py   # 合成档案生成器(重复正文/缺失引用/损坏记录)
  server.py     # HTTP API + 静态页(仅监听本地)
  static/       # 单页前端(原生 JS)
run.py          # 启动入口
tests/          # 36 个单元与端到端测试
```

## 主要 API

```
POST /api/archives?name=x.warc.gz   上传(octet-stream)
POST /api/fixtures                  生成合成示例档案
POST /api/archives/{id}/scan        开始/继续扫描 {"force": false}
POST /api/archives/{id}/stop        停止扫描(可恢复)
GET  /api/archives/{id}/records     检索:url/type/status/from/to/limit/offset
GET  /api/archives/{id}/urls        页面聚合(捕获数、正文版本数、时间范围)
GET  /api/records/{id}/content      解码正文(JSON);?raw=1 下载原始字节
GET  /api/records/{id}/preview      沙箱预览(严格 CSP)
POST /api/runs                      新建并执行抽取任务
POST /api/runs/{id}/rerun           重跑规则
GET  /api/runs/{id}/download        ?format=csv|json
POST /api/compare                   {"record_ids": [a, b], "rules": {...}}
```

## 数据安全说明

- 上传的档案是不可信数据:每个档案存放在 `data/archives/<id>/` 独立目录,
  解析全程只读,索引只记录偏移与摘要,正文按需从原档重读。
- 损坏记录(CRC 错误、非 gzip 垃圾、截断 member)以错误条目入库并保留偏移,
  扫描器重同步继续,不会静默跳过。
- 服务仅绑定 `127.0.0.1`;预览页面 CSP `default-src 'none'` + iframe 全沙箱,
  双重保证不访问档案中引用的外部网址。
