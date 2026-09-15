```markdown
# 更新日志

本项目所有重要变更均记录于此文件。

## [1.2.0]

### 变更
- **改为纯指令模式**：现在仅 `/163music <关键词>` 可以触发点歌，不再响应自然语言（如"来一首""播放""点歌"等）
- **移除命令别名**：`/music`、`/听歌` 不再可用，只保留 `/163music`
- **禁用 AI 介入**：`/163music`、`/login`、`/logins` 执行时不再触发 AI 回复，点歌全程由插件独立完成

### 修复
- 修复 `_send_song_with_cover` 中多行三引号字符串在部分编辑器下被破坏导致的 `invalid syntax` 报错，改用圆括号拼接的单行 f-string

### 说明
- 选歌交互保持不变：搜索出列表后 60 秒内回复数字 `1~5` 仍可点歌

---

## [1.0.0] - 2026-09-09

### 新增
- 首次发布 AstrBot 网易云音乐点歌插件
- 支持 `/163music`（含 `music`、`听歌` 别名）及自然语言点歌
- 交互式选歌：回复数字即可选择搜索结果中的歌曲
- 多音质支持：`lossless` / `exhigh` / `higher` / `standard`，自动降级
- 歌曲信息卡片：附封面图片及歌名、歌手、专辑、时长、音质
- 语音消息支持：检测到 ffmpeg 时自动转为 24kHz 单声道 WAV，适配 QQ 语音（SILK）编码
- 扫码登录：通过网易云 APP 扫码登录，获取高音质源（需会员），Cookie 本地持久化
- 登录状态查询：`/logins` 查看当前登录账号
- 歌手搜索：关键词命中歌手时返回其热门歌曲
- 自动清理：定时清理过期会话与临时文件
- WebUI 配置：支持在 AstrBot 后台配置各项参数

---

## 相关链接

- 项目地址：[xiaoxin-qaq/astrbot-plugin-163Music](https://github.com/xiaoxin-qaq/astrbot-plugin-163Music)
- API 服务：[NeteaseCloudMusicApiEnhanced/api-enhanced](https://github.com/neteasecloudmusicapienhanced/api-enhanced)
```

发布时的简短说明（可直接用作 Release 描述）：

```markdown
## 本次更新

**⚠️ 破坏性变更**
- 改为纯指令模式：只响应 `/163music <关键词>`，不再支持自然语言和 `/music`、`/听歌` 别名
- `/163music`、`/login`、`/logins` 执行时不再触发 AI 回复

**修复**
- 修复 `_send_song_with_cover` 在部分编辑器保存后出现 `invalid syntax` 的问题

**保持不变**
- 搜出列表后 60 秒内回复数字 `1~5` 仍可点歌
``