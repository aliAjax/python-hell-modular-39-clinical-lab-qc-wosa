# 临床实验室质量控制与结果拦截

只使用Python标准库和SQLite的模块化服务，默认端口`8339`。覆盖检测项目、质控品批次、质控规则、允许范围、仪器校准、连续偏差、趋势、失控、结果拦截、复测、调查、批次切换和历史更正。

## 模块结构

- `app.py`：参数解析、依赖组装和服务生命周期。
- `src/domain.py`：角色、领域异常和数据对象。
- `src/rules.py`：QC规则计算、状态机、校准与放行约束。
- `src/repository.py`：SQLite持久化、乐观锁、幂等和审计查询。
- `src/service.py`：用例编排、权限校验和版本控制。
- `src/http_api.py`：JSON接口和统一错误响应。
- `src/audit.py`：操作审计。
- `static/index.html`：最小演示页面。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8339
```

## 核心对象

`assay`为检测项目，`qc_lot`为质控品批次，`instrument`为仪器，`qc_run`为质控结果，`result_batch`为患者结果批次，`handover`为质控品批号接管单。

## 批号接管单（handover）

换质控品批号时，新批号必须在每台仪器上逐台平行试跑，全部合格后才能接管；任一仪器不合格则保留旧批号。接管单把质控品批次、患者结果批次、质控结果和仪器挂到同一张单子上，替代纸单传递。

- `POST /api/handovers`：主管创建接管单，指定检测项目、新批号和需要试跑的仪器清单。系统自动带出当前在用的旧批号，并为每台仪器生成一条待试跑记录。
- `POST /api/entities/<handover_id>/actions`，`action=record_trial`：逐台登记试跑结果（引用新批号上已评估的质控结果）。同一台仪器重复登记同一结果幂等，不重复占用仪器名额、不重复写审计。
- `action=confirm`：全部仪器试跑合格后确认接管。接管在确认时刻生效：新批号置为在用、旧批号停用；此前未放行的患者批次改用新批号，已放行批次仍对应旧批号（历史可查）。
- `action=abort`：放弃本次接管，旧批号继续有效。

并发与重试：

- 两名主管并发确认时，乐观锁只让一人成功；后到者收到 `409`，响应体带 `current_version`（最新版本号）、`unfinished`（未完成/不合格仪器）和 `handover_id`，据此用最新版本重试。
- 确认写入失败后按接管单继续重试：已确认的接管单重试直接返回结果，不重复写审计；批次改号在同一事务内完成，不会重复占用仪器名额。
- 旧数据升级时，没有接管单的批次按未接管处理，沿用原批号放行；历史质控结果和原批号仍可查询。

## 接口

- `GET /health`
- `GET /api/<kind>`，可用`?status=`过滤
- `GET /api/entities/<id>`
- `POST /api/<kind>`
- `POST /api/entities/<id>/actions`
- `GET /api/audit`

身份通过`X-User-Id`和`X-Role`请求头传入。可选`Idempotency-Key`防止重复创建。并发写通过请求体里的`expected_version`做乐观锁重试。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

质控规则为可运行的简化模型，包含1-3s、连续偏移和趋势检查，但不替代CLIA、ISO 15189、Westgard完整规则集或实验室信息系统接口。
