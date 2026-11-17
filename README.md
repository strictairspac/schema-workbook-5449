# 实验委托模型拆解册

南麓材料实验室（9人）把委托单上的业务依赖整理为可验证的3NF关系，并保留候选键、闭包、最小覆盖和无损校验证据。

## 业务工作流

原先设计员在Excel里堆叠委托号、客户、检测项、方法与结果，再手工画表结构。痛点：依赖口径难确认、复合键容易漏判、拆表后无法证明无损。上游为委托单和已确认业务规则，本项目承接关系草案登记、函数依赖编辑、3NF合成、验证报告导出，下游为实验数据录入系统的数据库设计人员。

核心实体：关系草案 Relation、函数依赖 Dependency、跨草案引用 Reference、分解报告 Report。5业务页和独立登录页；管理员另有审计页。业务规则不执行为SQL，属性名是纯数据。

## 跨草案引用与规则复用

多份委托草案复用同一套业务规则时，在「跨草案引用」页建立引用：选取源草案属性并逐条映射为目标草案属性（`源属性=目标属性`）。

- **派生规则**：源 FD 按映射翻译为目标 FD；只登记被选取属性，**经未选属性传递得到的依赖自动保留**（如源 `A→B`、`B→C` 而 B 未选取，仍保留映射后的 `A→C`）。支持隐含空决定项（`∅→A` 常量依赖）。
- **建立即拒绝**：多个源属性映射到同一目标属性（合并属性）、引用成环、引用自身、引用关系外属性，均返回 422 且不落库。
- **三态与来源**：引用处于 `pending 待检查 / effective 已生效 / conflict 冲突`。派生规则与目标自有规则决定项相同但被决定项不同时标冲突并写明原因；每条规则附带推导来源（源规则映射 / 经未选属性传递 / 目标自有）。
- **更正传播**：源自有规则更正后沿引用链拓扑更新，目标自有规则原样保留。传播是单事务：任一环节失败，引擎从备份恢复、数据库整体回滚，**整链保留旧版**；重试按 `(引用, 源版本)` 幂等，**绝不重复产生规则**。
- **报告不混用**：受理瞬间冻结一个一致版本的有效依赖（自有 + 已提交派生）。更正与传播原子换版，因此每份报告只采用**全旧或全新**的有效依赖，快照不可变，后续更正不影响历史报告。
- **并发**：引擎内建可重入锁；源更正与报告受理并发时，快照要么读到换版前要么换版后，不会半新半旧（见并发测试）。


## 快速启动

```sh
cp .env.example .env
docker compose up -d --build
docker compose exec -T backend python -m scripts.seed_demo
```

前端 http://localhost:18243 ，后端 http://localhost:19243/docs ，MySQL localhost:17743。

|角色|账号|密码|权限|
|---|---|---|---|
|admin|admin@nanlu.test|Demo2026!|草案/依赖写入、运行、审计|
|operator|operator@nanlu.test|Demo2026!|草案/依赖写入、运行、导出|
|viewer|viewer@nanlu.test|Demo2026!|只读与导出，写API403|

## 一天的工作剧本

09:00 operator登录；09:05打开 `NL-1117-A` 材料检测委托单；09:10查看五属性 `委托号,客户,检测项,方法,结果`；09:15确认 `委托号→客户`、`检测项→方法`、`委托号,检测项→结果`；09:20执行 `shift-20261117` 分解任务；09:25候选键为 `{委托号,检测项}`，拆出委托客户、检测项方法、委托检测结果3个关系；09:30导出 `report-1.json`；09:40 viewer复核3NF/依赖保持/无损均通过；10:00 admin审计。

另有 `NL-1117-B` 的循环依赖 A→B/B→A与独立属性C，候选键AC/BC；`NL-1117-C` 没有确认依赖，完整属性集作为唯一候选键，不凭空猜测业务规则。

## 算法说明

Python真算，MySQL保存草案/依赖与结果，Redis传递任务，独立worker执行。

1. 属性闭包：迭代应用所有X→a，直到不再增加属性。
2. 候选键：先取不出现在RHS的必需属性，再按大小枚举其余属性子集；若闭包覆盖全集且不含已知候选键，加入结果。**最多10个属性/64条依赖**，最多1024个子集；超限API422/算法ValueError，避免指数搜索失控。
3. 最小覆盖：RHS拆成单属性，去平凡依赖；逐一删除左侧多余属性、移除冗余依赖。
4. 3NF合成：按最小覆盖左侧分组，删除被包含关系；若无关系含候选键，补入候选键关系。无依赖时保留全属性关系。
5. 验证：精确生成F+在各分解关系上的投影，枚举局部候选键，逐非平凡投影依赖检查3NF；所有原始依赖须可由投影并集推出。tableau追赶法依据FD合并符号，出现全a符号行则无损，报告包含见证行、合并步骤与最终表。

复合、循环、空决定项、无依赖均支持。不在任何RHS中的属性强制进入候选键。输入入队时冻结快照，后续修改不会改变已有报告。结果JSON含SHA256，固定输入应有固定输出。失败态包括引用关系外属性、重复/空属性、属性超过10、依赖超过64、队列错误；页面可恢复并重新执行。

这是小型关系模型设计辅助工具，FD由人工提供，不能从脏数据自动推断业务规则；不连接生产数据或自动执行DDL。精确投影在10属性内有界，仍可能随依赖复杂度增加耗时。单worker串行队列，不带生产任务重试/崩溃续跑。

## 工程结构

|服务|技术|职责|
|---|---|---|
|frontend|原生HTML/CSS/JS + Nginx|4业务页、登录、共享业务表格与报告组件|
|backend|Python3.11/FastAPI/SQLAlchemy/PyJWT|校验、权限、审计、快照与导出|
|worker|Python3.11|闭包、候选键、3NF与chase|
|db|MySQL8.0|JSON属性与依赖、任务报告|
|redis|Redis7|持久任务队列|

目录：`frontend/src/{api,stores,types,components/common,hooks,pages,router,utils,constants}`；`backend/src/{routes,controllers,services,models,schemas,middlewares,utils,constants,config}`；`backend/scripts/seed_demo.py`；`backend/migrations/001_initial.sql`；测试在 `backend/tests/`。每核心实体贯穿模型/schema/service/route/API/store/page。组件table/badge/frame复用，hooks/session/feedback和utils/forms/html共用；request_id/errors双后端中间件。

## 测试

纯函数内核 `src/services/references.py`（闭包/映射/传递/成环/传播/快照）与持久化桥接 `src/services/reference_store.py` 分离，无网络与数据库依赖即可精确验证：

```sh
cd backend && python3 -m unittest discover -s tests -v
```

覆盖：菱形引用双路径落地、隐含空决定项、合并属性/成环/自引用拒绝、经未选属性传递保留、源更正沿三级链更新且目标自有规则不变、传播失败整链回滚、重试幂等不重复加规则、报告快照全旧或全新、源更正与报告受理并发不混用。本机未装第三方包时，`tests/_shims.py` 注入最小内存替身运行桥接/端到端测试；安装 `requirements.txt` 后垫片自动失效，走真实 SQLAlchemy/FastAPI。


枚举：角色admin/operator/viewer、任务queued/running/completed/failed；前后端定义位于 `backend/src/constants/enums.py` 和 `frontend/src/constants/enums.js`，security/reports、frame/badge消费；worker持久化状态。

环境变量见.env.example：COMPOSE_PROJECT_NAME、FRONTEND_PORT/BACKEND_PORT/DB_PORT、DB_PASSWORD、JWT_SECRET、SEED_DEMO。默认密码仅本地演示，JWT8小时。seed按自然键只补缺失关系及初始依赖，不覆盖用户修改。schema由SQLAlchemy metadata.create_all初始化，无在线迁移引擎。

本地开发：Python3.11安装backend/requirements.txt，配置DATABASE_URL/REDIS_URL后从backend运行 `uvicorn src.main:app --reload`、`python -m src.worker`；前端用Nginx同源代理/api。5服务均有健康检查和健康依赖，数据库/Redis/worker产物为独立命名卷。停止清空：`docker compose down -v --remove-orphans`。

UI为酒红档案册，双字体由Google Fonts加载，离线有回退且业务不依赖字体网络。

## License

MIT。真实测试脚本、截图与结果保存在源码外 `../../test-artifacts/gb-4104/`。
