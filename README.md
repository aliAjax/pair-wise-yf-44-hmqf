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
- `static/index.html`：临时变更协调台页面（登记、确认、恢复、续期评估）。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8310
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `unit`：装置运行状态；`change`：变更申请；`action_item`：风险控制行动项。
- `temp_change`：夜间抢修临时变更，登记受影响装置、作业时段（`work_start`/`work_end`）、隔离措施和恢复负责人。

## 临时变更协调流程

1. 值班员/工程师登记后进入`pending`，须安全员执行`confirm`才变为`active`（占用装置）。
2. `confirm`时若作业时段与同装置已生效单相撞，申请被退回，错误信息列出冲突单号。
3. `active`且超过`work_end`未`restore`的装置禁止`startup`，错误信息列出阻塞单号。
4. 续期不能直接延长：`renew`要求新的`work_end`和重新风险评估（`risk_level`/`risk_note`），单据回到`pending`并递增`revision`，须安全员再次确认形成新版本。
5. `GET /api/board`返回协调台视图：各装置的启动阻塞项、占用中作业，以及各临时变更单的到期与冲突标记。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/board`：临时变更协调台视图（装置阻塞项与单据状态）。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

风险分级和投产规则用于流程演示，不替代HAZOP、LOPA、法定许可和现场安全审查。
