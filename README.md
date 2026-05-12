# WizNote to Obsidian One Click

把为知笔记云端内容迁移到 Obsidian，并支持后续增量同步。

这个脚本是在一次真实迁移中沉淀出来的，重点解决这些坑：

- 从为知笔记云端拉取，而不是只依赖本地缓存
- 支持协作笔记 WebSocket 内容解析
- 图片集中到统一的 `attachments/` 目录
- Markdown 中自动改写图片引用
- 去掉迁移工具生成的 YAML frontmatter 和重复标题
- 支持后续新增、修改、删除的增量同步
- 复杂表格会展开为 Obsidian 原生 Markdown 表格，避免内容错位

## 依赖

仓库已经内置为知笔记下载器源码，位于：

```text
vendor/wiznote_downloader
```

不需要额外 clone 其它仓库。

安装 Python 依赖：

```bash
pip3 install -r requirements.txt
```

## 使用

首次全量同步：

```bash
python3 wiznote_to_obsidian_one_click.py \
  --user your@email.com \
  --output-base /path/to/obsidian-parent \
  --clean
```

输出目录会是：

```text
/path/to/obsidian-parent/wiznote_download
```

之后增量同步：

```bash
python3 wiznote_to_obsidian_one_click.py \
  --user your@email.com \
  --output-base /path/to/obsidian-parent
```

脚本会提示输入为知笔记密码。也可以用环境变量：

```bash
export WIZNOTE_USER="your@email.com"
export WIZNOTE_PASSWORD="your-password"

python3 wiznote_to_obsidian_one_click.py --output-base /path/to/obsidian-parent
```

## 常用参数

- `--clean`：删除已有 `wiznote_download` 后全量重建
- `--full`：全量跑一遍，但不先删除目录
- `--force-title TEXT`：强制同步标题包含 `TEXT` 的笔记
- `--force-guid GUID`：强制同步指定 docGuid
- `--workers N`：下载线程数，默认 `3`
- `--wiz-tool-dir PATH`：使用其它 `wiznote_downloader.py`，一般不需要

## 增量同步原理

脚本会在 Obsidian 目录下保存：

```text
.wiznote_sync_state.json
```

它记录已同步笔记的云端元数据。下次运行时，只同步新增或变更的笔记。

如果某条笔记同步失败，脚本不会把它标记成已同步；下次运行会继续尝试。

## 表格说明

Obsidian 原生 Markdown 表格不支持合并单元格。为知笔记里的 `rowspan/colspan` 会被展开为普通 Markdown 表格：

```markdown
| 类型 | 账号 | 密码 |
| --- | --- | --- |
| AppleId | a@example.com | xxx |
| AppleId | b@example.com | yyy |
```

这样不能 100% 保留合并样式，但能保证 Obsidian 编辑模式可读、可编辑，且数据不串列。

## 图片说明

所有能从云端拿到的图片会集中到：

```text
wiznote_download/attachments
```

如果原笔记里引用的是旧本机 `file://` 路径，而云端没有这个资源，脚本无法凭空恢复图片，会在最终校验中显示为 missing image links。

## 注意

- 不要把 `.wiznote_sync_state.json` 删掉，否则下次会失去增量判断依据。
- 不要把密码写进 README、提交记录或 issue。
- 建议先在一个临时目录跑通，再把 `wiznote_download` 作为 Obsidian vault 打开。
