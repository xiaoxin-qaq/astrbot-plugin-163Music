<div align="center">

# 🎧 网易云音乐点歌插件

**把想听的歌，交给一只会叼歌的猫娘。**

<p>基于 <a href="https://github.com/AstrBotDevs/AstrBot">AstrBot</a> 的网易云音乐点歌与歌单下载插件</p>

<p>
  <img alt="AstrBot" src="https://img.shields.io/badge/AstrBot-Plugin-ff69b4?style=flat-square">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white">
  <img alt="API" src="https://img.shields.io/badge/API-required-00A1D6?style=flat-square">
  <img alt="License" src="https://img.shields.io/badge/License-MIT-green?style=flat-square">
</p>

<p>
  点歌 · 歌单下载 · 多音质 · 歌词内嵌 · 语音消息 · 自动清理
</p>

</div>

---

<p align="center">
  ⚠️ <b>本插件不内置网易云 API 服务，请先准备可访问的接口，并在配置中填写 <code>api_url</code>。</b>
</p>

---

<h2 align="center">✨ 亮点</h2>

<p align="center">一只猫娘，承包你的点歌、下载、歌词和语音。</p>

| 🎵 **点歌** | 🎚️ **多音质** | 🖼️ **信息卡** |
|:---:|:---:|:---:|
| `/点歌` / `/163music`<br>搜歌曲或歌手，列表选歌、翻页 | 超清母带、杜比、无损、Hi-Res<br>按账号 VIP 自动降级 | 封面 + 歌名 + 歌手<br>专辑 + 时长 + 音质 |
| 📁 **文件 + 语音** | 📝 **歌词内嵌** | 🧹 **自动收尾** |
| 音频文件 + WAV 语音<br>失败时补播放链接 | `/163下载` 写入原文 + 翻译歌词<br>保留封面与曲目标签 | 点歌 1:50、下载 100 秒<br>无操作自动清理中间消息 |
| 🛡️ **LLM 隔离** | 🔐 **扫码登录** | 📚 **歌单下载** |
| 插件交互全程阻断默认 LLM<br>不会误触发对话 | 管理员 `/login` 扫码<br>Cookie 自动保存 | 个人歌单、歌单 ID、分享链接<br>关键词搜索都能下载 |

---

<h2 align="center">🚀 快速开始</h2>

<p align="center">四步就能开始听歌。</p>

<h3 align="center">1. 准备 API 服务</h3>

<p align="center">本插件只负责调用网易云 API。请准备一个可访问的服务地址，例如：</p>

```yaml
api_url: "http://127.0.0.1:3000"
```

<h3 align="center">2. 安装插件</h3>

<p align="center">将本目录放入 AstrBot 的 <code>data/plugins/</code>，或通过插件管理安装，然后重启 AstrBot。</p>

```text
插件目录：astrbot_plugin_163music
显示名称：网易云音乐点歌插件
```

<h3 align="center">3. 配置插件</h3>

<p align="center">在 WebUI 的插件配置中填写参数，或使用：</p>

```yaml
api_url: "http://127.0.0.1:3000"
quality: "jymaster"
search_limit: 30
page_size: 10
max_fetch_songs: 1000
cookie: ""
```

<p align="center"><code>cookie</code> 推荐留空，扫码登录后会自动写入。</p>

<h3 align="center">4. 语音依赖（可选）</h3>

<p align="center"><code>requirements.txt</code> 已包含 <code>imageio-ffmpeg</code>，AstrBot 会自动安装。也可以手动安装：</p>

```bash
pip install imageio-ffmpeg
```

<p align="center">或者安装系统 ffmpeg，并确保它在 <code>PATH</code> 中。</p>

---

<h2 align="center">🎮 命令</h2>

<p align="center">常用指令一览。</p>

| 命令 | 说明 | 权限 |
|:---:|:---:|:---:|
| `/点歌 <关键词>` | 搜索歌曲并展示列表 | 所有人 |
| `/163music <关键词>` | 与 `/点歌` 相同的入口 | 所有人 |
| `/163下载` | 查看并下载自己的歌单（需登录） | 所有人 |
| `/163下载 <歌单ID>` | 下载指定歌单 | 所有人 |
| `/163下载 <分享链接>` | 粘贴网易云歌单分享链接 | 所有人 |
| `/163下载 <关键词>` | 搜索歌手或歌曲后下载 | 所有人 |
| `p2` / `/p2` | 翻到第 2 页 | 所有人 |
| `1` / `1 2 3` | 选择歌曲；点歌一次一首，下载可多首 | 所有人 |
| `*` | 取消当前会话并清理消息 | 所有人 |
| `/163help` | 查看插件内置帮助 | 所有人 |
| `/login` | 扫码登录网易云账号 | 管理员 |
| `/logins` | 查看登录状态与 VIP 信息 | 管理员 |

<p align="center">💡 点歌回复多个数字时只取第一个；歌单下载需要先 <code>/login</code>。</p>

---

<h2 align="center">🎼 使用示例</h2>

<p align="center">看看一次完整点歌是什么样。</p>

```text
你  /点歌 Lemon
猫  要怎么去找呢？回复数字告诉喵：
    1. 搜歌曲
    2. 搜歌手
你  1
猫  「Lemon」的歌，本喵都叼回来啦
    1. Lemon - 米津玄師 [4:16]
    2. ……
你  1
猫  [封面信息卡]
    [Lemon - 米津玄師.mp3]
    [语音消息]
猫  全部叼完啦！这次的战利品喵……
```

<p align="center">
  歌曲信息卡 → 原始音频文件 → WAV 语音消息<br>
  文件发送失败时，补发语音并附上网易云播放链接<br>
  发送完成后统一撤回列表与中间提示
</p>

<p align="center">
  歌单下载支持一次选择多首，文件内会写入封面、歌名、歌手、专辑与歌词。<br>
  下载固定使用标准音质；点歌会按配置的音质链尝试更高音质。
</p>

---

<h2 align="center">🎚️ 音质与 VIP</h2>

<p align="center">音质越高，越需要会员。</p>

| 名称 | Key | 备注 |
|:---:|:---:|:---:|
| 超清母带 | `jymaster` | 默认音质，SVIP 可用 |
| 杜比全景声 | `dolby` | SVIP 可用 |
| 臻音全景声 | `vivid` | 高音质 |
| 沉浸环绕声 | `sky` | 高音质 |
| 高清环绕声 | `jyeffect` | 高音质 |
| Hi-Res | `hires` | 高解析度 |
| 无损 | `lossless` | 黑胶 VIP 推荐 |
| 极高 | `exhigh` | 约 320 kbps |
| 较高 | `higher` | 约 192 kbps |
| 标准 | `standard` | 约 128 kbps |

<p align="center">
  点歌会优先尝试配置的音质，再按从高到低自动降级。<br>
  SVIP 可用全部音质；普通 VIP 会剔除母带与杜比；无 VIP 时回退到免费可用音质。
</p>

---

<h2 align="center">⚙️ 配置</h2>

<p align="center">改完重新加载插件即可生效。</p>

| 配置项 | 类型 | 默认值 | 说明 |
|:---:|:---:|:---:|:---:|
| `api_url` | string | `http://127.0.0.1:3000` | 网易云 API 服务地址 |
| `quality` | string | `jymaster` | 点歌优先音质 |
| `search_limit` | int | `30` | 搜索抓取数量，实际不低于 `page_size × 3` |
| `page_size` | int | `10` | 列表每页显示数量 |
| `max_fetch_songs` | int | `1000` | 歌单最大抓取数，上限 1000 |
| `cookie` | text | 空 | 网易云 Cookie，推荐 `/login` 自动获取 |

---

<h2 align="center">🔐 扫码登录</h2>

<p align="center">管理员发送 <code>/login</code>，用网易云 APP 扫码即可。</p>

<p align="center">
  1. 管理员发送 <code>/login</code><br>
  2. 使用网易云 APP 扫描二维码<br>
  3. 在手机上点击确认登录<br>
  4. Cookie 自动保存，重启后无需重复登录
</p>

<p align="center">登录成功后，<code>/logins</code> 可查看昵称、VIP 类型和登录状态。</p>

<p align="center">🔐 <b>Cookie 保存在插件目录的 <code>netease_login.json</code>，包含账号凭据，请勿提交到公开仓库。</b></p>

---

<h2 align="center">📦 更多细节</h2>

<p align="center">展开查看歌词、清理、LLM 隔离和语音细节。</p>

<details>
<summary align="center"><b>📝 歌词与标签</b></summary>

<p align="center">歌词内嵌仅用于 <code>/163下载</code>：</p>

<p align="center">
  按时间轴合并原文与翻译<br>
  MP3 使用标准 ID3v2 <code>USLT</code> 帧，避免部分 ffmpeg 版本写入 <code>TXXX(USLT)</code><br>
  非 MP3 由 ffmpeg 写入对应容器的歌词标签<br>
  保留封面、歌名、歌手、专辑等标签<br>
  API 没有歌词时，文件仍会正常下载
</p>

<p align="center"><code>/点歌</code> 不会改写音频歌词。</p>

</details>

<details>
<summary align="center"><b>🧹 会话清理与撤回</b></summary>

<p align="center">中间消息会自动清理，歌曲文件不会被误删。</p>

| 会话类型 | 无操作超时 | 收尾行为 |
|:---:|:---:|:---:|
| 点歌 | 1 分 50 秒 | 撤回列表与中间提示，发送超时提示 |
| 歌单下载 | 100 秒 | 撤回中间提示，保留歌曲文件与当前列表，发送提示并结束 |
| 手动取消 `*` | 立即 | 撤回当前会话消息并结束 |

<p align="center">
  临时文件保存在系统临时目录的 <code>netease_music_plugin</code> 文件夹，超过 1 小时自动删除。<br>
  点歌超时设置在 QQ 2 分钟撤回窗口内，尽量避免列表撤回失败。
</p>

</details>

<details>
<summary align="center"><b>🛡️ LLM 隔离</b></summary>

<p align="center">插件会在所有指令与会话交互中阻断 AstrBot 默认 LLM 请求：</p>

<p align="center">
  <code>/点歌</code>、<code>/163下载</code>、<code>/163help</code>、登录指令<br>
  翻页、数字选择、取消会话<br>
  会话进行中的其他消息
</p>

<p align="center">使用本插件时不会意外触发 LLM 对话。</p>

</details>

<details>
<summary align="center"><b>🗣️ 语音与文件发送</b></summary>

<p align="center">点歌在 ffmpeg 可用时会发送 WAV 语音，转换参数：</p>

```bash
-ac 1 -ar 24000 -acodec pcm_s16le -af loudnorm=I=-16:TP=-1.5:LRA=11
```

<p align="center">
  单声道、24 kHz，适配 QQ 语音<br>
  <code>loudnorm</code> 响度归一化，减少声音过小或发闷<br>
  文件发送失败时附上播放链接
</p>

```text
https://music.163.com/song?id=<歌曲ID>
```

<p align="center">歌单下载只发送音频文件，不额外发送语音。</p>

</details>

---

<h2 align="center">🛠️ 常见问题</h2>

<p align="center">遇到问题先看这里。</p>

| 现象 | 可能原因 | 处理方式 |
|:---:|:---:|:---:|
| 搜索无结果 / 连接失败 | API 未启动或地址错误 | 检查 `api_url` 和服务状态 |
| 点歌没有语音 | ffmpeg 未安装 | 安装 `imageio-ffmpeg` 或系统 ffmpeg |
| 音质偏低 | Cookie 失效或非会员 | 使用 `/login` 重新登录 |
| 二维码不显示 | API 不支持或网络异常 | 检查 API 日志，重启服务 |
| 下载没有歌词 | 使用了 `/点歌`，或 API 无歌词 | 歌词内嵌仅用于 `/163下载` |
| 会话突然结束 | 触发超时清理 | 重新发送命令即可 |
| 临时文件堆积 | 清理任务异常 | 检查系统临时目录 |
| 机器人不回复 LLM | 插件主动阻断 | 预期行为 |

---

<h2 align="center">📁 项目结构</h2>

<p align="center">一个文件就能跑起来的轻量插件。</p>

```text
astrbot_plugin_163music/
├── main.py                 # 插件主程序
├── metadata.yaml           # 插件元数据
├── _conf_schema.json       # WebUI 配置定义
├── requirements.txt        # Python 依赖
├── README.md               # 使用说明
└── netease_login.json      # 运行时 Cookie，请勿提交
```

| 项目 | 内容 |
|:---:|:---:|
| 插件名 | `astrbot_plugin_163music` |
| 显示名 | 网易云音乐点歌插件 |
| 作者 | [xiaoxin](https://github.com/xiaoxin-qaq) |
| 许可证 | MIT |

---

<h2 align="center">💖 致谢</h2>

<p align="center">感谢这些项目让插件变得更好。</p>

<p align="center">
  <a href="https://github.com/AstrBotDevs/AstrBot">AstrBot</a> ·
  <a href="https://github.com/neteasecloudmusicapienhanced/api-enhanced">NeteaseCloudMusicApiEnhanced</a> ·
  <a href="https://github.com/Binaryify/NeteaseCloudMusicApi">Binaryify / NeteaseCloudMusicApi</a> ·
  <a href="https://github.com/imageio/imageio-ffmpeg">imageio-ffmpeg</a>
</p>

---

<h2 align="center">📄 License</h2>

<p align="center">
  本项目遵循 MIT License，仅供学习与个人使用。<br>
  音乐版权归网易云音乐及版权方所有，请勿用于商业用途。
</p>

<div align="center">

**如果这只猫娘帮你叼到了喜欢的歌，欢迎给项目点一个 ⭐。**

Made with 🎵 and 🐾

</div>
