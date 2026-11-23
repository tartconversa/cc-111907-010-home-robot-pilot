# 家庭服务机器人试点的任务承诺与安全复查

本目录是一个**本地持续运营**的决策应用：把家庭允许的任务、设备能力证据、历史尝试、
工作人员覆盖与维护安排放在同一个分析里，回答：

- 家庭：哪些请求已经承接、哪些仍在等待确认。
- 运营：哪里需要补充人工、哪里证据太薄、哪类服务应暂停或改派。

应用不连接真实机器人，不下发物理动作。全部使用 `inputs/` 中的合成资料与本地
SQLite 状态。仅依赖 Python 3.14 标准库。

## 快速演示

```bash
python -m service_pilot -w work init --now 2026-11-19T08:00:00Z
python -m service_pilot -w work ops overview --now 2026-11-19T08:05:00Z
python -m service_pilot -w work ops assess Q1 --now 2026-11-19T08:05:00Z
```

完整中文说明见 [`docs/使用说明.md`](docs/使用说明.md)，扩展资料格式见
[`docs/扩展资料契约.md`](docs/扩展资料契约.md)。

## 资料检查与测试

```bash
# 原有来源完整性检查 + 业务行为测试（共 52 项：来源契约 3 项，业务 49 项）
python -m unittest discover -s tests -v
```

原有 `tests/test_source_contract.py` 检查清单哈希、字段与时间格式，未作修改。
新增的 `tests/test_pilot.py` 覆盖统计分层、机器人主办门槛、保障独占、并发确认、
维护/复查/授权影响、替代安排、回溯、去重/冲突、隐私与工作目录隔离。

容器测试入口仍为 `python -m unittest discover -s tests -v`，不需要网络或外部账号。
这些测试只使用合成资料与临时工作目录，不会访问真实机器人。

## 容器复核

容器环境以 Dockerfile 中的公开基础镜像摘要锁定。在本目录执行：

```bash
docker build -t local-analytics-inputs .
docker run --rm --network none local-analytics-inputs
```

可独立复核资料完整性。业务应用本身可直接在本机用 Python 3.14 运行。

## 目录

```
service_pilot/   应用（CLI、证据、规划、操作、家庭入口、视图、SQLite 状态）
tests/           来源契约测试 + 业务测试
docs/            中文使用说明、扩展资料契约、可直接导入的合成变更示例
inputs/          只读合成来源资料
work/            运行后生成的工作目录（每个目录独立）
```
