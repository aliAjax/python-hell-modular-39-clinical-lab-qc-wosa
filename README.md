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

`assay`为检测项目，`qc_lot`为质控品批次，`instrument`为仪器，`qc_run`为质控结果，`result_batch`为患者结果批次，`takeover`为质控品批次接管单。

## 接管单（质控品换批号）

换批号不再走纸单：新建`takeover`把旧批号、新批号、参与试跑的仪器和在制患者结果批次挂到同一张单上。

1. `POST /api/takeovers`（supervisor/admin）：传入`assay_id`、`previous_lot_id`（当前active批号）、`new_lot_id`（registered新批号）和`instrument_ids`，单据初始为`trialing`，每台仪器一个名额。
2. 逐台试跑：新批号在仪器上出`qc_run`并`evaluate`后，对接管单执行`record_trial`（operator及以上），引用`instrument_id`和`qc_run_id`；同一名额重复提交相同结果为重放（不重复占用名额、不重复写审计），换结果抢占名额返回409。
3. 任一台不合格，单据变`failed`，旧批号保持active，新批号保持registered，`confirm`返回409并带`pending_instrument_ids`和`current_version`；`failed`为终态，需另开接管单。
4. 全部合格后单据为`ready`，supervisor执行`confirm`：在**确认时刻**（服务端时间戳）于单个SQLite事务内激活新批号、停用旧批号，未放行（非`released`）的患者结果批次改挂新批号；已放行批次仍对应旧批号。
5. 两名主管并发`confirm`（乐观锁`expected_version` + `BEGIN IMMEDIATE`）只有一人成功；后到者得到409、未完成仪器和最新版本，重新读取后再试会收敛到已确认状态，不产生第二份副作用。
6. 写操作建议带`Idempotency-Key`（按动作+实体作用域），写入失败后凭接管单和同一键重试即可。
7. 旧数据：没有接管单即按未接管处理；未盖批号戳的历史`result_batch`通过其`qc_run`解析原批号，历史质控结果和原批号照常可查。旧库启动时自动补列，无需手工迁移。

`GET /api/takeovers/<id>`返回带引用展开的视图（assay、新旧批号、仪器名额、质控结果、在制批次及各自`effective_lot_id`、未完成仪器列表）。

## 接口

- `GET /health`
- `GET /api/<kind>`，可用`?status=`过滤
- `GET /api/entities/<id>`
- `POST /api/<kind>`
- `POST /api/entities/<id>/actions`
- `GET /api/audit`

身份通过`X-User-Id`和`X-Role`请求头传入。可选`Idempotency-Key`防止重复创建。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

质控规则为可运行的简化模型，包含1-3s、连续偏移和趋势检查，但不替代CLIA、ISO 15189、Westgard完整规则集或实验室信息系统接口。
