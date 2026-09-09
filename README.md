

```markdown
# AstrBot 网易云音乐点歌插件

[![version](https://img.shields.io/badge/version-2.2.2-blue.svg)](https://github.com/xiaoxin/astrbot-plugin-neteasemusic)
[![license](https://img.shields.io/github/license/xiaoxin/astrbot-plugin-neteasemusic.svg)](LICENSE)

> **基于原项目二次优化维护**  
> 原项目：[NachoCrazy/netease-music-astrbot-plugin](https://github.com/NachoCrazy/netease-music-astrbot-plugin)  
> 本插件由 [xiaoxin](https://github.com/xiaoxin-qaq) 维护，适配 [NeteaseCloudMusicApiEnhanced/api-enhanced](https://github.com/neteasecloudmusicapienhanced/api-enhanced) 作为后端 API 服务。

---

## ✨ 功能特性

- 🔍 **智能搜索**：支持命令（`/点歌`）和自然语言（如“来一首《晴天》”）快速搜索歌曲。
- 🎶 **交互式选歌**：搜索结果以列表展示，回复数字即可选择，操作直观。
- 🔊 **多音质支持**：可配置 `lossless`（无损）、`exhigh`（极高）、`higher`（高）、`standard`（标准）四种音质，自动降级。
- 🖼️ **豪华信息卡片**：发送歌曲时附带封面图片及歌名、歌手、专辑、时长、音质等详细信息。
- 🗣️ **语音消息支持**：若系统安装 **ffmpeg**，自动将音频转换为 24kHz 单声道 WAV，适配 QQ 语音（SILK）编码，并发送为 `Record` 消息（第三方机器人优先）；QQ 官方机器人则先发送文件卡片，失败再回退。
- 🔐 **扫码登录**：支持通过网易云 APP 扫码登录，获取高音质源（需会员），并持久化保存 Cookie。
- 👤 **登录状态查询**：使用 `/logins` 查看当前登录账号信息。
- 🗑️ **自动清理**：定时清理过期会话和临时文件，节省资源。
- ⚙️ **WebUI 配置**：可在 AstrBot 网页后台轻松配置各项参数。

---

## 📦 安装与配置

### 1. 依赖服务

本插件需要依赖外部的网易云音乐 API 服务，推荐使用 **[NeteaseCloudMusicApiEnhanced/api-enhanced](https://github.com/neteasecloudmusicapienhanced/api-enhanced)** —— 一个功能更丰富、持续维护的网易云音乐 Node.js API 服务。

**主要特性**：
- 支持登录/注册、用户信息、歌单、歌曲/专辑/歌手/MV/歌词/评论、搜索、推荐、私人 FM、签到、云盘等丰富接口
- 支持歌曲解锁（解灰）功能
- 支持无损音质（FLAC）和最高码率选择
- 支持 Docker 一键部署
- 支持环境变量灵活配置（跨域、代理、随机中国 IP 等）

**部署方式**（任选其一）：

- **Docker 部署**（推荐）：
  ```bash
  docker pull moefurina/ncm-api:latest
  docker run -d -p 3000:3000 --name ncm-api moefurina/ncm-api:latest
```

· Node.js 部署：
  ```bash
  git clone https://github.com/neteasecloudmusicapienhanced/api-enhanced.git
  cd api-enhanced
  pnpm i
  node app.js
  ```

部署成功后，默认服务地址为 http://127.0.0.1:3000。

2. 安装插件

在 AstrBot 的插件商店中搜索 astrbot_plugin_netease_music 并安装；或者将本项目克隆到 data/plugins 目录下。

3. 配置参数

安装并重启 AstrBot 后，可通过两种方式配置：

· WebUI 界面：在“插件配置”中找到 astrbot_plugin_netease_music，填写各项参数。
· 配置文件（config.yaml 或类似）：
  ```yaml
  plugins:
    - name: "netease_music"
      enabled: true
      config:
        api_url: "http://127.0.0.1:3000"     # 你的 api-enhanced 服务地址
        quality: "lossless"                  # 默认音质
        search_limit: 5                      # 搜索结果数量
        cookie: ""                           # 自动从扫码登录获取，无需手动填
  ```

音质选项：

· lossless：无损（FLAC，需会员）
· exhigh：极高（320kbps MP3）
· higher：高（192kbps）
· standard：标准（128kbps）

若指定音质不可用，插件会自动降级尝试。

4. 可选依赖（语音消息）

若需要发送语音消息（Record），请确保系统中安装了 ffmpeg：

· 推荐安装 imageio-ffmpeg（自动携带 ffmpeg 二进制）：
  ```bash
  pip install imageio-ffmpeg
  ```
· 或手动安装系统 ffmpeg，并确保其在 PATH 中。

---

🚀 使用方法

命令列表

命令 说明 权限
/点歌 <关键词> 或 /music / /听歌 / /网易云 搜索歌曲并展示列表 所有人
自然语言：来一首 <歌名>、播放 <歌名>、听听 <歌名> 等 自动触发搜索 所有人
回复数字 1~N 在搜索结果中选择歌曲播放 所有人
/login 扫码登录网易云账号（获取高音质） 管理员
/logins 查看当前登录状态和账号信息 管理员

完整交互示例

1. 搜索歌曲
      发送：/点歌 起风了 或 来一首 起风了
2. 查看结果
      机器人返回前 N 首歌曲列表（由 search_limit 决定），每首包含编号、歌名、歌手、专辑、时长。
3. 选择歌曲
      在 60 秒内回复对应数字（如 2），机器人开始下载音频。
4. 接收内容
   · 歌曲信息卡片（含封面图片）
   · 原始音频文件（MP3 / M4A 等，格式来自源链接）
   · 若 ffmpeg 可用，额外发送一段 WAV 语音消息（Record），便于直接播放。
5. 自动清理
      临时文件会在 1 小时后自动删除，搜索结果会话 60 秒后失效。

---

🔑 扫码登录（获取高音质）

· 只有管理员可以执行 /login。
· 机器人会生成二维码并提示用网易云 APP 扫描。
· 扫码后需在手机端点击确认。
· 登录成功后，Cookie 会保存在插件目录下的 netease_login.json 中，后续重启无需重新登录。
· 使用 /logins 可随时查看账号昵称和 VIP 状态。

💡 提示：登录后，部分歌曲可获取 lossless 无损音质（需账号为黑胶 VIP）。
若未登录或账号非会员，高音质请求可能被降级，插件会自动处理。

---

⚙️ 高级特性与说明

语音消息转换参数

转换音频时使用以下 ffmpeg 参数（可自行修改代码中的 -af 滤镜）：

```bash
-ac 1 -ar 24000 -acodec pcm_s16le -af loudnorm=I=-16:TP=-1.5:LRA=11
```

· 单声道、24kHz 采样率，匹配 QQ 语音编码最佳实践。
· 使用 loudnorm 滤镜进行响度归一化，解决声音过小或闷沉问题。

源音质降级策略

若请求的音质无法获取（例如无会员），插件会按以下顺序自动降级尝试：
lossless → exhigh → higher → standard。
日志中会记录实际获取的比特率，方便排查。

文件发送兼容性

· QQ 官方机器人：先按官方富媒体流程发送 File 文件卡片（file_type=4 → msg_type=7），若失败则尝试 Record 或链接。
· 第三方机器人：优先发送 Record 语音消息，若不可用则发送文件。

---

🛠️ 故障排查

问题 可能原因 解决方案
搜索无结果 / 连接失败 API 服务未启动或地址错误 检查 api_url 配置，确保 api-enhanced 服务可访问
语音消息无法发送 ffmpeg 未安装或路径未找到 安装 imageio-ffmpeg 或系统 ffmpeg，并检查权限
音频质量差（低码率） Cookie 无效或非会员 使用 /login 重新登录有效账号
二维码不显示 / 扫码失败 API 服务限制或网络问题 检查 API 日志，尝试重启服务；确保 API 支持二维码接口
临时文件堆积 清理任务未运行 确认插件正确初始化，或手动清理 tempfile.gettempdir()/netease_music_plugin

---

📁 文件说明

· main.py：插件主程序
· netease_login.json：自动生成的 Cookie 存储文件（与插件同目录）

---

💖 致谢

· 感谢 AstrBot 提供了如此强大的机器人框架。
· 感谢原作者 NachoCrazy 的原项目。
· 感谢 NeteaseCloudMusicApiEnhanced 团队维护的 api-enhanced 项目，为本插件提供稳定、功能丰富的 API 支持。
· 感谢 Binaryify 的原始 NeteaseCloudMusicApi 项目。

---

📄 许可证

本插件遵循 MIT License，您可以自由使用、修改和分发。

---

如有问题，欢迎提交 Issue 或联系维护者。祝您听歌愉快！ 🎧

---

Based on original work by NachoCrazy · Maintained by xiaoxin

```

---