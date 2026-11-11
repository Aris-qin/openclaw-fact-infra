# openclaw-fact-infra v2

为 OpenClaw 提供 SQLite 项目事实层：项目库管理、单项目全流程与进度追踪，以及轻量工作记录。任务、决策、问题、实验、文件引用和审计历史统一保存在每个工作目录的 `fact.db`，中央索引只是可重建的导览，不是业务真相源。

## 版本与兼容

- **产品/发行名称：openclaw-fact-infra v2**。
- **插件包版本：3.0.1**，沿用已安装 Node/TypeScript 版本序列，避免从3.0.0倒退到2.0.0。Git tag 为 `v3.0.1`，GitHub Release 显示名为「openclaw-fact-infra v2」。
- **数据库 schema：v3**（`PRAGMA user_version=3`），本次补丁不修改schema、不自动迁移业务库。
- 插件ID仍为 `fact-infra`，npm包名仍为 `openclaw-plugin-fact-infra`；不会引入第二套工具名或第二份项目库。
- 支持 Node **24.16+（24.x）或26.1+**；OpenClaw **>=2026.5.17**。使用宿主的 `openclaw/plugin-sdk/tool-plugin` 和 Node 内建SQLite，无第三方运行时依赖。
- 本仓库默认私有。package设置 `private: true` 防止误发npm；发行包由GitHub Releases提供，不执行npm publish。

## 五个工具

| 工具 | action | 用途 |
| --- | --- | --- |
| `fact_create` | create | 创建project或light工作与持久回执 |
| `fact_read` | overview / current / detail / history | 导览、现状、实体/集合与审计 |
| `fact_plan` | propose / apply / reject | 目标、任务、依赖及结构变更，先提案后确认 |
| `fact_record` | append / update / start / block | 事实记录与任务启动/阻塞 |
| `fact_finish` | prepare / confirm / continue | 分别准备/确认任务、目标、工作完成或工作归档/恢复 |

每次写入需要 `request_id`；同一逻辑操作重试须复用相同参数和ID。提案、回执、业务变更与history在单一SQLite事务中提交。终态重放返回原响应，不制造新修订。结构性修改与收尾需要真实确认，不自动级联完成上级。

项目（project）目标平级、新任务必须属于goal；轻量工作（light）允许根任务。显式 `parent_id` 必须指向存在且未移除的goal：propose及apply均验证，非法父项返回 `VALIDATION_ERROR`，事务不产生部分修改。同批新建父目标可使用 `ref/parent_ref`。

## 获取与安装

从本仓库 [Releases](https://github.com/Aris-qin/openclaw-fact-infra/releases) 下载 `openclaw-plugin-fact-infra-3.0.1.tgz` 和 `SHA256SUMS`。私有仓库需先登录有权限的GitHub账号。

```sh
sha256sum -c SHA256SUMS
openclaw plugins install npm-pack:./openclaw-plugin-fact-infra-3.0.1.tgz
openclaw plugins inspect fact-infra --runtime --json
```

安装前备份现有插件、配置和一致性SQLite快照。安装/升级可能触发重载或要求重启，应在获授权的维护窗口执行。需要配置时在宿主配置一次性合并以下完整对象，保留其他插件设置：

```json
{
  "plugins": {
    "entries": {
      "fact-infra": {
        "enabled": true,
        "config": {
          "workRoot": "/absolute/path/to/workspace",
          "indexPath": "/absolute/path/to/fact-index-v3.db"
        }
      }
    }
  }
}
```

| 配置项 | 含义 |
| --- | --- |
| workRoot | 必填；工作根目录 |
| indexPath | 必填；中央SQLite导览路径 |
| staleDays | 可选；导览过期天数，默认14 |
| sqliteTimeoutMs | 可选；锁等待毫秒，默认30000 |

未知配置项被拒绝。新建默认路径为 `(root_path ?? workRoot)/{projects|work}/<slug>/fact.db`。不要把含既有库的目录当作新工作覆盖创建。

## 从源码构建与测试

```sh
npm ci --include=dev --no-audit --no-fund
npm run build
npm test
npm run plugin:build
openclaw plugins build --check --entry ./dist/index.js
npm run validate
npm pack
```

`build` 编译源码和测试；`plugin:build` 输出单文件ESM `dist/index.js`。更改注册元数据后先执行 `openclaw plugins build --entry ./dist/index.js` 再做 `--check`。发行tarball只包含package.json、README、manifest和bundle，不包含数据库、测试、node_modules、凭据、个人状态或原始运行日志。

3.0.1验收：**125项测试全部通过，0失败/0跳过**（113项原回归+12项父项校验回归）。独立同脚本修前5/10、修后10/10。测试使用临时库，包含非法父项拒绝、失败事务无变更、有效goal、light根任务、forward parent_ref、过期提案和apply重校验。测试不等于所有边界或生产环境已经验证。

## 遗留库与边界

- 旧schema读写均返回 `MIGRATION_REQUIRED`，未来schema返回 `UNSUPPORTED_SCHEMA`；插件不含离线迁移CLI，不自动升级旧库，也不把旧库当空目录重建。
- 已迁移但未审核的库返回 `ADOPTION_REQUIRED`；审核迁移证据并获授权后，通过独立adopt提案接管，不把确认接管视为遗留问题已解决。
- 中央导览刷新为best-effort，失败不推翻已提交业务事实；freshness为fresh/stale/unverified/unavailable。没有path_ok/index_pending字段。读不自动登记，不为刷新导览制造业务note。
- detail集合支持分页；current中的部分裸数组按limit截断且无续页cursor。问题总数看 `counts.open_issues`，完整枚举转detail对应集合分页。
- `fact_record start/block` 的reason参数当前不保留原文；必须另记issue/note保存原因。record links只接受真实item_id；plan depend可用同批item_ref/requires_ref。
- 完成模式只在prepare指定，默认complete；confirm不能改mode。restore仅接受archived工作；completed可经获授权的archive→restore恢复。归档不是删除库。
- 期刊画像不属于五工具；旧journals不自动接管，不能写旧表兜底。
- SQLite调用同步；跨运行时含浮点载荷的旧回执不保证重放一致，-0归一为0。不修复历史层级或任意同批remove/add的最终结构语义；source_session当前为null。
- 本补丁强化显式parent_id校验，不扩展schema、公开工具集、依赖版本或旧数据迁移。

Python来源与兼容基线来自 [hermes-fact-infra v2.1.0](https://github.com/Aris-qin/hermes-fact-infra/tree/v2.1.0)；本仓库是Node/OpenClaw适配实现。发布变更见 [CHANGELOG.md](CHANGELOG.md)。
