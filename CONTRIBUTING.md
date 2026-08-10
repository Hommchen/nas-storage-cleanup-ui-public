# Contributing to NAS 清理台

感谢你愿意参与 NAS 清理台。

这个项目的首要原则是：**任何清理能力都必须默认安全、可审计，并在状态不明确时失败关闭（fail closed）**。涉及删除、退出做种、H&R、硬链接或媒体索引的改动，必须优先证明“不会误删”，而不是只证明“正常路径可以工作”。

## 开始之前

1. Fork 或创建功能分支。
2. 使用 Node.js 22.13+ 和 Python 3.12。
3. 安装依赖：

```bash
npm ci
```

4. 默认使用只读模式开发：

```bash
npm run local
```

除非你明确在隔离环境中测试执行链，否则不要启用真实执行模式。

## 提交前检查

请至少运行：

```bash
npm test
npm audit --json
npm audit --omit=dev --json
npm run smoke:readonly
```

如果改动涉及执行链、事务恢复、H&R、路径白名单、硬链接或 MoviePilot/qBittorrent 联动，请补充针对失败场景的测试。

## Pull Request 说明

PR 请说明：

- 改了什么；
- 为什么要改；
- 对用户的影响；
- 如何验证；
- 是否涉及删除、文件移动、qBittorrent、MoviePilot、H&R 或私有站数据。

涉及安全门禁的 PR，请明确列出“什么情况下必须拒绝执行”。

## 数据与隐私

不要提交真实的：

- Cookie、passkey、tracker、infohash；
- NAS 本机绝对路径或真实媒体库存；
- 控制令牌、站点身份、账号信息；
- 真实私有站 H&R 列表或恢复材料。

测试数据必须使用虚构 fixture，并保持公开快照脱敏约束。

## Issue

Bug 报告尽量包含：复现步骤、预期行为、实际行为、环境版本和已脱敏日志。涉及潜在误删或权限绕过的问题，请不要公开贴出敏感材料，优先参照 `SECURITY.md`。
