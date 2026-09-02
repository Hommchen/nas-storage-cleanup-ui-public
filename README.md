# NAS 清理台

[![CI](https://github.com/Hommchen/nas-storage-cleanup-ui-public/actions/workflows/ci.yml/badge.svg)](https://github.com/Hommchen/nas-storage-cleanup-ui-public/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> 面向 Jellyfin + qBittorrent + MoviePilot 的本地 NAS 媒体清理控制台。

NAS 清理台把媒体库、做种任务、硬链接关系和可选的 H&R 保护状态聚合成一份可审计的资源视图，让你先看清楚“删什么、会影响什么、为什么能删”，再决定是否执行。

项目默认 **只读 + fail closed**：任何关键状态不明确时拒绝执行，而不是猜测后继续。

**项目状态：** 活跃开发中，当前主要面向自托管单机 NAS。真实删除能力属于高风险功能，首次部署应先在只读模式完成拓扑核对。

## 它能做什么

- 聚合 Jellyfin 媒体、qBittorrent 任务、真实硬链接和 MoviePilot 索引；
- 电影一部一行、电视剧一剧一行，优先展示中英文身份；
- 提供三档操作：停止做种、退出做种、完整删除；
- 执行前生成短时有效的真实预演，并再次刷新资源状态；成功执行后先标记库存过期，再由页面发起只读刷新；
- 自动拦截 H&R、未完成下载、未知硬链接、越界路径、缺失文件、符号链接和占用文件；
- 对删除流程保留事务状态、qB 恢复材料和同盘隔离区，失败时优先回滚；
- 可作为独立页面运行，也可通过 MoviePilot 原生页面进入。

## 适合谁

这个项目更适合已经在 NAS 上使用 Jellyfin、qBittorrent、MoviePilot，并且需要长期维护媒体库存和做种关系的用户。

它不是“自动帮你扫一遍然后删垃圾”的脚本。核心目标是：**把复杂关联显示清楚，并把误删风险压到最低。**

## 快速开始

要求：

- Node.js 22.13+
- Python 3.12

安装依赖：

```bash
npm ci
```

默认以只读模式启动：

```bash
npm run local
```

然后打开：

```text
http://localhost:3000/
```

开发、生产前端与控制服务默认只监听 `127.0.0.1`。

> 不建议第一次运行就开启执行能力。先用示例配置和只读模式确认资源识别、路径边界和硬链接关系都符合你的 NAS 拓扑。

## 三档操作

| 操作 | qB 任务 | 媒体文件 | 适用场景 |
| --- | --- | --- | --- |
| 停止做种 | 保留，仅停止 | 保留 | 暂时释放上传/磁盘活动 |
| 退出做种 | 备份后移除 | 保留 | 不再做种，但继续保留媒体 |
| 完整删除 | 校验后退出 | 校验后删除 | 确认媒体和做种都不再需要 |

完整删除不是直接 `rm`。系统会复核 qB 文件清单、硬链接、允许清理根目录、文件状态和 MoviePilot 索引，并先把精确文件移动到同盘隔离区，再完成后续事务。

成功执行不会同步等待第二次全量扫描；页面会先收到执行结果并锁定后续计划，刷新完成后才恢复操作。

## 安全模型

每次真实执行至少需要：

1. 生成五分钟有效的真实安全预演；
2. 单独确认私有站风险；
3. 在页面上再次确认；
4. 执行前重新刷新 NAS 状态，并确认任务和文件指纹没有变化。

以下状态默认阻止清理：H&R 风险、未完成下载、未知硬链接、越界路径、缺失文件、符号链接、文件正在使用、媒体身份冲突、未完成事务。

公开页面和浏览器接口不会暴露本机路径、tracker、passkey、infohash、控制令牌或私有 H&R 标题。真实执行数据只保存在本地私有库存中。

详细组件边界见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)，安全报告与边界见 [`SECURITY.md`](SECURITY.md)。

## H&R 支持

Hit and Run 默认关闭。开启后，可从 MoviePilot 已配置的登录站点读取站点身份，并按受支持的入口解析 H&R 状态。

H&R 保护只在站点成功验证后生效；查询失败时采用失败关闭策略，只保护受影响任务，不会因为接口异常而误放行删除。

恢复缺失任务时，项目优先使用官方 `.torrent` 与 qB 完整逐文件 payload 做精确匹配，不使用标题相似度代替文件证据。

## 跨 NAS 配置

MoviePilot 插件的“设置”页用于填写当前 NAS 拓扑，配置保存在主机侧 `shared/config.json`；开发环境默认为 `.runtime/config.json`。

可配置：

- qBittorrent 地址；
- Jellyfin / MoviePilot 数据库位置；
- qB 种子与事务备份目录；
- 资源快照有效期（默认 3600 秒，过期后清理动作自动锁定）；
- 允许扫描 / 清理的根目录；
- 每个数据卷对应的同盘隔离目录；
- H&R 站点配置。

仓库提供 `config/config.example.json` 作为非敏感模板。配置保存时会执行结构校验和只读路径探测；路径越界、符号链接或跨文件系统隔离目录会被拒绝。

## 正式部署

当前部署脚本和 systemd 单元最初按 PiNAS 拓扑编写。迁移到其他 NAS 前，请先调整用户、监听地址、数据目录、媒体根、下载根和允许删除根。

示例：

```bash
/opt/homebrew/bin/python3.12 scripts/deploy-to-pi.py \
  --pi-host nas-user@nas-host \
  --pi-base /srv/storage-cleanup-ui
```

部署器会在独立版本目录安装、测试、构建并采集真实快照，通过后再原子切换 `current`。旧版本不会被直接覆盖。

服务支持两条入口：

- 独立页面：NAS 网关 `:3000`；
- MoviePilot 原生页面：登录 MoviePilot 后从侧栏进入“存储清理”。

网关只允许同源浏览器元数据访问会话和控制接口；健康检查与脱敏快照保持只读公开，MoviePilot 使用独立的 Docker 桥接令牌。主动伪造请求头的同网段客户端仍属于可信局域网边界，若需要身份级防护，应再接入 Tailscale 或反向代理认证。

## 开发

常用命令：

```bash
npm run local
npm run local:production
npm run lint
npm run typecheck
npm test
npm run smoke:readonly
```

只有在你明确知道当前环境允许真实执行时，才使用：

```bash
npm run local:enabled
npm run local:production:enabled
```

提交前建议运行：

```bash
npm test
npm audit --json
npm audit --omit=dev --json
npm run smoke:readonly
```

`smoke:readonly` 只接受 loopback 控制服务，并要求执行模式关闭；它不会生成有效清理计划，也不会触发 NAS 或 qB 写操作。

GitHub Actions 会在 `main` 的 push 和 Pull Request 上运行完整项目验证，并检查生产依赖的高危漏洞。

## 事务与恢复

退出做种和完整删除会记录事务阶段、原 qB 运行态、恢复材料和隔离映射。

存在非终态事务时，新的清理操作会被锁定。恢复前先只读检查：

```bash
/opt/homebrew/bin/python3.12 scripts/recover-transaction.py plan_xxx
```

恢复器会再次核对允许路径、隔离映射、inode、大小、硬链接数和 qB 状态，不提供跳过确认的快捷入口。

## 媒体身份

名称优先使用 Jellyfin / TMDB 身份，再结合与同一硬链接关联的 qB 发布名互证。标题与年份无法闭合时，资源会进入“名称待核”并锁定清理。

发布名解析会先剥离画质、编码、音轨和发布组等噪声，提取标题、年份和季号，形成稳定的解析查询；同一标题的 1080p/2160p、站点和发布组变体可以复用已确认的身份。带有明确中英文标题的发布名可以生成仅基于标题族的人工身份，但不会凭此猜测 TMDB ID；英文标题或年份不明确时仍需 MoviePilot/TMDB 解析或人工复核。

`db/media-name-overrides.json` 只作为审计过的静态兜底和初次确认来源。映射只补充展示身份，不会覆盖 H&R、未完成下载、硬链接或路径安全门禁；身份冲突、年份冲突和低置信度结果仍进入“名称待核”并锁定清理。

## 贡献

欢迎提交 Bug、适配和安全改进。涉及删除链路的改动必须同时覆盖失败场景测试。

贡献规范见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。

## 许可证

本项目采用 [MIT License](LICENSE)。

## 隐私

仓库不应包含真实媒体清单、运行缓存、Cookie、tracker、passkey、infohash、控制令牌或真实私有站 H&R 数据。测试 fixture 必须使用虚构数据。
