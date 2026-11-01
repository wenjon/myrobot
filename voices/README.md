# 本地音色目录

每个子目录是一个逻辑音色，目录名就是音色 ID。后端启动时只扫描这一层，
不会递归，也不会因为某个 `meta.json` 写坏而让整份音色列表失败。

```
voices/
  xiaoyou/
    meta.json     # 必填
    ref.wav       # source=clone/trained 时必填（已被 .gitignore 忽略）
    ref.txt       # 可选：参考音频逐字文本，供克隆模型对齐
    preview.mp3   # 可选：音色面板试听（已被 .gitignore 忽略）
```

## meta.json

最小可用（映射到一个 Edge 预设音色，不需要参考音频）：

```json
{
  "display_name": "小柚·温柔",
  "source": "edge",
  "locale": "zh-CN",
  "edge_voice": "zh-CN-XiaoyiNeural",
  "license": "Microsoft Edge TTS 预设音色"
}
```

本地克隆音色（P1 才会真正用于合成，P0 只是先让注册表认识它）：

```json
{
  "display_name": "我的声音",
  "source": "clone",
  "provider": "cosyvoice",
  "locale": "zh-CN",
  "ref_audio": "ref.wav",
  "ref_text": "这是一段用于克隆音色的参考录音。",
  "consent": { "type": "self" },
  "license": "本人授权，仅限本项目使用",
  "tags": ["温柔", "日常"]
}
```

字段说明：

- `source`：`edge` / `clone` / `trained` / `cloud`，缺省时按是否存在参考音频推断。
- `consent.type`：`self` / `authorized` / `public-figure`。**公众人物一律拒绝加载**。
- `available` 不需要手写：缺 `ref.wav` 会显示为 `available=false`，
  原因是 `missing_ref_audio`；授权被拒则是 `consent_denied`。
- `edge_voice`：P0 的降级出口。本地克隆音色也建议填一个相近的 Edge 预设，
  这样 CosyVoice 没启动时仍能出声。

## 隐私

`ref.wav` 是生物特征数据，已在 `.gitignore` 中排除。不要提交、不要上传，
也不要写进日志。仓库里只保留 `meta.json` 与说明文件。
