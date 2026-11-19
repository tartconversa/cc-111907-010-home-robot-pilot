# 家庭服务机器人试点的任务承诺与安全复查

此仓库包含业务开发所需的合成输入、字段契约及资料完整性检查，业务应用尚未开发。`INPUT_CONTRACT.md` 与 `inputs/schema.json` 说明业务事实含义，输入不包含预计算建议、处置结果或规划答案。

## 环境与资料检查

使用 Python 3.14，仅依赖标准库。运行 `python -m unittest discover -s tests -v` 检查资料字段、时间和清单哈希。该命令只检查输入集，不验收尚未实现的应用。

容器环境以 Dockerfile 中的公开基础镜像摘要锁定。在本目录执行 `docker build -t local-analytics-inputs .`，然后执行 `docker run --rm --network none local-analytics-inputs` 可独立复核资料。

## 开发与运行

请根据业务需求实现应用，为最终使用入口、工作目录、人工操作和业务测试补充中文说明。正式业务演练使用本地输入和本地状态，不需要真实外部账号。业务功能完成前没有可宣称通过的分析或规划结果。
