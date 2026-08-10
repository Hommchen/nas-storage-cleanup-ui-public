## What changed

<!-- 简要说明本 PR 做了什么 -->

## Why

<!-- 为什么需要这个改动 -->

## Validation

- [ ] `npm test`
- [ ] `npm audit --json`
- [ ] `npm audit --omit=dev --json`
- [ ] `npm run smoke:readonly`

## Safety impact

- [ ] 不涉及真实删除 / 文件移动 / qBittorrent 任务变更
- [ ] 涉及执行链，并已补充失败关闭场景测试
- [ ] 不包含 Cookie、passkey、tracker、infohash、控制令牌或真实 NAS 私有数据

如果改动涉及删除、H&R、硬链接、MoviePilot 索引或事务恢复，请说明哪些异常状态必须阻止执行。
