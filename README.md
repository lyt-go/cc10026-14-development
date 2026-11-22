# 数据服务开放与订阅管理平台

面向数据API注册、查询路由、订阅审批、用量计量与SLA保障的企业数据服务开放平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| API注册 | `api_registry` | 数据API | API编号、API名称、接口路径 |
| 查询路由 | `query_route` | 路由规则 | 规则编号、关联API、路由条件 |
| 订阅管理 | `subscription` | 订阅申请 | 申请编号、订阅API、申请方 |
| 频率限制 | `rate_limiter` | 限流策略 | 策略编号、目标API、调用方 |
| 用量计量 | `usage_billing` | 用量记录 | 记录编号、调用方、API名称 |
| 缓存分层 | `cache_layer` | 缓存策略 | 策略编号、关联API、缓存层级 |
| 数据过滤 | `data_filter` | 过滤规则 | 规则编号、关联API、过滤字段 |
| 字段脱敏 | `field_mask` | 脱敏规则 | 规则编号、目标字段、脱敏算法 |
| SLA监控 | `sla_monitor` | SLA指标 | 指标编号、关联API、可用率目标 |
| 结果格式化 | `result_format` | 格式模板 | 模板编号、关联API、输出格式 |
| 查询优化 | `query_optimize` | 优化规则 | 规则编号、关联API、索引建议 |
| 异常归类 | `error_classify` | 异常类型 | 类型编号、类型名称、错误码段 |
| 数据归并 | `data_merge` | 归并规则 | 规则编号、主API、从API |
| 导出任务 | `export_task` | 导出任务 | 任务编号、关联API、查询参数 |
| 调用审计 | `call_audit` | 调用记录 | 记录编号、调用方、目标API |
| 版本兼容 | `version_compat` | 版本映射 | 映射编号、关联API、旧版本 |
| 批量查询 | `batch_query` | 批量任务 | 任务编号、关联API、查询清单 |
| SDK配置 | `sdk_config` | SDK配置 | 配置编号、关联API、SDK语言 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## SLA 分类重算

`/sla-recompute`（前端）与 `/api/sla-recompute/*`（后端）提供历史 SLA 事件的
批量分类重算，不改变原有 18 个模块的资源路由。

- **按事件原时间窗口选版**：事件的 `window_start` 落在哪个分类版本生效区间
  （`[effective_from, effective_to)`），就用哪一版错误码规则，与采集时间无关。
- **跨批次去重**：同一 `request_id` 被多批采集覆盖时只保留采集时间最晚的一条，
  当次重算只计一次；被合并的事件号、批次留在 `duplicate_event_ids` /
  `source_batches` 中可追溯。交付报告按原始采集记录统计，因此报告量会高于重算量。
- **重算留痕**：每次重算固化 `versions_snapshot`、逐事件 `window_start` /
  `window_end`、`classification_version` 与归类结果，可导出 CSV 复核。
- **差异分解**：`volume_effect`（去重）与 `classification_effect`（口径）分开
  呈现，避免把版本切换误读成异常增长。
- **交付与核对**：重算结果可交付为冻结报告（不可覆盖），
  `GET /api/sla-recompute/reconcile` 会把报告复现结果、重算结果、去重/口径差异
  和运营可读原因一起返回。
- **SLA监控消费**：`POST /api/sla-recompute/runs/{id}/consume` 按监控周期把
  指标明细重新分桶统计，每个异常类型都能逐条关联到事件，并回写SLA指标告警。

示例数据（`backend/app/domain_seed.py`）覆盖查询窗口横跨 v1.0/v2.0 两个生效日期、
504 在新旧规则下归入不同异常类型，以及两个请求被跨批次重复采集。

领域：数据工程
子方向：数据分析与数据应用
