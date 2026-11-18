# 渔获上岸核秤台

港口离线登记航次与实物箱组、秤具校准与复秤、分物种配额放行。各输入原始字节及哈希保留在 SQLite，身份冲突拒绝覆盖；可信净重才占配额，重复导入和重复放行不会重复占额。

运行：`python -m pip install . --no-deps --no-build-isolation`，`python -m landingweigh demo`，`python -m pytest -q`。三个业务入口是 `trip-import`、`weigh-import`、`quota-import`，均接受 `--db` 和 `--file`。正常与冲突输入在 fixtures；`report` 输出来源、航次、箱组、秤票、配额和放行证据。演示数据仅为合成离线数据。

跨港直接封箱交接可用 `relay-import` 记录。先导入原航次和接收船许可，再提交带两端交接凭证的 `fixtures/relay_normal.json`；当前持有人和封签保存在 `crate_custody`，每次实际交接保存在 `crate_handover`。原箱组所属航次不改。重复文件幂等，旧持有人、旧封签或接收船许可不匹配会拒绝整份交接文件并保留失败来源。该入口只处理整箱一对一交接，不处理重新分装。

许可清单限定航次、物种及时间。现场 `inspect-import` 能把箱组核定物种改为许可内物种，因而改用另一配额；不在许可内的物种会扣留。复秤冲突后，`weigh-adjudicate` 可引用仍具有效校准的票据形成新可信重量，后到票据会使旧裁定失效。跨船 `quota-transfer` 需要来源船的授权额度和未用余额，转移后重算放行。每次放行状态变化写入 release_event；`demo-decisions` 展示物种复核、配额转移、复秤争议和裁定的完整链路。
