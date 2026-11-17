# 渔获上岸核秤台

港口离线登记航次与实物箱组、秤具校准与复秤、分物种配额放行。各输入原始字节及哈希保留在 SQLite，身份冲突拒绝覆盖；可信净重才占配额，重复导入和重复放行不会重复占额。

运行：`python -m pip install . --no-deps --no-build-isolation`，`python -m landingweigh demo`，`python -m pytest -q`。三个业务入口是 `trip-import`、`weigh-import`、`quota-import`，均接受 `--db` 和 `--file`。正常与冲突输入在 fixtures；`report` 输出来源、航次、箱组、秤票、配额和放行证据。演示数据仅为合成离线数据。
