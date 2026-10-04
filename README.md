# 自适应中文能力诊断后端

面向东盟职业院校中文培训中心的自适应测评后端：维护可版本化的能力图谱、题库与
评分规则，在一次测评会话中根据已确认作答选择下一题，支持断网续测、教师按证据
作废异常作答、受控重评，并向教师解释每一次选题与每一项作答对能力判断的贡献。

纯 Python 3.11 标准库实现，无第三方依赖。

## 需求对应

| 需求 | 实现位置 |
| --- | --- |
| 可版本化能力图谱、先修关系 | `src/adaptive_exam/graph.py`（环检测、缺口传播、解释链） |
| 题目覆盖、难度、曝光限制 | `src/adaptive_exam/bank.py` |
| 版本化评分/选题规则 | `src/adaptive_exam/rules.py`（等级分段、阈值、停测条件） |
| 只追加、多修订共存的版本目录 | `src/adaptive_exam/catalog.py`，定义文件 `examples/catalog_seed.json` |
| 根据已确认作答选下一题 | `src/adaptive_exam/engine.py`（Beta 证据模型 + 信息量/匹配度/新颖度评分） |
| 并列候选题稳定复现 | 排序键 `(-综合分, tie_rank, 题目ID)`，纯函数无随机源 |
| 网络中断后继续 | 会话/曝光账本原子落盘；每次操作从磁盘重放（`persistence.py`） |
| 请求重试不重复扣曝光 | 取题按 `request_id` 预留曝光（`ExposureRepository.reserve`）；作答按 `answer_token` 幂等 |
| 崩溃窗口恢复 | 预留已落盘、选题解释未写入时，重试自动补齐且不二次扣减（`test_recovery.py`） |
| 教师按证据作废 | `POST /sessions/{id}/invalidate`，必须带教师标识与证据理由 |
| 受控重评 | `POST /sessions/{id}/regrade`，重放仍有效作答，结果谱系只追加；版本校验拒绝夹带新题库 |
| 分数/等级绑定当时规则版本 | 会话保存图谱/题库/规则的限定版本标识 + 内容 SHA-256 摘要；后续题库修订不影响旧结果 |
| 逐题选择依据 + 作答贡献 | `GET /sessions/{id}/report`（全候选评分、阻塞原因、每技能掌握度边际贡献） |
| 进程重启后未结束会话继续 | 能力状态完全由已确认作答派生，重启即重放（`test_service.py` 覆盖） |

## 领域模型

- **能力图谱 SkillGraph**：技能节点 + 有向无环先修边。直接缺口沿先修链传播为
  推断缺口（`propagate_gaps`），并给出从直接缺口到每个推断先修的解释链
  （`gap_chains`），回答“学员为什么在高阶技能上失分”。
- **题库 ItemBank**：题目声明覆盖技能集合、标定难度 `[0,1]`、全系统曝光上限
  （`null` 表示不限）、以及并列次序键 `tie_rank`。题库是不可变值对象。
- **评分规则 GradingRules**：等级分段（六段制 A1–C2）、缺口阈值、证据强度、
  最少/最大题量、目标不确定度。规则同样不可变、带摘要。
- **会话 Session**：只追加事件流——投放、作答、作废、结测、重评全部留痕。
  作废是状态翻转而非删除，旧结测结果进入结果谱系，不被改写。

### 选题模型

每个技能维护 Beta 形式证据 `alpha/beta`（先验强度 2、先验均值取
`initial_ability`），掌握度 `p = α/(α+β)`，方差 `p(1-p)/(α+β)`。

候选题综合分：

```
score = Σ覆盖技能方差(信息量) + 0.30×(1−|难度−总体能力|) + 0.15×新颖度
```

排序：综合分降序 → `tie_rank` 升序 → 题目ID升序。无随机数，相同输入必然得到
相同结果，跨进程/重启可复现。

## 持久化

- `sessions/<会话ID>.json`：会话完整事件流，tmp + rename 原子写入。
- `exposures.json`：题目全系统累计曝光数 + `会话ID:请求ID → 预留` 映射。

## HTTP API

启动：

```bash
python run_server.py --catalog examples/catalog_seed.json --data ./.data --port 8080
```

| 方法 路径 | 说明 |
| --- | --- |
| `POST /sessions` | 开始会话，冻结图谱/题库/规则版本 |
| `POST /sessions/{id}/next` | 取下一题；body 必须带 `request_id`，重试幂等 |
| `POST /sessions/{id}/answers` | 提交作答；`answer_token` 相同则幂等重放 |
| `POST /sessions/{id}/invalidate` | 教师作废（`teacher_id` + `reason` 证据必填） |
| `POST /sessions/{id}/finish` | 结测，结果绑定冻结版本；`{"force": true}` 可强制 |
| `POST /sessions/{id}/regrade` | 受控重评，返回 `previous` 与 `latest` |
| `GET  /sessions/{id}/report` | 逐题选题依据、候选评分、作答贡献、缺口链、结果谱系、审计流 |
| `GET  /healthz` | 存活与已登记版本 |

客户端约定：网络中断后用**同一个 `request_id`** 重试取题、用**同一个
`answer_token`** 重提作答；服务端保证不重复扣减、不重复计分。

## 测试

```bash
python -m unittest discover -s tests -v   # 40 个测试
python -m compileall -q src tests run_cli.py run_server.py
python run_cli.py                          # 命令行冒烟
```

测试覆盖：图谱环检测与缺口传播、并列选题确定性、曝光耗尽阻塞、幂等扣减、
作答令牌幂等、进程重启续测、崩溃窗口补齐、作废+申诉重评谱系、重评拒绝版本
篡改、新题库修订不改写旧结果、HTTP 全流程。

## 目录结构

```
src/adaptive_exam/
  contracts.py    # 基础版本契约（沿用既有 SkillGraphVersion/ExamItemRecord）
  versioning.py   # 限定版本标识、规范化 JSON、内容摘要
  graph.py        # 能力图谱、先修关系、缺口传播
  bank.py         # 题目、覆盖、难度、曝光上限
  rules.py        # 等级分段与自适应规则（版本化）
  engine.py       # 能力估计与下一题选择（纯函数，可重放）
  catalog.py      # 只追加版本目录 + JSON 定义装载
  session.py      # 会话聚合：事件流、作废、结果谱系、序列化
  persistence.py  # 原子落盘的会话仓库与曝光账本（幂等预留）
  service.py      # 应用服务：编排、结测/重评、教师报告
  api.py          # 标准库 HTTP 适配层
examples/catalog_seed.json  # 东盟职业院校场景种子（图谱/18 题/六段制规则）
```
