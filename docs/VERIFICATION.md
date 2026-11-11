# Release verification — 3.0.1

发布源码、测试、fixtures和可复现构建配置，不含运行环境数据库或凭据。Python兼容fixtures为合成输入产生，基线为hermes-fact-infra v2.1.0。

## 本地门禁

1. npm run build — exit 0。
2. node --test dist/test/*.test.js — 125 pass、0 fail、0 skipped、0 cancelled、0 todo。
3. npm run plugin:build — exit 0。
4. openclaw plugins build --check --entry ./dist/index.js — exit 0。
5. openclaw plugins validate --entry ./dist/index.js — exit 0。
6. npm pack — 四个文件：README.md、package.json、openclaw.plugin.json、dist/index.js。

原113项测试、fixtures与helper未修改，新增12项父项验证。旧实现对同一新增测试7通过/5失败，新实现12通过/0失败。另一个独立十项脚本旧实现5通过/5失败、新实现10通过/0失败，覆盖project/light对task或removed goal父项的拒绝，以及apply重验遗留错误提案。

拒绝请求核对items/work/history/proposals/request_receipts不变。有效goal、缺失父项、project缺父项、light根任务、forward parent_ref及提案后移除父项已覆盖。测试使用临时SQLite库，不伪装为真实业务运行。

未改变schema、依赖版本或插件ID。未扩展同批remove/add最终状态语义。宿主静态校验与本地测试通过不等于所有部署环境都已验证。
