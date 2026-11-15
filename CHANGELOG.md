# Changelog

## Unreleased

- Add optional offline Python migration/verification and central-handoff helpers with synthetic regression tests; keep them separate from the Node runtime bundle.
- Fix table-scoped legacy IDs in parentless-task verification: nested goal IDs no longer hide unrelated root tasks with the same numeric ID.
- Document non-destructive catalog maintenance; no automatic pruning of missing paths.
- No change to plugin version, schema, runtime bundle or public tool contracts.

## 3.0.1 — openclaw-fact-infra v2

- 产品发行名为openclaw-fact-infra v2；插件包沿用3.x版本序列，schema保持v3。
- 五工具：fact_create、fact_read、fact_plan、fact_record、fact_finish；基于持久request_id回执、单事务审计、提案/确认及独立收尾。
- 修复add(parent_id)未校验父项kind/removed_at的问题：propose和apply都要求父项存在、为goal且未移除；错误请求原子拒绝。
- 补充12项回归；总计125项通过，0失败/0跳过。原113项断言未改。
- 保留light根任务、同批forward parent_ref及有效goal父项；提案后变更仍受快照检测保护。
- 前期兼容修复：内外部JSON大小配额分离，终态原回执重放，finish history协议，错误details默认为对象，以及旧库写前版本闸门。
- 不增加数据库迁移、自动修复历史数据、期刊接管或跨项目依赖；不改变五工具契约。

产品名称、包版本、schema版本互不等同。该仓库未执行npm公开发布。
