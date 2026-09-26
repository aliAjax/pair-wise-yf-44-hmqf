# 化工装置变更与工艺安全管理

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8310`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8310
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `unit`：装置运行状态；`change`：变更申请；`action_item`：风险控制行动项。
- `temp_change`：临时变更（夜间抢修协调）。登记受影响装置、作业时段（`window_start`/`window_end`）、隔离措施（`isolation`）和恢复负责人（`restore_owner`）。

## 临时变更流程

1. 登记后为`draft`，`submit`后进入`submitted`。
2. 安全员（safety）执行`confirm`才生效（`active`）；若作业时段与该装置已生效单据重叠，确认被退回并指出冲突单号。
3. `restore`（需`restored_by`）完成恢复，进入`restored`。
4. 生效中未到期的单据可`renew`：需重新填写时段和风险评估（`risk_level`、`analyst`），生成新的确认版本（`revision`+1）并回到`submitted`，须安全员再次确认。已到期未恢复的单据不能续期，须重新登记。
5. 装置存在到期未恢复的临时变更时，`startup`被拒绝并列出阻塞单号；`GET /api/units/<id>/blockers`返回该装置的阻塞单明细和`can_startup`。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/units/<id>/blockers`：查询装置到期未恢复的临时变更阻塞项。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

风险分级和投产规则用于流程演示，不替代HAZOP、LOPA、法定许可和现场安全审查。
