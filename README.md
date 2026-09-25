# 企业排污许可与超标处置

汇总监测和工况，判断排放超标并跟踪复测、整改、执法与复查。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限、关闭不变量与复查确认规则。
- `src/repository.py`：SQLite建表、事务、版本控制、复查确认批次和审计链。
- `src/service.py`：权限检查、用例编排、并发控制、复查确认和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：演示页（推进、补材料、复查确认、关闭与阻塞原因）。
- `tests/`：完整流程、规则、失败与复查确认测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8313
```

默认端口为`8313`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/review-confirmations`，复查阶段由compliance_officer按当前全部材料编号与事件版本确认
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

允许角色：operator, compliance_officer, director, viewer。按浓度与许可限值计算超标倍数，异常读数先进入评估；关闭前必须没有未完成整改项。

### 复查确认规则

事件进入复查（inspection）后，关闭前必须由compliance_officer完成复查确认：确认快照记录当时的全部材料编号与事件版本。确认后再新增任何材料（或事件版本变化），旧批次立即失效，director关闭返回`409`并提示"请重新确认"；同一批材料重复确认幂等返回，不新增批次（新批次HTTP 201，重复HTTP 200）。关闭判定在数据库事务与锁内原子完成，补材料与关闭并发时不会漏掉新材料。`GET /api/items/{id}`的`review`字段给出`can_close`、`close_blockers`与`latest_batch_no`，演示页可直接确认并展示阻塞原因。事件关闭后不再接受新材料。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
